"""Flask dashboard: browse scored postings, mark applied/dismissed."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request

from .config import DATA_DIR
from .store import DB_PATH, MIGRATIONS, POST_MIGRATION_INDEXES, SCHEMA, dedupe_key

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

VALID_STATUSES = {"pending", "visited", "applied", "dismissed"}


def _age_limits() -> tuple[int, dict[str, int]]:
    """Stale-cutoffs for the dashboard, from settings: (global max_age_days,
    per-ATS overrides). Fail open (0, {} = no filter) so a settings hiccup
    never blanks the whole board."""
    try:
        from .config import load_settings
        s = load_settings()
        return int(s.max_age_days), dict(s.ats_max_age_days)
    except Exception:
        return 0, {}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


_migration_applied = False


def _try_ensure_schema() -> None:
    """Apply schema + migrations if possible. Tolerant of a concurrent writer
    holding the DB lock — we can re-try on the next request."""
    global _migration_applied
    if _migration_applied or not DB_PATH.exists():
        return
    try:
        with _connect() as con:
            con.executescript(SCHEMA)
            existing = {row["name"] for row in con.execute("PRAGMA table_info(postings)")}
            for col, ddl in MIGRATIONS:
                if col not in existing:
                    con.execute(ddl)
            con.executescript(POST_MIGRATION_INDEXES)
            con.commit()
        _migration_applied = True
    except sqlite3.OperationalError:
        # DB is locked by a running scan, or schema not yet ready. Leave the
        # flag false and try again on the next request; queries will still
        # work using whichever columns exist now.
        pass


def _existing_columns() -> set[str]:
    if not DB_PATH.exists():
        return set()
    try:
        with _connect() as con:
            return {row["name"] for row in con.execute("PRAGMA table_info(postings)")}
    except sqlite3.OperationalError:
        return set()


def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    if "strengths" in d.keys():
        d["strengths"] = json.loads(d["strengths"]) if d.get("strengths") else []
    if "concerns" in d.keys():
        d["concerns"] = json.loads(d["concerns"]) if d.get("concerns") else []
    return d


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES_DIR),
        static_folder=str(STATIC_DIR),
    )
    # Pick up template + static edits without a server restart, since this
    # is always a local dev-mode-equivalent deployment.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    @app.before_request
    def _before():
        _try_ensure_schema()

    @app.get("/")
    def index():
        return render_template("dashboard.html")

    @app.get("/api/postings")
    def list_postings():
        min_score = int(request.args.get("min_score", 0))
        statuses = request.args.getlist("status") or ["pending", "visited", "applied"]
        q = request.args.get("q", "").strip().lower()

        if not DB_PATH.exists():
            return jsonify([])

        cols = _existing_columns()
        has_status = "status" in cols
        # Freshest-first tie-break when the column exists (top scores clamp at
        # 100, so freshness is what separates strong roles). NULL ages sort last.
        if "freshness_days" in cols:
            order = ("ORDER BY score DESC, "
                     "freshness_days IS NULL, freshness_days ASC, first_seen DESC")
        else:
            order = "ORDER BY score DESC, first_seen DESC"
        # Hide postings whose board listing has disappeared (dead links), unless
        # explicitly requested with ?include_gone=1.
        gone_clause = ""
        if "gone" in cols and request.args.get("include_gone") != "1":
            gone_clause = " AND COALESCE(gone, 0) = 0"
        # Hide stale postings: a known post date older than max_age_days, else
        # how long we've been seeing it (first_seen). ?include_stale=1 overrides.
        # Per-ATS limits (ats_max_age_days) key off the fingerprint's ats prefix
        # ("ats:slug:id"); a limit of 0 keeps that ATS unfiltered.
        age_clause = ""
        max_age, ats_ages = _age_limits()
        if (max_age or ats_ages) and "first_seen" in cols and request.args.get("include_stale") != "1":
            date_expr = ("COALESCE(NULLIF(posted_at, ''), first_seen)"
                         if "posted_at" in cols else "first_seen")
            if ats_ages:
                whens = " ".join(f"WHEN '{ats}' THEN {int(days)}"
                                 for ats, days in sorted(ats_ages.items()))
                limit_expr = ("CASE substr(fingerprint, 1, instr(fingerprint, ':') - 1) "
                              f"{whens} ELSE {int(max_age)} END")
            else:
                limit_expr = str(int(max_age))
            age_clause = (f" AND ({limit_expr} = 0 OR "
                          f"julianday('now') - julianday({date_expr}) <= {limit_expr})")

        with _connect() as con:
            if has_status:
                placeholders = ",".join("?" * len(statuses))
                sql = f"""
                    SELECT * FROM postings
                    WHERE score >= ? AND COALESCE(status, 'pending') IN ({placeholders})
                    {gone_clause}
                    {age_clause}
                    {order}
                """
                rows = con.execute(sql, [min_score, *statuses]).fetchall()
            else:
                # Older schema: every entry is implicitly 'pending'.
                if "pending" not in statuses:
                    return jsonify([])
                rows = con.execute(
                    f"SELECT * FROM postings WHERE score >= ? {gone_clause} {age_clause} {order}",
                    [min_score],
                ).fetchall()

        out = [_row_to_dict(r) for r in rows]
        # Backfill defaults for rows from a pre-migration DB.
        for d in out:
            d.setdefault("status", "pending")
            d.setdefault("description", "")
            d.setdefault("strengths", [])
            d.setdefault("concerns", [])
        # Collapse the same role re-listed under multiple board IDs. Rows are
        # already ordered best-first (score, then freshness), so the first
        # occurrence per (company, title) is the representative we keep.
        seen_keys: set[str] = set()
        deduped = []
        for d in out:
            k = dedupe_key(d.get("company"), d.get("title"))
            if k in seen_keys:
                continue
            seen_keys.add(k)
            deduped.append(d)
        out = deduped
        if q:
            out = [
                d for d in out
                if q in (d["company"] or "").lower()
                or q in (d["title"] or "").lower()
                or q in (d["location"] or "").lower()
                or q in (d["rationale"] or "").lower()
            ]
        return jsonify(out)

    @app.post("/api/postings/<fingerprint>/status")
    def set_status(fingerprint: str):
        data = request.get_json(silent=True) or {}
        status = data.get("status")
        if status not in VALID_STATUSES:
            abort(400, f"status must be one of {sorted(VALID_STATUSES)}")
        if "status" not in _existing_columns():
            # Pre-migration DB. Status changes need the column; retry once it lands.
            return jsonify({
                "fingerprint": fingerprint,
                "status": status,
                "warning": "status column not yet available — will persist after next scan completes",
            }), 202
        with _connect() as con:
            cur = con.execute(
                "UPDATE postings SET status = ? WHERE fingerprint = ?",
                (status, fingerprint),
            )
            con.commit()
            if cur.rowcount == 0:
                abort(404)
        return jsonify({"fingerprint": fingerprint, "status": status})

    @app.get("/api/stats")
    def stats():
        if not DB_PATH.exists():
            return jsonify({"total": 0, "by_status": {}, "by_score_bucket": {}})
        cols = _existing_columns()
        has_status = "status" in cols
        with _connect() as con:
            if has_status:
                by_status = {
                    (r["status"] or "pending"): r["n"]
                    for r in con.execute(
                        "SELECT COALESCE(status, 'pending') AS status, COUNT(*) AS n "
                        "FROM postings GROUP BY status"
                    )
                }
            else:
                by_status = {"pending": con.execute("SELECT COUNT(*) FROM postings").fetchone()[0]}
            buckets = {"90+": 0, "80-89": 0, "70-79": 0, "60-69": 0, "<60": 0}
            for r in con.execute("SELECT score FROM postings WHERE score IS NOT NULL"):
                s = r["score"]
                if s >= 90: buckets["90+"] += 1
                elif s >= 80: buckets["80-89"] += 1
                elif s >= 70: buckets["70-79"] += 1
                elif s >= 60: buckets["60-69"] += 1
                else: buckets["<60"] += 1
            # Postings collected while the LLM was unavailable (defer mode) —
            # invisible to every score cutoff until rescore fills them in.
            unscored = con.execute(
                "SELECT COUNT(*) FROM postings WHERE score IS NULL"
            ).fetchone()[0]
            total = sum(by_status.values())
        return jsonify({"total": total, "by_status": by_status,
                        "by_score_bucket": buckets, "unscored": unscored})

    return app


def main(host: str = "127.0.0.1", port: int = 8765) -> None:
    app = create_app()
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
