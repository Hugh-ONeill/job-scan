"""Cross-company job aggregators.

Unlike the ATS scrapers (one company per slug), these surface jobs from many
companies at once — the fix for good roles at companies that aren't in
companies.yaml. Each returns plain Postings that flow through the same
filter -> dedup -> score pipeline, so the title/location filters still apply.

Sources: Hacker News "Who is hiring" (Algolia API), RemoteOK, Remotive,
WeWorkRemotely (RSS). A source that errors is logged and skipped, never fatal.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import requests

from .config import Aggregators
from .filter import passes
from .scrapers.base import Posting, html_to_markdown, html_to_text, normalize_date

log = logging.getLogger(__name__)

TIMEOUT = 30
UA = {"User-Agent": "Mozilla/5.0 (job-scan)"}


def fetch_aggregators(
    agg: Aggregators,
    known_fps: set[str] | None = None,
    filters=None,
) -> list[Posting]:
    """known_fps / filters exist for sources that pay a per-posting hydration
    cost (LinkedIn detail pages): the first lets them skip what the DB already
    has, the second lets them drop obvious misses before fetching."""
    from .linkedin import fetch_linkedin

    out: list[Posting] = []
    sources = [
        ("hackernews", agg.hackernews, _hackernews),
        ("remoteok", agg.remoteok, _remoteok),
        ("remotive", agg.remotive, _remotive),
        ("weworkremotely", agg.weworkremotely, _weworkremotely),
        ("freehire", agg.freehire, _freehire),
        ("linkedin", agg.linkedin, lambda a: fetch_linkedin(a, known_fps, filters)),
    ]
    for name, enabled, fn in sources:
        if not enabled:
            continue
        try:
            got = _cap_new(fn(agg), agg.max_per_source, known_fps or set(), filters)
            log.info("aggregator %s: %d postings", name, len(got))
            out.extend(got)
        except Exception as e:
            log.warning("aggregator %s failed: %s", name, e)
    return out


def _cap_new(got: list[Posting], cap: int, known: set[str], filters) -> list[Posting]:
    """Apply max_per_source to what would actually be SCORED: postings the DB
    has not seen that pass the pre-filters. Known postings cost nothing and are
    kept, so touch_seen and liveness stay correct; overflow stays unseen and is
    picked up by the next run.

    Capping the raw list instead kept the same prefix every run. freehire lists
    results in search-term order, so "backend" and "frontend" kept 0 of 100
    each, every day from 2026-08-31 to 2026-09-18; HN's monthly thread lost its
    back half for the whole month.
    """
    fresh = [p for p in got
             if p.fingerprint not in known and (filters is None or passes(p, filters))]
    if len(fresh) <= cap:
        return got
    drop = {p.fingerprint for p in fresh[cap:]}
    log.info("capping %d new postings at %d; the rest wait for the next run", len(fresh), cap)
    return [p for p in got if p.fingerprint not in drop]


def _hackernews(agg: Aggregators) -> list[Posting]:
    """Latest 'Ask HN: Who is hiring?' thread; each top-level comment is a job."""
    search = requests.get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"query": '"Ask HN: Who is hiring"', "tags": "story", "hitsPerPage": 5},
        headers=UA, timeout=TIMEOUT,
    ).json()
    thread = next(
        (h for h in search.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()),
        None,
    )
    if not thread:
        return []
    tid = thread["objectID"]
    item = requests.get(f"https://hn.algolia.com/api/v1/items/{tid}", headers=UA, timeout=TIMEOUT).json()

    out: list[Posting] = []
    for c in item.get("children", []) or []:
        text = c.get("text") or ""
        if not text or c.get("author") == item.get("author"):
            continue  # skip empties and the OP's rules comment
        company, title, location = _parse_hn(text)
        if not title:
            continue
        # Posters mark filled roles "(CLOSED)" / "[CLOSED]" at the front.
        if "CLOSED" in (company[:20] + title[:20]).upper():
            continue
        out.append(Posting(
            company=company or "(HN)",
            ats="hackernews",
            slug="whoishiring",
            posting_id=str(c.get("id")),
            title=title,
            location=location,
            url=f"https://news.ycombinator.com/item?id={c.get('id')}",
            description=html_to_markdown(text),
            posted_at=normalize_date(c.get("created_at")),
        ))
    return out


def _parse_hn(text: str) -> tuple[str, str, str]:
    """HN posts conventionally lead with 'Company | Role | Location | ...'."""
    header = html_to_text(text.split("<p>")[0])
    parts = [p.strip() for p in header.split("|") if p.strip()]
    if not parts:
        return "", "", ""
    company = parts[0]
    title = parts[1] if len(parts) > 1 else ""
    location = " | ".join(parts[2:]) if len(parts) > 2 else ""
    return company, title, location


def _remoteok(agg: Aggregators) -> list[Posting]:
    data = requests.get("https://remoteok.com/api", headers=UA, timeout=TIMEOUT).json()
    out: list[Posting] = []
    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue  # first element is legal metadata
        loc = j.get("location") or "Remote"
        out.append(Posting(
            company=j.get("company") or "(RemoteOK)",
            ats="remoteok",
            slug="remoteok",
            posting_id=str(j.get("id") or j.get("slug")),
            title=j.get("position", "").strip(),
            location=f"Remote · {loc}" if loc and loc.lower() != "remote" else "Remote",
            url=j.get("url") or j.get("apply_url", ""),
            description=html_to_markdown(j.get("description", "")),
            posted_at=normalize_date(j.get("date")),
        ))
    return out


def _remotive(agg: Aggregators) -> list[Posting]:
    seen: dict[str, Posting] = {}
    for term in agg.queries:
        data = requests.get(
            "https://remotive.com/api/remote-jobs",
            params={"search": term, "limit": agg.max_per_source},
            headers=UA, timeout=TIMEOUT,
        ).json()
        for j in data.get("jobs", []) or []:
            pid = str(j.get("id"))
            if pid in seen:
                continue
            loc = j.get("candidate_required_location") or "Remote"
            seen[pid] = Posting(
                company=j.get("company_name") or "(Remotive)",
                ats="remotive",
                slug="remotive",
                posting_id=pid,
                title=(j.get("title") or "").strip(),
                location=f"Remote · {loc}",
                url=j.get("url", ""),
                description=html_to_markdown(j.get("description", "")),
                posted_at=normalize_date(j.get("publication_date")),
            )
    return list(seen.values())


def _freehire(agg: Aggregators) -> list[Posting]:
    """freehire.me tech-job aggregator — public REST API, JSON, no key.

    The /agent/jobs/search variant hydrates each hit's full description
    server-side (asked for as markdown), so one request per page reads every
    result without a follow-up detail call per hit. Results come in relevance
    order, 100 per page; `freehire_pages` pages are read per query (measured
    2026-10-13: page 2 of "full stack engineer" still held 25 Greenhouse and
    16 Oracle postings). `include_description` and `semantic_ratio` are
    reported back as ignored by this endpoint, so they are not sent.

    Per-record fields the API computes and this reads (seekter's notes,
    verified live): `closed_at` (dropped), `reality` (its own first-seen age,
    a repost flag, and how many postings share this text), `created_at` (when
    freehire first indexed it). A reposted job carries a fresh posted_at but
    an old created_at: 26 of 300 results inside a 7-day window on 2026-10-13.
    The earlier of the two is the posting's real age.
    """
    seen: dict[str, Posting] = {}
    for term in agg.queries:
        for page in range(max(1, agg.freehire_pages)):
            params: list[tuple[str, str]] = [
                ("q", term),
                ("limit", "100"),
                ("offset", str(page * 100)),
                ("description_format", "markdown"),
                ("posted_within_days", str(agg.freehire_days)),
            ]
            # Repeated ?countries=<iso> params; the facet vocabulary is ISO codes.
            params += [("countries", c) for c in agg.freehire_countries]
            env = requests.get(
                "https://freehire.me/api/v1/agent/jobs/search",
                params=params, headers=UA, timeout=TIMEOUT,
            ).json()
            rows = env.get("data", []) or []
            for j in rows:
                slug = str(j.get("public_slug") or "")
                if not slug or slug in seen or j.get("closed_at"):
                    continue
                loc = j.get("location") or ""
                if (j.get("work_mode") or "").lower() == "remote":
                    loc = f"Remote · {loc}" if loc and loc.lower() != "remote" else "Remote"
                posted = normalize_date(j.get("posted_at"))
                indexed = normalize_date(j.get("created_at"))
                seen[slug] = Posting(
                    company=j.get("company") or "(freehire)",
                    ats="freehire",
                    slug="freehire",
                    posting_id=slug,
                    title=(j.get("title") or "").strip(),
                    location=loc,
                    url=j.get("url") or f"https://freehire.me/jobs/{slug}",
                    # Already markdown per description_format; html_to_markdown is a
                    # no-op on tag-free text and cleans up if HTML slips through.
                    description=html_to_markdown(j.get("description") or ""),
                    posted_at=min((d for d in (posted, indexed) if d), default=""),
                    flags=_freehire_flags(j.get("reality") or {}, j.get("source") or ""),
                )
            if len(rows) < 100:
                break
    return list(seen.values())


def _freehire_flags(reality: dict, source: str) -> list[str]:
    """Concerns freehire's own `reality` block supports."""
    out: list[str] = []
    age = reality.get("age_days")
    reposts = reality.get("repost_count") or 0
    if reality.get("fake_freshness") and age:
        out.append(f"Repost: the listed post date is new but freehire first indexed this "
                   f"posting {age} days ago (seen {reposts} time{'s' if reposts != 1 else ''}).")
    mass = reality.get("mass_posting_count") or 0
    if mass >= 3:
        out.append(f"Mass posting: freehire counts {mass} postings sharing this text "
                   f"(one hire advertised under many titles or cities, or a farm). "
                   f"Verify the employer before applying.")
    return out


