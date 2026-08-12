"""Workday job boards - the platform large enterprises actually use.

Why this source exists
----------------------
The three ATS adapters that came first - Greenhouse, Lever, Ashby - cover
startups, quant firms, and mid-size tech very well, and large enterprises
almost not at all, because large enterprises do not use them. That gap was not
theoretical: an IBM Summer 2027 posting went out the same day this was written
and the pipeline never saw it, along with most of the big-company tier. The
community feeds were the only reason any large employer appeared at all, and
they carry whatever a contributor happened to submit.

Workday's ``/wday/cxs`` endpoint is public, needs no key, and returns JSON.
It is what NVIDIA, Intel, Micron, Boeing, Salesforce, Adobe and most of the
Fortune 500 run their careers site on.

Two facts about the API shape the whole adapter
-----------------------------------------------
**1. Ordering depends on whether you search.** With no ``searchText`` the API
returns strict newest-first order. Pass ``searchText="intern"`` and it switches
to relevance ordering, where a 30-day-old posting outranks yesterday's. Since
the digest only cares about the last 24 hours, date order is worth far more
than server-side keyword matching: this issues an empty search and stops
walking as soon as the postings get older than the window. A company with 2000
open roles costs one or two pages, not a hundred.

**2. The list endpoint has no real dates.** ``postedOn`` is a rendered string -
"Posted Today", "Posted 5 Days Ago", "Posted 30+ Days Ago". Only the detail
endpoint carries ``startDate`` as an actual date, along with the description
and the canonical apply URL. So the string is used for what it is good for -
deciding cheaply when to stop walking - and every posting that survives gets
one detail fetch to obtain a date the freshness gate can trust.

The relative string is also why the walk stops at a *margin* beyond the window
rather than exactly at it: "Posted 1 Day Ago" spans anywhere from 24 to 48
hours, so trusting it to the hour would drop postings that are genuinely
inside the window once their real date is known.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

import requests

import config
from models import Job
from normalize import filter_us_locations, infer_work_mode
from sources.ats import (categorize_title, infer_terms, looks_like_internship,
                         strip_html)

log = logging.getLogger(__name__)

# How many list pages to walk per company before giving up. A company posting
# more than 400 jobs newer than the window is not a case worth paging through;
# it is a signal something is wrong with the date parsing.
MAX_PAGES = 20
PAGE_SIZE = 20

# Extra days walked past the window before stopping. "Posted 1 Day Ago" covers
# 24-48 hours, so stopping exactly at the window would drop postings that turn
# out to be inside it once the detail fetch supplies a real date.
STOP_MARGIN_DAYS = 2


class WorkdayBoard(NamedTuple):
    """One employer's Workday career site.

    Three parts because Workday's URLs need all three and none is derivable
    from the others: ``nvidia`` is hosted on ``wd5`` at site
    ``NVIDIAExternalCareerSite``, while ``intel`` is on ``wd1`` at ``External``.
    """

    tenant: str
    host: str
    site: str
    name: str

    @property
    def base(self) -> str:
        return (f"https://{self.tenant}.{self.host}.myworkdayjobs.com"
                f"/wday/cxs/{self.tenant}/{self.site}")


def load_workday_boards(path: Optional[str] = None) -> List[WorkdayBoard]:
    """Read the ``workday:`` block out of companies.yml.

    Each entry needs tenant, host, site and name::

        workday:
          - {tenant: nvidia, host: wd5, site: NVIDIAExternalCareerSite, name: NVIDIA}

    A malformed entry is skipped with a warning rather than aborting the run,
    matching how ``load_boards`` treats the other three platforms.
    """
    import yaml

    location = Path(path or config.COMPANIES_FILE)
    if not location.is_absolute():
        location = Path(__file__).resolve().parent.parent / location

    if not location.exists():
        log.warning("%s not found - no Workday boards configured", location)
        return []

    try:
        data = yaml.safe_load(location.read_text()) or {}
    except yaml.YAMLError as exc:
        log.warning("could not parse %s: %s", location, exc)
        return []

    entries = data.get("workday") or []
    if not isinstance(entries, list):
        log.warning("%s: expected a list under 'workday'", location)
        return []

    boards: List[WorkdayBoard] = []
    for entry in entries:
        if not isinstance(entry, dict):
            log.warning("workday: skipping non-mapping entry %r", entry)
            continue
        tenant = str(entry.get("tenant") or "").strip()
        site = str(entry.get("site") or "").strip()
        if not tenant or not site:
            log.warning("workday: entry needs both tenant and site: %r", entry)
            continue
        boards.append(WorkdayBoard(
            tenant=tenant,
            host=str(entry.get("host") or "wd1").strip(),
            site=site,
            name=str(entry.get("name") or tenant).strip(),
        ))
    return boards


_DAYS_RE = re.compile(r"(\d+)\+?\s*days?", re.I)


def days_since_posted(posted_on: str) -> Optional[int]:
    """Turn Workday's rendered ``postedOn`` string into a number of days.

    "Posted Today" -> 0, "Posted Yesterday" -> 1, "Posted 5 Days Ago" -> 5,
    "Posted 30+ Days Ago" -> 30. Returns None for anything unrecognised, which
    the caller treats as "keep walking" - an unparsed string must never be the
    reason a fresh posting is skipped.
    """
    if not posted_on:
        return None
    text = posted_on.strip().lower()
    if "today" in text or "just posted" in text:
        return 0
    if "yesterday" in text:
        return 1
    match = _DAYS_RE.search(text)
    if match:
        return int(match.group(1))
    if "month" in text or "year" in text:
        return 365
    return None


def _parse_start_date(value: Any) -> Optional[datetime]:
    """Workday's ``startDate``, as an aware UTC datetime.

    Seen as "2026-05-06" and occasionally "2026-05-06T00:00:00Z". Anything
    else returns None and the posting falls back to first-seen handling.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    for form in (None, "%Y-%m-%d", "%m/%d/%Y"):
        try:
            parsed = (datetime.fromisoformat(text) if form is None
                      else datetime.strptime(text, form))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


