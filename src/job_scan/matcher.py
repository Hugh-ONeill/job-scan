"""Score postings against the resume using a local Ollama model.

The model rates a posting on three independent 0-100 sub-dimensions and picks a
location tier; the final 0-100 score is then assembled deterministically in
Python (see `combine`). Decomposing the judgement this way spreads the scores
out (no more a dozen roles tied at 98), keeps location/freshness math out of the
model's hands, and makes every knob tunable from settings.yaml.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from .config import OllamaConfig, ScoringConfig
from .scrapers.base import Posting

log = logging.getLogger(__name__)


@dataclass
class Match:
    score: int              # final combined 0-100
    skill_fit: int          # 0-100, tech-stack / responsibilities overlap
    seniority_fit: int      # 0-100, level vs a ~7-yr generalist
    domain_fit: int         # 0-100, how applicable a full-stack generalist is
    location_tier: int      # 1-7, per the location-preference block
    freshness_days: int | None  # age of the posting in days, None if unknown
    interview_style: str    # e.g. "take-home", "pair", "" if no signal
    rationale: str          # one-line summary
    strengths: list[str]
    concerns: list[str]


SYSTEM = """You are a career-fit evaluator. You will be given a candidate's
resume, a location preference statement, and a single job posting. Rate the
posting on the dimensions below. Do NOT compute an overall score — that is done
downstream from your ratings.

Rate each of these 0-100:

skill_fit — overlap between the posting's required tech stack / day-to-day
responsibilities and the candidate's demonstrated skills.
  90-100: core stack + responsibilities align tightly, plus nice-to-haves
  75-89:  most required tools/skills overlap
  60-74:  meaningful transferable overlap, some real gaps
  40-59:  partial overlap only
  0-39:   little overlap

