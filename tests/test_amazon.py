"""Amazon's own careers search.

Amazon was one of the six bespoke-portal employers reachable only by hand,
but unlike the others it publishes a plain JSON search that needs no key and
no session - and, checked before building, one that robots.txt permits. That
is the whole difference between this adapter and the LinkedIn guest API,
which returns the same shape of data under a path LinkedIn disallows.
"""

from datetime import datetime, timezone

import pytest

from sources.amazon import AmazonSource, _locations_from, _parse_posted

RAW = {
    "title": "Automation Engineer Intern, (Nationwide) - Summer 2027",
    "city": "Mt. Juliet", "state": "TN",
    "normalized_location": "Mt Juliet, Tennessee, USA",
    "posted_date": "August 13, 2026",
    "job_path": "/en/jobs/10501526/automation-engineer-intern-nationwide-summer-2027",
    "description": "Operations roles at Amazon.<br/>",
    "basic_qualifications": "- Currently enrolled in a bachelor's degree program.",
    "preferred_qualifications": "- Strong communication skills.",
}


def _source(monkeypatch, jobs):
    source = AmazonSource(queries=["automation engineer intern"])
    monkeypatch.setattr(source, "_search", lambda q: jobs)
    return source


@pytest.mark.parametrize("value,expected", [
    ("August 13, 2026", datetime(2026, 8, 13, tzinfo=timezone.utc)),
    ("Aug 13, 2026", datetime(2026, 8, 13, tzinfo=timezone.utc)),
    ("2026-08-13", datetime(2026, 8, 13, tzinfo=timezone.utc)),
])
def test_amazons_date_format_parses(value, expected):
    assert _parse_posted(value) == expected


@pytest.mark.parametrize("value", ["", None, "recently", 5])
def test_an_unusable_date_is_none(value):
    assert _parse_posted(value) is None


def test_the_structured_city_and_state_are_preferred():
    """normalized_location spells the state out and appends a country."""
    assert _locations_from(RAW) == ["Mt. Juliet, TN"]


def test_the_country_suffix_is_stripped_when_falling_back():
    assert _locations_from({"normalized_location": "Seattle, Washington, USA"}) \
        == ["Seattle, Washington"]


def test_a_posting_becomes_a_job(monkeypatch):
    job = _source(monkeypatch, [RAW]).scrape()[0]
    assert job.company == "Amazon"
    assert job.locations == ["Mt. Juliet, TN"]
    assert job.posted_at == datetime(2026, 8, 13, tzinfo=timezone.utc)
    assert job.terms == ["Summer 2027"]
    assert job.url.startswith("https://www.amazon.jobs/en/jobs/")
    assert job.source == "amazon"


def test_the_qualifications_reach_the_description(monkeypatch):
    """The undergraduate and sponsorship gates read requirement text, and
    Amazon states both in the qualification blocks rather than the preamble."""
    job = _source(monkeypatch, [RAW]).scrape()[0]
    assert "bachelor's degree" in job.description
    assert "communication skills" in job.description


def test_a_sponsorship_bar_in_the_qualifications_is_detected(monkeypatch):
    """The real posting says Amazon cannot sponsor - which is why it is
    dropped, and the reason has to be readable to be dropped for."""
    from eligibility import detect_restriction

    raw = dict(RAW, basic_qualifications=(
        "- Currently enrolled in a bachelor's degree program.\n"
        "Please note we are not able to provide sponsorship now or in the "
        "future for these positions."))
    job = _source(monkeypatch, [raw]).scrape()[0]
    assert detect_restriction(job)[0] == "No"


def test_a_non_internship_is_skipped(monkeypatch):
    assert _source(monkeypatch, [dict(RAW, title="Senior Automation Engineer")]).scrape() == []


def test_a_posting_with_no_us_location_is_skipped(monkeypatch):
    raw = dict(RAW, city="", state="", normalized_location="Hyderabad, India")
    assert _source(monkeypatch, [raw]).scrape() == []


def test_duplicates_across_queries_are_merged(monkeypatch):
    source = AmazonSource(queries=["a", "b"])
    monkeypatch.setattr(source, "_search", lambda q: [RAW])
    assert len(source.scrape()) == 1


def test_no_queries_configured_is_an_empty_result():
    assert AmazonSource(queries=[]).scrape() == []


def test_a_failed_search_does_not_raise(monkeypatch):
    import requests

    source = AmazonSource(queries=["x"])

    def _boom(*a, **kw):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(source.session, "get", _boom)
    assert source.scrape() == []
