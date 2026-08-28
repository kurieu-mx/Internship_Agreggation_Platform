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
    source = AmazonSource(queries=["automation engineer intern"], business_categories=[])
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
    source = AmazonSource(queries=["a", "b"], business_categories=[])
    monkeypatch.setattr(source, "_search", lambda q: [RAW])
    assert len(source.scrape()) == 1


def test_no_queries_configured_is_an_empty_result():
    assert AmazonSource(queries=[], business_categories=[]).scrape() == []


def test_a_failed_search_does_not_raise(monkeypatch):
    import requests

    source = AmazonSource(queries=["x"], business_categories=[])

    def _boom(*a, **kw):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(source.session, "get", _boom)
    assert source.scrape() == []


class _Page:
    """One search.json response."""

    def __init__(self, jobs):
        self._jobs = jobs

    def raise_for_status(self):
        pass

    def json(self):
        return {"jobs": self._jobs}


def _paged(monkeypatch, source, pages):
    """Serve `pages` in order, recording the offset each request asked for."""
    seen = []

    def _get(url, params=None, **kw):
        seen.append(params["offset"])
        index = params["offset"] // 50
        return _Page(pages[index] if index < len(pages) else [])

    monkeypatch.setattr(source.session, "get", _get)
    return seen


def test_a_full_page_is_followed_by_the_next(monkeypatch):
    """A query with 54 results used to return the first 50 and lose the rest."""
    source = AmazonSource(queries=["intern"], business_categories=[])
    first = [dict(RAW, job_path=f"/en/jobs/{n}/intern", title="Software Engineer Intern")
             for n in range(50)]
    second = [dict(RAW, job_path=f"/en/jobs/{n}/intern", title="Software Engineer Intern")
              for n in range(50, 54)]
    offsets = _paged(monkeypatch, source, [first, second])

    assert len(source.scrape()) == 54
    assert offsets == [0, 50]


def test_a_short_page_stops_the_walk(monkeypatch):
    """No request is spent confirming that a partial page was the last one."""
    source = AmazonSource(queries=["intern"], business_categories=[])
    offsets = _paged(monkeypatch, source, [[RAW]])

    assert len(source.scrape()) == 1
    assert offsets == [0]


def test_paging_stops_at_the_ceiling(monkeypatch):
    """A query broad enough to never run short still terminates."""
    from sources.amazon import MAX_PAGES

    source = AmazonSource(queries=["intern"], business_categories=[])
    full = [dict(RAW, job_path=f"/en/jobs/{n}/intern") for n in range(50)]
    offsets = _paged(monkeypatch, source, [full] * (MAX_PAGES + 3))

    source.scrape()
    assert offsets == [n * 50 for n in range(MAX_PAGES)]


def test_a_failure_mid_walk_keeps_the_earlier_pages(monkeypatch):
    """A partial board beats no board - and page one is the recent end."""
    import requests

    source = AmazonSource(queries=["intern"], business_categories=[])
    first = [dict(RAW, job_path=f"/en/jobs/{n}/intern") for n in range(50)]

    def _get(url, params=None, **kw):
        if params["offset"] == 0:
            return _Page(first)
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(source.session, "get", _get)
    assert len(source.scrape()) == 50


def test_the_default_query_is_broad_enough_to_survive_the_category_gate():
    """The old default matched one posting, categorised Other, which prefilter
    dropped - so Amazon could never appear in a digest at all."""
    import config

    assert config.AMAZON_QUERIES == ["intern"]


INTERNSHIP_TITLED = dict(
    RAW,
    title="2027 Applied Science Internship - Reinforcement Learning",
    job_path="/en/jobs/20001/applied-science-internship",
)


def test_the_business_category_is_swept_alongside_the_queries(monkeypatch):
    """base_query matches whole words, so "intern" misses "...Internship".
    The student-programmes category is what catches those."""
    source = AmazonSource(queries=["intern"], business_categories=["studentprograms"])
    monkeypatch.setattr(source, "_search", lambda q: [RAW])
    monkeypatch.setattr(source, "_search_category", lambda c: [INTERNSHIP_TITLED])

    titles = {job.title for job in source.scrape()}
    assert "2027 Applied Science Internship - Reinforcement Learning" in titles
    assert len(titles) == 2


def test_the_two_sweeps_are_merged_not_intersected(monkeypatch):
    """A posting found by both routes is one posting, not two."""
    source = AmazonSource(queries=["intern"], business_categories=["studentprograms"])
    monkeypatch.setattr(source, "_search", lambda q: [RAW])
    monkeypatch.setattr(source, "_search_category", lambda c: [RAW])

    assert len(source.scrape()) == 1


def test_a_category_sweep_alone_still_collects(monkeypatch):
    """No base_query configured is not the same as nothing to do."""
    source = AmazonSource(queries=[], business_categories=["studentprograms"])
    monkeypatch.setattr(source, "_search_category", lambda c: [INTERNSHIP_TITLED])

    assert len(source.scrape()) == 1


def test_neither_queries_nor_categories_is_an_empty_result():
    assert AmazonSource(queries=[], business_categories=[]).scrape() == []


def test_the_category_sweep_sends_the_bracketed_parameter(monkeypatch):
    """`business_category[]` is the form the API honours; the bare name is
    ignored and silently returns the whole board."""
    source = AmazonSource(queries=[], business_categories=["studentprograms"])
    sent = {}

    def _get(url, params=None, **kw):
        sent.update(params)
        return _Page([])

    monkeypatch.setattr(source.session, "get", _get)
    source.scrape()
    assert sent["business_category[]"] == "studentprograms"
    assert "base_query" not in sent
    assert sent["country"] == "USA"


def test_the_default_sweeps_the_student_programmes_category():
    import config

    assert config.AMAZON_BUSINESS_CATEGORIES == ["studentprograms"]
