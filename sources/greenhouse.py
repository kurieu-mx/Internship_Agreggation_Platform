"""Greenhouse public job boards.

``boards-api.greenhouse.io/v1/boards/{token}/jobs`` is Greenhouse's documented
public endpoint - no key, no scraping, no terms to violate. With
``content=true`` it also returns the full posting body, which is the richest
description any of our sources provides.

``first_published`` is the field that matters: it is when the posting went
live, and it does not move when a recruiter edits the description afterwards.
``updated_at`` does move, which is why the freshness gate reads the former.
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
    parse_iso,
    strip_html,
)

log = logging.getLogger(__name__)


class GreenhouseSource(AtsSource):
    name = "greenhouse"
    kind = "greenhouse"
    rank = 10

    def board_url(self, board: Board) -> str:
        return (
            "https://boards-api.greenhouse.io/v1/boards/"
            f"{board.token}/jobs?content=true"
        )

    def parse_board(self, board: Board, payload: Any) -> List[Job]:
        jobs: List[Job] = []

        for raw in (payload or {}).get("jobs", []):
            if not isinstance(raw, dict):
                continue

            title = (raw.get("title") or "").strip()
            departments = ", ".join(
                d.get("name", "") for d in (raw.get("departments") or [])
                if isinstance(d, dict)
            )
            # Greenhouse has no employment-type field, so the title is the only
            # signal. `departments` deliberately is not passed as a hint - it
            # names teams ("Internal Audit"), not employment types.
            if not looks_like_internship(title):
                continue

            location_name = ((raw.get("location") or {}).get("name") or "").strip()
            locations = filter_us_locations([location_name] if location_name else [])
            if not locations:
                continue

            description = strip_html(raw.get("content"))
            posted_at = parse_iso(raw.get("first_published")) or parse_iso(
                raw.get("updated_at")
            )
            terms, _inferred = infer_terms(title, description, posted_at)
            if config.TERM_FILTER and not any(
                config.TERM_FILTER.lower() in term.lower() for term in terms
            ):
                continue

            jobs.append(
                Job(
                    company=(raw.get("company_name") or board.name).strip(),
                    title=title,
                    locations=locations,
                    field_category=categorize_title(title, departments),
                    terms=terms,
                    sponsorship="Unknown",
                    work_mode=infer_work_mode(locations),
                    url=(raw.get("absolute_url") or "").strip(),
                    source="Greenhouse",
                    posted_at=posted_at,
                    active=True,
                    description=description,
                    deadline=parse_iso(raw.get("application_deadline")),
                    external_id=f"greenhouse:{board.token}:{raw.get('id')}",
                    provider_rank=self.rank,
                )
            )

        return jobs
