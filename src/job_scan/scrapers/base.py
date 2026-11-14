"""Normalized job posting + dispatcher."""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from ..config import Company

log = logging.getLogger(__name__)


# source_meta["apply_method"] value for LinkedIn's one-click applications.
EASY_APPLY = "Easy Apply"


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
    # Concerns the source itself raised (a repost, a mass posting); stored
    # ahead of the model's own concerns.
    flags: list[str] = field(default_factory=list)
    # What the source lists beside the description, keyed by
    # store.SOURCE_META_COLUMNS (LinkedIn: seniority_label, employment_type,
    # applicants, apply_method). Display only, except apply_method, which
    # earns scoring.easy_apply_points when it is "Easy Apply".
    source_meta: dict[str, str] = field(default_factory=dict)

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


_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_LINK = re.compile(r"<a\b[^>]*?href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_BOLD = re.compile(r"<(strong|b)\b[^>]*>(.*?)</\1>", re.I | re.S)
_ITALIC = re.compile(r"<(em|i)\b[^>]*>(.*?)</\1>", re.I | re.S)
_HEADING = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.I | re.S)
_LI = re.compile(r"<li\b[^>]*>(.*?)</li>", re.I | re.S)
_BLOCK_END = re.compile(r"</(p|div|ul|ol|tr|table|section|h[1-6])>", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_MANY_NL = re.compile(r"\n{3,}")


def html_to_markdown(html_str: str) -> str:
    """Convert job-description HTML to lightweight Markdown.

    Preserves the structure that makes a posting readable (headings, bullet
    lists, bold, links, paragraphs) instead of flattening to one plain line.
    Regex-based and intentionally simple, like html_to_text; good enough for
    the messy, varied HTML that ATS boards emit.
    """
    if not html_str:
        return ""
    # ATS boards often entity-encode their HTML ("&lt;li&gt;"), sometimes twice.
    # Unescape up front so the tags are real before we convert them.
    s = html.unescape(html_str)
    if "&lt;" in s or "&gt;" in s:
        s = html.unescape(s)
    # If there are still no tags, it's already plain/markdown text.
    if "<" not in s:
        return _MANY_NL.sub("\n\n", s).strip()
    s = _SCRIPT_STYLE.sub("", s)
    s = _LINK.sub(lambda m: f"[{_inline(m.group(2))}]({m.group(1).strip()})", s)
    s = _BOLD.sub(lambda m: f"**{m.group(2).strip()}**", s)
    s = _ITALIC.sub(lambda m: f"*{m.group(2).strip()}*", s)
    s = _HEADING.sub(lambda m: f"\n\n{'#' * int(m.group(1))} {_inline(m.group(2))}\n\n", s)
    s = _LI.sub(lambda m: f"\n- {_inline(m.group(1))}", s)
    s = _BR.sub("\n", s)
    s = _BLOCK_END.sub("\n\n", s)
    s = _TAG.sub("", s)            # strip any remaining tags
    # Tidy each line, then collapse runs of blank lines.
    lines = [re.sub(r"[ \t]+", " ", ln).rstrip() for ln in s.split("\n")]
    return _MANY_NL.sub("\n\n", "\n".join(lines)).strip()


def _inline(fragment: str) -> str:
    """Collapse inline tags/whitespace inside a heading/list-item/link label."""
    return _WS.sub(" ", _TAG.sub("", fragment)).strip()


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
