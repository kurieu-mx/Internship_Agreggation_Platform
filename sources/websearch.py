"""Web search, via Composio's search toolkit.

The point of this source is coverage of the long tail: a company whose board
is not in ``companies.yml`` and whose posting no aggregator has picked up yet.
It is the only leg that can find a posting nobody has catalogued.

It is also, by a wide margin, the noisiest, and the code reflects that:

* Results are **undated**. Search engines report when they indexed a page, not
  when the posting went up, so these jobs carry no ``posted_at`` and reach the
  digest only through the first-sighting path in ``freshness.py`` - seen once,
  never "new" again.
* Results are **unstructured**. A search hit is a title, a URL, and a snippet.
  Company and location have to be recovered from those, and anything that
  cannot be attributed to a company is dropped rather than guessed at.
* Results **outrank nothing**. ``rank`` is the highest number of any source,
  so wherever a board or feed has the same posting, that copy wins the merge
  and this one only contributes locations.

Everything here degrades to an empty list when Composio is unconfigured.
"""

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import config
from composio_gateway import execute
from models import Job
from normalize import filter_us_locations, infer_work_mode
from sources.ats import categorize_title, looks_like_internship

log = logging.getLogger(__name__)

# Composio's search toolkit exposes several providers behind one slug each.
# Tavily returns clean title/url/content triples, which is what this parser
# wants; override if you have a different provider connected.
SEARCH_SLUG = config.SEARCH_SLUG

# Boards worth searching. Restricting to ATS domains keeps out the aggregator
# spam sites that dominate an unrestricted "internship" query and whose links
# are usually dead or duplicated.
_TARGET_SITES = [
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "careers.google.com",
    "myworkdayjobs.com",
]

_QUERIES = [
    'Summer 2027 software engineering internship apply',
    'Summer 2027 machine learning internship apply',
    'Summer 2027 quantitative research internship apply',
    'Summer 2027 quantitative trading internship apply',
    'Summer 2027 AI research internship apply',
]

# "Company | Software Engineer Intern", "Software Engineer Intern at Company",
# "Software Engineer Intern - Company Careers"
_TITLE_SPLIT_RE = re.compile(r"\s+[|–—-]\s+|\s+\bat\b\s+", re.I)
_NOISE_RE = re.compile(
    r"\b(careers?|jobs?|job board|hiring|apply|greenhouse|lever|ashby|workday)\b", re.I
)

# ATS pages title themselves "Job Application for <role> at <company>". Left in
# place, that phrase becomes the role and ends up in a PDF filename.
_APPLICATION_PREFIX_RE = re.compile(
    r"^\s*(?:job\s+application\s+for|application\s+for|apply\s+(?:for|to))\s+", re.I
)

# Path segments that are part of an ATS's own URL structure, not a company.
_NOT_A_COMPANY = {
    "job app", "job apps", "job boards", "jobs", "embed", "boards",
    "careers", "career", "apply", "postings", "job", "search", "en",
}

_YEAR_IN_TITLE_RE = re.compile(r"\b(20\d{2})\b")


