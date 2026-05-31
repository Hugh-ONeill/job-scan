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

        capability = skill_weight*skill_fit + (1-skill_weight)*seniority_fit
        base       = capability * (domain_floor + (1-domain_floor)*domain_fit/100)
        final      = clamp(base + location_points[tier] + freshness_points(age))
    """
    skill_weight: float = 0.7          # seniority gets the remaining weight
    domain_floor: float = 0.45         # out-of-domain roles keep this fraction
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
    # (max_age_days, points) tiers, checked in order; first match wins.
    freshness_buckets: list[tuple[int, int]] = field(
        default_factory=lambda: [(1, 12), (7, 6), (21, 3), (60, 0), (180, -4)]
    )
    freshness_stale_points: int = -8   # older than the last bucket
    freshness_unknown_points: int = 0  # posting date missing


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
    buckets = s.get("freshness_buckets")
    return ScoringConfig(
        skill_weight=float(s.get("skill_weight", defaults.skill_weight)),
        domain_floor=float(s.get("domain_floor", defaults.domain_floor)),
        interview_points=int(s.get("interview_points", defaults.interview_points)),
        location_points=(
            {int(k): int(v) for k, v in loc.items()} if loc else defaults.location_points
        ),
        freshness_buckets=(
            [(int(b["max_days"]), int(b["points"])) for b in buckets]
            if buckets else defaults.freshness_buckets
        ),
        freshness_stale_points=int(
            s.get("freshness_stale_points", defaults.freshness_stale_points)
        ),
        freshness_unknown_points=int(
            s.get("freshness_unknown_points", defaults.freshness_unknown_points)
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
        queries=list(a.get("queries") or defaults.queries),
        max_per_source=int(a.get("max_per_source", defaults.max_per_source)),
    )