# Workday tenants each write addresses their own way. These are the two shapes
# seen live that normalize.py cannot read: Northrop writes
# "United States-California-Redondo Beach", Boeing writes "USA - Everett, WA".
_COUNTRY_DASH_RE = re.compile(r"^(united states|usa|us)\s*-", re.I)
_USA_PREFIX_RE = re.compile(r"^(united states|usa|us)\s*-\s*", re.I)


def _locations_from(info: Dict[str, Any]) -> List[str]:
    """Every location a posting lists, primary first.

    Workday splits these across two fields and writes them "US, CA, Santa
    Clara" - reversed from how every other source writes an address, and
    normalize.py expects "Santa Clara, CA".
    """
    raw = [info.get("location") or ""]
    extra = info.get("additionalLocations") or []
    if isinstance(extra, list):
        raw += [str(x) for x in extra]

    cleaned: List[str] = []
    for item in raw:
        item = str(item).strip()
        if not item:
            continue

        # "United States-California-Redondo Beach" -> "Redondo Beach, California"
        if _COUNTRY_DASH_RE.match(item):
            segments = [s.strip() for s in item.split("-") if s.strip()]
            if len(segments) >= 3:
                cleaned.append(f"{segments[-1]}, {segments[-2]}")
                continue
            if len(segments) == 2:
                cleaned.append(segments[1])
                continue

        # "USA - Everett, WA" -> "Everett, WA"
        item = _USA_PREFIX_RE.sub("", item).strip()

        parts = [p.strip() for p in item.split(",") if p.strip()]
        # "US, CA, Santa Clara" -> "Santa Clara, CA". Anything that does not
        # match a known shape is passed through rather than mangled.
        if len(parts) == 3 and parts[0].upper() in {"US", "USA"}:
            cleaned.append(f"{parts[2]}, {parts[1]}")
        elif len(parts) == 2 and parts[0].upper() in {"US", "USA"}:
            cleaned.append(parts[1])
        elif item:
            cleaned.append(item)
    return cleaned


