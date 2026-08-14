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


# Degree vocabulary. The distinction that matters is not "does this mention a
# graduate degree" but "is a bachelor's student eligible" - "Bachelor's or
# Master's" and "BS/MS" are open to undergraduates and must be kept.
_BACHELORS_RE = re.compile(
    # "ug" is included because postings abbreviate it: "[UG/Masters]" is an
    # either/or open to undergraduates, and without it that reads as
    # Masters-only and gets dropped.
    r"\b(bachelor'?s?|b\.?s\.?e?\.?|b\.?a\.?|undergrad(uate)?|ug|bs/ms|bs\s*/\s*ms)\b",
    re.I,
)
_GRADUATE_RE = re.compile(
    r"\b(ph\.?d\.?|doctoral|doctorate|master'?s?|m\.?s\.?c?\.?|m\.?eng\.?|mba)\b",
    re.I,
)
# "pursuing a PhD", "enrolled in a Master's program", "must be a PhD candidate"
_GRAD_REQUIREMENT_RE = re.compile(
    r"(pursuing|enrolled in|working toward|candidate for|currently in|must be)"
    r"[^.]{0,80}?\b(ph\.?d|doctoral|master'?s|m\.?s\.?c?\b|graduate program)",
    re.I,
)


# A company value that is really a piece of the job title. The search sources
# split a posting's heading on a dash and take whatever follows as the
# employer, which produces "Master's: Summer 2027" and
# "Elevate/Data Science [UG/Masters]" as company names.
#
# Deliberately shape-based rather than keyword-based: a real employer name
# does not carry a colon, a bracket, or a hiring term, and matching on shape
# means this keeps working for fragments nobody has seen yet.
_TITLE_FRAGMENT_RE = re.compile(
    r"[:\[\]]"                                    # "Master's: Summer 2027"
    r"|\b(summer|fall|winter|spring)\s+20\d{2}"   # a term, not a company
    r"|\b(intern(ship)?|co-?op|new ?grad)\b",     # a role, not a company
    re.I,
)


def looks_like_title_fragment(company: str) -> bool:
    """Is this 'company' actually a piece of the job title?

    Used to decide whether the company field is worth reading for signals that
    belong to the posting - and, in the digest, whether a letter addressed to
    it would embarrass you.
    """
    return bool(company and _TITLE_FRAGMENT_RE.search(company))


def requires_graduate_degree(job: Job) -> bool:
    """Is this posting closed to an undergraduate?

    Three signals, in descending order of authority:

    1. The feed's own ``degrees`` list, which is structured and unambiguous.
       Measured live: 34 postings list only PhD, 10 only Master's - and 114
       list *both* Bachelor's and Master's, which are open to undergraduates
       and must not be caught.
    2. The title, when it names a degree level: "Quantitative Research Intern -
       PhD" is closed, "Quantitative Research Intern (BS/MS)" is not.
    3. The description, when it states a requirement. "Pursuing a PhD" closes
       it; "pursuing a Bachelor's or Master's" does not.

    Silence means eligible. Most postings say nothing about degree level, and
    treating that as a graduate requirement would discard the majority of them.
    """
    # 1. The structured field is decisive when present.
    if job.degrees:
        listed = " ".join(job.degrees)
        if _GRADUATE_RE.search(listed) and not _BACHELORS_RE.search(listed):
            return True
        return False

    # 2. The title, plus a company field that is obviously a fragment of one.
    #
    # The search sources split a posting's heading into title and company, and
    # they do it badly: "Quantitative Research Internship - Master's: Summer
    # 2027" arrived as title="Quantitative Research Internship" with
    # company="Master's: Summer 2027". The degree requirement was on the page,
    # just in the wrong field, and reading only the title let a Master's-only
    # quant role through as undergraduate-eligible.
    #
    # Only *malformed* company values are folded in, never well-formed ones -
    # a real employer called "Masters Gallery Foods" must not be mistaken for
    # a degree requirement.
    heading = job.title
    if looks_like_title_fragment(job.company):
        heading = f"{job.title} {job.company}"

    if _GRADUATE_RE.search(heading) and not _BACHELORS_RE.search(heading):
        return True

    # 3. The description, checking each matched requirement in isolation so a
    #    "Bachelor's or Master's" elsewhere in a long posting cannot excuse a
    #    genuine "must hold a PhD" - and vice versa.
    for match in _GRAD_REQUIREMENT_RE.finditer(job.description or ""):
        if not _BACHELORS_RE.search(match.group(0)):
            return True

    return False