def _company_from_url(url: str) -> str:
    """Recover the employer from an ATS URL, which encodes it in the path.

    ``jobs.lever.co/matchgroup/...`` -> ``Matchgroup``. Greenhouse and Ashby
    follow the same shape. Anything else returns empty, and the caller drops
    the result rather than inventing an employer.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""

    host = (parsed.hostname or "").lower()
    if not any(marker in host for marker in
               ("greenhouse.io", "lever.co", "ashbyhq.com")):
        return ""

    # Greenhouse's embedded application form puts the employer in a query
    # parameter instead of the path: /embed/job_app?for=stripe
    query = parse_qs(parsed.query or "")
    if query.get("for"):
        return re.sub(r"[-_]+", " ", query["for"][0]).strip().title()

    parts = [p for p in (parsed.path or "").split("/") if p]
    if not parts:
        return ""

    token = parts[0]
    if token in {"embed", "boards"} and len(parts) > 1:
        token = parts[1]
    return re.sub(r"[-_]+", " ", token).strip().title()


def _company_from_title(title: str) -> str:
    """Pull an employer out of a page title, or return empty."""
    for part in _TITLE_SPLIT_RE.split(title):
        part = part.strip()
        if not part or looks_like_internship(part) or _NOISE_RE.search(part):
            continue
        if 2 <= len(part) <= 40:
            return part
    return ""


def _best_company(url: str, raw_title: str) -> str:
    """Prefer the URL for *identity*, the page title for *spelling*.

    A board token is reliable but unpunctuated: ``aquaticcapitalmanagement``
    becomes "Aquaticcapitalmanagement", which is fine in a log and wrong at
    16pt on a cover letter's letterhead. The page title usually carries the
    real spelling - "Aquatic Capital Management" - but titles are noisy and
    sometimes name the wrong entity entirely.

    So the URL decides *which* company it is, and the title is used for how to
    write it only when the two agree once punctuation and case are stripped.
    """
    from_url = _company_from_url(url)
    from_title = _company_from_title(raw_title)

    if from_url and from_title:
        squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
        if squash(from_url) == squash(from_title):
            return from_title       # same company, better spelling
        return from_url             # they disagree - trust the URL

    return from_url or from_title


def _clean_title(title: str) -> str:
    """Keep the fragment of a page title that is actually the role."""
    for part in _TITLE_SPLIT_RE.split(title):
        part = _APPLICATION_PREFIX_RE.sub("", part).strip()
        if looks_like_internship(part):
            return part
    return _APPLICATION_PREFIX_RE.sub("", title).strip()


def title_names_another_year(title: str, target_year: str) -> bool:
    """Does the title plainly name a year other than the one we want?

    The snippet check alone is too permissive: a Summer 2026 posting whose page
    happens to mention 2027 anywhere - a graduation window, a start date, a
    "for our 2027 cohort apply here" cross-link - passes it. Observed live: an
    Optiver "Quantitative Research Intern, PhD (Summer 2026)" reached the
    digest that way. A year in the *title* is unambiguous, so it is decisive.
    """
    if not target_year:
        return False
    years = set(_YEAR_IN_TITLE_RE.findall(title))
    return bool(years) and target_year not in years


class WebSearchSource:
    """Finds postings the catalogued sources have not reached yet."""

    name = "websearch"
    rank = 90  # lowest precedence: loses every merge

    def __init__(self, queries: Optional[List[str]] = None,
                 sites: Optional[List[str]] = None,
                 slug: str = SEARCH_SLUG):
        self.queries = queries if queries is not None else _QUERIES
        self.sites = sites if sites is not None else _TARGET_SITES
        self.slug = slug

    def _search(self, query: str) -> List[Dict[str, Any]]:
        payload = execute(self.slug, {"query": query, "max_results": 20})
        if payload is None:
            return []

        # Providers disagree on the envelope; accept the common shapes.
        if isinstance(payload, dict):
            for key in ("results", "organic_results", "data", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def scrape(self) -> List[Job]:
        from composio_gateway import available

        if not available():
            log.info("websearch: Composio not configured - contributing nothing")
            return []

        jobs: List[Job] = []
        seen_urls = set()
        dropped = {"not_internship": 0, "no_company": 0, "wrong_term": 0, "duplicate": 0}

        site_filter = " OR ".join(f"site:{site}" for site in self.sites)

        for query in self.queries:
            for hit in self._search(f"{query} ({site_filter})" if site_filter else query):
                url = (hit.get("url") or hit.get("link") or "").strip()
                raw_title = (hit.get("title") or "").strip()
                snippet = (hit.get("content") or hit.get("snippet")
                           or hit.get("description") or "").strip()

                if not url or not raw_title:
                    continue
                if url in seen_urls:
                    dropped["duplicate"] += 1
                    continue

                title = _clean_title(raw_title)
                if not looks_like_internship(title):
                    dropped["not_internship"] += 1
                    continue

                # No posting date is available, so the term has to be stated
                # outright somewhere in the hit. Inferring it from an index
                # date would be guessing on top of guessing.
                target_year = (
                    _YEAR_IN_TITLE_RE.search(config.TERM_FILTER or "").group(1)
                    if _YEAR_IN_TITLE_RE.search(config.TERM_FILTER or "") else ""
                )
                if title_names_another_year(title, target_year):
                    dropped["wrong_term"] += 1
                    continue

                haystack = f"{raw_title} {snippet}"
                if config.TERM_FILTER and config.TERM_FILTER.lower() not in haystack.lower():
                    if not target_year or target_year not in haystack:
                        dropped["wrong_term"] += 1
                        continue

                company = _best_company(url, raw_title)
                if not company or company.lower() in _NOT_A_COMPANY:
                    dropped["no_company"] += 1
                    continue

                locations = filter_us_locations(
                    [hit.get("location") or ""] if hit.get("location") else []
                )

                seen_urls.add(url)
                jobs.append(
                    Job(
                        company=company,
                        title=title,
                        locations=locations,
                        field_category=categorize_title(title),
                        terms=[config.TERM_FILTER] if config.TERM_FILTER else [],
                        sponsorship="Unknown",
                        work_mode=infer_work_mode(locations),
                        url=url,
                        source="Web search",
                        posted_at=None,  # deliberately: see the module docstring
                        active=True,
                        description=snippet,
                        external_id=f"websearch:{url}",
                        provider_rank=self.rank,
                    )
                )

        log.info("websearch: %d postings (dropped: %s)", len(jobs),
                 ", ".join(f"{k}={v}" for k, v in dropped.items() if v) or "none")
        return jobs
