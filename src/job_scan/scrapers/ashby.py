"""Ashby job board API: api.ashbyhq.com/posting-api/job-board/<slug>"""
from __future__ import annotations

import logging

import requests

from ..config import Company
from .base import Posting, html_to_text, normalize_date

log = logging.getLogger(__name__)

URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
TIMEOUT = 30


def fetch_ashby(company: Company) -> list[Posting]:
    url = URL.format(slug=company.slug)
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "job-scan/0.1"})
    if r.status_code == 404:
        log.info("ashby 404 for %s (%s)", company.name, company.slug)
        return []
    r.raise_for_status()
    data = r.json()
    out: list[Posting] = []
    for job in data.get("jobs", []) or []:
        loc = job.get("location") or ""
        desc = job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml", ""))
        out.append(
            Posting(
                company=company.name,
                ats="ashby",
                slug=company.slug,
                posting_id=str(job.get("id")),
                title=(job.get("title") or "").strip(),
                location=str(loc).strip(),
                url=job.get("jobUrl", ""),
                description=desc.strip(),
                posted_at=normalize_date(job.get("publishedAt")),
                priority=company.priority,
            )
        )
    return out
