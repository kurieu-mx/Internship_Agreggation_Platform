"""The credentialed sources: Composio search, LinkedIn, Handshake.

These are the legs most likely to be unconfigured, expired, or broken, so what
is tested here is mostly the *degradation*: every one of them must return an
empty list and keep going rather than raise, because the digest has to go out
on the strength of the public sources regardless.
"""

import pytest
import requests

import composio_gateway
import config
from sources.handshake import HandshakeSource
from sources.linkedin import LinkedInSource
from sources.websearch import (
    WebSearchSource,
    _clean_title,
    _company_from_title,
    _company_from_url,
)


@pytest.fixture(autouse=True)
def reset_composio_cache(monkeypatch):
    """The client caches its availability decision; don't leak it between tests."""
    monkeypatch.setattr(composio_gateway, "_client", None)
    monkeypatch.setattr(composio_gateway, "_client_error", None)


# -- the Composio client -----------------------------------------------------


def test_no_api_key_means_unavailable_not_an_exception(monkeypatch):
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    assert composio_gateway.get_client() is None
    assert composio_gateway.available() is False


def test_execute_without_a_client_returns_none(monkeypatch):
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    assert composio_gateway.execute("ANY_SLUG", {"query": "x"}) is None


def test_a_failing_tool_call_returns_none(monkeypatch):
    class _Tools:
        def execute(self, *args, **kwargs):
            raise RuntimeError("connection expired")

    monkeypatch.setattr(composio_gateway, "_client", type("C", (), {"tools": _Tools()})())
    assert composio_gateway.execute("SLUG", {}) is None


def test_a_success_envelope_is_unwrapped(monkeypatch):
    class _Tools:
        def execute(self, *args, **kwargs):
            return {"successful": True, "data": {"results": [1, 2]}}

    monkeypatch.setattr(composio_gateway, "_client", type("C", (), {"tools": _Tools()})())
    assert composio_gateway.execute("SLUG", {}) == {"results": [1, 2]}


def test_a_failure_envelope_becomes_none(monkeypatch):
    class _Tools:
        def execute(self, *args, **kwargs):
            return {"successful": False, "error": "no connected account"}

    monkeypatch.setattr(composio_gateway, "_client", type("C", (), {"tools": _Tools()})())
    assert composio_gateway.execute("SLUG", {}) is None


# -- attributing a search hit to an employer ---------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://jobs.lever.co/matchgroup/abc-123", "Matchgroup"),
        ("https://boards.greenhouse.io/janestreet/jobs/456", "Janestreet"),
        ("https://job-boards.greenhouse.io/applied-intuition/jobs/1", "Applied Intuition"),
        ("https://jobs.ashbyhq.com/etched/uuid", "Etched"),
        # Greenhouse's embed form names the employer in a query param, not the path.
        ("https://boards.greenhouse.io/embed/job_app?for=stripe", "Stripe"),
        ("https://example.com/careers/swe-intern", ""),
        ("not a url at all", ""),
    ],
)
def test_the_employer_is_recovered_from_an_ats_url(url, expected):
    assert _company_from_url(url) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Ramp | Software Engineer Intern", "Ramp"),
        ("Software Engineer Intern at Anthropic", "Anthropic"),
        ("Machine Learning Intern - Databricks", "Databricks"),
        ("Software Engineer Intern - Careers", ""),   # noise word, not a company
        ("Software Engineer Intern", ""),             # nothing but the role
    ],
)
def test_the_employer_is_recovered_from_a_page_title(title, expected):
    assert _company_from_title(title) == expected


def test_the_role_is_isolated_from_a_page_title():
    assert _clean_title("Ramp | Software Engineer Intern") == "Software Engineer Intern"


def test_a_title_with_no_separator_is_left_alone():
    assert _clean_title("Software Engineer Intern") == "Software Engineer Intern"


# -- the web search source ---------------------------------------------------


def test_web_search_contributes_nothing_when_composio_is_absent(monkeypatch):
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    assert WebSearchSource().scrape() == []


def _stub_search(monkeypatch, hits):
    monkeypatch.setattr(composio_gateway, "_client", object())
    monkeypatch.setattr(WebSearchSource, "_search", lambda self, query: hits)
    monkeypatch.setattr("sources.websearch.available", lambda: True, raising=False)
    import sources.websearch as ws
    monkeypatch.setattr(ws, "execute", lambda *a, **k: {"results": hits})


