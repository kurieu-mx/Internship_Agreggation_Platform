"""The Source protocol, and the fan-out that runs every configured source.

Design note
-----------
Every source is fail-soft and independent. A source that times out, changes
its schema, or loses its credentials costs you that source's postings for one
run - not the run. This is the same discipline ``enrichment.py`` already
applies to Ollama: log one warning and carry on with what still works.

That matters more here than it did there, because the sources are not equally
reliable. The ATS boards and the community feeds are documented, structured
artifacts. The search-based and logged-in sources are neither, and they will
break. Isolating them keeps the reliable legs of the pipeline running when the
unreliable ones fall over.
"""

import logging
from typing import Dict, Iterable, List, Protocol, runtime_checkable

from models import Job

log = logging.getLogger(__name__)


class FeedError(RuntimeError):
    """Raised when a source cannot be fetched or parsed."""


@runtime_checkable
class Source(Protocol):
    """One place postings come from.

    ``name`` is what appears in ``SOURCES`` and in the run log. ``rank`` is
    dedup precedence, lower being more authoritative - see ``deduplicate``.
    """

    name: str
    rank: int

    def scrape(self) -> List[Job]:
        """Return every posting this source currently lists.

        Filtering to the target term, window, and geography happens downstream;
        a source's job is to report what it has, not to decide what matters.
        """
        ...


def collect(sources: Iterable[Source]) -> List[Job]:
    """Run every source, isolating failures, and return the merged postings.

    Sources are run in the order given. Each one's failure is logged and
    skipped. The result still needs de-duplicating - see ``deduplicate``.
    """
    jobs: List[Job] = []
    tally: Dict[str, str] = {}

    for source in sources:
        try:
            found = source.scrape()
        except Exception as exc:  # defensive: one bad source never kills a run
            log.warning("source %s failed: %s", source.name, exc)
            tally[source.name] = f"failed ({type(exc).__name__})"
            continue

        for job in found:
            if not job.source:
                job.source = source.name
            job.provider_rank = source.rank
        jobs.extend(found)
        tally[source.name] = str(len(found))

    log.info(
        "collected %d postings (%s)",
        len(jobs),
        ", ".join(f"{name}={count}" for name, count in tally.items()) or "no sources",
    )
    return jobs


def deduplicate(jobs: Iterable[Job]) -> List[Job]:
    """Collapse repeats by (company, title), merging their location lists.

    Where the same posting arrives from several sources, the one with the
    lowest ``provider_rank`` becomes the surviving record - an ATS board is the
    system of record, so its timestamp, URL, and description beat an
    aggregator's copy. Fields the winner left empty are backfilled from the
    loser rather than dropped, so merging never loses data.
    """
    merged: Dict[str, Job] = {}

    for job in jobs:
        existing = merged.get(job.key)
        if existing is None:
            merged[job.key] = job
            continue

        winner, loser = (
            (job, existing) if job.provider_rank < existing.provider_rank
            else (existing, job)
        )

        for location in loser.locations:
            if location not in winner.locations:
                winner.locations.append(location)

        # Backfill anything the winner is missing.
        for field in ("url", "description", "external_id", "field_category"):
            if not getattr(winner, field) and getattr(loser, field):
                setattr(winner, field, getattr(loser, field))
        for field in ("posted_at", "deadline"):
            if getattr(winner, field) is None and getattr(loser, field) is not None:
                setattr(winner, field, getattr(loser, field))
        for field in ("terms", "degrees"):
            if not getattr(winner, field) and getattr(loser, field):
                setattr(winner, field, list(getattr(loser, field)))

        merged[job.key] = winner

    return list(merged.values())
