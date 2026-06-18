"""SQLite store: track which postings we've already scored."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

DB_PATH = DATA_DIR / "seen.db"


def dedupe_key(company: str | None, title: str | None) -> str:
    """Collapse near-duplicate board reqs to a single logical role.

    Each posting keeps its own fingerprint (ats:slug:job-id), so the same role
    re-listed under multiple job IDs — Brex/Oscar post one req per office,
    RemoteOK bumps a job with a fresh ID — is correctly stored as distinct rows
    but is the SAME role to a job seeker. This key (normalized company + title)
    is what the report and dashboard collapse on for display.
    """
    c = " ".join((company or "").split()).casefold()
    t = " ".join((title or "").split()).casefold()
    return f"{c}\x1f{t}"

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    fingerprint TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    location    TEXT,
    score       INTEGER,
    skill_fit     INTEGER,
    seniority_fit INTEGER,
    domain_fit    INTEGER,
    location_tier INTEGER,
    posted_at     TEXT,     -- ISO-8601 original publish time, "" if unknown
    freshness_days INTEGER, -- age in days at time of scoring
    interview_style TEXT,   -- "take-home"/"pair"/... if known, else ""
    rationale   TEXT,
    strengths   TEXT,       -- JSON array
    concerns    TEXT,       -- JSON array
    description TEXT,
    status      TEXT DEFAULT 'pending',  -- pending | applied | dismissed
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    notified    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_score ON postings(score DESC);
CREATE INDEX IF NOT EXISTS idx_last_seen ON postings(last_seen);
"""

# Columns added after v0.1.0. Applied to old DBs on connect().
# Run BEFORE the post-migration indexes below.
MIGRATIONS = [
    ("strengths",   "ALTER TABLE postings ADD COLUMN strengths TEXT"),
    ("concerns",    "ALTER TABLE postings ADD COLUMN concerns TEXT"),
    ("description", "ALTER TABLE postings ADD COLUMN description TEXT"),
    ("status",      "ALTER TABLE postings ADD COLUMN status TEXT DEFAULT 'pending'"),
    # Decomposed-scoring columns (the LLM sub-scores + posting freshness).
    ("skill_fit",      "ALTER TABLE postings ADD COLUMN skill_fit INTEGER"),
    ("seniority_fit",  "ALTER TABLE postings ADD COLUMN seniority_fit INTEGER"),
    ("domain_fit",     "ALTER TABLE postings ADD COLUMN domain_fit INTEGER"),
    ("location_tier",  "ALTER TABLE postings ADD COLUMN location_tier INTEGER"),
    ("posted_at",      "ALTER TABLE postings ADD COLUMN posted_at TEXT"),
    ("freshness_days", "ALTER TABLE postings ADD COLUMN freshness_days INTEGER"),
    ("interview_style", "ALTER TABLE postings ADD COLUMN interview_style TEXT"),
    # 1 once a posting disappears from its board (filled/pulled) — dead link.
    ("gone",     "ALTER TABLE postings ADD COLUMN gone INTEGER DEFAULT 0"),
    ("gone_at",  "ALTER TABLE postings ADD COLUMN gone_at TEXT"),
]

POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_status ON postings(status);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _apply_migrations(con: sqlite3.Connection) -> None:
    existing = {row["name"] for row in con.execute("PRAGMA table_info(postings)")}
    for col, ddl in MIGRATIONS:
        if col not in existing:
            con.execute(ddl)
    con.executescript(POST_MIGRATION_INDEXES)


@contextmanager
def connect(path: Path = DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA)
        _apply_migrations(con)
        yield con
        con.commit()
    finally:
        con.close()


def filter_new(con: sqlite3.Connection, fingerprints: list[str]) -> set[str]:
    """Return the subset of fingerprints we have NOT scored yet."""
    if not fingerprints:
        return set()
    qs = ",".join("?" * len(fingerprints))
    rows = con.execute(
        f"SELECT fingerprint FROM postings WHERE fingerprint IN ({qs})",
        fingerprints,
    ).fetchall()
    seen = {r["fingerprint"] for r in rows}
    return {fp for fp in fingerprints if fp not in seen}


