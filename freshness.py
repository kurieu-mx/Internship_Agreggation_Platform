"""The "posted in the last 24 hours" gate.

Two kinds of source, two different questions:

* **Sources that publish a posting date** - the Simplify feed, Greenhouse,
  Lever, Ashby. ``date_posted`` is an authoritative Unix timestamp, so the
  window is an exact comparison and there is nothing to infer.

* **Sources that do not** - search results, and the logged-in boards. For
  these the honest answer is "we don't know when this was posted", and the
  best available proxy is when *we* first saw it, which ``store.py`` records.

The asymmetry is deliberate and conservative in one specific direction: a
posting with no date that we have seen before is **not** fresh. Without that
rule a dateless source would re-qualify its entire catalogue on every run and
flood the digest forever. The cost is that a dateless source's back catalogue
is invisible on the day you first add it, which is the right trade - a digest
that cries wolf daily gets ignored, and the ATS legs cover the same ground with
real timestamps anyway.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Set

import config
from models import Job

log = logging.getLogger(__name__)


def cutoff(window_hours: Optional[int] = None,
           now: Optional[datetime] = None) -> datetime:
    """The oldest timestamp that still counts as inside the window."""
    now = now or datetime.now(timezone.utc)
    hours = config.WINDOW_HOURS if window_hours is None else window_hours
    return now - timedelta(hours=hours)


def is_fresh(job: Job, window_hours: Optional[int] = None,
             now: Optional[datetime] = None,
             new_keys: Optional[Set[str]] = None) -> bool:
    """Was this posting published inside the window?

    ``new_keys`` is the set returned by :meth:`store.Store.mark_seen` - the
    postings seen for the first time this run. It is only consulted for jobs
    with no ``posted_at``; pass it whenever you have it.
    """
    edge = cutoff(window_hours, now)

    if job.posted_at is not None:
        posted = job.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return posted >= edge

    # No publication date. Fall back to when we first saw it, and require that
    # this run is the one that discovered it - see the module docstring.
    if new_keys is not None:
        return job.key in new_keys

    if job.first_seen is None:
        return False
    seen = job.first_seen
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return seen >= edge


def filter_fresh(jobs: Iterable[Job], window_hours: Optional[int] = None,
                 now: Optional[datetime] = None,
                 new_keys: Optional[Set[str]] = None) -> List[Job]:
    """Keep only postings inside the window, logging what was dropped and why."""
    jobs = list(jobs)
    kept: List[Job] = []
    dropped_dated = 0
    dropped_undated = 0

    for job in jobs:
        if is_fresh(job, window_hours, now, new_keys):
            kept.append(job)
        elif job.posted_at is not None:
            dropped_dated += 1
        else:
            dropped_undated += 1

    log.info(
        "%d/%d postings inside the %dh window (dropped %d stale, %d undated-and-known)",
        len(kept), len(jobs),
        config.WINDOW_HOURS if window_hours is None else window_hours,
        dropped_dated, dropped_undated,
    )
    return kept
