"""Load and validate config files."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"


@dataclass
class OllamaConfig:
    host: str = "http://localhost:11434"
    model: str = "gemma4:26b-a4b-it-q4_K_M"
    timeout: int = 180
    num_ctx: int = 8192
    # Adopt whatever compatible model is already loaded (see resident.py).
    prefer_resident: bool = True
    resident_families: list[str] = field(default_factory=lambda: ["gemma4"])


@dataclass
class ScoringConfig:
    """Knobs for the deterministic combination of LLM sub-scores.

    The LLM rates skill_fit / seniority_fit / domain_fit (0-100) and picks a
    location_tier (1-4). We combine them here so the math is transparent and
    tunable without touching the prompt:

        base  = skill_fit
                * (seniority_floor + (1-seniority_floor)*seniority_fit/100)
                * (domain_floor    + (1-domain_floor)   *domain_fit/100)
        final = clamp(base + location_points[tier] + interview_points
                      + easy_apply_points)

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
    # Nudge for LinkedIn Easy Apply postings. Not a fit signal: it is here
    # because a one-click application costs minutes, not an hour, so between
    # two similar matches the cheap one is worth sending first. Kept small so
    # it breaks near-ties without lifting a weak match over a strong one.
    easy_apply_points: int = 3
    # Finer-grained location tiers (see location_preference for definitions):
    #   1 remote · 2 explicit relo/visa · 3 commute-NJ · 4 commute-NYC
    #   5 commute-Philly · 6 neutral (US, no relo language) · 7 explicit-no-relo
    location_points: dict[int, int] = field(
        default_factory=lambda: {1: 8, 2: 7, 3: 6, 4: 5, 5: 4, 6: 0, 7: -6}
    )
    # Breadth discount. A posting naming React AND Angular AND Vue, five
    # backends, four databases and three clouds overlaps any resume by
    # construction, so a high skill_fit against it means less. Measured over
    # 2,529 scored postings: median names 5 technologies, p90 13, p95 15, and
    # mean score rose from 65 (0-3 named) to 86 (24-27 named) purely on breadth.
    # Deliberately bounded: this reorders the list, it does not exclude broad
    # but legitimate enterprise postings.
    breadth_threshold: int = 16        # ~p95; no penalty at or below this
    breadth_points: float = 0.8        # points deducted per technology above it
    breadth_penalty_max: int = 8       # hard cap on the deduction


@dataclass
class WatchConfig:
    """Intraday watch: a cheap pass over the fast sources between daily runs.

    Only LinkedIn and freehire are fetched (they surface nearly every strong
    match; the company-board sweep is the slow part of a run and stays daily).
    """
    linkedin_hours: int = 3       # overlaps a 2-hour cadence so nothing slips between
    # A full-footprint watch (4 pages, 65 details, ~110 requests) drew ten 429s
    # at 11:25 on 2026-09-18; the daily run sees ~3 a month. Keep watches small.
    linkedin_pages: int = 1
    linkedin_max_details: int = 20
    freehire_days: int = 1        # freehire's smallest server-side window
    max_score: int = 30           # LLM scores per watch; the rest wait for the morning run
    notify_min_score: int = 90    # only interrupt for postings at or above this


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
    # Days of silence before an open application (applied/interview) is flagged
    # as needing a follow-up. Standard practice is a brief, polite follow-up
    # after one to two weeks, at most twice.
    followup_after_days: int = 10
    watch: WatchConfig = field(default_factory=WatchConfig)


