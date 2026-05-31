"""Company interview-style lookup, from the community-maintained
`poteto/hiring-without-whiteboards` list (companies that hire with take-homes /
pair programming / real-work exercises instead of algorithmic puzzles).

The list is fetched once and cached to data/ for a week. Matching is by
normalized company name. This is meta-knowledge about a company's process — it
almost never appears in the posting itself — so it's applied as a deterministic
score nudge + a visible tag, the same way location and freshness are.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime

import requests

from .config import DATA_DIR

log = logging.getLogger(__name__)

RAW_URL = "https://raw.githubusercontent.com/poteto/hiring-without-whiteboards/master/README.md"
CACHE = DATA_DIR / "no_whiteboard.json"
MAX_AGE_DAYS = 7

# A bullet like: "- [Name](url) | Location | Process description..."
_ENTRY = re.compile(r"^- \[(?P<name>[^\]]+)\]\((?P<url>[^)]*)\)\s*\|\s*(?P<rest>.+)$")
_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|co|technologies|technology|labs|software|the)\b")


def normalize(name: str) -> str:
    name = name.lower()
    name = re.sub(r"\(.*?\)", " ", name)        # drop "(2020)" etc.
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    name = _SUFFIXES.sub(" ", name)
    return re.sub(r"\s+", " ", name).strip()


def _parse(md: str) -> dict[str, dict]:
    table: dict[str, dict] = {}
    for line in md.splitlines():
        m = _ENTRY.match(line.strip())
        if not m:
            continue
        name = m.group("name").strip()
        rest = m.group("rest")
        # rest is "Location | Process"; the process is everything after the
        # first pipe (process text itself may contain pipes).
        process = rest.split("|", 1)[1].strip() if "|" in rest else rest.strip()
        key = normalize(name)
        if key:
            table[key] = {"name": name, "url": m.group("url"), "process": process}
    return table


def _fresh_cache() -> dict | None:
    if not CACHE.exists():
        return None
    try:
        blob = json.loads(CACHE.read_text())
        fetched = datetime.fromisoformat(blob["fetched"]).date()
        if (date.today() - fetched).days <= MAX_AGE_DAYS:
            return blob["table"]
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
    return None


def load_no_whiteboard(force: bool = False) -> dict[str, dict]:
    """Return {normalized_name: {name, url, process}}; cached for a week."""
    if not force:
        cached = _fresh_cache()
        if cached is not None:
            return cached
    try:
        md = requests.get(RAW_URL, headers={"User-Agent": "job-scan/0.1"}, timeout=30).text
        table = _parse(md)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"fetched": date.today().isoformat(), "table": table}))
        log.info("hiring-without-whiteboards: %d companies", len(table))
        return table
    except Exception as e:
        log.warning("could not refresh no-whiteboard list: %s", e)
        stale = _fresh_cache()
        return stale if stale is not None else {}


# Posting-description signals, for companies not on the curated list.
_DESC_SIGNALS = {
    "take-home": re.compile(r"take[- ]home|work sample", re.I),
    "pair-programming": re.compile(r"pair[- ]program|pairing (session|interview|exercise)", re.I),
    "no-whiteboard": re.compile(r"no whiteboard|without whiteboard|no leet[- ]?code|real[- ]world (problem|exercise)", re.I),
}


def _tag_from_process(process: str) -> str:
    p = process.lower()
    tags = []
    if re.search(r"take[- ]home|take home", p):
        tags.append("take-home")
    if "pair" in p:
        tags.append("pair")
    return ", ".join(tags) if tags else "no-whiteboard"


def classify(company: str, description: str, table: dict[str, dict]) -> str:
    """Return a short interview-style tag, or '' if no signal.

    Curated-list membership wins (it's higher-confidence); otherwise fall back
    to keywords in the posting description.
    """
    entry = table.get(normalize(company))
    if entry:
        return _tag_from_process(entry["process"])
    desc = description or ""
    hits = [name for name, rx in _DESC_SIGNALS.items() if rx.search(desc)]
    if hits:
        # Mark description-sourced hits so they're distinguishable from the list.
        return ", ".join(hits) + " (posting)"
    return ""
