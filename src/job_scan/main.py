"""Entry point: scrape -> filter -> dedup -> score -> report -> notify."""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import replace

from .aggregators import fetch_aggregators
from .config import (
    CONFIG_DIR, Company, load_aggregators, load_companies, load_filters,
    load_gap_vocabulary, load_no_whiteboard_companies, load_settings,
)
from .filter import passes
from .interview_style import load_no_whiteboard
from .matcher import combine, freshness_days, score_posting
from .report import Scored, notify, notify_deferred, notify_failure, notify_watch, write_report
from .resident import apply_resident
from .resume import load_resume_text
from .scrapers.base import Posting, fetch_all
from .scrapers import (
    fetch_ashby, fetch_greenhouse, fetch_lever, fetch_smartrecruiters, fetch_workday,
)
from .store import (
    connect, count_unscored, filter_new, followups_due, known_by_job_key,
    mark_liveness, mark_notified, record_scored,
    record_unscored, touch_seen,
)

WEB_HOST = "127.0.0.1"
WEB_PORT = 8765
WEB_URL = f"http://{WEB_HOST}:{WEB_PORT}/"

# If at least this fraction of attempted scores error out (and >= 2 fail), treat
# the run as broken rather than quiet — see the alert at the end of cmd_run.
SCORE_FAILURE_ALERT_RATE = 0.5

log = logging.getLogger("job-scan")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _ollama_up(host: str) -> bool:
    """Cheap reachability probe so a run without the GPU/model degrades to
    collect-only instead of burning a retry cycle per posting."""
    import requests
    try:
        requests.get(f"{host}/api/tags", timeout=5).raise_for_status()
        return True
    except Exception:
        return False


