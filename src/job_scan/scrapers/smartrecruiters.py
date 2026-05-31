"""SmartRecruiters public posting API.

  GET https://api.smartrecruiters.com/v1/companies/<co>/postings?q=<term>
  GET https://api.smartrecruiters.com/v1/companies/<co>/postings/<id>  (detail)

The list omits the description, so we fetch each posting's detail for the
jobAd sections. Narrowed by the configured search terms to bound detail calls.
Configure: {name: Visa, ats: smartrecruiters, slug: Visa}  (slug is case-sensitive)
"""
from __future__ import annotations

import logging

import requests

from ..config import Company
from .base import Posting, html_to_markdown, normalize_date

log = logging.getLogger(__name__)

API = "https://api.smartrecruiters.com/v1/companies/{co}/postings"
TIMEOUT = 30
PAGE = 100
MAX_JOBS = 80
SEARCH_TERMS = ["software engineer", "full stack", "backend", "frontend"]


def _location(loc: dict) -> str:
    if not loc:
        return ""
    if loc.get("remote"):
        return "Remote"
    return loc.get("fullLocation") or ", ".join(
        x for x in (loc.get("city"), loc.get("region"), loc.get("country", "").upper()) if x
    )


def _description(jobAd: dict) -> str:
    secs = (jobAd or {}).get("sections") or {}
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        section = secs.get(key) or {}
        text = section.get("text") or ""
        if text:
            title = section.get("title") or ""
            if title:
                parts.append(f"**{title}**\n")
            parts.append(html_to_markdown(text))
    return "\n\n".join(parts)


def fetch_smartrecruiters(company: Company) -> list[Posting]:
    url = API.format(co=company.slug)
    sess = requests.Session()
    sess.headers.update({"User-Agent": "job-scan/0.1"})

    stubs: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        r = sess.get(url, params={"q": term, "limit": PAGE}, timeout=TIMEOUT)
        if r.status_code == 404:
            log.info("smartrecruiters 404 for %s (%s)", company.name, company.slug)
            return []
        r.raise_for_status()
        for j in r.json().get("content", []) or []:
            stubs.setdefault(str(j.get("id")), j)
        if len(stubs) >= MAX_JOBS:
            break
    if len(stubs) >= MAX_JOBS:
        log.info("smartrecruiters %s: capped at %d jobs", company.name, MAX_JOBS)

    out: list[Posting] = []
    for pid, j in list(stubs.items())[:MAX_JOBS]:
        try:
            full = sess.get(f"{url}/{pid}", timeout=TIMEOUT).json()
        except Exception as e:
            log.debug("smartrecruiters detail fetch failed (%s): %s", pid, e)
            full = {}
        out.append(
            Posting(
                company=company.name,
                ats="smartrecruiters",
                slug=company.slug,
                posting_id=pid,
                title=(j.get("name") or "").strip(),
                location=_location(j.get("location") or {}),
                url=full.get("postingUrl") or full.get("applyUrl") or j.get("ref", ""),
                description=_description(full.get("jobAd") or {}),
                posted_at=normalize_date(j.get("releasedDate")),
                priority=company.priority,
            )
        )
    return out
