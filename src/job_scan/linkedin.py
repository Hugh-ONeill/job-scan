"""LinkedIn public "jobs-guest" endpoints (no auth required).

Search returns an HTML list of job cards; detail returns a single job's HTML.
Both are parsed with regex — the markup is shallow and stable, and it keeps
this source dependency-free like the rest of the scrapers.

NOTE: automated access is against LinkedIn's ToS, so this source is built for
personal use at deliberately low volume: one search page per (query, location)
pair per run, detail pages fetched only for postings not already stored
(known fingerprints are passed in and skipped), a hard per-run detail cap, and
a polite pause between requests. Keep it that way.
"""
from __future__ import annotations

import html as html_mod
import logging
import random
import re
import time

import requests

from .config import Aggregators
from .scrapers.base import Posting, html_to_markdown, normalize_date

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-scan/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}
TIMEOUT = 20
# Base pause between requests (plus jitter) — politeness, not rate-limit dodging.
PAUSE = 1.5


def _sleep() -> None:
    time.sleep(PAUSE + random.uniform(0, 0.7))


def _get(url: str, params: dict) -> str:
    """GET with exponential backoff on 429/5xx. Returns "" on a 404."""
    delay = 1.0
    for attempt in range(5):
        r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 404:
            return ""
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == 4:
                r.raise_for_status()
            time.sleep(delay + random.uniform(0, 0.5))
            delay = min(delay * 2, 8.0)
            continue
        r.raise_for_status()
        return r.text
    return ""


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _clean(fragment: str) -> str:
    """Strip tags, unescape entities, collapse whitespace."""
    return _WS.sub(" ", html_mod.unescape(_TAG.sub(" ", fragment))).strip()


# --- search cards -------------------------------------------------------------

_CARD_TITLE = re.compile(r'class="base-search-card__title"[^>]*>(.*?)</h3>', re.I | re.S)
_CARD_TITLE_SR = re.compile(r'class="sr-only"[^>]*>(.*?)</span>', re.I | re.S)
_CARD_COMPANY = re.compile(r'class="base-search-card__subtitle"[^>]*>(.*?)</h4>', re.I | re.S)
_CARD_LOCATION = re.compile(r'class="job-search-card__location"[^>]*>(.*?)</span>', re.I | re.S)
_CARD_DATE = re.compile(r'class="job-search-card__listdate[^"]*"[^>]*datetime="([^"]+)"', re.I)


def parse_cards(page: str) -> list[dict]:
    """Parse the search response: a flat list of <li> job cards. Split on the
    job-posting URN and parse each chunk independently so one malformed card
    cannot break the rest."""
    out = []
    for chunk in re.split(r'data-entity-urn="urn:li:jobPosting:', page)[1:]:
        m = re.match(r"(\d+)", chunk)
        if not m:
            continue
        title = _CARD_TITLE.search(chunk) or _CARD_TITLE_SR.search(chunk)
        if not title:
            continue
        company = _CARD_COMPANY.search(chunk)
        loc = _CARD_LOCATION.search(chunk)
        date = _CARD_DATE.search(chunk)
        out.append({
            "id": m.group(1),
            "title": _clean(title.group(1)),
            "company": _clean(company.group(1)) if company else "",
            "location": _clean(loc.group(1)) if loc else "",
            "posted_at": normalize_date(date.group(1)) if date else "",
        })
    return out


# --- job detail ---------------------------------------------------------------

def _div_content(page: str, class_name: str) -> str:
    """Inner HTML of the first <div> carrying class_name, tracking nested <div>
    depth so inner divs don't truncate the extraction."""
    open_re = re.compile(rf'<div[^>]*class="[^"]*{re.escape(class_name)}[^"]*"[^>]*>', re.I)
    m = open_re.search(page)
    if not m:
        return ""
    i, depth = m.end(), 1
    while depth > 0:
        nxt_open = page.find("<div", i)
        nxt_close = page.find("</div>", i)
        if nxt_close == -1:
            return ""
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            i = nxt_open + 4
        else:
            depth -= 1
            i = nxt_close + 6
    return page[m.end(): i - 6]


_DESC_START = re.compile(r'class="(?:show-more-less-html__markup|description__text)', re.I)
# A closed posting renders <figure class="closed-job ...> with "No longer
# accepting applications" in the top card. Scoped to the pre-description slice:
# recruiter boilerplate quotes these phrases, and a false positive talks the
# scan out of a live job.
_CLOSED = re.compile(r"closed-job__flavor|no longer accepting applications", re.I)


def fetch_detail(job_id: str) -> dict | None:
    """Fetch + parse one job's guest detail page.

    Returns None for a 404 or a posting no longer accepting applications;
    otherwise {"description": markdown} (empty string if the markup drifted).
    """
    page = _get(f"{DETAIL_URL}/{job_id}", {})
    if not page:
        return None
    ds = _DESC_START.search(page)
    topcard = page[: ds.start()] if ds else page
    if _CLOSED.search(topcard):
        return None
    desc_html = (_div_content(page, "show-more-less-html__markup")
                 or _div_content(page, "description__text"))
    return {"description": html_to_markdown(desc_html)}


# --- source entry point -------------------------------------------------------

def fetch_linkedin(agg: Aggregators, known_fps: set[str] | None = None) -> list[Posting]:
    known = known_fps or set()
    cards: dict[str, dict] = {}
    for location in agg.linkedin_locations:
        for term in agg.queries:
            try:
                page = _get(SEARCH_URL, {
                    "keywords": term,
                    "location": location,
                    # f_TPR = time-posted-range in seconds ("r172800" = 2 days).
                    "f_TPR": f"r{agg.linkedin_days * 86400}",
                    "start": "0",
                })
            except Exception as e:
                log.warning("linkedin search failed (%s / %s): %s", term, location, e)
                continue
            for c in parse_cards(page):
                cards.setdefault(c["id"], c)
            _sleep()
    log.info("linkedin: %d unique cards from %d searches",
             len(cards), len(agg.linkedin_locations) * len(agg.queries))

    out: list[Posting] = []
    hydrated = 0
    for c in cards.values():
        base = dict(
            company=c["company"] or "(LinkedIn)",
            ats="linkedin", slug="search", posting_id=c["id"],
            title=c["title"], location=c["location"],
            url=f"https://www.linkedin.com/jobs/view/{c['id']}",
            posted_at=c["posted_at"],
        )
        if f"linkedin:search:{c['id']}" in known:
            # Already stored (description and all) — emit without a detail
            # fetch so touch_seen keeps its last_seen fresh.
            out.append(Posting(description="", **base))
            continue
        if hydrated >= agg.linkedin_max_details:
            # Dropped, not recorded: the next run's f_TPR window re-serves
            # anything still live, so nothing is permanently missed.
            continue
        _sleep()
        try:
            d = fetch_detail(c["id"])
        except Exception as e:
            log.warning("linkedin detail failed for %s: %s", c["id"], e)
            continue
        hydrated += 1
        if d is None:
            continue  # 404 or no longer accepting applications
        out.append(Posting(description=d["description"], **base))
    log.info("linkedin: %d postings (%d details fetched)", len(out), hydrated)
    return out
