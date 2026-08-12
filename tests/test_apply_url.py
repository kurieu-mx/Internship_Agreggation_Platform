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
    findings = apply_url.check_gates(job)
    assert any("does not read as an internship" in m for _, m in findings)


# -- the gates ---------------------------------------------------------------


def test_a_sponsorship_bar_is_reported():
    from models import Job

    job = Job(company="Anduril", title="Software Intern", locations=["CA"],
              field_category="Software Engineering",
              description="Applicants must be US citizens. Requires a security clearance.")
    findings = apply_url.check_gates(job)
    assert any(level == "warn" and "closed to applicants needing sponsorship" in m
               for level, m in findings)


def test_a_graduate_requirement_is_reported():
    from models import Job

    job = Job(company="Acme", title="Research Intern", locations=["CA"],
              field_category="AI / ML / Data",
              description="Candidates must be pursuing a PhD in computer science.")
    findings = apply_url.check_gates(job)
    assert any(level == "warn" and "graduate degree" in m for level, m in findings)


def test_a_clean_posting_reports_nothing():
    from models import Job

    job = Job(company="Acme", title="Software Engineer Intern", locations=["TX"],
              field_category="Software Engineering",
              description="Build things in Python.")
    assert apply_url.check_gates(job) == []


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


# -- roles the digest would never surface ------------------------------------
#
# The point of this path is postings the 3pm run does not find, which means it
# has to cope with roles that are not Summer 2027 internships at all.


def _job_from(monkeypatch, **fields):
    extraction = {"company": "Stripe", "title": "Software Engineer, New Grad",
                  "locations": ["New York, NY"], "posted_date": "", "term": "",
                  "is_internship": False,
                  "description": "Build payments infrastructure."}
    extraction.update(fields)
    monkeypatch.setattr("llm.available", lambda: True)
    monkeypatch.setattr("llm.complete_json", lambda **kw: extraction)
    return apply_url.extract("https://example.com/job", PAGE)


def test_terms_is_a_list_of_strings_not_the_infer_terms_tuple(monkeypatch):
    """infer_terms returns (terms, inferred); assigning it whole broke joins.

    Every consumer joins this field - the CLI line, the store row, the
    dashboard card, the cover-letter header - so the tuple form raised
    TypeError on any posting whose page stated no term.
    """
    job = _job_from(monkeypatch)
    assert isinstance(job.terms, list)
    assert all(isinstance(term, str) for term in job.terms)
    ", ".join(job.terms)          # would raise on the tuple


def test_a_non_internship_with_no_stated_term_gets_no_term(monkeypatch):
    """The Summer-N+1 fallback is reasoning about internships specifically.

    Stamping it on a new-grad role puts a fabricated "Summer 2027" in the
    cover-letter header, which a recruiter reads.
    """
    job = _job_from(monkeypatch, title="Software Engineer, New Grad")
    assert job.terms == []


def test_a_full_time_role_gets_no_term(monkeypatch):
    job = _job_from(monkeypatch, title="Senior Machine Learning Engineer")
    assert job.terms == []


def test_an_internship_with_no_stated_term_still_gets_the_inference(monkeypatch):
    """The digest's behaviour must not regress - the heuristic holds here."""
    job = _job_from(monkeypatch, title="Software Engineer Intern",
                    is_internship=True)
    assert job.terms and all("2027" in term for term in job.terms)


def test_a_term_stated_and_grounded_wins_even_on_a_non_internship(monkeypatch):
    """A full-time posting that really does name a term keeps it."""
    monkeypatch.setattr("llm.available", lambda: True)
    monkeypatch.setattr("llm.complete_json", lambda **kw: {
        "company": "Stripe", "title": "Software Engineer, New Grad",
        "locations": ["New York, NY"], "posted_date": "", "term": "Fall 2027",
        "is_internship": False, "description": "Starts in the Fall 2027 cohort."})

    job = apply_url.extract("https://example.com/job",
                            "Starts in the Fall 2027 cohort.")
    assert job.terms == ["Fall 2027"]


def test_a_year_in_the_title_is_read_rather_than_inferred(monkeypatch):
    job = _job_from(monkeypatch, title="Software Engineer Intern, Summer 2028",
                    is_internship=True)
    assert job.terms == ["Summer 2028"]


def test_a_non_internship_is_noted_not_warned(monkeypatch):
    """Applying to a new-grad role is the point, not a problem to flag.

    A warning on every non-internship card would sit next to the sponsorship
    warning until neither got read.
    """
    job = _job_from(monkeypatch, title="Software Engineer, New Grad")
    levels = {level for level, message in apply_url.check_gates(job)
              if "does not read as an internship" in message}
    assert levels == {"note"}


