"""Load and validate config files."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"


@dataclass
class OllamaConfig:
    host: str = "http://localhost:11434"
    model: str = "gemma4:26b-a4b-it-q4_K_M"
    timeout: int = 180


@dataclass
class ScoringConfig:
    """Knobs for the deterministic combination of LLM sub-scores.

    The LLM rates skill_fit / seniority_fit / domain_fit (0-100) and picks a
    location_tier (1-4). We combine them here so the math is transparent and
    tunable without touching the prompt:

        base  = skill_fit
                * (seniority_floor + (1-seniority_floor)*seniority_fit/100)
                * (domain_floor    + (1-domain_floor)   *domain_fit/100)
        final = clamp(base + location_points[tier] + interview_points)

    Seniority and domain are multiplicative GATES, not averaged terms: a role
    the candidate isn't plausibly hireable into (low seniority_fit, e.g. asking
    8+ years) caps the score regardless of how well the skills match, rather
    than being washed out by a high skill_fit.

    Posting age is NOT in the score — it's a sort dimension only (see
    matcher.combine). The report/dashboard already order by date within a
    score, so scoring freshness too just double-counted it and saturated the
    top into a wall of 100s.
    """
    seniority_floor: float = 0.35      # over/under-leveled roles keep this fraction (gate)
    domain_floor: float = 0.45         # out-of-domain roles keep this fraction (gate)
    # Nudge for companies that interview with take-homes / pair programming /
    # real-work exercises instead of algorithmic puzzles (hiring-without-
    # whiteboards list, or take-home/pair keywords in the posting).
    interview_points: int = 10
    # Finer-grained location tiers (see location_preference for definitions):
    #   1 remote · 2 explicit relo/visa · 3 commute-NJ · 4 commute-NYC
    #   5 commute-Philly · 6 neutral (US, no relo language) · 7 explicit-no-relo
    location_points: dict[int, int] = field(
        default_factory=lambda: {1: 8, 2: 7, 3: 6, 4: 5, 5: 4, 6: 0, 7: -6}
    )


@dataclass
class Settings:
    resume_path: Path
    ollama: OllamaConfig
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    location_preference: str = ""
    min_score: int = 60
    top_n: int = 10
    notify_urgency: str = "normal"
    max_score_per_run: int = 80
    # Postings older than this are skipped at scan time (when a post date is
    # known) and hidden from the dashboard (by post date, else by how long we've
    # been seeing them). 0 disables the age filter.
    max_age_days: int = 90
    # Per-ATS overrides for max_age_days (e.g. {"workday": 180} — enterprise
    # boards keep live reqs open far longer than startup ATSs). A value of 0
    # disables the age filter for that ATS.
    ats_max_age_days: dict[str, int] = field(default_factory=dict)


@dataclass
class Filters:
    title_include: list[str] = field(default_factory=list)
    title_exclude: list[str] = field(default_factory=list)
    location_include: list[str] = field(default_factory=list)
    location_exclude: list[str] = field(default_factory=list)


@dataclass
class Company:
    name: str
    ats: str
    slug: str
    priority: bool = False
    # Optional, only needed by multi-part ATSs (e.g. Workday): the board host
    # and the career-site name. Ignored by single-slug ATSs.
    host: str = ""
    board: str = ""


@dataclass
class Aggregators:
    """Keyword-driven, cross-company job sources (no per-company slug)."""
    hackernews: bool = True
    remoteok: bool = True
    remotive: bool = True
    weworkremotely: bool = True
    freehire: bool = True
    # Server-side freshness window for freehire (posted_within_days). The API
    # indexes multi-market volume, so a tight window keeps the pull relevant.
    freehire_days: int = 7
    # Country facet, ISO codes (verified live: "United States"/"USA" match
    # nothing, "us" works). Empty list = no country filter — but the index is
    # heavily international, so unfiltered pulls waste most of the per-query
    # limit on roles the location filters reject anyway.
    freehire_countries: list[str] = field(default_factory=lambda: ["us"])
    # Search terms for sources that support server-side search (Remotive, HN
    # filtering). Also used to filter free-text sources client-side.
    queries: list[str] = field(default_factory=lambda: [
        "software engineer", "full stack", "backend", "frontend",
    ])
    max_per_source: int = 200


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def _load_scoring(raw: dict) -> ScoringConfig:
    s = raw.get("scoring") or {}
    defaults = ScoringConfig()
    loc = s.get("location_points")
    return ScoringConfig(
        seniority_floor=float(s.get("seniority_floor", defaults.seniority_floor)),
        domain_floor=float(s.get("domain_floor", defaults.domain_floor)),
        interview_points=int(s.get("interview_points", defaults.interview_points)),
        location_points=(
            {int(k): int(v) for k, v in loc.items()} if loc else defaults.location_points
        ),
    )


def load_settings() -> Settings:
    raw = yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text())
    o = raw.get("ollama") or {}
    return Settings(
        resume_path=_expand(raw["resume_path"]),
        ollama=OllamaConfig(
            host=o.get("host", "http://localhost:11434"),
            model=o.get("model", "gemma4:26b-a4b-it-q4_K_M"),
            timeout=int(o.get("timeout", 180)),
        ),
        scoring=_load_scoring(raw),
        location_preference=(raw.get("location_preference") or "").strip(),
        min_score=int(raw.get("min_score", 60)),
        top_n=int(raw.get("top_n", 10)),
        notify_urgency=raw.get("notify_urgency", "normal"),
        max_score_per_run=int(raw.get("max_score_per_run", 80)),
        max_age_days=int(raw.get("max_age_days", 90)),
        ats_max_age_days={
            str(k): int(v) for k, v in (raw.get("ats_max_age_days") or {}).items()
        },
    )


def load_filters() -> Filters:
    raw = yaml.safe_load((CONFIG_DIR / "filters.yaml").read_text()) or {}
    return Filters(
        title_include=[s.lower() for s in raw.get("title_include", [])],
        title_exclude=[s.lower() for s in raw.get("title_exclude", [])],
        location_include=[s.lower() for s in raw.get("location_include", [])],
        location_exclude=[s.lower() for s in raw.get("location_exclude", [])],
    )


def load_companies() -> list[Company]:
    raw = yaml.safe_load((CONFIG_DIR / "companies.yaml").read_text()) or {}
    out: list[Company] = []
    for entry in raw.get("priority", []) or []:
        out.append(_company(entry, priority=True))
    for entry in raw.get("sweep", []) or []:
        out.append(_company(entry, priority=False))
    return out


def _company(entry: dict, priority: bool) -> Company:
    return Company(
        name=entry["name"], ats=entry["ats"], slug=entry["slug"], priority=priority,
        host=entry.get("host", ""), board=entry.get("board", ""),
    )


def load_no_whiteboard_companies() -> list[Company]:
    """Companies resolved from the hiring-without-whiteboards list (weekly scan).
    Empty if `resolve-nw` hasn't been run yet."""
    path = CONFIG_DIR / "no_whiteboard_companies.yaml"
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    return [_company(e, priority=False) for e in raw.get("companies", []) or []]


def load_aggregators() -> Aggregators:
    raw = yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text()) or {}
    a = raw.get("aggregators") or {}
    defaults = Aggregators()
    return Aggregators(
        hackernews=bool(a.get("hackernews", defaults.hackernews)),
        remoteok=bool(a.get("remoteok", defaults.remoteok)),
        remotive=bool(a.get("remotive", defaults.remotive)),
        weworkremotely=bool(a.get("weworkremotely", defaults.weworkremotely)),
        freehire=bool(a.get("freehire", defaults.freehire)),
        freehire_days=int(a.get("freehire_days", defaults.freehire_days)),
        freehire_countries=list(a.get("freehire_countries", defaults.freehire_countries) or []),
        queries=list(a.get("queries") or defaults.queries),
        max_per_source=int(a.get("max_per_source", defaults.max_per_source)),
    )
