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

from .aggregators import fetch_aggregators
from .config import (
    Company, load_aggregators, load_companies, load_filters,
    load_no_whiteboard_companies, load_settings,
)
from .filter import passes
from .interview_style import load_no_whiteboard
from .matcher import combine, freshness_days, score_posting
from .report import Scored, notify, notify_failure, write_report
from .resume import load_resume_text
from .scrapers.base import fetch_all
from .scrapers import (
    fetch_ashby, fetch_greenhouse, fetch_lever, fetch_smartrecruiters, fetch_workday,
)
from .store import connect, filter_new, mark_liveness, mark_notified, record_scored, touch_seen

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
        agg_postings = fetch_aggregators(load_aggregators())
        log.info("fetched %d aggregator postings in %.1fs", len(agg_postings), time.monotonic() - t1)
        postings += agg_postings

    filtered = [p for p in postings if passes(p, filters)]
    log.info("after pre-LLM filters: %d postings", len(filtered))

    # Drop postings whose KNOWN post date is older than max_age_days, before we
    # spend LLM time on them. Postings without a date (most ATS boards) have no
    # known age yet and pass here; the dashboard later ages them out by how long
    # they've been seen. max_age_days=0 disables.
    if settings.max_age_days:
        before = len(filtered)
        filtered = [
            p for p in filtered
            if (freshness_days(p.posted_at) or 0) <= settings.max_age_days
        ]
        if before - len(filtered):
            log.info("dropped %d stale postings (> %d days old)",
                     before - len(filtered), settings.max_age_days)

    with connect() as con:
        all_fps = [p.fingerprint for p in filtered]
        new_fps = filter_new(con, all_fps)
        touch_seen(con, [fp for fp in all_fps if fp not in new_fps])

        # Liveness: flag stored postings that vanished from boards we scanned.
        fetched_fps = {p.fingerprint for p in postings}
        covered = {(p.ats, p.slug) for p in postings}
        gone, revived = mark_liveness(con, fetched_fps, covered)
        if gone or revived:
            log.info("liveness: %d postings now gone (dead links), %d revived", gone, revived)

        new_postings = [p for p in filtered if p.fingerprint in new_fps]
        log.info("new (not previously scored): %d", len(new_postings))

        if args.limit:
            new_postings = new_postings[: args.limit]
            log.info("limited to %d for this run", len(new_postings))
        elif len(new_postings) > settings.max_score_per_run:
            log.info("capping at max_score_per_run=%d", settings.max_score_per_run)
            new_postings = new_postings[: settings.max_score_per_run]

        scored: list[Scored] = []
        failed = 0
        for i, p in enumerate(new_postings, 1):
            log.info("[%d/%d] scoring %s | %s", i, len(new_postings), p.company, p.title)
            try:
                m = score_posting(settings.ollama, resume, p, settings.scoring,
                                  settings.location_preference, no_whiteboard)
            except Exception as e:
                log.warning("scoring failed for %s/%s: %s", p.company, p.title, e)
                failed += 1
                continue
            scored.append(Scored(posting=p, match=m))
            record_scored(
                con, p.fingerprint, p.company, p.title, p.url, p.location,
                m.score, m.rationale,
                strengths=m.strengths, concerns=m.concerns, description=p.description,
                skill_fit=m.skill_fit, seniority_fit=m.seniority_fit,
                domain_fit=m.domain_fit, location_tier=m.location_tier,
                posted_at=p.posted_at, freshness_days=m.freshness_days,
                interview_style=m.interview_style,
            )

    report_path = write_report(scored, settings)
    log.info("report written: %s", report_path)

    if not args.no_notify:
        notify(scored, settings, report_path)

    above = sum(1 for s in scored if s.match.score >= settings.min_score)
    print(f"\nDone. {above} new matches above cutoff. Report: {report_path}")

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
    resume = load_resume_text(settings.resume_path)
    log.info("resume loaded: %d chars", len(resume))
    no_whiteboard = load_no_whiteboard()

    with connect() as con:
        rows = con.execute(
            "SELECT fingerprint, company, title, url, location, description, "
            "posted_at, first_seen FROM postings ORDER BY first_seen DESC"
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
    run.set_defaults(func=cmd_run)

    probe = sub.add_parser("probe", help="test an ATS+slug combo")
    probe.add_argument("ats", choices=["greenhouse", "lever", "ashby", "workday", "smartrecruiters"])
    probe.add_argument("slug")
    probe.add_argument("--host", help="board host (Workday, e.g. nvidia.wd5.myworkdayjobs.com)")
    probe.add_argument("--board", help="career-site name (Workday, e.g. NVIDIAExternalCareerSite)")
    probe.set_defaults(func=cmd_probe)

    rescore = sub.add_parser("rescore", help="re-score stored postings under the current mechanism")
    rescore.add_argument("--limit", type=int, help="cap postings re-scored (for testing)")
    rescore.set_defaults(func=cmd_rescore)

    recompute = sub.add_parser("recompute", help="recombine stored sub-scores under current config (no LLM)")
    recompute.set_defaults(func=cmd_recompute)

    resolve_nw = sub.add_parser("resolve-nw", help="resolve hiring-without-whiteboards list to scannable ATS companies")
    resolve_nw.add_argument("--no-validate", action="store_true", help="skip the live-board validation pass")
    resolve_nw.set_defaults(func=cmd_resolve_nw)

    prune = sub.add_parser("prune", help="re-fetch boards and hide postings with dead links (no scoring)")
    prune.set_defaults(func=cmd_prune)

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