def test_a_sponsorship_bar_is_still_a_warning(monkeypatch):
    """The distinction is only useful if real eligibility bars stay loud."""
    from models import Job

    job = Job(company="Anduril", title="Software Engineer, New Grad",
              locations=["CA"], field_category="Software Engineering",
              description="Applicants must be US citizens.")
    findings = apply_url.check_gates(job)
    assert any(level == "warn" and "sponsorship" in message
               for level, message in findings)


# -- pasted descriptions -----------------------------------------------------


def test_a_pasted_description_skips_the_fetch(monkeypatch, tmp_path):
    """A gated page costs nothing when the text is supplied."""
    def explode(url):
        raise AssertionError("nothing should be fetched when text is pasted")

    monkeypatch.setattr(apply_url, "fetch_posting", explode)

    captured = {}

    def fake_extract(url, text):
        captured["text"] = text
        return None                # stop before tailoring; the fetch is the point

    monkeypatch.setattr(apply_url, "extract", fake_extract)

    apply_url.prepare("https://example.com/job", tmp_path,
                      description="Pasted posting body.")
    assert captured["text"] == "Pasted posting body."


def test_a_blank_description_falls_back_to_fetching(monkeypatch, tmp_path):
    monkeypatch.setattr(apply_url, "fetch_posting", lambda url: "FETCHED")

    captured = {}

    def fake_extract(url, text):
        captured["text"] = text
        return None

    monkeypatch.setattr(apply_url, "extract", fake_extract)

    apply_url.prepare("https://example.com/job", tmp_path, description="   ")
    assert captured["text"] == "FETCHED"


def test_a_missing_description_file_exits_two(tmp_path):
    assert apply_url.run("https://example.com/job",
                         description_file=str(tmp_path / "nope.txt")) == 2


def test_an_empty_description_file_exits_two(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n")
    assert apply_url.run("https://example.com/job",
                         description_file=str(empty)) == 2


def test_a_stated_term_absent_from_the_posting_is_dropped(monkeypatch):
    """The model invents seasons for full-time roles that state only a year.

    Told plainly that a start date is not a term it still answered
    "Summer 2027" for a new-grad posting whose text says "starting in 2027",
    so the answer is checked against the text rather than trusted.
    """
    monkeypatch.setattr("llm.available", lambda: True)
    monkeypatch.setattr("llm.complete_json", lambda **kw: {
        "company": "Stripe", "title": "Software Engineer, New Grad",
        "locations": ["New York, NY"], "posted_date": "",
        "term": "Summer 2027", "is_internship": False,
        "description": "Full-time role starting in 2027."})

    job = apply_url.extract("https://example.com/job",
                            "Full-time role starting in 2027. No season named.")
    assert job.terms == []


def test_a_stated_term_present_in_the_posting_is_kept(monkeypatch):
    monkeypatch.setattr("llm.available", lambda: True)
    monkeypatch.setattr("llm.complete_json", lambda **kw: {
        "company": "Ramp", "title": "Software Engineer Co-op",
        "locations": ["New York, NY"], "posted_date": "",
        "term": "Fall 2027", "is_internship": True,
        "description": "Our Fall 2027 co-op cohort."})

    job = apply_url.extract("https://example.com/job",
                            "Applications for the Fall 2027 co-op are open.")
    assert job.terms == ["Fall 2027"]


def test_the_grounding_check_ignores_case_and_spacing(monkeypatch):
    monkeypatch.setattr("llm.available", lambda: True)
    monkeypatch.setattr("llm.complete_json", lambda **kw: {
        "company": "Ramp", "title": "SWE Intern", "locations": ["NY"],
        "posted_date": "", "term": "Summer 2027", "is_internship": True,
        "description": "x"})

    job = apply_url.extract("https://example.com/job",
                            "Join us for   SUMMER   2027 in New York.")
    assert job.terms == ["Summer 2027"]


def test_a_dropped_term_still_falls_back_for_a_real_internship(monkeypatch):
    """Dropping an ungrounded term must not strip a genuine internship."""
    monkeypatch.setattr("llm.available", lambda: True)
    monkeypatch.setattr("llm.complete_json", lambda **kw: {
        "company": "Acme", "title": "Software Engineer Intern",
        "locations": ["NY"], "posted_date": "", "term": "Winter 2099",
        "is_internship": True, "description": "Build things."})

    job = apply_url.extract("https://example.com/job", "Build things. No term named.")
    assert job.terms and "Winter 2099" not in job.terms
