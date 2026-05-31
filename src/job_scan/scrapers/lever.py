"""Lever job board API: api.lever.co/v0/postings/<slug>?mode=json"""
from __future__ import annotations

import logging

import requests

from ..config import Company
from .base import Posting, html_to_markdown, normalize_date

log = logging.getLogger(__name__)

URL = "https://api.lever.co/v0/postings/{slug}?mode=json"
TIMEOUT = 30


def fetch_lever(company: Company) -> list[Posting]:
    url = URL.format(slug=company.slug)
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "job-scan/0.1"})
    if r.status_code == 404:
        log.info("lever 404 for %s (%s)", company.name, company.slug)
        return []
    r.raise_for_status()
    data = r.json()
    out: list[Posting] = []
    for job in data or []:
        cats = job.get("categories") or {}
        loc = cats.get("location") or ""
        if not loc and cats.get("allLocations"):
            loc = cats["allLocations"][0]
        desc = html_to_markdown(job.get("description", ""))
        for block in job.get("lists", []) or []:
            content = block.get("content", "")
            if content:
                title = (block.get("text") or "").strip()
                if title:
                    desc += f"\n\n**{title}**\n"
                desc += "\n" + html_to_markdown(content)
        out.append(
            Posting(
                company=company.name,
                ats="lever",
                slug=company.slug,
                posting_id=str(job.get("id")),
                title=(job.get("text") or "").strip(),
                location=str(loc).strip(),
                url=job.get("hostedUrl", ""),
                description=desc.strip(),
                # createdAt is epoch milliseconds.
                posted_at=normalize_date(job.get("createdAt")),
                priority=company.priority,
            )
        )
    return out