class WorkdaySource:
    """Every configured Workday board, each fetched independently."""

    name = "workday"
    rank = 11        # same tier as the other ATS adapters, just after Greenhouse

    def __init__(self, boards: Optional[List[WorkdayBoard]] = None,
                 session: Optional[requests.Session] = None,
                 window_hours: Optional[int] = None):
        self.boards = boards if boards is not None else load_workday_boards()
        self.session = session or requests.Session()
        self.window_hours = window_hours or config.WINDOW_HOURS

    # -- http ---------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {"User-Agent": config.USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json"}

    def _search(self, board: WorkdayBoard, offset: int) -> Optional[dict]:
        try:
            response = self.session.post(
                f"{board.base}/jobs",
                json={"appliedFacets": {}, "limit": PAGE_SIZE,
                      "offset": offset, "searchText": ""},
                headers=self._headers(), timeout=20,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            log.debug("workday %s: search failed at offset %d (%s)",
                      board.tenant, offset, exc)
            return None

    def _detail(self, board: WorkdayBoard, external_path: str) -> Optional[dict]:
        try:
            response = self.session.get(f"{board.base}{external_path}",
                                        headers=self._headers(), timeout=20)
            response.raise_for_status()
            return (response.json() or {}).get("jobPostingInfo")
        except (requests.RequestException, ValueError) as exc:
            log.debug("workday %s: detail failed for %s (%s)",
                      board.tenant, external_path, exc)
            return None

    # -- parsing ------------------------------------------------------------

    def _build(self, board: WorkdayBoard, listing: dict,
               info: dict) -> Optional[Job]:
        title = (info.get("title") or listing.get("title") or "").strip()
        if not title:
            return None

        locations = filter_us_locations(_locations_from(info))
        # A posting with no US location is not necessarily non-US - Workday
        # sometimes gives only a country - so fall back to the country field
        # before discarding it.
        if not locations:
            country = info.get("country")
            descriptor = (country or {}).get("descriptor", "") if isinstance(country, dict) else ""
            if "united states" not in descriptor.lower():
                return None
            locations = ["United States"]

        description = strip_html(info.get("jobDescription") or "")
        url = (info.get("externalUrl") or "").strip() or (
            f"https://{board.tenant}.{board.host}.myworkdayjobs.com"
            f"/{board.site}{listing.get('externalPath', '')}"
        )

        return Job(
            company=board.name,
            title=title,
            locations=locations,
            url=url,
            description=description,
            field_category=categorize_title(title),
            terms=infer_terms(title, description),
            work_mode=infer_work_mode(locations),
            posted_at=_parse_start_date(info.get("startDate")),
            external_id=str(info.get("jobReqId") or info.get("id") or "").strip(),
            source=self.name,
            provider_rank=self.rank,
        )

    # -- the walk -----------------------------------------------------------

    def scrape_board(self, board: WorkdayBoard) -> List[Job]:
        """Walk one company's board newest-first, stopping past the window."""
        stop_after = self.window_hours / 24 + STOP_MARGIN_DAYS
        jobs: List[Job] = []
        seen_paths = set()

        for page in range(MAX_PAGES):
            payload = self._search(board, page * PAGE_SIZE)
            if not payload:
                break

            postings = payload.get("jobPostings") or []
            if not postings:
                break

            stop = False
            for listing in postings:
                if not isinstance(listing, dict):
                    continue

                age = days_since_posted(listing.get("postedOn") or "")
                # An unrecognised string must not end the walk - it would
                # silently truncate the board - but a clearly old one should.
                if age is not None and age > stop_after:
                    stop = True
                    break

                title = (listing.get("title") or "").strip()
                if not looks_like_internship(title):
                    continue

                path = listing.get("externalPath") or ""
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)

                info = self._detail(board, path)
                if not info:
                    continue

                job = self._build(board, listing, info)
                if job:
                    jobs.append(job)

            if stop or len(postings) < PAGE_SIZE:
                break

        log.debug("workday %s: %d internship(s) inside the walk", board.name, len(jobs))
        return jobs

    def scrape(self) -> List[Job]:
        if not self.boards:
            log.info("workday: no boards configured")
            return []

        jobs: List[Job] = []
        reached = 0
        for board in self.boards:
            try:
                found = self.scrape_board(board)
            except Exception as exc:            # one board must not end the source
                log.warning("workday %s failed (%s): %s",
                            board.name, type(exc).__name__, exc)
                continue
            reached += 1
            jobs.extend(found)

        log.info("workday: %d postings from %d/%d boards",
                 len(jobs), reached, len(self.boards))
        return jobs
