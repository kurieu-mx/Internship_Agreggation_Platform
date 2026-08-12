"""Applying to one posting by link.

This is the escape hatch for the six employers no source reaches - IBM,
Amazon, Google, Apple, Meta, Microsoft - each of which runs its own portal.
The tests here are mostly about the fetch ladder and about not lying: a page
that cannot be read must fail loudly rather than produce a confident Job built
from nothing.
"""

from datetime import datetime, timezone

import pytest

import apply_url
from apply_url import _parse_date, fetch_posting

PAGE = "x" * 5000


class _Response:
    def __init__(self, status=200, body=""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = body
        self.content = body.encode()


# -- the fetch ladder --------------------------------------------------------


def test_a_readable_page_never_reaches_the_paid_fetcher(monkeypatch):
    """requests is free; Composio is not. Try the free one first."""
    called = {"rendered": False}
    monkeypatch.setattr(apply_url, "_fetch_plain", lambda url: "posting text")
    monkeypatch.setattr(apply_url, "_fetch_rendered",
                        lambda url: called.__setitem__("rendered", True) or "")
    assert fetch_posting("https://example.com/job") == "posting text"
    assert called["rendered"] is False


def test_a_bot_protected_page_falls_through_to_the_renderer(monkeypatch):
    """IBM answers a plain request with 202 and an empty body."""
    monkeypatch.setattr(apply_url, "_fetch_plain", lambda url: "")
    monkeypatch.setattr(apply_url, "_fetch_rendered", lambda url: "rendered text")
    assert fetch_posting("https://careers.ibm.com/job") == "rendered text"


def test_neither_route_working_returns_empty_not_a_guess(monkeypatch):
    monkeypatch.setattr(apply_url, "_fetch_plain", lambda url: "")
    monkeypatch.setattr(apply_url, "_fetch_rendered", lambda url: "")
    assert fetch_posting("https://example.com/job") == ""


def test_an_empty_202_is_not_treated_as_a_page(monkeypatch):
    """The exact shape IBM returns: a challenge response, not content."""
    monkeypatch.setattr("requests.get", lambda url, **kw: _Response(202, ""))
    assert apply_url._fetch_plain("https://careers.ibm.com/job") == ""


def test_a_tiny_body_is_not_treated_as_a_page(monkeypatch):
    monkeypatch.setattr("requests.get", lambda url, **kw: _Response(200, "<html>hi</html>"))
    assert apply_url._fetch_plain("https://example.com/job") == ""


def test_scripts_and_styles_are_stripped_from_a_real_page(monkeypatch):
    body = ("<html><head><style>body{color:red}</style>"
            "<script>var secret=1</script></head><body>"
            + "Software Engineer Intern. " * 120 + "</body></html>")
    monkeypatch.setattr("requests.get", lambda url, **kw: _Response(200, body))
    text = apply_url._fetch_plain("https://example.com/job")
    assert "Software Engineer Intern" in text
    assert "var secret" not in text and "color:red" not in text


def test_a_network_failure_is_not_an_exception(monkeypatch):
    import requests

    def _boom(url, **kw):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr("requests.get", _boom)
    assert apply_url._fetch_plain("https://example.com/job") == ""


# -- dates -------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("2026-08-11", datetime(2026, 8, 11, tzinfo=timezone.utc)),
    ("11-Aug-2026", datetime(2026, 8, 11, tzinfo=timezone.utc)),
    ("08/11/2026", datetime(2026, 8, 11, tzinfo=timezone.utc)),
    ("August 11, 2026", datetime(2026, 8, 11, tzinfo=timezone.utc)),
])
def test_the_date_formats_portals_use(value, expected):
    assert _parse_date(value) == expected


@pytest.mark.parametrize("value", ["", None, "recently", "Posted Today"])
def test_an_unusable_date_is_none(value):
    """Better no date - which the freshness gate handles - than a wrong one."""
    assert _parse_date(value) is None


def test_a_parsed_date_is_timezone_aware():
    assert _parse_date("2026-08-11").tzinfo is not None


# -- extraction --------------------------------------------------------------


