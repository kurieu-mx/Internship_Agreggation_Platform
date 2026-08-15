"""Filling a posting in from the ATS its own URL points at.

Two postings reached a real digest and were tailored for because the gates
had nothing to read: a quantitative research internship whose location field
was empty and whose Ashby board says "Bratislava", and a Prudential posting
whose Workday description says they do not sponsor. Measured on that run, 84%
of the corpus carried no description at all.
"""

import pytest

from models import Job
from sources.enrich import board_of, enrich


@pytest.mark.parametrize("url,expected", [
    ("https://job-boards.greenhouse.io/quantbot/jobs/4340833009",
     ("greenhouse", "quantbot", "4340833009")),
    ("https://jobs.lever.co/matchgroup/abc-123",
     ("lever", "matchgroup", "abc-123")),
    ("https://jobs.ashbyhq.com/wincent/984bf12c-8d80",
     ("ashby", "wincent", "984bf12c-8d80")),
])
def test_a_board_is_recognised_from_its_url(url, expected):
    assert board_of(url) == expected


def test_a_workday_url_yields_its_detail_path():
    platform, token, path = board_of(
        "https://pru.wd5.myworkdayjobs.com/Careers/job/Newark-NJ-USA/PGIM_R-1")
    assert platform == "workday"
    assert token == "pru.wd5.myworkdayjobs.com|pru|Careers"
    assert path == "/job/Newark-NJ-USA/PGIM_R-1"


@pytest.mark.parametrize("url", [
    "https://careers.ibm.com/en_US/careers/JobDetail?jobId=1",
    "https://www.linkedin.com/jobs/view/x-at-y-1",
    "https://example.com",
    "",
])
def test_an_unknown_host_is_not_enrichable(url):
    assert board_of(url) is None


def _job(**kw):
    base = dict(company="X", title="Software Engineer Intern", locations=[],
                field_category="Software Engineering", description="")
    base.update(kw)
    return Job(**base)


def test_a_missing_location_is_filled(monkeypatch):
    """The Wincent failure: an empty location hid an office in Bratislava."""
    monkeypatch.setattr("sources.enrich._board_records",
                        lambda p, t, s: {"uuid": {"location": "Bratislava",
                                                  "descriptionPlain": "Trade crypto."}})
    job = _job(url="https://jobs.ashbyhq.com/wincent/uuid")
    enrich([job])
    assert job.locations == ["Bratislava"]
    assert "Trade crypto." in job.description


def test_a_missing_description_is_filled(monkeypatch):
    """The Prudential failure: the sponsorship bar was in text nobody had."""
    monkeypatch.setattr("sources.enrich._board_records",
                        lambda p, t, s: {"1": {
                            "location": {"name": "Newark, NJ"},
                            "content": "<p>Prudential does not provide visa sponsorship.</p>"}})
    job = _job(company="Prudential", locations=["Newark, NJ"],
               url="https://job-boards.greenhouse.io/pru/jobs/1")
    enrich([job])

    from eligibility import detect_restriction
    assert detect_restriction(job)[0] == "No"


def test_a_good_location_is_never_overwritten(monkeypatch):
    """A source that published something real keeps it."""
    monkeypatch.setattr("sources.enrich._board_records",
                        lambda p, t, s: {"1": {"location": {"name": "Somewhere Else"},
                                               "content": "text"}})
    job = _job(locations=["Austin, TX"],
               url="https://job-boards.greenhouse.io/x/jobs/1")
    enrich([job])
    assert job.locations == ["Austin, TX"]


def test_a_posting_needing_nothing_makes_no_request(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("should not have fetched")

    monkeypatch.setattr("sources.enrich._board_records", _boom)
    job = _job(locations=["Austin, TX"], description="Full text.",
               url="https://job-boards.greenhouse.io/x/jobs/1")
    assert enrich([job]) == [job]


def test_a_board_that_does_not_answer_is_survivable(monkeypatch):
    monkeypatch.setattr("sources.enrich._board_records", lambda p, t, s: {})
    job = _job(url="https://job-boards.greenhouse.io/x/jobs/1")
    enrich([job])
    assert job.locations == []


def test_a_non_us_location_is_kept_visible_not_discarded(monkeypatch):
    """filter_us_locations returns nothing for Bratislava; the raw value is
    kept so the drop can explain itself rather than looking like no data."""
    monkeypatch.setattr("sources.enrich._board_records",
                        lambda p, t, s: {"u": {"location": "Bratislava",
                                               "descriptionPlain": "x"}})
    job = _job(url="https://jobs.ashbyhq.com/w/u")
    enrich([job])
    assert job.locations == ["Bratislava"]