def touch_seen(con: sqlite3.Connection, fingerprints: list[str]) -> None:
    """Update last_seen for postings we already had (keeps them 'alive')."""
    if not fingerprints:
        return
    now = _now()
    con.executemany(
        "UPDATE postings SET last_seen = ? WHERE fingerprint = ?",
        [(now, fp) for fp in fingerprints],
    )


def record_scored(
    con: sqlite3.Connection,
    fingerprint: str,
    company: str,
    title: str,
    url: str,
    location: str,
    score: int,
    rationale: str,
    strengths: list[str] | None = None,
    concerns: list[str] | None = None,
    description: str = "",
    skill_fit: int | None = None,
    seniority_fit: int | None = None,
    domain_fit: int | None = None,
    location_tier: int | None = None,
    posted_at: str = "",
    freshness_days: int | None = None,
    interview_style: str = "",
) -> None:
    import json
    now = _now()
    s_json = json.dumps(strengths or [])
    c_json = json.dumps(concerns or [])
    con.execute(
        """INSERT INTO postings
           (fingerprint, company, title, url, location, score, rationale,
            strengths, concerns, description, status,
            skill_fit, seniority_fit, domain_fit, location_tier,
            posted_at, freshness_days, interview_style,
            first_seen, last_seen, notified)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending',
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
           ON CONFLICT(fingerprint) DO UPDATE SET
             last_seen     = excluded.last_seen,
             score         = excluded.score,
             rationale     = excluded.rationale,
             strengths     = excluded.strengths,
             concerns      = excluded.concerns,
             description   = excluded.description,
             skill_fit     = excluded.skill_fit,
             seniority_fit = excluded.seniority_fit,
             domain_fit    = excluded.domain_fit,
             location_tier = excluded.location_tier,
             posted_at     = excluded.posted_at,
             freshness_days = excluded.freshness_days,
             interview_style = excluded.interview_style""",
        (fingerprint, company, title, url, location, score, rationale,
         s_json, c_json, description,
         skill_fit, seniority_fit, domain_fit, location_tier,
         posted_at, freshness_days, interview_style, now, now),
    )


# ATS sources whose boards are authoritative about liveness (a removed job is
# simply absent from the API). Aggregator feeds (HN/RemoteOK/...) are partial and
# rolling, so we never auto-mark their postings gone.
LIVENESS_ATS = {"greenhouse", "lever", "ashby", "workday", "smartrecruiters"}


def mark_liveness(
    con: sqlite3.Connection,
    fetched_fps: set[str],
    covered_sources: set[tuple[str, str]],
) -> tuple[int, int]:
    """Flag postings that have vanished from a board we successfully scanned.

    A posting is marked gone only if (a) it's from a liveness-tracked ATS,
    (b) that exact (ats, slug) board returned at least one posting this run (so a
    transient board error doesn't nuke everything), and (c) its fingerprint
    wasn't in the fetch. Postings seen again are un-marked. Returns (gone, revived).
    """
    now = _now()
    gone, revived = [], []
    for row in con.execute("SELECT fingerprint, gone FROM postings"):
        fp = row["fingerprint"]
        parts = fp.split(":", 2)
        ats, slug = (parts[0], parts[1] if len(parts) > 1 else "")
        if fp in fetched_fps:
            if row["gone"]:
                revived.append(fp)
        elif ats in LIVENESS_ATS and (ats, slug) in covered_sources and not row["gone"]:
            gone.append(fp)
    if gone:
        con.executemany(
            "UPDATE postings SET gone = 1, gone_at = ? WHERE fingerprint = ?",
            [(now, fp) for fp in gone],
        )
    if revived:
        con.executemany(
            "UPDATE postings SET gone = 0, gone_at = NULL WHERE fingerprint = ?",
            [(fp,) for fp in revived],
        )
    return len(gone), len(revived)


def mark_notified(con: sqlite3.Connection, fingerprints: list[str]) -> None:
    if not fingerprints:
        return
    qs = ",".join("?" * len(fingerprints))
    con.execute(f"UPDATE postings SET notified = 1 WHERE fingerprint IN ({qs})", fingerprints)