seniority_fit — whether the candidate (~7 years, mid/senior generalist) is
QUALIFIED for the role and PLAUSIBLY HIREABLE into it. The candidate is open to
ANY level, including junior/entry-level, so being over-qualified is fine and
only MILDLY lowers fit (it's still a real option). Penalize hard only when the
role needs substantially MORE than ~7 years. Use the full range — give a slight
edge to roles squarely at the candidate's level:
  95-100: a plain mid-level role at the candidate's most ATTAINABLE level —
          "Software Engineer" / "Software Engineer II", no "senior" gatekeeping.
          The candidate has a recent multi-year resume gap, so untitled
          mid-level roles are the most realistic strong-outcome matches.
  85-94:  a "Senior Software Engineer" / "Software Engineer III" role — the
          candidate can do the work, but the resume gap makes the senior bar a
          bit less likely to clear
  70-84:  junior / entry-level / new-grad / associate — very attainable and the
          candidate is open to it, just over-qualified; OR a role asking
          slightly above range (~8-10 years, "lead")
  40-69:  a clear level gap in either direction that still isn't hopeless
  0-39:   out of reach above the candidate — staff / principal / distinguished
          scope, or 10+ years required — where an application is likely rejected
  Do NOT bury junior roles; the candidate wants them. The penalty band is for
  roles well ABOVE the candidate's experience.

domain_fit — whether the candidate could do this role's ACTUAL day-to-day
engineering work. Judge the work itself, NOT the company's industry: a
full-stack, product, frontend, backend, platform, or internal-tools role at an
AI/ML (or fintech, biotech, security, etc.) company is still generalist
software work the candidate can do — score it HIGH. Being adjacent to,
integrating with, or building product on top of AI/ML is NOT a penalty.
Use the FULL range — pick the number that fits, not just a band edge:
  85-100: generalist full-stack / backend / frontend / product / web / platform
          / internal-tools / automation work — INCLUDING such roles at
          specialized companies (e.g. full-stack product engineer at an AI lab,
          backend engineer on a fintech billing platform)
  60-84:  mostly generalist work with some real specialized content the
          candidate would pick up on the job (e.g. an AI-product role that
          expects some model/prompt familiarity but is mostly app/services work)
  30-59:  a meaningfully specialized core with only partial generalist overlap
  0-29:   the role's CORE responsibility IS a specialization outside the
          candidate's background — training or researching ML models,
          ML/data-pipeline engineering as the primary job, security/cryptography
          internals, SRE/infra/DevOps, embedded/firmware/kernel, or native
          mobile (iOS/Android). Only score this low when the specialization is
          the job, not just the surrounding domain.

In "concerns", list only role-specific fit gaps: missing skills, a
specialization mismatch, or a level mismatch. Do NOT mention the candidate's
employment gap, recent short tenure, time since last role, or career break —
that is already accounted for in seniority_fit and must not appear in concerns
or rationale.

location_tier — read the LOCATION PREFERENCE block and the posting's location
plus any relocation/visa language, then output the single integer tier (1 to 7)
it falls into. Use the exact tier numbers defined in that block; do not invent
other numbers.

Respond with STRICT JSON only, no prose, no markdown fences:
{
  "skill_fit": int 0-100,
  "seniority_fit": int 0-100,
  "domain_fit": int 0-100,
  "location_tier": 1 | 2 | 3 | 4 | 5 | 6 | 7,
  "rationale": "one short sentence, <= 25 words, mentions the biggest driver",
  "strengths": ["bullet", "bullet"],
  "concerns": ["bullet", "bullet"]
}"""


USER_TMPL = """RESUME:
{resume}

---

LOCATION PREFERENCE:
{location_preference}

---

JOB POSTING:
Company: {company}
Title: {title}
Location: {location}

Description:
{description}
"""


def _trim(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + " ..."


def _clamp(v: float, lo: int = 0, hi: int = 100) -> int:
    return int(max(lo, min(hi, round(v))))


def freshness_days(posted_at: str) -> int | None:
    """Whole days since the posting was published, or None if unknown."""
    if not posted_at:
        return None
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    return max(0, delta.days)


def freshness_points(days: int | None, scoring: ScoringConfig) -> int:
    """Map a posting age to a score adjustment via the configured buckets."""
    if days is None:
        return scoring.freshness_unknown_points
    for max_days, points in scoring.freshness_buckets:
        if days <= max_days:
            return points
    return scoring.freshness_stale_points


def combine(
    skill_fit: int,
    seniority_fit: int,
    domain_fit: int,
    location_tier: int,
    days: int | None,
    scoring: ScoringConfig,
    interview_bonus: int = 0,
) -> int:
    """Assemble the final 0-100 score from the LLM sub-scores + adjustments."""
    capability = scoring.skill_weight * skill_fit + (1 - scoring.skill_weight) * seniority_fit
    gate = scoring.domain_floor + (1 - scoring.domain_floor) * (domain_fit / 100)
    base = capability * gate
    loc = scoring.location_points.get(location_tier, 0)
    fresh = freshness_points(days, scoring)
    return _clamp(base + loc + fresh + interview_bonus)


def score_posting(
    cfg: OllamaConfig,
    resume_text: str,
    posting: Posting,
    scoring: ScoringConfig,
    location_preference: str = "",
    no_whiteboard: dict | None = None,
) -> Match:
    user = USER_TMPL.format(
        resume=_trim(resume_text, 6000),
        location_preference=location_preference or "(no preference provided — treat location as Tier 6, neutral)",
        company=posting.company,
        title=posting.title,
        location=posting.location or "(unspecified)",
        description=_trim(posting.description, 5000),
    )
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": "json",
        # Disable Gemma's chain-of-thought: a long thinking trace per posting
        # turns scoring from seconds into minutes for the same JSON output.
        "think": False,
        # Keep the model resident across the whole run so we don't pay a
        # cold-load cost on every posting.
        "keep_alive": "10m",
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }
    content = _post_with_retry(cfg, payload)
    return _parse(content, posting, scoring, no_whiteboard or {})


# Brief Ollama outages (auto-restart after OOM, transient network blips) shouldn't
# lose the rest of the batch. Retry on connection/timeout errors only — not on HTTP
# errors from the model server itself, which usually indicate something we can't
# fix by retrying.
_RETRYABLE = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _post_with_retry(cfg: OllamaConfig, payload: dict, max_attempts: int = 4) -> str:
    backoffs = [2.0, 8.0, 20.0]  # seconds; only used between attempts 1->2, 2->3, 3->4
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(f"{cfg.host}/api/chat", json=payload, timeout=cfg.timeout)
            r.raise_for_status()
            return r.json().get("message", {}).get("content", "")
        except _RETRYABLE as e:
            last_exc = e
            if attempt == max_attempts:
                break
            delay = backoffs[attempt - 1]
            log.warning(
                "ollama transient error (attempt %d/%d): %s — sleeping %.0fs",
                attempt, max_attempts, type(e).__name__, delay,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)

# Concern bullets about the employment gap / short recent tenure are noise: the
# gap is already priced into seniority_fit, and repeating it on every posting is
# demoralizing without adding signal. Drop those; KEEP skill/seniority "gap"
# concerns (e.g. "gap in ML infrastructure", "seniority gap — role needs 10+").
_GAP_CONCERN = re.compile(
    r"\btenure\b"
    r"|\bstint\b"
    r"|(?:resume|employment|career|recent)[^.]*\bgap\b"
    r"|\bgap\b[^.]*(?:resume|employment|work history|job history)"
    r"|recent work (?:history|experience)"
    r"|most recent (?:role|company|position|experience)"
    r"|career break"
    r"|job[- ]?hopp",
    re.I,
)


def filter_concerns(concerns: list[str]) -> list[str]:
    return [c for c in concerns if not _GAP_CONCERN.search(c)]


def _parse(content: str, posting: Posting, scoring: ScoringConfig, no_whiteboard: dict) -> Match:
    from .interview_style import classify

    style = classify(posting.company, posting.description, no_whiteboard)
    bonus = scoring.interview_points if style else 0

    # format=json should give clean JSON, but be defensive.
    text = content.strip()
    if not text.startswith("{"):
        m = _JSON_OBJ.search(text)
        text = m.group(0) if m else "{}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("matcher JSON parse failed for %s/%s: %s", posting.company, posting.title, e)
        return Match(
            score=0, skill_fit=0, seniority_fit=0, domain_fit=0, location_tier=6,
            freshness_days=freshness_days(posting.posted_at), interview_style=style,
            rationale="(matcher returned unparseable response)", strengths=[], concerns=[],
        )

    skill = _clamp(data.get("skill_fit", 0))
    seniority = _clamp(data.get("seniority_fit", 0))
    domain = _clamp(data.get("domain_fit", 0))
    tier = data.get("location_tier", 6)
    try:
        tier = int(tier)
    except (TypeError, ValueError):
        tier = 6
    if tier not in (1, 2, 3, 4, 5, 6, 7):
        tier = 6  # neutral
    days = freshness_days(posting.posted_at)

    return Match(
        score=combine(skill, seniority, domain, tier, days, scoring, bonus),
        skill_fit=skill,
        seniority_fit=seniority,
        domain_fit=domain,
        location_tier=tier,
        freshness_days=days,
        interview_style=style,
        rationale=str(data.get("rationale", "")).strip(),
        strengths=[str(s) for s in data.get("strengths", []) or []],
        concerns=filter_concerns([str(s) for s in data.get("concerns", []) or []]),
    )
