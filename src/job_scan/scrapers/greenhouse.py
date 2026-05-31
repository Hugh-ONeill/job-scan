"""Greenhouse job board API: boards-api.greenhouse.io/v1/boards/<slug>/jobs"""
from __future__ import annotations

import logging

import requests

from ..config import Company
from .base import Posting, html_to_markdown, normalize_date

log = logging.getLogger(__name__)

URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
TIMEOUT = 30


def fetch_greenhouse(company: Company) -> list[Posting]:
    url = URL.format(slug=company.slug)
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "job-scan/0.1"})
    if r.status_code == 404:
        log.info("greenhouse 404 for %s (%s)", company.name, company.slug)
        return []
    r.raise_for_status()
    data = r.json()
    out: list[Posting] = []
    for job in data.get("jobs", []) or []:
        loc = (job.get("location") or {}).get("name") or ""
        out.append(
            Posting(
                company=company.name,
                ats="greenhouse",
                slug=company.slug,
                posting_id=str(job.get("id")),
                title=job.get("title", "").strip(),
                location=loc.strip(),
                url=job.get("absolute_url", ""),
                description=html_to_markdown(job.get("content", "")),
                # first_published is the original post date; fall back to
                # updated_at when a board doesn't expose it.
                posted_at=normalize_date(job.get("first_published") or job.get("updated_at")),
                priority=company.priority,
            )
        )
    return out
