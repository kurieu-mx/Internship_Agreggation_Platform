"""The vanshb03 Summer2027-Internships community feed.

A second, independently maintained aggregator with the same publishing habit
as the Simplify repo - a machine-readable ``listings.json`` in the repo - but a
leaner schema. Two differences that matter:

* **Term.** There is no ``terms`` list, only ``season`` ("Summer", "Fall", ...).
  The year lives in the repo name and nowhere in the payload, so it comes from
  ``config.VANSH_YEAR`` rather than being parsed out of anything.
* **Category.** There is no ``category`` field at all, so the title has to
  carry it - the same classifier the ATS adapters use.

It lists roughly 400 postings against Simplify's 14,000, so it earns its place
by disagreeing occasionally, not by volume. It ranks below Simplify.
"""

import logging
import time
from typing import Any, Dict, Iterable, List

import requests

import config
from models import Job, epoch_to_datetime
from normalize import filter_us_locations, infer_work_mode, normalize_sponsorship
from sources.ats import categorize_title
from sources.base import FeedError

log = logging.getLogger(__name__)


class VanshFeedScraper:
    """Reads the vanshb03 ``listings.json`` feed."""

    name = "vansh"
    rank = 60  # below Simplify, above the search-based sources

    def __init__(self, url: str = None, session: requests.Session = None,
                 year: int = None):
        self.url = url or config.VANSH_URL
        self.year = year or config.VANSH_YEAR
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})

    def fetch(self) -> List[Dict[str, Any]]:
        last_error = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                response = self.session.get(self.url, timeout=config.REQUEST_TIMEOUT)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise FeedError(
                        f"expected a JSON array of listings, got {type(payload).__name__}"
                    )
                return payload
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < config.MAX_RETRIES:
                    time.sleep(config.RETRY_BACKOFF * attempt)
        raise FeedError(f"could not fetch {self.url}: {last_error}") from last_error

    def scrape(self) -> List[Job]:
        return self.parse(self.fetch())

    def parse(self, raw_listings: Iterable[Dict[str, Any]]) -> List[Job]:
        jobs: List[Job] = []
        skipped = {"hidden": 0, "closed": 0, "term": 0, "non_us": 0, "malformed": 0}

        for raw in raw_listings:
            if not isinstance(raw, dict):
                skipped["malformed"] += 1
                continue

            if not raw.get("is_visible", True):
                skipped["hidden"] += 1
                continue

            active = bool(raw.get("active", True))
            if config.ACTIVE_ONLY and not active:
                skipped["closed"] += 1
                continue

            season = (raw.get("season") or "").strip()
            terms = [f"{season} {self.year}"] if season else []
            if config.TERM_FILTER and not any(
                config.TERM_FILTER.lower() in term.lower() for term in terms
            ):
                skipped["term"] += 1
                continue

            us_locations = filter_us_locations(raw.get("locations") or [])
            if not us_locations:
                skipped["non_us"] += 1
                continue

            company = (raw.get("company_name") or "").strip()
            title = (raw.get("title") or "").strip()
            if not company or not title:
                skipped["malformed"] += 1
                continue

            jobs.append(
                Job(
                    company=company,
                    title=title,
                    locations=us_locations,
                    field_category=categorize_title(title),
                    terms=terms,
                    sponsorship=normalize_sponsorship(raw.get("sponsorship")),
                    work_mode=infer_work_mode(us_locations),
                    url=(raw.get("url") or "").strip(),
                    source=(raw.get("source") or "vanshb03").strip(),
                    posted_at=epoch_to_datetime(raw.get("date_posted")),
                    active=active,
                    external_id=f"vansh:{raw.get('id') or ''}",
                    provider_rank=self.rank,
                )
            )

        log.info(
            "vansh: kept %d jobs (skipped: %s)",
            len(jobs),
            ", ".join(f"{k}={v}" for k, v in skipped.items() if v) or "none",
        )
        return jobs
