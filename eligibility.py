"""Work-authorisation filtering, for an applicant who needs sponsorship.

Why this is not a one-line field check
--------------------------------------
The obvious implementation - drop anything whose ``sponsorship`` field says
"No" - removes nothing. Measured against the live corpus, **100% of postings
report ``Unknown``**: the community feeds carry the field but almost nobody
fills it in, and the ATS boards have no such field at all. A filter on that
field alone would be a no-op that reads like a safeguard, which is worse than
no filter.

The signal that does exist is in the posting text: clearance requirements,
ITAR/export-control language, and explicit "we cannot sponsor" statements. So
this module reads the description and *derives* a restriction, then filters on
that.

The honest limitation
---------------------
Only sources that publish a description can be inspected, which today means
the ATS boards - roughly a tenth of the corpus. For everything else there is
no text to read and the posting is **kept**, because dropping every posting we
cannot verify would discard the overwhelming majority of real opportunities.

That is the deliberate trade: this catches the postings that *say* they are
closed to you, not all of the ones that are. ``filter_eligible`` reports both
numbers so the gap is visible rather than implied away.
"""

import logging
import re
from typing import Iterable, List, Optional, Tuple

import config
from models import Job

log = logging.getLogger(__name__)

# Explicitly declines to sponsor. Written to require the negation adjacent to
# the sponsorship word, so "we are able to sponsor" cannot match.
_NO_SPONSORSHIP_RE = re.compile(
    r"(?:not|unable|cannot|can't|won't|will not|does not|do not|no)\s+"
    r"(?:\w+\s+){0,4}?sponsor"
    r"|sponsorship\s+is\s+not\s+(?:available|offered|provided)"
    r"|no\s+(?:visa\s+)?sponsorship"
    r"|without\s+the\s+need\s+for\s+(?:current\s+or\s+future\s+)?(?:visa\s+)?sponsorship"
    r"|not\s+(?:be\s+)?(?:able|eligible)\s+to\s+sponsor",
    re.I,
)

# Requires US citizenship or permanent residency outright.
_CITIZEN_RE = re.compile(
    r"must\s+be\s+(?:a\s+)?u\.?s\.?\s+citizen"
    # Both word orders: "US citizenship is required" and "requires US citizenship".
    r"|u\.?s\.?\s+citizenship\s+(?:is\s+)?(?:required|mandatory)"
    r"|(?:requires?|requiring)\s+(?:\w+\s+){0,2}?u\.?s\.?\s+citizenship"
    r"|restricted\s+to\s+u\.?s\.?\s+citizens"
    r"|u\.?s\.?\s+persons?\s+(?:only|as\s+defined)"
    r"|citizens?\s+only"
    r"|permanent\s+resident\s+status\s+(?:is\s+)?required",
    re.I,
)

# Clearance and export-control requirements. These do not name citizenship,
# but a security clearance cannot be granted to a student on an F-1 visa and
# ITAR/EAR roles are restricted to US persons, so in practice they are the
# same restriction stated in the language of the defence industry. Given the
# target list includes Anduril, Palantir, Shield AI and RTX, this catches more
# real cases than the explicit wording does.
_CLEARANCE_RE = re.compile(
    r"security\s+clearance"
    r"|\bitar\b"
    r"|export[\s-]control"
    r"|\bts/sci\b"
    r"|(?:top\s+secret|secret)\s+clearance"
    r"|ability\s+to\s+obtain\s+(?:a\s+)?clearance",
    re.I,
)

# Explicitly offers sponsorship - checked last, and only used to upgrade an
# otherwise-unknown posting, never to override a restriction found above.
_SPONSORS_RE = re.compile(
    r"(?:will|do|does|can|able\s+to|happy\s+to)\s+sponsor"
    r"|sponsorship\s+(?:is\s+)?available"
    r"|we\s+sponsor",
    re.I,
)