def cmd_probe(args: argparse.Namespace) -> int:
    """Test an ATS+slug combo without running a full scan."""
    handlers = {
        "greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
        "workday": fetch_workday, "smartrecruiters": fetch_smartrecruiters,
    }
    h = handlers.get(args.ats)
    if not h:
        print(f"unknown ATS: {args.ats}", file=sys.stderr)
        return 2
    postings = h(Company(
        name=args.slug, ats=args.ats, slug=args.slug,
        host=getattr(args, "host", "") or "", board=getattr(args, "board", "") or "",
    ))
    print(f"{args.ats}/{args.slug}: {len(postings)} postings")
    for p in postings[:10]:
        print(f"  - {p.title} | {p.location} | {p.url}")
    if len(postings) > 10:
        print(f"  ... and {len(postings) - 10} more")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings()
    filters = load_filters()
    companies = load_companies()
    if getattr(args, "with_no_whiteboard", False):
        nw = load_no_whiteboard_companies()
        log.info("including %d no-whiteboard companies (weekly sweep)", len(nw))
        companies += nw
    log.info("loaded %d companies (%d priority)",
             len(companies),
             sum(1 for c in companies if c.priority))

    resume = load_resume_text(settings.resume_path)
    log.info("resume loaded: %d chars", len(resume))

    no_whiteboard = load_no_whiteboard()

    log.info("fetching postings ...")
    t0 = time.monotonic()
    postings = fetch_all(companies)
    log.info("fetched %d company postings in %.1fs", len(postings), time.monotonic() - t0)

    if not args.no_aggregators:
        t1 = time.monotonic()
        # Known fingerprints let LinkedIn skip detail-page fetches for
        # postings already stored (its per-posting hydration cost).
        with connect() as con:
            known = {r["fingerprint"] for r in con.execute("SELECT fingerprint FROM postings")}
        agg_postings = fetch_aggregators(load_aggregators(), known, filters)
        log.info("fetched %d aggregator postings in %.1fs", len(agg_postings), time.monotonic() - t1)
        postings += agg_postings

    filtered = [p for p in postings if passes(p, filters)]
    log.info("after pre-LLM filters: %d postings", len(filtered))

    # Drop postings whose KNOWN post date is older than max_age_days, before we
    # spend LLM time on them. Postings without a date (most ATS boards) have no
    # known age yet and pass here; the dashboard later ages them out by how long
    # they've been seen. Per-ATS overrides in ats_max_age_days (enterprise
    # boards keep live reqs open far longer than startup ATSs). 0 disables,
    # globally or per ATS.
    filtered = _drop_stale(filtered, settings)

    with connect() as con:
        all_fps = [p.fingerprint for p in filtered]
        new_fps = filter_new(con, all_fps)
        touch_seen(con, [fp for fp in all_fps if fp not in new_fps])
        # The same job found by a second source is not new.
        new_fps -= known_by_job_key(con, [p for p in filtered if p.fingerprint in new_fps])

        # Liveness: flag stored postings that vanished from boards we scanned.
        fetched_fps = {p.fingerprint for p in postings}
        covered = {(p.ats, p.slug) for p in postings}
        gone, revived = mark_liveness(con, fetched_fps, covered)
        if gone or revived:
            log.info("liveness: %d postings now gone (dead links), %d revived", gone, revived)

        new_postings = [p for p in filtered if p.fingerprint in new_fps]
        log.info("new (not previously scored): %d", len(new_postings))

        # Collect-only mode: no LLM available (GPU out for repair, Ollama down)
        # or explicitly requested. Record every new posting unscored — no cap,
        # since inserting rows is free — and let `rescore --unscored-only`
        # sweep them when the model is back.
        defer = args.no_llm or not _ollama_up(settings.ollama.host)
        if defer:
            reason = "--no-llm" if args.no_llm else f"Ollama unreachable at {settings.ollama.host}"
            log.warning("scoring deferred (%s): recording %d postings unscored",
                        reason, len(new_postings))
            for p in new_postings:
                record_unscored(con, p)
            backlog = count_unscored(con)
        else:
            apply_resident(settings.ollama)
            if args.limit:
                new_postings = new_postings[: args.limit]
                log.info("limited to %d for this run", len(new_postings))
            elif len(new_postings) > settings.max_score_per_run:
                log.info("capping at max_score_per_run=%d", settings.max_score_per_run)
                new_postings = new_postings[: settings.max_score_per_run]

        scored: list[Scored] = []
        failed = 0
        to_score = [] if defer else new_postings
        for i, p in enumerate(to_score, 1):
            log.info("[%d/%d] scoring %s | %s", i, len(to_score), p.company, p.title)
            try:
                m = score_posting(settings.ollama, resume, p, settings.scoring,
                                  settings.location_preference, no_whiteboard)
            except Exception as e:
                log.warning("scoring failed for %s/%s: %s", p.company, p.title, e)
                failed += 1
                continue
            scored.append(Scored(posting=p, match=m))
            _record(con, p, m)

        # Open applications gone quiet — surfaced in the same report/notify
        # pass as new matches, so chasing what's already sent isn't a separate
        # thing to remember.
        followups = followups_due(con, settings.followup_after_days)
    due, stale = followups
    if due or stale:
        log.info("follow-ups: %d due, %d look ghosted", len(due), len(stale))

    report_path = write_report(scored, settings,
                               deferred=len(new_postings) if defer else 0,
                               followups=followups)
    log.info("report written: %s", report_path)

    fu_note = f" {len(due)} follow-up(s) due." if due else ""

    if defer:
        if not args.no_notify:
            notify_deferred(len(new_postings), backlog, followups)
        print(f"\nDone. {len(new_postings)} postings collected, not scored "
              f"({backlog} unscored total).{fu_note} Report: {report_path}")
        print("Run `job-scan rescore --unscored-only` once Ollama is back.")
        return 0

    if not args.no_notify:
        notify(scored, settings, report_path, followups)

    above = sum(1 for s in scored if s.match.score >= settings.min_score)
    print(f"\nDone. {above} new matches above cutoff.{fu_note} Report: {report_path}")

    if not args.no_open and above > 0:
        _ensure_web_running()
        _xdg_open(WEB_URL)

    # Distinguish "0 matches because nothing fit" from "0 because scoring broke"
    # (Ollama down, model unloaded). A high failure rate means the run's numbers
    # are not trustworthy — alert loudly and exit non-zero so the systemd unit
    # shows as failed instead of silently reporting an empty day.
    attempted = len(new_postings)
    if attempted and failed >= 2 and failed / attempted >= SCORE_FAILURE_ALERT_RATE:
        log.error("scoring failed for %d/%d postings this run — is Ollama up and the model loaded?",
                  failed, attempted)
        if not args.no_notify:
            notify_failure(failed, attempted)
        return 1

    return 0


