"""SQLite store: track which postings we've already scored."""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .config import DATA_DIR

DB_PATH = DATA_DIR / "seen.db"

log = logging.getLogger(__name__)


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
    # Application lifecycle (post-'applied' tracking). applied_at anchors the
    # follow-up quiet-clock; followups is a JSON array of ISO dates a follow-up
    # was sent; notes is free-form (interview stages, contacts, feedback).
    ("applied_at",        "ALTER TABLE postings ADD COLUMN applied_at TEXT"),
    ("status_updated_at", "ALTER TABLE postings ADD COLUMN status_updated_at TEXT"),
    ("followups",         "ALTER TABLE postings ADD COLUMN followups TEXT"),
    ("notes",             "ALTER TABLE postings ADD COLUMN notes TEXT"),
    # sha1 of the normalised description. The same text under DIFFERENT company
    # names is a posting farm, not competing employers.
    ("desc_hash",         "ALTER TABLE postings ADD COLUMN desc_hash TEXT"),
]

POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_status ON postings(status);
CREATE INDEX IF NOT EXISTS idx_desc_hash ON postings(desc_hash);
"""


# --- duplicate-description detection -------------------------------------
#
# On 2026-09-14 thirty-three postings landed on careers-page.com inside six
# minutes under thirty different company names. Five of them (Avenue A Staffing,
# "L3harris", MeetHires, Snapx, Viper Staffing Services) shared one
# byte-identical description; Bridgewater Overhead Doors and Logic Hire shared
# another. Several scored 99-100, because a generic posting overlaps any resume.
# One operator, many names.
#
# A description shared across genuinely different companies is the cheapest
# reliable signal for that. Names are normalised first so real variants
# ("Precisely" / "Precisely US Jobs", "Fanatics" / "Fanatics Betting & Gaming")
# are not mistaken for separate employers.

# Deducted when the same text appears under enough UNRELATED names to mean a
# posting farm. Large on purpose: it should push the posting under min_score
# (60) so it stops being reported, while staying visible with the reason.
DUPLICATE_POSTER_PENALTY = 45

# How many distinct, unrelated companies must share one description before it
# counts as a farm rather than corporate structure. Measured on 2,627 postings:
# at a threshold of 2 the check fires 14 times and 12 of those are parents,
# subsidiaries or abbreviations posting one req (Xplor/Clubessential,
# RELX/Elsevier, Hearst/CDS Global, Sandvik/GibbsCAM, Caesars/Caesars). At 3 it
# fires on the two real farms. Two-name matches are still reported as a concern,
# they just are not penalised -- knowing that two "employers" are one company is
# useful on its own.
DUPLICATE_POSTER_MIN_COMPANIES = 3

# --- posting-burst detection ---------------------------------------------
#
# The duplicate check above only catches an operator who reuses one description.
# Richmond Public Schools (2026-09-14 16:09) had unique text and a real
# institution's name, and still almost certainly was not that institution: the
# district recruits through PowerSchool TalentEd and SchoolSpring, the body
# mentioned nothing about schools, and it landed inside a six-minute window that
# produced ~30 listings under ~25 company names on one self-serve ATS.
#
# Measured over every timestamped posting in the DB: the largest LEGITIMATE
# cluster on a per-employer ATS host is 3 distinct companies in 15 minutes
# (greenhouse, 2026-08-27). The careers-page.com burst was 25. There is a wide
# gap to sit a threshold in.
#
# Aggregators are excluded because they cluster by construction: their
# posted_at is when the board indexed a job, not when an employer posted it.
# Hacker News "Who is hiring" arrives as one thread; WeWorkRemotely, whatjobs
# and Wellfound all show legitimate 3-18 company windows.
BURST_ATS_HOSTS = (
    "careers-page.com", "greenhouse.io", "ashbyhq.com", "ats.rippling.com",
    "comeet.com", "smartrecruiters.com", "lever.co", "workable.com",
    "recruitee.com", "bamboohr.com", "jazzhr.com", "breezy.hr", "myworkday.com",
)
BURST_WINDOW_MINUTES = 15
BURST_MIN_COMPANIES = 8      # legit ATS max observed is 3; the farm was 25
# Moderate on purpose. Some of that burst were real (BridgePoint Works has a
# website and a Clutch profile; David Joseph & Company is a real recruiter), so
# this reorders rather than buries. The named concern is the real payload.
BURST_PENALTY = 25


def _ats_host(url: str) -> str:
    """The per-employer ATS host for a posting URL, or "" if not one."""
    m = re.match(r"https?://([^/]+)", url or "")
    if not m:
        return ""
    host = m.group(1).lower().replace("www.", "")
    for known in BURST_ATS_HOSTS:
        if host.endswith(known):
            return known
    return ""


def burst_cohort(con: sqlite3.Connection, url: str, posted_at: str, company: str) -> int:
    """Distinct OTHER companies that posted to the same ATS within the window.

    Returns 0 when the host is not a per-employer ATS, or when posted_at has no
    real time component (many sources report midnight, which would cluster a
    whole day's postings together).
    """
    host = _ats_host(url)
    if not host or not posted_at or posted_at[11:19] in ("00:00:00", ""):
        return 0
    try:
        when = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    lo = (when - timedelta(minutes=BURST_WINDOW_MINUTES)).isoformat()
    hi = (when + timedelta(minutes=BURST_WINDOW_MINUTES)).isoformat()
    rows = con.execute(
        "SELECT DISTINCT company FROM postings "
        "WHERE url LIKE ? AND posted_at >= ? AND posted_at <= ?",
        (f"%{host}%", lo, hi),
    )
    return len({r[0] for r in rows if r[0] and not _same_company(r[0], company)})

_LEGAL_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|co|gmbh|plc|lp|llp|pllc"
    r"|group|holdings|solutions|services|staffing)\b"
)


def _norm_company(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    n = _LEGAL_SUFFIX.sub(" ", n)
    return " ".join(n.split())


def _acronym(name: str) -> str:
    """Initials of a company name, before legal suffixes are stripped."""
    words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
    return "".join(w[0] for w in words if w)


def _same_company(a: str, b: str) -> bool:
    """True when two names plausibly denote the same employer.

    Catches exact matches, prefixes ("Precisely" / "Precisely US Jobs") and
    acronyms ("gdit" / "General Dynamics Information Technology"). Corporate
    families posting one req under a parent and a brand are NOT a posting farm.
    """
    na, nb = _norm_company(a), _norm_company(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 4 and long.startswith(short):
        return True
    # acronym, computed on the raw names so stripped suffixes still contribute
    ra, rb = a or "", b or ""
    for tiny, full in ((ra, rb), (rb, ra)):
        t = re.sub(r"[^a-z0-9]+", "", tiny.lower())
        if 2 <= len(t) <= 5 and t == _acronym(full):
            return True
    return False


def desc_hash(description: str) -> str:
    """Stable hash of a posting body, or "" when too short to be meaningful."""
    text = " ".join((description or "").split()).lower()
    if len(text) < 200:
        return ""
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def duplicate_posters(con: sqlite3.Connection, dhash: str, company: str) -> list[str]:
    """Other company names that posted this exact description."""
    if not dhash:
        return []
    rows = con.execute("SELECT DISTINCT company FROM postings WHERE desc_hash = ?", (dhash,))
    return sorted({r[0] for r in rows if r[0] and not _same_company(r[0], company)})


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# Data backfills, run on every connect rather than only when a column is first
# added: tying them to column creation makes them silently skippable (a caller
# that adds the column its own way loses the backfill), and each is written to
# be idempotent so repeating it is free.
BACKFILLS = [
    # Rows already 'applied' predate applied_at; their real submission date was
    # never recorded, so seed the quiet-clock with first_seen. A proxy, usually
    # a few days early, which only makes the follow-up nudge err on the prompt
    # side. The NULL guard keeps it from ever overwriting a real date.
    "UPDATE postings SET applied_at = first_seen "
    "WHERE applied_at IS NULL AND status IN ('applied', 'interview', 'offer')",
]


def _apply_migrations(con: sqlite3.Connection) -> None:
    existing = {row["name"] for row in con.execute("PRAGMA table_info(postings)")}
    for col, ddl in MIGRATIONS:
        if col not in existing:
            con.execute(ddl)
    con.executescript(POST_MIGRATION_INDEXES)
    for sql in BACKFILLS:
        con.execute(sql)
    _merge_wwr_duplicates(con)


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

    # Same body, different company name = posting farm. Demote hard and say why.
    dhash = desc_hash(description)
    others = duplicate_posters(con, dhash, company)
    if others:
        shown = ", ".join(others[:4]) + (f" and {len(others) - 4} more" if len(others) > 4 else "")
        if len(others) + 1 >= DUPLICATE_POSTER_MIN_COMPANIES:
            concerns = [
                f"Posting farm: identical text published under {len(others) + 1} unrelated "
                f"company names ({shown})."
            ] + list(concerns or [])
            score = max(0, score - DUPLICATE_POSTER_PENALTY)
            log.warning("posting farm: %s shares its description with %s", company, shown)
        else:
            concerns = [
                f"Identical posting text also published by {shown} "
                f"(likely the same company under another name)."
            ] + list(concerns or [])

    # Burst: many unrelated companies posting to one self-serve ATS at once.
    cohort = burst_cohort(con, url, posted_at, company)
    if cohort + 1 >= BURST_MIN_COMPANIES:
        concerns = [
            f"Posted in a burst: {cohort + 1} different companies published to "
            f"{_ats_host(url)} within {BURST_WINDOW_MINUTES} minutes of this one. "
            f"Verify the employer independently before applying."
        ] + list(concerns or [])
        score = max(0, score - BURST_PENALTY)
        log.warning("burst posting: %s is 1 of %d companies on %s within %dmin",
                    company, cohort + 1, _ats_host(url), BURST_WINDOW_MINUTES)

    s_json = json.dumps(strengths or [])
    c_json = json.dumps(concerns or [])
    con.execute(
        """INSERT INTO postings
           (fingerprint, company, title, url, location, score, rationale,
            strengths, concerns, description, status, desc_hash,
            skill_fit, seniority_fit, domain_fit, location_tier,
            posted_at, freshness_days, interview_style,
            first_seen, last_seen, notified)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?,
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
             interview_style = excluded.interview_style,
             desc_hash       = excluded.desc_hash""",
        (fingerprint, company, title, url, location, score, rationale,
         s_json, c_json, description, dhash,
         skill_fit, seniority_fit, domain_fit, location_tier,
         posted_at, freshness_days, interview_style, now, now),
    )


# Application lifecycle. pending/visited are pre-application; applied/interview
# are open applications (the quiet-clock runs on them); offer/rejected/
# no_response are resolved; dismissed means "not applying".
STATUSES = {"pending", "visited", "applied", "interview", "offer",
            "rejected", "no_response", "dismissed"}
ACTIVE_APP_STATUSES = ("applied", "interview")
POST_APPLIED_STATUSES = ("applied", "interview", "offer", "rejected", "no_response")


def set_status(con: sqlite3.Connection, fingerprint: str, status: str) -> bool:
    """Change a posting's status, stamping the lifecycle timestamps.

    applied_at is set once, on the first transition into 'applied'; later
    stages keep it, since it anchors "days since I applied".

    Setting a status a posting ALREADY has is a no-op: status_updated_at feeds
    the quiet-day clock as "when something last actually happened", so letting
    a re-click of the current value stamp it would silently reset the clock and
    hide a long-quiet application from the follow-up nudge.
    """
    row = con.execute(
        "SELECT status FROM postings WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    if row is None:
        return False
    if (row["status"] or "pending") == status:
        return True  # found, nothing to change

    now = _now()
    con.execute(
        "UPDATE postings SET status = ?, status_updated_at = ? WHERE fingerprint = ?",
        (status, now, fingerprint),
    )
    if status == "applied":
        con.execute(
            "UPDATE postings SET applied_at = ? WHERE fingerprint = ? AND applied_at IS NULL",
            (now, fingerprint),
        )
    return True


def add_followup(con: sqlite3.Connection, fingerprint: str) -> list[str] | None:
    """Log that a follow-up was sent today; returns the updated date list
    (None if the posting doesn't exist). The latest entry resets the
    quiet-clock — an unlogged follow-up breaks the days-quiet math."""
    import json
    row = con.execute(
        "SELECT followups FROM postings WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    if row is None:
        return None
    try:
        dates = json.loads(row["followups"]) if row["followups"] else []
    except json.JSONDecodeError:
        dates = []
    dates.append(datetime.now().date().isoformat())
    con.execute(
        "UPDATE postings SET followups = ? WHERE fingerprint = ?",
        (json.dumps(dates), fingerprint),
    )
    return dates


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def quiet_days(row: sqlite3.Row) -> int | None:
    """Days since the last touch on an application: applied_at, the latest
    logged follow-up, or the latest status change — whichever is newest (a
    status bump like applied -> interview means they contacted you)."""
    import json
    touches = [_parse_dt(row["applied_at"]), _parse_dt(row["status_updated_at"])]
    try:
        touches += [_parse_dt(d) for d in json.loads(row["followups"] or "[]")]
    except json.JSONDecodeError:
        pass
    touches = [t for t in touches if t]
    if not touches:
        return None
    return max(0, (datetime.now() - max(touches)).days)


def followups_due(
    con: sqlite3.Connection, threshold_days: int,
) -> tuple[list[dict], list[dict]]:
    """Split open applications gone quiet into (due, stale).

    due: threshold passed since the last touch, fewer than two follow-ups sent
    (after two silent follow-ups the honest move is recording no_response, not
    a third nudge), and not yet ancient — a follow-up note is still plausible.
    stale: quiet at least 3x the threshold, or two follow-ups already silent —
    past chasing; the suggestion is recording no_response instead.
    """
    import json
    due, stale = [], []
    stale_after = threshold_days * 3
    qs = ",".join("?" * len(ACTIVE_APP_STATUSES))
    for r in con.execute(
        f"SELECT * FROM postings WHERE status IN ({qs})", ACTIVE_APP_STATUSES
    ):
        q = quiet_days(r)
        if q is None or q < threshold_days:
            continue
        try:
            n_fu = len(json.loads(r["followups"] or "[]"))
        except json.JSONDecodeError:
            n_fu = 0
        entry = {"row": dict(r), "quiet_days": q, "followups_sent": n_fu}
        if q >= stale_after or n_fu >= 2:
            stale.append(entry)
        else:
            due.append(entry)
    due.sort(key=lambda d: -d["quiet_days"])
    stale.sort(key=lambda d: -d["quiet_days"])
    return due, stale


# How much a status is worth keeping when two rows for one job are merged:
# a real action you took beats a judgement, which beats an untouched default.
_STATUS_RANK = {"offer": 7, "interview": 6, "applied": 5, "rejected": 4,
                "no_response": 3, "dismissed": 2, "visited": 1, "pending": 0}


def _merge_wwr_duplicates(con: sqlite3.Connection) -> int:
    """Re-key WeWorkRemotely rows that carry an RSS category as their slug.

    The category a job was found in used to sit in its fingerprint, so a job
    cross-listed in several feeds was stored (and LLM-scored) once per feed.
    The slug is now a constant, and this folds the historical duplicates into
    one row each, keeping the furthest-along status and the widest seen-window.
    Idempotent: after it runs, the guard below matches nothing.
    """
    stale = con.execute(
        "SELECT * FROM postings WHERE fingerprint LIKE 'weworkremotely:%' "
        "AND fingerprint NOT LIKE 'weworkremotely:weworkremotely:%'"
    ).fetchall()
    if not stale:
        return 0

    # Group every WWR row by the identity it SHOULD have had. Rows already
    # correctly keyed join their group so an existing row is never orphaned
    # by a PRIMARY KEY collision.
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in con.execute("SELECT * FROM postings WHERE fingerprint LIKE 'weworkremotely:%'"):
        parts = r["fingerprint"].split(":", 2)
        if len(parts) != 3:
            continue
        groups.setdefault(f"weworkremotely:weworkremotely:{parts[2]}", []).append(r)

    merged = 0
    for new_fp, rows in groups.items():
        if len(rows) == 1 and rows[0]["fingerprint"] == new_fp:
            continue
        # Which copy supplies the descriptive columns. They are near-identical
        # (same RSS item), so this only has to be deterministic: furthest-along
        # status, then the most recently refreshed copy.
        keep = max(rows, key=lambda r: (_STATUS_RANK.get(r["status"] or "pending", 0),
                                        r["last_seen"] or ""))
        first = min((r["first_seen"] for r in rows if r["first_seen"]), default=keep["first_seen"])
        last = max((r["last_seen"] for r in rows if r["last_seen"]), default=keep["last_seen"])
        status = max((r["status"] or "pending" for r in rows),
                     key=lambda s: _STATUS_RANK.get(s, 0))
        # Lifecycle data is rare and precious: take it from whichever copy has it.
        def first_set(col: str):
            return next((r[col] for r in rows if r[col]), None)

        for r in rows:
            if r["fingerprint"] != new_fp:
                con.execute("DELETE FROM postings WHERE fingerprint = ?", (r["fingerprint"],))
        con.execute(
            """INSERT INTO postings (fingerprint, company, title, url, location, score,
                   skill_fit, seniority_fit, domain_fit, location_tier, posted_at,
                   freshness_days, interview_style, rationale, strengths, concerns,
                   description, status, first_seen, last_seen, notified, gone, gone_at,
                   applied_at, status_updated_at, followups, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_fp, keep["company"], keep["title"], keep["url"], keep["location"],
             keep["score"], keep["skill_fit"], keep["seniority_fit"], keep["domain_fit"],
             keep["location_tier"], keep["posted_at"], keep["freshness_days"],
             keep["interview_style"], keep["rationale"], keep["strengths"], keep["concerns"],
             keep["description"], status, first, last,
             max(int(r["notified"] or 0) for r in rows),
             min(int(r["gone"] or 0) for r in rows), first_set("gone_at"),
             first_set("applied_at"), first_set("status_updated_at"),
             first_set("followups"), first_set("notes")),
        )
        merged += len(rows) - 1
    if merged:
        log.info("re-keyed WeWorkRemotely fingerprints, merging %d duplicate row(s)", merged)
    return merged


def record_unscored(con: sqlite3.Connection, posting) -> None:
    """Insert a fetched posting with no score (LLM unavailable at scan time).

    The row enters dedup immediately — the next run's filter_new skips it, so
    nothing is fetched or queued twice — but score stays NULL, which keeps it
    below every dashboard cutoff until `rescore --unscored-only` fills it in.
    """
    now = _now()
    con.execute(
        """INSERT INTO postings
           (fingerprint, company, title, url, location, description,
            posted_at, status, first_seen, last_seen, notified)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, 0)
           ON CONFLICT(fingerprint) DO NOTHING""",
        (posting.fingerprint, posting.company, posting.title, posting.url,
         posting.location, posting.description, posting.posted_at, now, now),
    )


def count_unscored(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM postings WHERE score IS NULL").fetchone()[0]


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
