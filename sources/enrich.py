"""Filling in a posting from the ATS its own URL points at.

The problem this solves
-----------------------
The gates that decide whether a posting is worth applying to - is it in the
US, does it require a graduate degree, will they sponsor - all read the
posting's text. Measured on a live run, **84% of the corpus carries no
description at all** and 28 postings carry no location either, because the
community feeds publish neither and search results carry only what the
indexer chose to show.

That is not a cosmetic gap. Two postings reached a real digest and were
tailored for:

* a quantitative research internship whose location field was empty and whose
  Ashby board says ``Bratislava`` - on-site in Slovakia, no use to someone who
  needs US sponsorship;
* a Prudential internship whose Workday description says "Prudential does not
  provide visa sponsorship for this position", which the feed did not carry,
  so the sponsorship gate saw an empty string and kept it.

Both were one API call away from a board this project already speaks to.

What it does
------------
For a posting whose URL is a Greenhouse, Lever, Ashby or Workday page, fetch
the structured record and fill in **only the fields that are missing**. A
source that published a good location keeps it; this never overwrites.

Where it runs
-------------
After the freshness window, not before. The gates run twice: once cheaply
over the whole corpus, then again over the handful of postings that survived
the window, once this has given them something to read. Enriching everything
first would mean hundreds of requests to answer a question about twenty
postings.
"""

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

import config
from models import Job
from normalize import filter_us_locations
from sources.ats import parse_iso, strip_html

log = logging.getLogger(__name__)

TIMEOUT = 20


def _headers() -> Dict[str, str]:
    return {"User-Agent": config.USER_AGENT, "Accept": "application/json"}


def board_of(url: str):
    """``(platform, token, identifier)`` for a posting URL, or None.

    The identifier is whatever that platform uses to pick one posting out of
    its board - a numeric id for Greenhouse and Lever, a uuid for Ashby, and
    for Workday the path of its detail endpoint.
    """
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not host or not parts:
        return None

    if "greenhouse.io" in host:
        # job-boards.greenhouse.io/<token>/jobs/<id>
        if len(parts) >= 3 and parts[1] == "jobs":
            return ("greenhouse", parts[0], parts[2])
    elif "lever.co" in host:
        # jobs.lever.co/<token>/<uuid>
        if len(parts) >= 2:
            return ("lever", parts[0], parts[1])
    elif "ashbyhq.com" in host:
        # jobs.ashbyhq.com/<token>/<uuid>
        if len(parts) >= 2:
            return ("ashby", parts[0], parts[1])
    elif "myworkdayjobs.com" in host:
        # <tenant>.<host>.myworkdayjobs.com/<site>/job/<loc>/<slug>
        tenant = host.split(".")[0]
        site = parts[0]
        job_path = "/" + "/".join(parts[1:])
        if job_path.startswith("/job/"):
            return ("workday", f"{host}|{tenant}|{site}", job_path)
    return None


def _get(url: str, session: requests.Session) -> Optional[Any]:
    try:
        response = session.get(url, headers=_headers(), timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        log.debug("enrich: %s -> %s", url, exc)
        return None


def _board_records(platform: str, token: str,
                   session: requests.Session) -> Dict[str, Dict[str, Any]]:
    """Every posting on one board, keyed by the identifier its URL uses."""
    if platform == "greenhouse":
        payload = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}"
                       f"/jobs?content=true", session)
        return {str(j.get("id")): j for j in (payload or {}).get("jobs", [])}

    if platform == "lever":
        payload = _get(f"https://api.lever.co/v0/postings/{token}?mode=json", session)
        return {str(j.get("id")): j for j in (payload or [])}

    if platform == "ashby":
        payload = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}", session)
        records = {}
        for job in (payload or {}).get("jobs", []):
            # Ashby's board API does not publish the uuid in the URL, so match
            # on the jobUrl it does publish.
            key = str(job.get("jobUrl") or "").rstrip("/").rsplit("/", 1)[-1]
            if key:
                records[key] = job
        return records

    return {}


def _fields_from(platform: str, record: Dict[str, Any]):
    """``(locations, description, posted_at)`` from one platform's record."""
    if platform == "greenhouse":
        locations = [(record.get("location") or {}).get("name", "")]
        return (locations, strip_html(record.get("content") or ""),
                parse_iso(record.get("first_published")))

    if platform == "lever":
        categories = record.get("categories") or {}
        return ([categories.get("location", "")],
                strip_html(record.get("descriptionPlain")
                           or record.get("description") or ""), None)

    if platform == "ashby":
        locations = [record.get("location") or ""]
        locations += [l.get("location", "") for l in
                      (record.get("secondaryLocations") or []) if isinstance(l, dict)]
        return (locations,
                record.get("descriptionPlain")
                or strip_html(record.get("descriptionHtml") or ""),
                parse_iso(record.get("publishedAt")))

    if platform == "workday":
        return ([record.get("location") or ""],
                strip_html(record.get("jobDescription") or ""), None)

    return ([], "", None)


def _workday_record(token: str, job_path: str,
                    session: requests.Session) -> Optional[Dict[str, Any]]:
    host, tenant, site = token.split("|")
    payload = _get(f"https://{host}/wday/cxs/{tenant}/{site}{job_path}", session)
    return (payload or {}).get("jobPostingInfo")


def enrich(jobs: List[Job], session: Optional[requests.Session] = None) -> List[Job]:
    """Fill missing locations and descriptions in place. Never overwrites.

    One request per board rather than per posting, except on Workday, where
    the description only exists on the per-posting detail endpoint.
    """
    session = session or requests.Session()
    jobs = list(jobs)

    wanted = [job for job in jobs
              if not job.locations or not (job.description or "").strip()]
    if not wanted:
        return jobs

    boards: Dict[tuple, List[Job]] = {}
    for job in wanted:
        found = board_of(job.url)
        if not found:
            continue
        platform, token, identifier = found
        boards.setdefault((platform, token), []).append((job, identifier))

    filled = 0
    for (platform, token), entries in boards.items():
        if platform == "workday":
            records = {}
            for _, job_path in entries:
                record = _workday_record(token, job_path, session)
                if record:
                    records[job_path] = record
        else:
            records = _board_records(platform, token, session)

        for job, identifier in entries:
            record = records.get(identifier)
            if not record:
                continue
            locations, description, posted = _fields_from(platform, record)

            if not job.locations:
                cleaned = [l for l in locations if l and l.strip()]
                # A board saying "Bratislava" is the answer to "is this in the
                # US", and the answer is no. Keep the raw value when it is not
                # a US location so the caller can see why it was dropped.
                job.locations = filter_us_locations(cleaned) or cleaned
            if not (job.description or "").strip() and description:
                job.description = description
            if job.posted_at is None and posted is not None:
                job.posted_at = posted
            filled += 1

    if filled:
        log.info("enriched %d posting(s) from their own ATS", filled)
    return jobs