def _drop_stale(postings: list[Posting], settings) -> list[Posting]:
    """Drop postings whose KNOWN post date is older than max_age_days (per-ATS
    overrides in ats_max_age_days; 0 disables). Undated postings pass."""
    if not (settings.max_age_days or settings.ats_max_age_days):
        return postings

    def _fresh(p: Posting) -> bool:
        limit = settings.ats_max_age_days.get(p.ats, settings.max_age_days)
        return not limit or (freshness_days(p.posted_at) or 0) <= limit

    kept = [p for p in postings if _fresh(p)]
    if len(postings) - len(kept):
        log.info("dropped %d stale postings", len(postings) - len(kept))
    return kept


def _record(con, p: Posting, m) -> None:
    # Flags the source or the body raised come first: a repost
    # matters more than any gap the model found.
    concerns = list(p.flags) + list(m.concerns)
    record_scored(
        con, p.fingerprint, p.company, p.title, p.url, p.location,
        m.score, m.rationale,
        strengths=m.strengths, concerns=concerns, description=p.description,
        skill_fit=m.skill_fit, seniority_fit=m.seniority_fit,
        domain_fit=m.domain_fit, location_tier=m.location_tier,
        posted_at=p.posted_at, freshness_days=m.freshness_days,
        interview_style=m.interview_style,
    )


def _unit_active(unit: str) -> bool:
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", unit],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() in ("active", "activating")
    except (OSError, subprocess.SubprocessError):
        return False


def _foreign_model_resident(cfg) -> str | None:
    """Name of a loaded model outside our families (e.g. an RP chat on qwen),
    or None. A watch must not evict someone's interactive session."""
    import requests
    try:
        models = requests.get(f"{cfg.host}/api/ps", timeout=5).json().get("models") or []
    except (requests.RequestException, ValueError):
        return None
    fams = {f.lower() for f in cfg.resident_families}
    ours = [m for m in models if m.get("name", "").split(":", 1)[0].lower() in fams]
    others = [m["name"] for m in models if m not in ours and "embed" not in m.get("name", "").lower()]
    return others[0] if others and not ours else None