def _extraction(**overrides):
    base = {"company": "IBM", "title": "Software Developer Intern 2027",
            "locations": ["Austin, TX"], "posted_date": "2026-08-11",
            "term": "Summer 2027", "is_internship": True,
            "description": "Build software. Python, Java, React."}
    base.update(overrides)
    return base


def test_extraction_builds_a_usable_job(monkeypatch):
    monkeypatch.setattr("llm.available", lambda: True)
    monkeypatch.setattr("llm.complete_json", lambda **kw: _extraction())
    job = apply_url.extract("https://careers.ibm.com/job?jobId=1", PAGE)
    assert job.company == "IBM"
    assert job.title == "Software Developer Intern 2027"
    assert job.terms == ["Summer 2027"]
    assert job.posted_at == datetime(2026, 8, 11, tzinfo=timezone.utc)
    assert job.source == "manual"


def test_a_hand_added_posting_outranks_every_collected_source(monkeypatch):
    """You asked for this one by name; a feed's copy should not overwrite it."""
    monkeypatch.setattr("llm.available", lambda: True)
    monkeypatch.setattr("llm.complete_json", lambda **kw: _extraction())
    job = apply_url.extract("https://careers.ibm.com/job", PAGE)
    assert job.provider_rank < 10


def test_a_page_with_no_company_or_title_is_refused(monkeypatch):
    """A login wall must not become a confident Job built from nothing."""
    monkeypatch.setattr("llm.available", lambda: True)
    monkeypatch.setattr("llm.complete_json",
                        lambda **kw: _extraction(company="", title=""))
    assert apply_url.extract("https://example.com/login", PAGE) is None


def test_a_failed_model_call_is_refused(monkeypatch):
    monkeypatch.setattr("llm.available", lambda: True)
    monkeypatch.setattr("llm.complete_json", lambda **kw: None)
    assert apply_url.extract("https://example.com/job", PAGE) is None


def test_no_key_means_no_invented_posting(monkeypatch):
    monkeypatch.setattr("llm.available", lambda: False)
    assert apply_url.extract("https://example.com/job", PAGE) is None


def test_a_non_internship_still_parses_but_is_flagged(monkeypatch, capsys):
    """You may have linked it deliberately; say so rather than refusing."""
    monkeypatch.setattr("llm.available", lambda: True)
    monkeypatch.setattr("llm.complete_json", lambda **kw: _extraction(
        title="Senior Staff Engineer", is_internship=False))
    job = apply_url.extract("https://example.com/job", PAGE)
    assert job is not None
    apply_url._report_gates(job)
    assert "does not read as an internship" in capsys.readouterr().out


# -- the gates ---------------------------------------------------------------


def test_a_sponsorship_bar_is_reported(capsys):
    from models import Job

    job = Job(company="Anduril", title="Software Intern", locations=["CA"],
              field_category="Software Engineering",
              description="Applicants must be US citizens. Requires a security clearance.")
    apply_url._report_gates(job)
    assert "closed to applicants needing sponsorship" in capsys.readouterr().out


def test_a_graduate_requirement_is_reported(capsys):
    from models import Job

    job = Job(company="Acme", title="Research Intern", locations=["CA"],
              field_category="AI / ML / Data",
              description="Candidates must be pursuing a PhD in computer science.")
    apply_url._report_gates(job)
    assert "graduate degree" in capsys.readouterr().out


def test_a_clean_posting_reports_nothing(capsys):
    from models import Job

    job = Job(company="Acme", title="Software Engineer Intern", locations=["TX"],
              field_category="Software Engineering",
              description="Build things in Python.")
    apply_url._report_gates(job)
    assert capsys.readouterr().out.strip() == ""


# -- the command -------------------------------------------------------------


def test_a_non_url_is_rejected_before_anything_is_fetched():
    assert apply_url.run("not-a-url") == 2


def test_an_unreadable_page_exits_nonzero(monkeypatch):
    monkeypatch.setattr(apply_url, "fetch_posting", lambda url: "")
    assert apply_url.run("https://example.com/job") == 1


def test_the_flag_is_wired_into_the_cli():
    from main import build_parser

    args = build_parser().parse_args(["--apply-url", "https://example.com/job"])
    assert args.apply_url == "https://example.com/job"
