"""Today's picks: the few best open postings to apply to, every day.

The floor, not the ceiling: on a tired day, these are the ones to send. Picks
are computed live, so a posting applied to or dismissed drops out and the next
best takes its place. Rules:

  - open (pending/visited), still on its board, at or above min_score;
  - not a repeat of a role already applied to (store.applied_before);
  - one per company, so a reposter can't fill the list;
  - no company already sent MAX_PER_COMPANY applications (the ~3-per-employer
    ceiling: never suggest a 4th);
  - published in the last RECENT_DAYS days (first seen, when the source has
    no publish date).

They show 14:00-20:00 only (outside that window the applying doesn't happen)
and go away for the day once DAILY_GOAL applications are marked.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import datetime

from .store import _COMPANY_NOISE, applied_before, prior_applications, role_key

RECENT_DAYS = 3
SHOW_FROM_HOUR, SHOW_UNTIL_HOUR = 14, 20
DAILY_GOAL = 3
# A company with this many applications already is not suggested again.
MAX_PER_COMPANY = 3
APPLIED_STATUSES = ("applied", "interview", "offer", "rejected", "no_response")
_PARENS = re.compile(r"\([^)]*\)")


def company_key(name: str | None) -> str:
    """One employer across name variants: "JPMorgan Chase & Co." and
    "JPMorganChase", "Enveritas (YC S18, non-profit)" and "Enveritas"."""
    c = _COMPANY_NOISE.sub("", _PARENS.sub("", (name or "").casefold()))
    return re.sub(r"[^a-z0-9]", "", c)


def applications_by_company(con: sqlite3.Connection) -> Counter[str]:
    marks = ",".join("?" * len(APPLIED_STATUSES))
    rows = con.execute(
        f"SELECT company FROM postings WHERE applied_at IS NOT NULL OR status IN ({marks})",
        APPLIED_STATUSES)
    return Counter(company_key(r[0]) for r in rows)


def daily_picks(con: sqlite3.Connection, min_score: int, n: int = 3) -> list[dict]:
    """The `n` best open postings published in the last RECENT_DAYS days."""
    prior = prior_applications(con)
    sent = applications_by_company(con)
    rows = con.execute(
        """SELECT * FROM postings
           WHERE score >= ? AND COALESCE(status, 'pending') IN ('pending', 'visited')
             AND COALESCE(gone, 0) = 0
             AND julianday('now') - julianday(COALESCE(NULLIF(posted_at, ''), first_seen)) <= ?
           ORDER BY score DESC, freshness_days IS NULL, freshness_days ASC, first_seen DESC""",
        (min_score, RECENT_DAYS)).fetchall()
    picks: list[dict] = []
    companies: set[str] = set()
    roles: set[str] = set()
    for r in rows:
        company = company_key(r["company"])
        rk = role_key(r["company"], r["title"])
        if company in companies or rk in roles or sent[company] >= MAX_PER_COMPANY:
            continue
        if applied_before(prior, r["fingerprint"], r["company"], r["title"]):
            continue
        companies.add(company)
        roles.add(rk)
        picks.append({**dict(r), "company_applications": sent[company]})
        if len(picks) == n:
            break
    return picks


def hidden_reason(done_today: int, now: datetime | None = None) -> str:
    """Why the picks are off right now ("" when they should show)."""
    hour = (now or datetime.now()).hour
    if not SHOW_FROM_HOUR <= hour < SHOW_UNTIL_HOUR:
        return f"picks show {SHOW_FROM_HOUR}:00-{SHOW_UNTIL_HOUR}:00"
    if done_today >= DAILY_GOAL:
        return f"{done_today} applied today; done"
    return ""


def applied_today(con: sqlite3.Connection) -> int:
    """Applications marked today (local time; store writes naive local stamps)."""
    return con.execute(
        "SELECT COUNT(*) FROM postings WHERE applied_at IS NOT NULL "
        "AND date(applied_at) = date('now', 'localtime')").fetchone()[0]
