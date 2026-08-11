"""Ashby public job boards.

``api.ashbyhq.com/posting-api/job-board/{token}`` returns ``{jobs: [...]}``.

Ashby is the one board here that tells you whether a posting is currently
listed (``isListed``) and gives a first-class ``employmentType``, so the
internship check has a structured signal to lean on rather than only the title.
Locations are split across ``location`` and ``secondaryLocations``, and a
US-only role at a company with EU offices will have the foreign ones in the
latter - so both are collected and then filtered, never just the primary.
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
)

log = logging.getLogger(__name__)


class AshbySource(AtsSource):
    name = "ashby"
    kind = "ashby"
    rank = 12

    def board_url(self, board: Board) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{board.token}"

    def parse_board(self, board: Board, payload: Any) -> List[Job]:
        jobs: List[Job] = []

        for raw in (payload or {}).get("jobs", []):
            if not isinstance(raw, dict):
                continue
            if raw.get("isListed") is False:
                continue

            title = (raw.get("title") or "").strip()
            department = " ".join(
                filter(None, [raw.get("department"), raw.get("team")])
            )
            if not looks_like_internship(
                title, employment_type=raw.get("employmentType") or ""
            ):
                continue

            candidates = [raw.get("location") or ""]
            for secondary in raw.get("secondaryLocations") or []:
                if isinstance(secondary, dict):
                    candidates.append(secondary.get("location") or "")
                elif isinstance(secondary, str):
                    candidates.append(secondary)
            locations = filter_us_locations(
                [loc for loc in dict.fromkeys(candidates) if loc]
            )
            if not locations:
                continue

            description = (raw.get("descriptionPlain") or "").strip()
            posted_at = parse_iso(raw.get("publishedAt"))
            terms, _inferred = infer_terms(title, description, posted_at)
            if config.TERM_FILTER and not any(
                config.TERM_FILTER.lower() in term.lower() for term in terms
            ):
                continue

            work_mode = infer_work_mode(locations)
            if raw.get("isRemote"):
                work_mode = "Remote"
            workplace = (raw.get("workplaceType") or "").strip().lower()
            if workplace == "hybrid":
                work_mode = "Hybrid"

            jobs.append(
                Job(
                    company=board.name,
                    title=title,
                    locations=locations,
                    field_category=categorize_title(title, department),
                    terms=terms,
                    sponsorship="Unknown",
                    work_mode=work_mode,
                    url=(raw.get("jobUrl") or raw.get("applyUrl") or "").strip(),
                    source="Ashby",
                    posted_at=posted_at,
                    active=True,
                    description=description,
                    external_id=f"ashby:{board.token}:{raw.get('id')}",
                    provider_rank=self.rank,
                )
            )

        return jobs
