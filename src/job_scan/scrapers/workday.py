"""Workday CXS API.

Workday's careers UI is a SPA, but it's backed by a stable JSON API:
  POST https://<host>/wday/cxs/<tenant>/<site>/jobs   (paged list)
  GET  https://<host>/wday/cxs/<tenant>/<site><externalPath>  (one job's detail)

The list response has no description, so we fetch each job's detail page. To
keep that bounded we narrow the list with the configured search terms and cap
per company. Configure a company as:
  {name: NVIDIA, ats: workday, slug: nvidia,
   host: nvidia.wd5.myworkdayjobs.com, board: NVIDIAExternalCareerSite}
"""
from __future__ import annotations

import logging

import requests

from ..config import Company
from .base import Posting, html_to_markdown, normalize_date

log = logging.getLogger(__name__)

TIMEOUT = 30
PAGE = 20
MAX_JOBS = 60  # per company, after search-narrowing
SEARCH_TERMS = ["software engineer", "full stack", "backend", "frontend"]


def _base(company: Company) -> str:
    return f"https://{company.host}/wday/cxs/{company.slug}/{company.board}"


def fetch_workday(company: Company) -> list[Posting]:
    if not company.host or not company.board:
        log.warning("workday %s missing host/board, skipping", company.name)
        return []
    base = _base(company)
    sess = requests.Session()
    sess.headers.update({"User-Agent": "job-scan/0.1", "Accept": "application/json"})

    # Collect a narrowed, de-duplicated set of stubs across search terms.
    stubs: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        offset = 0
        while len(stubs) < MAX_JOBS:
            body = {"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": term}
            r = sess.post(f"{base}/jobs", json=body, timeout=TIMEOUT)
            if r.status_code == 404:
                log.info("workday 404 for %s (%s)", company.name, company.slug)
                return []
            r.raise_for_status()
            data = r.json()
            page = data.get("jobPostings") or []
            for jp in page:
                path = jp.get("externalPath")
                if path and path not in stubs:
                    stubs[path] = jp
            total = data.get("total", 0)
            offset += PAGE
            if offset >= total or not page:
                break
    if len(stubs) >= MAX_JOBS:
        log.info("workday %s: capped at %d jobs (more available)", company.name, MAX_JOBS)

    out: list[Posting] = []
    for path, jp in list(stubs.items())[:MAX_JOBS]:
        try:
            d = sess.get(f"{base}{path}", timeout=TIMEOUT).json()
        except Exception as e:
            log.debug("workday detail fetch failed (%s): %s", path, e)
            continue
        info = d.get("jobPostingInfo") or {}
        req_id = (jp.get("bulletFields") or [path])[0]
        out.append(
            Posting(
                company=company.name,
                ats="workday",
                slug=company.slug,
                posting_id=str(info.get("jobReqId") or req_id),
                title=(info.get("title") or jp.get("title") or "").strip(),
                location=(info.get("location") or jp.get("locationsText") or "").strip(),
                url=info.get("externalUrl") or f"https://{company.host}{path}",
                description=html_to_markdown(info.get("jobDescription", "")),
                posted_at=normalize_date(info.get("startDate")),
                priority=company.priority,
            )
        )
    return out