def test_a_well_formed_hit_becomes_a_job(monkeypatch):
    hits = [{
        "url": "https://jobs.lever.co/matchgroup/abc",
        "title": "Match Group | Software Engineer Intern",
        "content": "Join us for Summer 2027 in New York.",
    }]
    _stub_search(monkeypatch, hits)

    jobs = WebSearchSource(queries=["q"], sites=[]).scrape()
    assert len(jobs) == 1
    # The URL says which company; the page title supplies the spacing the
    # board token lacks. See test_the_page_title_supplies_the_spelling.
    assert jobs[0].company == "Match Group"
    assert jobs[0].title == "Software Engineer Intern"
    assert jobs[0].field_category == "Software Engineering"


def test_search_results_never_claim_a_posting_date(monkeypatch):
    """They only know when a crawler saw the page, which is not when it was posted."""
    hits = [{
        "url": "https://jobs.lever.co/matchgroup/abc",
        "title": "Match Group | Software Engineer Intern",
        "content": "Summer 2027.",
    }]
    _stub_search(monkeypatch, hits)
    assert WebSearchSource(queries=["q"], sites=[]).scrape()[0].posted_at is None


def test_search_results_rank_below_every_other_source(monkeypatch):
    hits = [{
        "url": "https://jobs.lever.co/matchgroup/abc",
        "title": "Match Group | Software Engineer Intern",
        "content": "Summer 2027.",
    }]
    _stub_search(monkeypatch, hits)
    job = WebSearchSource(queries=["q"], sites=[]).scrape()[0]
    assert job.provider_rank == 90


def test_a_hit_for_the_wrong_year_is_dropped(monkeypatch):
    hits = [{
        "url": "https://jobs.lever.co/matchgroup/abc",
        "title": "Match Group | Software Engineer Intern",
        "content": "Summer 2026 programme.",
    }]
    _stub_search(monkeypatch, hits)
    assert WebSearchSource(queries=["q"], sites=[]).scrape() == []


def test_a_hit_that_is_not_an_internship_is_dropped(monkeypatch):
    hits = [{
        "url": "https://jobs.lever.co/matchgroup/abc",
        "title": "Match Group | Staff Software Engineer",
        "content": "Summer 2027.",
    }]
    _stub_search(monkeypatch, hits)
    assert WebSearchSource(queries=["q"], sites=[]).scrape() == []


def test_a_hit_with_no_attributable_employer_is_dropped(monkeypatch):
    """Better to lose a posting than to invent the company on a cover letter."""
    hits = [{
        "url": "https://example.com/careers/123",
        "title": "Software Engineer Intern",
        "content": "Summer 2027.",
    }]
    _stub_search(monkeypatch, hits)
    assert WebSearchSource(queries=["q"], sites=[]).scrape() == []


def test_the_same_url_twice_yields_one_job(monkeypatch):
    hit = {
        "url": "https://jobs.lever.co/matchgroup/abc",
        "title": "Match Group | Software Engineer Intern",
        "content": "Summer 2027.",
    }
    _stub_search(monkeypatch, [hit, dict(hit)])
    assert len(WebSearchSource(queries=["q"], sites=[]).scrape()) == 1


# -- LinkedIn ----------------------------------------------------------------


def test_linkedin_contributes_nothing_when_composio_is_absent(monkeypatch):
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    assert LinkedInSource().scrape() == []


def test_linkedin_ranks_last():
    assert LinkedInSource().rank > WebSearchSource().rank


# -- Handshake ---------------------------------------------------------------


def test_handshake_contributes_nothing_without_a_cookie():
    assert HandshakeSource(cookie="").scrape() == []


class _Response:
    def __init__(self, status=200, payload=None, text_body=None, url="https://x/stu/postings"):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.url = url
        self._payload = payload
        self._text = text_body

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_an_expired_session_is_reported_not_raised(monkeypatch):
    """The single most likely failure: the cookie aged out."""
    source = HandshakeSource(cookie="session=abc")
    monkeypatch.setattr(source.session, "get", lambda *a, **k: _Response(status=401))
    assert source.scrape() == []