@dataclass
class Filters:
    title_include: list[str] = field(default_factory=list)
    title_exclude: list[str] = field(default_factory=list)
    location_include: list[str] = field(default_factory=list)
    location_exclude: list[str] = field(default_factory=list)
    # Hosts dropped before scoring, whichever aggregator surfaced them.
    blocked_domains: list[str] = field(default_factory=list)
    # Company names (case-insensitive substrings) dropped before scoring:
    # talent marketplaces, AI-training gig mills, reposters with no conversion.
    blocked_companies: list[str] = field(default_factory=list)


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
    # Relevance-ordered pages of 100 read per query (one request each).
    freehire_pages: int = 2
    # LinkedIn public jobs-guest endpoints. Off by default: automated access is
    # against LinkedIn ToS, so enabling it is an explicit personal-use choice —
    # and the knobs below exist to keep the volume low (see linkedin.py).
    linkedin: bool = False
    linkedin_locations: list[str] = field(default_factory=lambda: [
        "New Jersey, United States", "New York City Metropolitan Area",
    ])
    linkedin_days: int = 3          # f_TPR window: only postings this fresh
    # Hours window for the intraday watch; 0 = use linkedin_days. LinkedIn's
    # f_TPR takes seconds, so a watch can ask for just the last few hours.
    linkedin_hours: int = 0
    linkedin_pages: int = 4          # search pages per query x location (10 results each)
    linkedin_max_details: int = 120  # detail pages per run (new, pre-filtered postings only)
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
        easy_apply_points=int(s.get("easy_apply_points", defaults.easy_apply_points)),
        location_points=(
            {int(k): int(v) for k, v in loc.items()} if loc else defaults.location_points
        ),
        breadth_threshold=int(s.get("breadth_threshold", defaults.breadth_threshold)),
        breadth_points=float(s.get("breadth_points", defaults.breadth_points)),
        breadth_penalty_max=int(s.get("breadth_penalty_max", defaults.breadth_penalty_max)),
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
            num_ctx=int(o.get("num_ctx", 8192)),
            prefer_resident=bool(o.get("prefer_resident", True)),
            resident_families=[str(x) for x in (o.get("resident_families") or ["gemma4"])],
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
        followup_after_days=int(raw.get("followup_after_days", 10)),
        watch=_load_watch(raw),
    )


def _load_watch(raw: dict) -> WatchConfig:
    w = raw.get("watch") or {}
    dflt = WatchConfig()
    return WatchConfig(
        linkedin_hours=int(w.get("linkedin_hours", dflt.linkedin_hours)),
        linkedin_pages=int(w.get("linkedin_pages", dflt.linkedin_pages)),
        linkedin_max_details=int(w.get("linkedin_max_details", dflt.linkedin_max_details)),
        freehire_days=int(w.get("freehire_days", dflt.freehire_days)),
        max_score=int(w.get("max_score", dflt.max_score)),
        notify_min_score=int(w.get("notify_min_score", dflt.notify_min_score)),
    )


def load_filters() -> Filters:
    raw = yaml.safe_load((CONFIG_DIR / "filters.yaml").read_text()) or {}
    return Filters(
        title_include=[s.lower() for s in raw.get("title_include", [])],
        title_exclude=[s.lower() for s in raw.get("title_exclude", [])],
        location_include=[s.lower() for s in raw.get("location_include", [])],
        location_exclude=[s.lower() for s in raw.get("location_exclude", [])],
        blocked_domains=[s.lower() for s in raw.get("blocked_domains", [])],
        blocked_companies=[s.lower() for s in raw.get("blocked_companies", [])],
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


def load_auto_companies() -> list[Company]:
    """Boards promoted from aggregator rows by `resolve-agg` (generated weekly).
    Empty if it hasn't been run yet."""
    path = CONFIG_DIR / "companies.auto.yaml"
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    return [_company(e, priority=False) for e in raw.get("companies", []) or []]


def load_gap_vocabulary() -> dict[str, "re.Pattern[str]"]:
    """Compile config/gap_terms.yaml into {term: compiled alternation}.

    Patterns are joined with | and wrapped in word boundaries, so a term's
    alternatives all match whole words. An entry whose regex doesn't compile is
    skipped with a warning rather than killing the whole rollup.
    """
    import re
    path = CONFIG_DIR / "gap_terms.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    out: dict[str, re.Pattern[str]] = {}
    for term, patterns in (raw.get("terms") or {}).items():
        alternation = "|".join(f"(?:{p})" for p in patterns)
        try:
            out[str(term)] = re.compile(rf"\b(?:{alternation})", re.I)
        except re.error as e:
            log.warning("gap_terms.yaml: skipping %r (bad pattern: %s)", term, e)
    return out


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
        freehire_pages=int(a.get("freehire_pages", defaults.freehire_pages)),
        linkedin=bool(a.get("linkedin", defaults.linkedin)),
        linkedin_locations=list(a.get("linkedin_locations") or defaults.linkedin_locations),
        linkedin_days=int(a.get("linkedin_days", defaults.linkedin_days)),
        linkedin_pages=int(a.get("linkedin_pages", defaults.linkedin_pages)),
        linkedin_max_details=int(a.get("linkedin_max_details", defaults.linkedin_max_details)),
        queries=list(a.get("queries") or defaults.queries),
        max_per_source=int(a.get("max_per_source", defaults.max_per_source)),
    )