def cmd_watch(args: argparse.Namespace) -> int:
    """Intraday watch between daily runs. Cheap: re-fetches only LinkedIn (a
    few-hours window) and freehire, scores only postings never seen before, and
    notifies only on strong ones. No report file, no dashboard pop, and no
    liveness pass (a partial fetch would mark every unfetched posting gone)."""
    settings = load_settings()
    w = settings.watch
    for unit in ("job-scan.service", "job-scan-weekly.service"):
        if _unit_active(unit) and not args.force:
            log.info("%s is running; skipping this watch", unit)
            return 0
    filters = load_filters()
    agg = load_aggregators()
    agg = replace(agg, hackernews=False, remoteok=False, remotive=False,
                  weworkremotely=False, freehire_days=w.freehire_days,
                  linkedin=agg.linkedin and not args.no_linkedin,
                  linkedin_hours=w.linkedin_hours, linkedin_pages=w.linkedin_pages,
                  linkedin_max_details=w.linkedin_max_details)

    t0 = time.monotonic()
    with connect() as con:
        known = {r["fingerprint"] for r in con.execute("SELECT fingerprint FROM postings")}
    postings = fetch_aggregators(agg, known, filters)
    filtered = _drop_stale([p for p in postings if passes(p, filters)], settings)

    with connect() as con:
        fps = [p.fingerprint for p in filtered]
        new_fps = filter_new(con, fps)
        touch_seen(con, [fp for fp in fps if fp not in new_fps])
        new_fps -= known_by_job_key(con, [p for p in filtered if p.fingerprint in new_fps])
        # Commit early and after every posting: an open write transaction held
        # across ~20 min of LLM calls would lock the dashboard out of marking
        # postings visited/applied for the whole watch.
        con.commit()
        new = [p for p in filtered if p.fingerprint in new_fps]
        log.info("watch: %d fetched in %.1fs, %d pass filters, %d new",
                 len(postings), time.monotonic() - t0, len(filtered), len(new))
        if not new:
            return 0

        # Anything not scored here is deliberately NOT recorded: left unseen,
        # the morning run (3-day LinkedIn, 7-day freehire window) scores it.
        foreign = _foreign_model_resident(settings.ollama)
        if args.no_llm or foreign or not _ollama_up(settings.ollama.host):
            why = ("--no-llm" if args.no_llm
                   else f"{foreign} is loaded (someone else's session)" if foreign
                   else "Ollama unreachable")
            log.info("watch: %s; %d new postings wait for the next run", why, len(new))
            return 0
        apply_resident(settings.ollama)
        # Freshest first: LinkedIn (its window is only a few hours, so every
        # card is new today), then freehire newest-first.
        new.sort(key=lambda p: p.posted_at or "", reverse=True)
        new.sort(key=lambda p: p.ats != "linkedin")
        if len(new) > w.max_score:
            log.info("watch: scoring %d of %d; the rest wait for the next run", w.max_score, len(new))
            new = new[: w.max_score]

        resume = load_resume_text(settings.resume_path)
        no_whiteboard = load_no_whiteboard()
        hits: list[Scored] = []
        for i, p in enumerate(new, 1):
            log.info("[%d/%d] scoring %s | %s", i, len(new), p.company, p.title)
            try:
                m = score_posting(settings.ollama, resume, p, settings.scoring,
                                  settings.location_preference, no_whiteboard)
            except Exception as e:
                log.warning("scoring failed for %s/%s: %s", p.company, p.title, e)
                continue
            _record(con, p, m)
            # Read back: record_scored applies the duplicate/burst demotions,
            # so the stored score is the one that decides a notification.
            m.score = con.execute("SELECT score FROM postings WHERE fingerprint = ?",
                                  (p.fingerprint,)).fetchone()["score"]
            con.commit()
            print(f"{m.score:3d}  {p.company} | {p.title}")
            if m.score >= w.notify_min_score:
                hits.append(Scored(posting=p, match=m))
        mark_notified(con, [s.posting.fingerprint for s in hits])

    if hits and not args.no_notify:
        notify_watch(hits, WEB_URL)
    log.info("watch: %d at or above %d", len(hits), w.notify_min_score)
    return 0


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _ensure_web_running() -> None:
    if _port_open(WEB_HOST, WEB_PORT):
        return
    # Spawn the dashboard server detached so the daily run can exit cleanly.
    log.info("starting dashboard server at %s", WEB_URL)
    subprocess.Popen(
        [sys.executable, "-m", "job_scan", "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Give it a moment to bind before we xdg-open.
    for _ in range(20):
        if _port_open(WEB_HOST, WEB_PORT):
            return
        time.sleep(0.25)
    log.warning("dashboard didn't come up in time")


def _xdg_open(url: str) -> None:
    if not shutil.which("xdg-open"):
        return
    # Suppress stderr because xdg-open chatters on some setups.
    env = {**os.environ}
    subprocess.Popen(
        ["xdg-open", url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )


def cmd_rescore(args: argparse.Namespace) -> int:
    """Re-score postings already in the DB under the current scoring mechanism.

    Uses the stored description for the LLM sub-scores (no re-fetch). Old rows
    predate posted_at, so freshness falls back to first_seen as a proxy; new
    runs populate the real posting date. Applied/dismissed status is preserved.
    """
    from .scrapers.base import Posting

    settings = load_settings()
    apply_resident(settings.ollama)
    resume = load_resume_text(settings.resume_path)
    log.info("resume loaded: %d chars", len(resume))
    no_whiteboard = load_no_whiteboard()

    with connect() as con:
        where = "WHERE score IS NULL " if args.unscored_only else ""
        rows = con.execute(
            "SELECT fingerprint, company, title, url, location, description, "
            f"posted_at, first_seen FROM postings {where}ORDER BY first_seen DESC"
        ).fetchall()
        if args.limit:
            rows = rows[: args.limit]
        log.info("re-scoring %d postings", len(rows))

        for i, r in enumerate(rows, 1):
            ats, slug, posting_id = (r["fingerprint"].split(":", 2) + ["", ""])[:3]
            proxy_posted = r["posted_at"] or r["first_seen"] or ""
            p = Posting(
                company=r["company"], ats=ats, slug=slug, posting_id=posting_id,
                title=r["title"], location=r["location"] or "", url=r["url"],
                description=r["description"] or "", posted_at=proxy_posted,
            )
            log.info("[%d/%d] %s | %s", i, len(rows), p.company, p.title)
            try:
                m = score_posting(settings.ollama, resume, p, settings.scoring,
                                  settings.location_preference, no_whiteboard)
            except Exception as e:
                log.warning("rescore failed for %s/%s: %s", p.company, p.title, e)
                continue
            record_scored(
                con, r["fingerprint"], p.company, p.title, p.url, p.location,
                m.score, m.rationale,
                strengths=m.strengths, concerns=m.concerns, description=p.description,
                skill_fit=m.skill_fit, seniority_fit=m.seniority_fit,
                domain_fit=m.domain_fit, location_tier=m.location_tier,
                # Don't write the proxy as a real post date; keep what we had.
                posted_at=r["posted_at"] or "", freshness_days=m.freshness_days,
                interview_style=m.interview_style,
            )

    print(f"\nDone. Re-scored {len(rows)} postings.")
    return 0


def cmd_recompute(args: argparse.Namespace) -> int:
    """Recombine stored sub-scores under the current settings — NO LLM calls.

    Use after tuning the scoring config (weights, tiers, interview nudge) or
    refreshing the no-whiteboard list. Reads each row's stored
    skill/seniority/domain/tier and recomputes the final score + interview tag
    in seconds. Rows without sub-scores (never LLM-scored) are skipped.
    """
    import json
    from .interview_style import classify
    from .matcher import filter_concerns

    settings = load_settings()
    sc = settings.scoring
    no_whiteboard = load_no_whiteboard()

    with connect() as con:
        rows = con.execute(
            "SELECT fingerprint, company, description, concerns, skill_fit, seniority_fit, "
            "domain_fit, location_tier FROM postings "
            "WHERE skill_fit IS NOT NULL"
        ).fetchall()
        n_tagged = n_cleaned = 0
        for r in rows:
            style = classify(r["company"], r["description"] or "", no_whiteboard)
            bonus = sc.interview_points if style else 0
            if style:
                n_tagged += 1
            score = combine(
                r["skill_fit"], r["seniority_fit"], r["domain_fit"],
                r["location_tier"] if r["location_tier"] is not None else 6,
                sc, bonus,
            )
            # Strip employment-gap / short-tenure concern bullets from stored rows.
            try:
                concerns = json.loads(r["concerns"]) if r["concerns"] else []
            except json.JSONDecodeError:
                concerns = []
            cleaned = filter_concerns([str(c) for c in concerns])
            if len(cleaned) != len(concerns):
                n_cleaned += 1
            con.execute(
                "UPDATE postings SET score = ?, interview_style = ?, concerns = ? WHERE fingerprint = ?",
                (score, style, json.dumps(cleaned), r["fingerprint"]),
            )

    print(f"\nDone. Recomputed {len(rows)} scores ({n_tagged} interview-tagged, "
          f"{n_cleaned} had gap/tenure concerns stripped).")
    return 0


def cmd_gaps(args: argparse.Namespace) -> int:
    """Tally recurring concerns across high-scoring postings — no LLM.

    Every scored posting already stores the model's concern bullets. Rolled up
    over the roles that actually fit (score >= --min-score), the repeats point
    at what is really costing matches, rather than one posting's opinion.
    """
    import json
    import sqlite3
    from collections import Counter

    from .store import DB_PATH

    # Read-only: open the DB directly rather than through connect(), whose
    # schema/migration pass takes a write lock a running scan would block on.
    if not DB_PATH.exists():
        print("No database yet — run a scan first.")
        return 0
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT concerns FROM postings WHERE score >= ? AND concerns IS NOT NULL "
            "AND COALESCE(gone, 0) = 0",
            (args.min_score,),
        ).fetchall()
    finally:
        con.close()

    # Concerns are free-text sentences, so exact-match counting finds nothing.
    # Count keyword hits instead: a curated vocabulary of the skills/tools that
    # actually show up as gaps, matched case-insensitively on word boundaries.
    vocab = load_gap_vocabulary()
    counts: Counter[str] = Counter()
    examples: dict[str, str] = {}
    n_concerns = 0
    for r in rows:
        try:
            concerns = json.loads(r["concerns"]) or []
        except json.JSONDecodeError:
            continue
        # One posting counts once per term, however often it repeats it.
        hits: set[str] = set()
        for c in concerns:
            n_concerns += 1
            for term, pattern in vocab.items():
                if pattern.search(c):
                    hits.add(term)
                    examples.setdefault(term, c)
        counts.update(hits)

    total = len(rows)
    if not total:
        print(f"No scored postings at or above {args.min_score}.")
        return 0

    print(f"\nSkill gaps across {total} postings scoring >= {args.min_score} "
          f"({n_concerns} concern bullets)\n")
    width = max((len(t) for t, _ in counts.most_common(args.top)), default=10)
    for term, n in counts.most_common(args.top):
        pct = 100 * n / total
        bar = "█" * round(pct / 2)
        print(f"  {term:<{width}}  {n:>4} ({pct:4.1f}%)  {bar}")
        if args.verbose and term in examples:
            print(f"  {'':<{width}}        e.g. {examples[term][:100]}")
    print(f"\nVocabulary: {CONFIG_DIR / 'gap_terms.yaml'} — add terms it misses.\n")
    return 0


def cmd_applications(args: argparse.Namespace) -> int:
    """List open applications and bulk-set their status — no LLM.

    Built for triaging a backlog: most quiet applications resolve to the same
    answer (`no_response`), with a handful of real rejections mixed in. Mark
    the exceptions by number, then sweep the remainder with --rest.
    """
    import json

    from .store import (
        ACTIVE_APP_STATUSES, POST_APPLIED_STATUSES, STATUSES, quiet_days, set_status,
    )

    open_only = not args.all
    statuses = ACTIVE_APP_STATUSES if open_only else POST_APPLIED_STATUSES

    with connect() as con:
        qs = ",".join("?" * len(statuses))
        rows = con.execute(
            f"SELECT * FROM postings WHERE status IN ({qs})", statuses
        ).fetchall()
        # Deterministic order so the printed numbers mean the same thing in the
        # listing and the follow-up --set call: quietest first, id as tie-break.
        listing = sorted(
            rows,
            key=lambda r: (-(quiet_days(r) or 0), r["fingerprint"]),
        )

        if not listing:
            print("No open applications." if open_only else "No applications recorded.")
            return 0

        targets: list[int] = []
        if args.set:
            status, picks = args.set[0], args.set[1:]
            if status not in STATUSES:
                print(f"unknown status {status!r}; expected one of {sorted(STATUSES)}",
                      file=sys.stderr)
                return 2
            if args.rest:
                chosen = set(int(p) for p in picks)
                targets = [i for i in range(1, len(listing) + 1) if i not in chosen]
            else:
                targets = [int(p) for p in picks]
            bad = [n for n in targets if not 1 <= n <= len(listing)]
            if bad:
                print(f"out of range: {bad} (have 1-{len(listing)})", file=sys.stderr)
                return 2
            if not targets:
                print("nothing selected", file=sys.stderr)
                return 2
            for n in targets:
                set_status(con, listing[n - 1]["fingerprint"], status)
            print(f"\nMarked {len(targets)} application(s) as {status}.\n")
            # Re-read: statuses just changed, so the listing below reflects them.
            rows = con.execute(
                f"SELECT * FROM postings WHERE status IN ({qs})", statuses
            ).fetchall()
            listing = sorted(
                rows, key=lambda r: (-(quiet_days(r) or 0), r["fingerprint"])
            )
            if not listing:
                print("No open applications left.")
                return 0

    print(f"{'#':>3}  {'COMPANY':<24} {'ROLE':<44} {'STATUS':<11} {'APPLIED':<11} {'QUIET':>6}  FU")
    print("-" * 112)
    for i, r in enumerate(listing, 1):
        q = quiet_days(r)
        try:
            n_fu = len(json.loads(r["followups"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            n_fu = 0
        print(f"{i:>3}  {(r['company'] or '')[:24]:<24} {(r['title'] or '')[:44]:<44} "
              f"{(r['status'] or ''):<11} {str(r['applied_at'] or '')[:10]:<11} "
              f"{(f'{q}d' if q is not None else '—'):>6}  {n_fu}")
    print(f"\n{len(listing)} application(s). Mark them with, e.g.:")
    print("  job-scan applications --set rejected 3 7")
    print("  job-scan applications --set no_response --rest 3 7   # everything except 3 and 7")
    return 0


def cmd_resolve_nw(args: argparse.Namespace) -> int:
    """Resolve the hiring-without-whiteboards list to scannable ATS companies."""
    from .resolve_companies import resolve_all, write_yaml
    companies = resolve_all(validate=not args.no_validate)
    if not companies:
        # A network outage makes every fetch fail and resolve_all return []. Writing
        # that would clobber the cached list with `companies: []`. Bail non-zero so
        # the weekly.sh fallback keeps the existing cache instead of wiping it.
        log.error("resolved 0 companies (likely a network failure); keeping cached list")
        return 1
    path = write_yaml(companies)
    from collections import Counter
    by_ats = dict(Counter(c["ats"] for c in companies))
    print(f"\nResolved {len(companies)} scannable no-whiteboard companies -> {path}")
    print(f"by ATS: {by_ats}")
    print("Scan them with: job-scan run --with-no-whiteboard")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Re-fetch the boards and flag postings that have vanished (dead links).

    No scoring/LLM — just refreshes liveness. The daily/weekly run does this
    inline; use `prune` for an on-demand check between runs.
    """
    companies = load_companies() + load_no_whiteboard_companies()
    log.info("fetching %d company boards for liveness check ...", len(companies))
    postings = fetch_all(companies)
    log.info("fetched %d postings", len(postings))

    with connect() as con:
        fetched_fps = {p.fingerprint for p in postings}
        covered = {(p.ats, p.slug) for p in postings}
        touch_seen(con, list(fetched_fps))
        gone, revived = mark_liveness(con, fetched_fps, covered)
        total_gone = con.execute("SELECT COUNT(*) FROM postings WHERE gone = 1").fetchone()[0]

    print(f"\nDone. {gone} newly gone, {revived} revived; {total_gone} dead links hidden total.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import main as web_main
    log.info("serving dashboard on http://%s:%d/", args.host, args.port)
    web_main(host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="job-scan", description="Daily job match scanner")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="run a full scan (default)")
    run.add_argument("--limit", type=int, help="cap postings scored this run (for testing)")
    run.add_argument("--with-no-whiteboard", action="store_true",
                     help="also scan resolved hiring-without-whiteboards companies (weekly sweep)")
    run.add_argument("--no-aggregators", action="store_true", help="skip cross-company aggregators")
    run.add_argument("--no-notify", action="store_true", help="skip notify-send")
    run.add_argument("--no-open", action="store_true", help="skip auto-opening the dashboard")
    run.add_argument("--no-llm", action="store_true",
                     help="collect + dedup only, defer scoring (also automatic when Ollama is unreachable)")
    run.set_defaults(func=cmd_run)

    wt = sub.add_parser("watch", help="intraday watch: LinkedIn + freehire only, notify on strong new postings")
    wt.add_argument("--no-llm", action="store_true", help="fetch and count only; score nothing")
    wt.add_argument("--no-notify", action="store_true", help="skip notify-send")
    wt.add_argument("--no-linkedin", action="store_true", help="freehire only (e.g. while LinkedIn is throttling)")
    wt.add_argument("--force", action="store_true", help="run even while a daily/weekly scan is running")
    wt.set_defaults(func=cmd_watch)

    probe = sub.add_parser("probe", help="test an ATS+slug combo")
    probe.add_argument("ats", choices=["greenhouse", "lever", "ashby", "workday", "smartrecruiters"])
    probe.add_argument("slug")
    probe.add_argument("--host", help="board host (Workday, e.g. nvidia.wd5.myworkdayjobs.com)")
    probe.add_argument("--board", help="career-site name (Workday, e.g. NVIDIAExternalCareerSite)")
    probe.set_defaults(func=cmd_probe)

    rescore = sub.add_parser("rescore", help="re-score stored postings under the current mechanism")
    rescore.add_argument("--limit", type=int, help="cap postings re-scored (for testing)")
    rescore.add_argument("--unscored-only", action="store_true",
                         help="only score postings collected while the LLM was unavailable")
    rescore.set_defaults(func=cmd_rescore)

    recompute = sub.add_parser("recompute", help="recombine stored sub-scores under current config (no LLM)")
    recompute.set_defaults(func=cmd_recompute)

    resolve_nw = sub.add_parser("resolve-nw", help="resolve hiring-without-whiteboards list to scannable ATS companies")
    resolve_nw.add_argument("--no-validate", action="store_true", help="skip the live-board validation pass")
    resolve_nw.set_defaults(func=cmd_resolve_nw)

    prune = sub.add_parser("prune", help="re-fetch boards and hide postings with dead links (no scoring)")
    prune.set_defaults(func=cmd_prune)

    apps = sub.add_parser("applications", aliases=["apps"],
                          help="list open applications and bulk-set their status (no LLM)")
    apps.add_argument("--all", action="store_true",
                      help="include already-resolved applications, not just open ones")
    apps.add_argument("--set", nargs="+", metavar="ARG",
                      help="STATUS N [N ...] — set those numbered rows to STATUS")
    apps.add_argument("--rest", action="store_true",
                      help="with --set: apply to every row EXCEPT the listed numbers")
    apps.set_defaults(func=cmd_applications)

    gaps = sub.add_parser("gaps", help="tally recurring skill gaps across good matches (no LLM)")
    gaps.add_argument("--min-score", type=int, default=75,
                      help="only count postings scoring at least this (default 75)")
    gaps.add_argument("--top", type=int, default=20, help="terms to show (default 20)")
    gaps.set_defaults(func=cmd_gaps)

    serve = sub.add_parser("serve", help="run the dashboard web server")
    serve.add_argument("--host", default=WEB_HOST)
    serve.add_argument("--port", type=int, default=WEB_PORT)
    serve.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    setup_logging(args.verbose)
    if not getattr(args, "func", None):
        args = p.parse_args([*(argv or []), "run"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
