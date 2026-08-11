"""Lever public job boards.

``api.lever.co/v0/postings/{token}?mode=json`` returns the same postings the
company's public careers page renders, as JSON.

Two Lever quirks worth knowing: ``createdAt`` is in **milliseconds** since the
epoch, unlike every other source here; and the title lives in ``text`` rather
than ``title``. Both have bitten this parser's ancestors.
"""

import logging
from typing import Any, List

import config
from models import Job
from normalize import filter_us_locations, infer_work_mode
from sources.ats import (
    AtsSource,
    Board,
    categorize_title,
    infer_terms,
    looks_like_internship,
    parse_epoch_millis,
)

log = logging.getLogger(__name__)


class LeverSource(AtsSource):
    name = "lever"
    kind = "lever"
    rank = 11

    def board_url(self, board: Board) -> str:
        return f"https://api.lever.co/v0/postings/{board.token}?mode=json"

    def parse_board(self, board: Board, payload: Any) -> List[Job]:
        jobs: List[Job] = []

        for raw in payload or []:
            if not isinstance(raw, dict):
                continue

            title = (raw.get("text") or "").strip()
            categories = raw.get("categories") or {}
            commitment = categories.get("commitment") or ""
            department = " ".join(
                filter(None, [categories.get("department"), categories.get("team")])
            )
            if not looks_like_internship(title, commitment=commitment):
                continue

            # allLocations is the complete list; location is just the primary.
            candidates = categories.get("allLocations") or []
            if categories.get("location"):
                candidates = [categories["location"], *candidates]
            locations = filter_us_locations(dict.fromkeys(candidates))
            if not locations:
                continue

            description = (raw.get("descriptionPlain") or "").strip()
            posted_at = parse_epoch_millis(raw.get("createdAt"))
            terms, _inferred = infer_terms(title, description, posted_at)
            if config.TERM_FILTER and not any(
                config.TERM_FILTER.lower() in term.lower() for term in terms
            ):
                continue

            work_mode = infer_work_mode(locations)
            workplace = (raw.get("workplaceType") or "").strip().lower()
            if workplace in {"remote", "hybrid", "on-site", "onsite"}:
                work_mode = {"onsite": "On-site"}.get(workplace, workplace.title())

            jobs.append(
                Job(
                    company=board.name,
                    title=title,
                    locations=locations,
                    field_category=categorize_title(title, department),
                    terms=terms,
                    sponsorship="Unknown",
                    work_mode=work_mode,
                    url=(raw.get("hostedUrl") or raw.get("applyUrl") or "").strip(),
                    source="Lever",
                    posted_at=posted_at,
                    active=True,
                    description=description,
                    external_id=f"lever:{board.token}:{raw.get('id')}",
                    provider_rank=self.rank,
                )
            )

        return jobs
