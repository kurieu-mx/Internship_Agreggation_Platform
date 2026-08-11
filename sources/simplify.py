"""The Pitt CSC / Simplify community feed.

Design note
-----------
The first version of this project scraped three sites by parsing HTML and a
rendered markdown README. All three broke within months: Simplify retired the
undocumented ``/api/jobs`` endpoint, Levels.fyi changed its DOM class names,
and the Pitt CSC repo replaced its markdown tables with a rendered site.

This reads the machine-readable ``listings.json`` that the same community repo
publishes. It is a documented artifact of that repo rather than an incidental
detail of its presentation layer, so it is far less prone to silent breakage -
and it arrives already structured, which removes the need to guess fields out
of prose.

It is the broadest source by volume but not the freshest: postings reach it
after a contributor adds them, which is why the ATS adapters outrank it.
"""

import logging
import time
from typing import Any, Dict, Iterable, List

import requests

import config
from models import Job, epoch_to_datetime
from normalize import (
    filter_us_locations,
    infer_work_mode,
    normalize_category,
    normalize_sponsorship,
)
from sources.base import FeedError

log = logging.getLogger(__name__)


class ListingsFeedScraper:
    """Reads the Pitt CSC / Simplify ``listings.json`` feed."""

    name = "simplify"
    rank = 50  # below the ATS boards, above the search-based sources

    def __init__(self, url: str = None, session: requests.Session = None):
        self.url = url or config.LISTINGS_URL
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})

    def fetch(self) -> List[Dict[str, Any]]:
        """Download the raw feed, retrying with exponential backoff."""
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
                log.info("fetched %d raw listings", len(payload))
                return payload
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                log.warning("feed fetch attempt %d/%d failed: %s",
                            attempt, config.MAX_RETRIES, exc)
                if attempt < config.MAX_RETRIES:
                    time.sleep(config.RETRY_BACKOFF * attempt)
        raise FeedError(f"could not fetch {self.url}: {last_error}") from last_error

    def scrape(self) -> List[Job]:
        return self.parse(self.fetch())

    # -- parsing ------------------------------------------------------------

    def parse(self, raw_listings: Iterable[Dict[str, Any]]) -> List[Job]:
        """Turn raw feed entries into Jobs, dropping anything out of scope.

        Filters applied, in order: hidden entries, closed entries (unless
        ACTIVE_ONLY is off), term mismatch, and non-US locations.
        """
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

            terms = [t for t in (raw.get("terms") or []) if t and t != "N/A"]
            if config.TERM_FILTER and not any(
                config.TERM_FILTER.lower() in t.lower() for t in terms
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
                    field_category=normalize_category(raw.get("category")),
                    terms=terms,
                    degrees=[d for d in (raw.get("degrees") or []) if d],
                    sponsorship=normalize_sponsorship(raw.get("sponsorship")),
                    work_mode=infer_work_mode(us_locations),
                    url=(raw.get("url") or "").strip(),
                    source=(raw.get("source") or "Simplify").strip(),
                    posted_at=epoch_to_datetime(raw.get("date_posted")),
                    active=active,
                    external_id=str(raw.get("id") or ""),
                    provider_rank=self.rank,
                )
            )

        log.info(
            "kept %d jobs (skipped: %s)",
            len(jobs),
            ", ".join(f"{k}={v}" for k, v in skipped.items() if v),
        )
        return jobs