_WWR_CATEGORIES = [
    "remote-programming-jobs",
    "remote-full-stack-programming-jobs",
    "remote-back-end-programming-jobs",
    "remote-front-end-programming-jobs",
]


def _weworkremotely(agg: Aggregators) -> list[Posting]:
    out: list[Posting] = []
    seen: set[str] = set()
    for cat in _WWR_CATEGORIES:
        r = requests.get(f"https://weworkremotely.com/categories/{cat}.rss", headers=UA, timeout=TIMEOUT)
        root = ET.fromstring(r.content)
        for it in root.findall(".//item"):
            link = it.findtext("link") or ""
            if not link or link in seen:
                continue
            seen.add(link)
            raw_title = (it.findtext("title") or "").strip()
            # WWR titles are "Company: Role".
            company, _, role = raw_title.partition(":")
            region = (it.findtext("region") or "Remote").strip()
            desc = html_to_markdown(it.findtext("description") or "")
            out.append(Posting(
                company=company.strip() or "(WWR)",
                ats="weworkremotely",
                # Constant, NOT the category: a job cross-listed in several
                # feeds must have ONE identity. Keying on the feed it happened
                # to be found in stored the same posting twice (once per
                # category) and paid to LLM-score each copy.
                slug="weworkremotely",
                posting_id=link.rstrip("/").split("/")[-1],
                title=(role.strip() or raw_title),
                location=f"Remote · {region}",
                url=link,
                # RSS gives little body; title+region is the searchable signal.
                description=desc or f"{raw_title} — {region}",
                posted_at=normalize_date(_rfc822(it.findtext("pubDate"))),
            ))
    return out


def _rfc822(value: str | None) -> str:
    """Convert an RSS RFC-822 date to ISO; '' on failure."""
    if not value:
        return ""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return ""