def test_a_login_redirect_returning_html_is_handled(monkeypatch):
    source = HandshakeSource(cookie="session=abc")
    monkeypatch.setattr(
        source.session, "get", lambda *a, **k: _Response(payload=None, text_body="<html>")
    )
    assert source.scrape() == []


def test_a_network_failure_is_contained(monkeypatch):
    source = HandshakeSource(cookie="session=abc")

    def _boom(*args, **kwargs):
        raise requests.ConnectionError("dns")

    monkeypatch.setattr(source.session, "get", _boom)
    assert source.scrape() == []


def test_handshake_records_are_parsed():
    payload = {
        "results": [
            {
                "id": 987,
                "title": "Software Engineer Intern",
                "employer": {"name": "Acme Robotics"},
                "locations": [{"name": "Ann Arbor, MI"}],
                "created_at": "2026-08-11T12:00:00Z",
            }
        ]
    }
    job = HandshakeSource(cookie="x").parse(payload)[0]
    assert job.company == "Acme Robotics"
    assert job.locations == ["Ann Arbor, MI"]
    assert job.posted_at is not None
    assert job.url.endswith("/jobs/987")


def test_a_handshake_record_missing_an_employer_is_dropped():
    payload = {"results": [{"id": 1, "title": "Software Engineer Intern",
                            "locations": [{"name": "Ann Arbor, MI"}]}]}
    assert HandshakeSource(cookie="x").parse(payload) == []


def test_a_non_us_handshake_record_is_dropped():
    payload = {"results": [{"id": 1, "title": "Software Engineer Intern",
                            "employer": {"name": "Acme"},
                            "locations": [{"name": "London, UK"}]}]}
    assert HandshakeSource(cookie="x").parse(payload) == []


def test_an_unexpected_handshake_shape_is_survivable():
    assert HandshakeSource(cookie="x").parse({"unexpected": True}) == []
    assert HandshakeSource(cookie="x").parse("garbage") == []


# -- rejecting the wrong year and non-companies ------------------------------
#
# Both of these reached a real digest before being fixed.


def test_a_title_naming_the_wrong_year_is_rejected():
    """Observed live: an Optiver 'Summer 2026' role reached the digest because
    its page mentioned 2027 elsewhere. A year in the title is decisive."""
    from sources.websearch import title_names_another_year

    assert title_names_another_year("Quantitative Research Intern, PhD (Summer 2026)", "2027")
    assert not title_names_another_year("Quantitative Research Intern (Summer 2027)", "2027")
    assert not title_names_another_year("Software Engineer Intern", "2027")


def test_a_title_naming_both_years_is_allowed():
    from sources.websearch import title_names_another_year

    assert not title_names_another_year("Intern - Summer 2026 & Summer 2027", "2027")


def test_the_job_application_prefix_is_stripped():
    """Otherwise 'Job Application for...' becomes the role, and the filename."""
    assert _clean_title("Job Application for Software Engineer Intern") == \
        "Software Engineer Intern"
    assert _clean_title("Apply for Machine Learning Intern") == "Machine Learning Intern"


def test_ats_path_segments_are_not_companies(monkeypatch):
    """greenhouse.io/embed/job_app yielded a company called 'Job App'."""
    hits = [{
        "url": "https://boards.greenhouse.io/embed/job_app",
        "title": "Job Application for Software Engineer Intern",
        "content": "Summer 2027.",
    }]
    _stub_search(monkeypatch, hits)
    assert WebSearchSource(queries=["q"], sites=[]).scrape() == []


def test_the_page_title_supplies_the_spelling_the_url_lacks():
    """A board token is unpunctuated; at 16pt on a letterhead that shows."""
    from sources.websearch import _best_company

    assert _best_company(
        "https://boards.greenhouse.io/aquaticcapitalmanagement/jobs/1",
        "Aquatic Capital Management | Software Engineer Intern",
    ) == "Aquatic Capital Management"


def test_a_disagreeing_title_does_not_override_the_url():
    """Titles sometimes name a job board or a parent brand, not the employer."""
    from sources.websearch import _best_company

    assert _best_company(
        "https://boards.greenhouse.io/quantbot/jobs/1",
        "SomeJobAggregator | Software Engineer Intern",
    ) == "Quantbot"


def test_the_url_alone_still_works():
    from sources.websearch import _best_company

    assert _best_company("https://jobs.lever.co/matchgroup/x", "") == "Matchgroup"
