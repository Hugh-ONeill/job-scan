"""Normalized job posting + dispatcher."""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from ..config import Company

log = logging.getLogger(__name__)


@dataclass
class Posting:
    company: str
    ats: str
    slug: str
    posting_id: str   # stable per company
    title: str
    location: str
    url: str
    description: str  # plain text
    posted_at: str = ""  # ISO-8601 original publish time, "" if unknown
    priority: bool = False

    @property
    def fingerprint(self) -> str:
        return f"{self.ats}:{self.slug}:{self.posting_id}"


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def html_to_text(html_str: str) -> str:
    if not html_str:
        return ""
    # Cheap HTML strip; good enough for job descriptions.
    text = _TAG.sub(" ", html_str)
    text = html.unescape(text)  # &amp; &#39; &#x2F; etc.
    return _WS.sub(" ", text).strip()


def normalize_date(value) -> str:
    """Normalize an ATS date field to an ISO-8601 string, or "" if unusable.

    Accepts ISO strings (Greenhouse first_published, Ashby publishedAt) and
    epoch milliseconds (Lever createdAt). Anything unparseable -> "".
    """
    if value in (None, ""):
        return ""
    # Epoch milliseconds (Lever).
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return ""
    s = str(value).strip()
    if not s:
        return ""
    # Numeric string that's really epoch ms.
    if s.isdigit():
        return normalize_date(int(s))
    try:
        # fromisoformat handles the "+00:00" / offset forms these APIs return.
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return ""


def fetch_all(companies: Iterable[Company]) -> list[Posting]:
    # Imported here to avoid circular import at module load.
    from .greenhouse import fetch_greenhouse
    from .lever import fetch_lever
    from .ashby import fetch_ashby
    from .workday import fetch_workday
    from .smartrecruiters import fetch_smartrecruiters

    handlers = {
        "greenhouse": fetch_greenhouse,
        "lever": fetch_lever,
        "ashby": fetch_ashby,
        "workday": fetch_workday,
        "smartrecruiters": fetch_smartrecruiters,
    }
    out: list[Posting] = []
    for c in companies:
        h = handlers.get(c.ats)
        if not h:
            log.warning("unknown ATS %r for %s, skipping", c.ats, c.name)
            continue
        try:
            postings = h(c)
        except Exception as e:
            log.warning("scrape failed for %s (%s/%s): %s", c.name, c.ats, c.slug, e)
            continue
        out.extend(postings)
    return out
