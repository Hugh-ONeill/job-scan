"""Today's picks: the few best open postings to apply to, every day.

The floor, not the ceiling: on a tired day, these are the ones to send. Picks
are computed live, so a posting applied to or dismissed drops out and the next
best takes its place. Rules:

  - open (pending/visited), still on its board, at or above min_score;
  - not a repeat of a role already applied to (store.applied_before);
  - one per company, so a reposter can't fill the list;
  - first seen recently, widening the window when fewer than `n` qualify.
"""
from __future__ import annotations

import sqlite3

from .store import applied_before, prior_applications, role_key

# Days since first seen, tried in order until there are enough picks.
WINDOWS: tuple[int, ...] = (2, 7, 30)


def daily_picks(con: sqlite3.Connection, min_score: int, max_age_days: int, n: int = 3) -> list[dict]:
    """The `n` best open postings, freshest window first."""
    prior = prior_applications(con)
    age_clause = ("AND julianday('now') - julianday(COALESCE(NULLIF(posted_at, ''), first_seen)) <= ?"
                  if max_age_days else "")
    picks: list[dict] = []
    companies: set[str] = set()
    roles: set[str] = set()
    for days in WINDOWS:
        params: list[int] = [min_score, days] + ([max_age_days] if max_age_days else [])
        rows = con.execute(
            f"""SELECT * FROM postings
                WHERE score >= ? AND COALESCE(status, 'pending') IN ('pending', 'visited')
                  AND COALESCE(gone, 0) = 0
                  AND julianday('now') - julianday(first_seen) <= ?
                  {age_clause}
                ORDER BY score DESC, freshness_days IS NULL, freshness_days ASC, first_seen DESC""",
            params).fetchall()
        for r in rows:
            company = (r["company"] or "").casefold()
            rk = role_key(r["company"], r["title"])
            if company in companies or rk in roles:
                continue
            if applied_before(prior, r["fingerprint"], r["company"], r["title"]):
                continue
            companies.add(company)
            roles.add(rk)
            picks.append({**dict(r), "window_days": days})
            if len(picks) == n:
                return picks
    return picks


def applied_today(con: sqlite3.Connection) -> int:
    """Applications marked today (local time; store writes naive local stamps)."""
    return con.execute(
        "SELECT COUNT(*) FROM postings WHERE applied_at IS NOT NULL "
        "AND date(applied_at) = date('now', 'localtime')").fetchone()[0]
