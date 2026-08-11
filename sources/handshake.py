"""Handshake postings, read through your own authenticated session.

Handshake is university-gated: postings live behind UMich SSO and there is no
public API. This adapter therefore does not authenticate on your behalf and
does not work around the login. It reuses a session **you** are already logged
into, via a cookie you export from your browser into ``HANDSHAKE_COOKIE``.

That has an honest consequence worth stating plainly: **the cookie expires**,
typically within days to weeks, and when it does this source starts returning
nothing until you refresh it. It is the least durable leg in the pipeline by
some distance. It is also the only one that can see school-restricted postings,
which is why it is here at all.

Behaviour when the cookie is absent, expired, or rejected: log once, return an
empty list. The digest still goes out on the strength of the public sources.
"""

import logging
from typing import Any, Dict, List

import requests

import config
from models import Job
from normalize import filter_us_locations, infer_work_mode
from sources.ats import categorize_title, looks_like_internship, parse_iso
from sources.base import FeedError

log = logging.getLogger(__name__)


class HandshakeSource:
    """Reads Handshake's internal job-search JSON with your session cookie."""

    name = "handshake"
    rank = 70  # real postings with real dates, but a fragile session

    def __init__(self, cookie: str = None, host: str = None,
                 session: requests.Session = None):
        self.cookie = cookie if cookie is not None else config.HANDSHAKE_COOKIE
        self.host = host or config.HANDSHAKE_HOST
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        if self.cookie:
            self.session.headers["Cookie"] = self.cookie

    def _search_url(self, page: int) -> str:
        return (
            f"https://{self.host}/stu/postings"
            f"?page={page}&per_page=50&sort=created_at&job.job_types[]=Internship"
            f"&query=Summer%202027&format=json"
        )

    def scrape(self) -> List[Job]:
        if not self.cookie:
            log.info("handshake: HANDSHAKE_COOKIE unset - contributing nothing")
            return []

        jobs: List[Job] = []
        for page in (1, 2):
            try:
                response = self.session.get(
                    self._search_url(page), timeout=config.REQUEST_TIMEOUT
                )
            except requests.RequestException as exc:
                log.warning("handshake: request failed (%s) - skipping", exc)
                break

            if response.status_code in (401, 403) or "login" in response.url:
                log.warning(
                    "handshake: session rejected (HTTP %s) - refresh HANDSHAKE_COOKIE",
                    response.status_code,
                )
                break
            if not response.ok:
                log.warning("handshake: HTTP %s - skipping", response.status_code)
                break

            try:
                payload = response.json()
            except ValueError:
                # A login redirect returns HTML, not JSON. Same diagnosis.
                log.warning("handshake: expected JSON, got HTML - session likely expired")
                break

            page_jobs = self.parse(payload)
            jobs.extend(page_jobs)
            if len(page_jobs) < 50:
                break

        log.info("handshake: %d postings", len(jobs))
        return jobs

    def diagnose(self) -> int:
        """Probe the endpoint and report exactly what came back.

        Handshake's student-facing search API is undocumented, so the URL this
        adapter calls is inferred rather than published. When it changes - or
        when it was never quite right - the failure is indistinguishable from
        an expired cookie unless you can see the response. This prints enough
        of it to tell those apart, and enough of the JSON shape to fix the
        parser against reality instead of guessing again.

        Prints nothing secret: the cookie is never echoed.
        """
        print(f"\nHandshake check\n  host:   {self.host}")
        if not self.cookie:
            print("  cookie: NOT SET")
            print("\n  -> set HANDSHAKE_COOKIE in .env (see the setup notes)")
            return 1
        print(f"  cookie: set ({len(self.cookie)} chars)")

        url = self._search_url(1)
        print(f"  url:    {url}\n")

        try:
            response = self.session.get(url, timeout=config.REQUEST_TIMEOUT,
                                        allow_redirects=True)
        except requests.RequestException as exc:
            print(f"  request failed: {type(exc).__name__}: {exc}")
            return 1

        content_type = response.headers.get("Content-Type", "?")
        print(f"  HTTP {response.status_code}  ({content_type})")
        if response.url != url:
            print(f"  redirected to: {response.url}")

        if "login" in response.url or response.status_code in (401, 403):
            print("\n  -> the session was rejected. The cookie is expired or "
                  "incomplete;\n     copy the whole Cookie header again from a "
                  "logged-in browser tab.")
            return 1

        try:
            payload = response.json()
        except ValueError:
            snippet = response.text[:200].replace("\n", " ")
            print(f"\n  response is not JSON. First 200 chars:\n    {snippet}")
            print("\n  -> usually a login page, i.e. the cookie is not being accepted.")
            return 1

        if isinstance(payload, dict):
            print(f"  JSON object, top-level keys: {sorted(payload.keys())[:12]}")
            records = payload.get("results") or payload.get("postings") or []
        elif isinstance(payload, list):
            print(f"  JSON array of {len(payload)} items")
            records = payload
        else:
            records = []

        print(f"  records found: {len(records)}")
        if records and isinstance(records[0], dict):
            print(f"  first record keys: {sorted(records[0].keys())[:20]}")

        parsed = self.parse(payload)
        print(f"  parsed into {len(parsed)} usable postings")
        for job in parsed[:5]:
            print(f"    {job.company[:24]:24s} | {job.title[:40]:40s} | "
                  f"{', '.join(job.locations)[:24]}")

        if records and not parsed:
            print("\n  -> the endpoint works but nothing parsed. Paste the "
                  "'first record keys'\n     line above and I will fix the parser "
                  "against the real shape.")
            return 1

        print("\n  Handshake is working.")
        return 0

    def parse(self, payload: Any) -> List[Job]:
        """Turn Handshake's search payload into Jobs, skipping what we can't read.

        Handshake's internal JSON is undocumented and has changed shape before,
        so every field is read defensively and a posting missing an employer or
        title is dropped rather than half-populated.
        """
        if isinstance(payload, dict):
            records = payload.get("results") or payload.get("postings") or []
        elif isinstance(payload, list):
            records = payload
        else:
            return []

        jobs: List[Job] = []
        for raw in records:
            if not isinstance(raw, dict):
                continue

            title = (raw.get("title") or raw.get("job_title") or "").strip()
            employer = raw.get("employer") or {}
            company = (
                employer.get("name") if isinstance(employer, dict) else employer
            ) or raw.get("employer_name") or ""
            company = str(company).strip()

            if not title or not company or not looks_like_internship(title):
                continue

            raw_locations = []
            for key in ("locations", "location_names"):
                value = raw.get(key)
                if isinstance(value, list):
                    raw_locations.extend(
                        v.get("name") if isinstance(v, dict) else str(v) for v in value
                    )
            if raw.get("location"):
                raw_locations.append(str(raw["location"]))
            locations = filter_us_locations([loc for loc in raw_locations if loc])
            if not locations:
                continue

            job_id = raw.get("id") or raw.get("job_id") or ""
            jobs.append(
                Job(
                    company=company,
                    title=title,
                    locations=locations,
                    field_category=categorize_title(title),
                    terms=[config.TERM_FILTER] if config.TERM_FILTER else [],
                    sponsorship="Unknown",
                    work_mode=infer_work_mode(locations),
                    url=f"https://{self.host}/jobs/{job_id}" if job_id else "",
                    source="Handshake",
                    posted_at=parse_iso(raw.get("created_at") or raw.get("posted_at")),
                    active=True,
                    description=(raw.get("description") or "").strip(),
                    external_id=f"handshake:{job_id}",
                    provider_rank=self.rank,
                )
            )

        return jobs