RESTRICTED = {"No", "US citizens only"}


def detect_restriction(job: Job) -> Tuple[str, str]:
    """Derive a sponsorship status for one posting from everything it says.

    Returns ``(status, reason)``. ``status`` uses the same vocabulary as
    ``normalize.normalize_sponsorship``; ``reason`` is the evidence, so a
    dropped posting can explain itself rather than vanishing silently.

    A status the source stated explicitly is trusted and returned unchanged -
    this only ever fills in ``Unknown``.
    """
    if job.sponsorship and job.sponsorship != "Unknown":
        return job.sponsorship, "stated by the source"

    haystack = f"{job.title}\n{job.description}"
    if not haystack.strip():
        return "Unknown", ""

    match = _CITIZEN_RE.search(haystack)
    if match:
        return "US citizens only", f"posting says {match.group(0).strip()!r}"

    match = _CLEARANCE_RE.search(haystack)
    if match:
        return "US citizens only", f"requires clearance/export control ({match.group(0).strip()!r})"

    match = _NO_SPONSORSHIP_RE.search(haystack)
    if match:
        return "No", f"posting says {match.group(0).strip()!r}"

    match = _SPONSORS_RE.search(haystack)
    if match:
        return "Yes", f"posting says {match.group(0).strip()!r}"

    return "Unknown", ""


def annotate(jobs: Iterable[Job]) -> List[Job]:
    """Fill in ``sponsorship`` from the posting text where it was Unknown."""
    jobs = list(jobs)
    for job in jobs:
        status, reason = detect_restriction(job)
        job.sponsorship = status
        if reason and status in RESTRICTED:
            job.score_reason = reason
    return jobs


def only_internships(jobs: Iterable[Job]) -> List[Job]:
    """Drop anything that is not actually an internship.

    The ATS and search adapters each check this while parsing, because they
    read boards that list every job a company has open. The community feeds
    were trusted not to need it - they are called "Summer2027-Internships",
    after all - and that trust was misplaced: measured live, six full-time and
    rotational-graduate roles were reaching the shortlist, including a plain
    "Software Engineer" and two "Investment Analyst Program" postings.

    So the check runs once more here, over everything, whatever its source. A
    duplicated filter is cheap; a tailored resume sent to a full-time req is
    not.
    """
    from sources.ats import looks_like_internship

    jobs = list(jobs)
    kept = [job for job in jobs if looks_like_internship(job.title)]
    dropped = len(jobs) - len(kept)
    if dropped:
        log.info("dropped %d posting(s) that are not internships", dropped)
        for job in jobs:
            if job not in kept:
                log.debug("  not an internship: %s - %s", job.company, job.title)
    return kept


def filter_eligible(jobs: Iterable[Job],
                    exclude: Optional[set] = None) -> List[Job]:
    """Drop postings that state they are closed to someone needing sponsorship.

    Postings whose status is still ``Unknown`` are **kept**. That is not an
    oversight: most sources publish nothing on the subject, so treating silence
    as a rejection would throw away most of the corpus. The log line reports how
    many were kept unverified, so the size of the blind spot stays visible.
    """
    exclude = exclude if exclude is not None else set(config.EXCLUDE_SPONSORSHIP)
    jobs = annotate(jobs)

    kept: List[Job] = []
    dropped: List[Job] = []
    for job in jobs:
        (dropped if job.sponsorship in exclude else kept).append(job)

    unverified = sum(1 for job in kept if job.sponsorship == "Unknown")
    inspectable = sum(1 for job in jobs if job.description)

    log.info(
        "eligibility: kept %d, dropped %d as closed to sponsorship "
        "(%d kept unverified; only %d/%d postings carried text to inspect)",
        len(kept), len(dropped), unverified, inspectable, len(jobs),
    )
    for job in dropped:
        log.debug("  dropped %s - %s (%s)", job.company, job.title, job.score_reason)

    return kept