def only_undergraduate_eligible(jobs: Iterable[Job]) -> List[Job]:
    """Drop postings that require a degree the candidate does not have.

    An internship asking for a PhD is not an opportunity for an undergraduate,
    and it should not occupy one of the tailoring slots - the same reasoning
    as the work-authorisation filter.
    """
    jobs = list(jobs)
    kept, dropped = [], []
    for job in jobs:
        (dropped if requires_graduate_degree(job) else kept).append(job)

    if dropped:
        log.info("dropped %d posting(s) requiring a graduate degree", len(dropped))
        for job in dropped:
            log.debug("  graduate-only: %s - %s (degrees=%s)",
                      job.company, job.title, job.degrees or "unstated")
    return kept


def drop_malformed(jobs: Iterable[Job]) -> List[Job]:
    """Drop postings whose employer name is a fragment of their own title.

    These come from the two search-backed sources, which split a heading on a
    dash and treat whatever follows as the company. The result is a posting
    that survives every other filter and then takes a tailoring slot, so a
    cover letter gets addressed to "Master's: Summer 2027" and a resume is
    tailored for an employer that does not exist.

    Only the malformed ones go. The underlying posting is usually real and
    usually reachable another way - Susquehanna's board is polled directly -
    so this costs a duplicate, not an opportunity.
    """
    jobs = list(jobs)
    kept = [job for job in jobs if not looks_like_title_fragment(job.company)]
    dropped = len(jobs) - len(kept)
    if dropped:
        log.info("dropped %d posting(s) whose company name is a title fragment",
                 dropped)
        for job in jobs:
            if job not in kept:
                log.debug("  malformed company: %r (title %r, source %s)",
                          job.company, job.title, job.source)
    return kept


# A co-op is a full-time work term during the academic year, usually a
# semester or two. It is not an internship you can take between school years,
# and taking one means not being enrolled - which for a student on an F-1 visa
# is a different conversation entirely.
_COOP_RE = re.compile(r"\bco-?ops?\b", re.I)

# "Intern/Co-op" names both, and those postings are real internships that also
# accept co-op students. Only a title that names *only* co-op is excluded.
_INTERN_WORD_RE = re.compile(r"\bintern(ship)?s?\b", re.I)


def is_coop_only(job: Job) -> bool:
    """Is this a co-op rather than an internship?

    Checked on the title alone. A description mentioning a co-op programme
    somewhere in its benefits blurb is not the same as a co-op posting.
    """
    title = job.title or ""
    return bool(_COOP_RE.search(title)) and not _INTERN_WORD_RE.search(title)


def exclude_coops(jobs: Iterable[Job]) -> List[Job]:
    """Drop co-op postings, which require a term out of school."""
    jobs = list(jobs)
    kept = [job for job in jobs if not is_coop_only(job)]
    dropped = len(jobs) - len(kept)
    if dropped:
        log.info("dropped %d co-op posting(s)", dropped)
        for job in jobs:
            if job not in kept:
                log.debug("  co-op: %s - %s", job.company, job.title)
    return kept


_SEASON_RE = re.compile(r"\b(summer|fall|autumn|winter|spring)\b", re.I)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _season_and_year(text: str):
    """The season and year a string names, either possibly None."""
    season = _SEASON_RE.search(text or "")
    year = _YEAR_RE.search(text or "")
    name = season.group(1).lower() if season else None
    if name == "autumn":            # the same term under two names
        name = "fall"
    return name, (year.group(1) if year else None)


def names_a_different_term(job: Job, target: str) -> bool:
    """Does this posting state a term other than the one being searched for?

    The web-search adapter checked only the *year*, which let "Machine
    Learning Intern/Co-op (Winter 2027)" through a Summer 2027 filter: the
    year matched, and nothing looked at the season. A winter term is a
    different job at a different time of year.

    Silence is not a mismatch. Most ATS postings state no term at all, and
    dropping those would discard the majority of the corpus - the same
    reasoning as the sponsorship and degree gates.
    """
    want_season, want_year = _season_and_year(target)
    if not want_season and not want_year:
        return False

    # The stated terms, plus the title, which is where a term is usually
    # written even when the source publishes no term field.
    stated = " ".join(job.terms or []) + " " + (job.title or "")
    got_season, got_year = _season_and_year(stated)

    if want_year and got_year and got_year != want_year:
        return True
    if want_season and got_season and got_season != want_season:
        return True
    return False


def only_target_term(jobs: Iterable[Job],
                     target: Optional[str] = None) -> List[Job]:
    """Drop postings that name a term other than the configured one."""
    target = target if target is not None else config.TERM_FILTER
    jobs = list(jobs)
    if not target:
        return jobs

    kept = [job for job in jobs if not names_a_different_term(job, target)]
    dropped = len(jobs) - len(kept)
    if dropped:
        log.info("dropped %d posting(s) naming a term other than %s",
                 dropped, target)
        for job in jobs:
            if job not in kept:
                log.debug("  wrong term: %s - %s (%s)",
                          job.company, job.title, job.terms)
    return kept


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
