"""Render markdown report and trigger notify-send."""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import date

from .config import REPORTS_DIR, Settings
from .matcher import Match
from .scrapers.base import Posting


@dataclass
class Scored:
    posting: Posting
    match: Match


def _rank(s: "Scored") -> tuple:
    """Sort key: best score first, then freshest (unknown age sorts last).

    Top scores clamp at 100, so the freshness bump alone can't separate strong
    roles — this tie-break guarantees a brand-new posting leads its score group.
    """
    days = s.match.freshness_days
    return (-s.match.score, days if days is not None else 1_000_000)


def render_markdown(scored: list[Scored], settings: Settings) -> str:
    today = date.today().isoformat()
    if not scored:
        return f"# job-scan {today}\n\nNo new postings cleared filters today.\n"

    new_high = [s for s in scored if s.match.score >= settings.min_score]
    priority_hits = [s for s in new_high if s.posting.priority]
    top = sorted(new_high, key=_rank)[: settings.top_n]
    rest = sorted(
        (s for s in new_high if s not in top),
        key=_rank,
    )
    below_cutoff = sorted(
        (s for s in scored if s.match.score < settings.min_score),
        key=_rank,
    )

    lines: list[str] = [f"# job-scan {today}", ""]
    lines.append(
        f"Scored **{len(scored)}** new postings | **{len(new_high)}** above cutoff "
        f"(>= {settings.min_score}) | **{len(priority_hits)}** at priority companies."
    )
    lines.append("")

    if priority_hits:
        lines += ["## Priority companies", ""]
        for s in sorted(priority_hits, key=_rank):
            lines += _entry(s)
        lines.append("")

    if top:
        lines += [f"## Top {len(top)} matches", ""]
        for s in top:
            lines += _entry(s)
        lines.append("")

    if rest:
        lines += [f"## Other matches above cutoff ({len(rest)})", ""]
        for s in rest:
            lines += _entry(s, compact=True)
        lines.append("")

    if below_cutoff:
        lines += [
            f"<details><summary>Below cutoff ({len(below_cutoff)})</summary>",
            "",
        ]
        for s in below_cutoff:
            lines += _entry(s, compact=True)
        lines += ["", "</details>"]

    return "\n".join(lines) + "\n"


def _age_str(days: int | None) -> str:
    if days is None:
        return ""
    if days <= 0:
        return " · posted today"
    if days == 1:
        return " · posted 1 day ago"
    return f" · posted {days} days ago"


def _entry(s: Scored, compact: bool = False) -> list[str]:
    p, m = s.posting, s.match
    head = f"### [{p.company}] {p.title} — **{m.score}**"
    style = f" · 🤝 {m.interview_style}" if getattr(m, "interview_style", "") else ""
    meta = f"*{p.location or '(location unspecified)'}*{_age_str(m.freshness_days)}{style} · [posting]({p.url})"
    body = [head, "", meta, "", f"> {m.rationale}" if m.rationale else ""]
    if compact:
        return [l for l in body if l != ""]
    if m.strengths:
        body += ["", "**Strengths:**"] + [f"- {x}" for x in m.strengths]
    if m.concerns:
        body += ["", "**Concerns:**"] + [f"- {x}" for x in m.concerns]
    body += [""]
    return body


def write_report(scored: list[Scored], settings: Settings) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{date.today().isoformat()}.md"
    path.write_text(render_markdown(scored, settings))
    return str(path)


def notify_failure(failed: int, attempted: int) -> None:
    """Critical desktop alert when scoring is broken (e.g. Ollama down), so a
    run that scored nothing isn't mistaken for a genuinely quiet day."""
    if not shutil.which("notify-send"):
        return
    subprocess.run(
        [
            "notify-send",
            "-u",
            "critical",
            "-a",
            "job-scan",
            "job-scan: scoring is failing",
            f"{failed}/{attempted} postings failed to score — check that Ollama "
            f"is up and the model is loaded.",
        ],
        check=False,
    )


def notify(scored: list[Scored], settings: Settings, report_path: str) -> None:
    if not shutil.which("notify-send"):
        return
    above = [s for s in scored if s.match.score >= settings.min_score]
    if not above:
        subprocess.run(
            ["notify-send", "-u", "low", "job-scan", "No new matches today."],
            check=False,
        )
        return

    top = sorted(above, key=_rank)[:3]
    lines = [f"<b>{len(above)} new match(es)</b> — report: {report_path}", ""]
    for s in top:
        lines.append(f"<b>{s.match.score}</b> · {s.posting.company} — {s.posting.title}")
    body = "\n".join(lines)
    subprocess.run(
        [
            "notify-send",
            "-u",
            settings.notify_urgency,
            "-a",
            "job-scan",
            f"job-scan: {len(above)} new match(es)",
            body,
        ],
        check=False,
    )
