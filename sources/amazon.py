"""Amazon's own careers search.

Amazon runs its own portal rather than an ATS with a public board, which put
it in the same bucket as IBM, Google, Apple, Meta and Microsoft - reachable
only by hand through ``--apply-url``. Unlike the others, it publishes a plain
JSON search endpoint that needs no key and no session::

    https://www.amazon.jobs/en/search.json?base_query=...&country=USA

Checked before building: ``amazon.jobs/robots.txt`` disallows only ``/internal``
paths. This one is permitted, which is the difference between this adapter and
the LinkedIn guest API - that endpoint returns exactly the same shape of data
and sits under a path LinkedIn explicitly disallows, so it is not used.

The payload is richer than most sources give: a real ``posted_date``, a
normalised location, and the qualifications split into basic and preferred.
That last part matters downstream - the undergraduate gate reads requirement
text, and Amazon states its degree requirement in ``basic_qualifications``
rather than burying it in prose.

Scope is deliberately narrow. ``AMAZON_QUERIES`` defaults to the one search
that was asked for; Amazon posts thousands of roles and a broad query would
swamp the digest with warehouse and operations postings that the category
filter would then have to throw away.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

import config
from models import Job
from normalize import filter_us_locations, infer_work_mode
from sources.ats import categorize_title, infer_terms, looks_like_internship, strip_html

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.amazon.jobs/en/search.json"
JOB_BASE = "https://www.amazon.jobs"

# How many results per query. Amazon caps the page size; this is one request
# per query and plenty for a narrow search.
PAGE_SIZE = 50


def _headers() -> Dict[str, str]:
    return {"User-Agent": config.USER_AGENT, "Accept": "application/json"}


def _parse_posted(value: Any) -> Optional[datetime]:
    """Amazon writes dates as "August 13, 2026"."""
    if not isinstance(value, str) or not value.strip():
        return None
    for form in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), form).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _locations_from(raw: Dict[str, Any]) -> List[str]:
    """A "City, ST" location, preferring the structured city/state fields.

    ``normalized_location`` reads "Mt Juliet, Tennessee, USA" - a spelled-out
    state and a country suffix that normalize.py does not expect - while
    ``city`` and ``state`` carry "Mt. Juliet" and "TN" separately.
    """
    city = str(raw.get("city") or "").strip()
    state = str(raw.get("state") or "").strip()
    if city and state:
        return [f"{city}, {state}"]

    normalized = str(raw.get("normalized_location") or "").strip()
    if normalized:
        # Drop a trailing country, which is always USA on a country=USA search.
        return [re.sub(r",\s*(USA|United States)\s*$", "", normalized, flags=re.I)]
    return []


class AmazonSource:
    """Amazon's public job search, scoped to the configured queries."""

    name = "amazon"
    rank = 12          # a first-party board, same tier as the ATS adapters

    def __init__(self, queries: Optional[List[str]] = None,
                 session: Optional[requests.Session] = None):
        self.queries = queries if queries is not None else config.AMAZON_QUERIES
        self.session = session or requests.Session()

    def _search(self, query: str) -> List[Dict[str, Any]]:
        try:
            response = self.session.get(
                SEARCH_URL,
                params={
                    "base_query": query,
                    "country": "USA",
                    "result_limit": PAGE_SIZE,
                    "sort": "recent",
                    "offset": 0,
                },
                headers=_headers(), timeout=25,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("amazon: search for %r failed: %s", query, exc)
            return []
        return payload.get("jobs") or []

    def _build(self, raw: Dict[str, Any]) -> Optional[Job]:
        title = str(raw.get("title") or "").strip()
        if not title or not looks_like_internship(title):
            return None

        locations = filter_us_locations(_locations_from(raw))
        if not locations:
            return None

        # Both qualification blocks carry the degree and enrolment language the
        # undergraduate gate reads, so they belong in the description rather
        # than only the marketing preamble.
        description = strip_html("\n\n".join(filter(None, [
            str(raw.get("description") or ""),
            str(raw.get("basic_qualifications") or ""),
            str(raw.get("preferred_qualifications") or ""),
        ])))

        path = str(raw.get("job_path") or "").strip()
        url = f"{JOB_BASE}{path}" if path.startswith("/") else path

        return Job(
            company="Amazon",
            title=title,
            locations=locations,
            url=url,
            description=description,
            field_category=categorize_title(title),
            terms=infer_terms(title, description)[0],
            work_mode=infer_work_mode(locations),
            posted_at=_parse_posted(raw.get("posted_date")),
            external_id=str(raw.get("id_icims") or path).strip(),
            source=self.name,
            provider_rank=self.rank,
        )

    def scrape(self) -> List[Job]:
        if not self.queries:
            log.info("amazon: no queries configured")
            return []

        jobs: List[Job] = []
        seen = set()
        for query in self.queries:
            for raw in self._search(query):
                job = self._build(raw)
                if job is None or job.url in seen:
                    continue
                seen.add(job.url)
                jobs.append(job)

        log.info("amazon: %d posting(s) from %d quer%s",
                 len(jobs), len(self.queries),
                 "y" if len(self.queries) == 1 else "ies")
        return jobs
