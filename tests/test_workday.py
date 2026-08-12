"""The Workday adapter, and the employer-prominence boost.

Workday exists here because the first three ATS adapters covered startups and
mid-size tech well and large enterprises not at all - the platforms differ by
company size, so the coverage gap ran along exactly the axis that mattered. An
IBM Summer 2027 posting went out unseen, and it was not unlucky: it was
structurally invisible, along with most of the Fortune 500.

The tests that matter most are the walk-termination ones. The list endpoint
carries no real dates, only rendered strings like "Posted 5 Days Ago", so
knowing when to stop paging is the difference between one request per company
and a hundred.
"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

import config
from models import Job
from sources.workday import (WorkdayBoard, _locations_from, _parse_start_date,
                             days_since_posted, load_workday_boards)
from tailor.score import employer_bonus

BOARD = WorkdayBoard(tenant="nvidia", host="wd5",
                     site="NVIDIAExternalCareerSite", name="NVIDIA")


# -- the relative date strings -----------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("Posted Today", 0),
    ("Posted Yesterday", 1),
    ("Posted 5 Days Ago", 5),
    ("Posted 1 Day Ago", 1),
    ("Posted 30+ Days Ago", 30),
    ("posted 12 days ago", 12),
])
def test_relative_dates_parse(text, expected):
    assert days_since_posted(text) == expected


def test_an_unrecognised_string_is_not_treated_as_old():
    """Returning a number here would silently truncate the walk.

    None means "keep going" - a string we cannot read must never be the reason
    a posting from this morning is skipped.
    """
    assert days_since_posted("Posted recently") is None
    assert days_since_posted("") is None


def test_months_and_years_are_definitely_old():
    assert days_since_posted("Posted 3 Months Ago") == 365


# -- the real dates ----------------------------------------------------------


def test_the_detail_start_date_parses():
    assert _parse_start_date("2026-05-06") == datetime(2026, 5, 6, tzinfo=timezone.utc)


def test_an_iso_timestamp_parses():
    assert _parse_start_date("2026-05-06T00:00:00Z").year == 2026


def test_a_naive_date_is_made_aware():
    """A naive datetime compared against an aware one raises at runtime."""
    assert _parse_start_date("2026-05-06").tzinfo is not None


@pytest.mark.parametrize("value", ["", None, "not a date", 12345])
def test_an_unusable_date_is_none(value):
    assert _parse_start_date(value) is None


# -- locations ---------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    # Workday writes addresses back-to-front; normalize.py expects the usual order.
    ({"location": "US, CA, Santa Clara"}, ["Santa Clara, CA"]),
    ({"location": "USA - Everett, WA"}, ["Everett, WA"]),
    ({"location": "United States-California-Redondo Beach"},
     ["Redondo Beach, California"]),
    ({"location": "Chicago, IL"}, ["Chicago, IL"]),
])
def test_workday_address_shapes_are_normalised(raw, expected):
    assert _locations_from(raw) == expected


def test_additional_locations_are_included():
    got = _locations_from({"location": "Chicago, IL",
                           "additionalLocations": ["US, CA, Santa Clara"]})
    assert got == ["Chicago, IL", "Santa Clara, CA"]


def test_a_missing_location_yields_nothing():
    assert _locations_from({}) == []


# -- the walk ----------------------------------------------------------------


def _page(*postings, total=100):
    return {"total": total, "jobPostings": list(postings)}


def _listing(title, posted, path=None):
    return {"title": title, "postedOn": posted,
            "externalPath": path or f"/job/{title.replace(' ', '-')}"}


def _source(monkeypatch, pages, details=None):
    """A WorkdaySource whose HTTP calls are replaced with canned pages."""
    from sources.workday import WorkdaySource

    source = WorkdaySource(boards=[BOARD], window_hours=24)
    calls = {"search": 0, "detail": 0}

    def _search(board, offset):
        index = offset // 20
        calls["search"] += 1
        return pages[index] if index < len(pages) else None

    def _detail(board, path):
        calls["detail"] += 1
        return (details or {}).get(path, {
            "title": path.rsplit("/", 1)[-1].replace("-", " "),
            "startDate": "2026-08-11",
            "location": "US, CA, Santa Clara",
            "jobDescription": "<p>Build things.</p>",
            "externalUrl": "https://example.com/job",
        })

    monkeypatch.setattr(source, "_search", _search)
    monkeypatch.setattr(source, "_detail", _detail)
    return source, calls


def test_the_walk_stops_once_postings_are_older_than_the_window(monkeypatch):
    """The whole efficiency argument. Without this, 2000 jobs is 100 requests."""
    pages = [_page(*[_listing(f"Software Intern {i}", "Posted Today")
                     for i in range(19)],
                   _listing("Old Intern", "Posted 40 Days Ago")),
             _page(_listing("Newer Intern", "Posted Today"))]
    source, calls = _source(monkeypatch, pages)
    source.scrape_board(BOARD)
    # Stopped inside page one; page two was never requested.
    assert calls["search"] == 1


def test_an_unparseable_date_does_not_stop_the_walk(monkeypatch):
    pages = [_page(_listing("Software Intern A", "Posted whenever"),
                   *[_listing(f"Software Intern {i}", "Posted Today")
                     for i in range(19)]),
             _page(_listing("Software Intern Z", "Posted Today"))]
    source, calls = _source(monkeypatch, pages)
    source.scrape_board(BOARD)
    assert calls["search"] == 2


def test_the_stop_margin_keeps_yesterdays_postings(monkeypatch):
    """"Posted 1 Day Ago" spans 24-48h, so a 24h window must not cut at 1."""
    pages = [_page(_listing("Software Intern", "Posted 1 Day Ago"))]
    source, _ = _source(monkeypatch, pages)
    assert len(source.scrape_board(BOARD)) == 1


def test_non_internships_never_reach_the_detail_endpoint(monkeypatch):
    """One detail fetch per posting - filtering first is what keeps it cheap."""
    pages = [_page(_listing("Senior Staff Engineer", "Posted Today"),
                   _listing("Software Intern", "Posted Today"))]
    source, calls = _source(monkeypatch, pages)
    source.scrape_board(BOARD)
    assert calls["detail"] == 1


def test_a_posting_is_only_fetched_once(monkeypatch):
    repeated = _listing("Software Intern", "Posted Today", path="/job/same")
    pages = [_page(repeated, repeated)]
    source, calls = _source(monkeypatch, pages)
    source.scrape_board(BOARD)
    assert calls["detail"] == 1


def test_a_built_job_carries_the_company_and_a_real_date(monkeypatch):
    pages = [_page(_listing("Software Intern", "Posted Today"))]
    source, _ = _source(monkeypatch, pages)
    job = source.scrape_board(BOARD)[0]
    assert job.company == "NVIDIA"
    assert job.posted_at == datetime(2026, 8, 11, tzinfo=timezone.utc)
    assert job.source == "workday"
    assert "Build things." in job.description


def test_a_non_us_posting_is_dropped(monkeypatch):
    details = {"/job/Software-Intern": {
        "title": "Software Intern", "startDate": "2026-08-11",
        "location": "Penang, Malaysia", "jobDescription": "",
        "country": {"descriptor": "Malaysia"}}}
    pages = [_page(_listing("Software Intern", "Posted Today"))]
    source, _ = _source(monkeypatch, pages, details)
    assert source.scrape_board(BOARD) == []


def test_a_us_posting_with_only_a_country_survives(monkeypatch):
    """Some tenants give no city at all; that is not grounds to discard it."""
    details = {"/job/Software-Intern": {
        "title": "Software Intern", "startDate": "2026-08-11",
        "location": "", "jobDescription": "",
        "country": {"descriptor": "United States of America"}}}
    pages = [_page(_listing("Software Intern", "Posted Today"))]
    source, _ = _source(monkeypatch, pages, details)
    assert len(source.scrape_board(BOARD)) == 1


def test_one_failing_board_does_not_end_the_source(monkeypatch):
    from sources.workday import WorkdaySource

    good = WorkdayBoard("intel", "wd1", "External", "Intel")
    source = WorkdaySource(boards=[BOARD, good])

    def _explode(board):
        if board.tenant == "nvidia":
            raise RuntimeError("boom")
        return [Job(company="Intel", title="Software Intern", locations=["Austin, TX"],
                    field_category="Software Engineering")]

    monkeypatch.setattr(source, "scrape_board", _explode)
    assert len(source.scrape()) == 1


def test_no_boards_configured_is_an_empty_result():
    from sources.workday import WorkdaySource

    assert WorkdaySource(boards=[]).scrape() == []


# -- config loading ----------------------------------------------------------


def test_boards_load_from_the_yaml(tmp_path):
    path = tmp_path / "companies.yml"
    path.write_text(
        "workday:\n"
        "  - {tenant: nvidia, host: wd5, site: NVIDIAExternalCareerSite, name: NVIDIA}\n"
    )
    boards = load_workday_boards(str(path))
    assert boards == [BOARD]
    assert boards[0].base.endswith("/wday/cxs/nvidia/NVIDIAExternalCareerSite")


def test_an_entry_missing_its_site_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "companies.yml"
    path.write_text("workday:\n  - {tenant: nvidia, host: wd5}\n  - bare-string\n")
    assert load_workday_boards(str(path)) == []


def test_a_missing_file_is_not_fatal(tmp_path):
    assert load_workday_boards(str(tmp_path / "nope.yml")) == []


def test_the_shipped_config_parses():
    """Every board in companies.yml was verified live; keep it loadable."""
    boards = load_workday_boards()
    assert len(boards) >= 15
    assert any(b.name == "NVIDIA" for b in boards)


# -- employer prominence -----------------------------------------------------


def job(company):
    return Job(company=company, title="Software Engineer Intern",
               locations=["Austin, TX"], field_category="Software Engineering")


def test_a_tier_one_employer_scores_above_a_tier_two_one():
    assert employer_bonus(job("NVIDIA")) > employer_bonus(job("Intel")) > 0


def test_an_unknown_employer_gets_nothing():
    assert employer_bonus(job("Quantbot Technologies")) == 0.0


def test_matching_is_on_word_boundaries_not_substrings():
    """"hp" inside "SharpSpring" would otherwise promote a random startup."""
    assert employer_bonus(job("SharpSpring")) == 0.0
    assert employer_bonus(job("Targeted Therapeutics")) == 0.0
    assert employer_bonus(job("HP")) > 0


def test_the_company_is_checked_not_the_title():
    """A posting mentioning Google in its stack is not a Google posting."""
    posting = job("Tiny Startup")
    posting.title = "Intern working with Google Cloud and Amazon Web Services"
    assert employer_bonus(posting) == 0.0


def test_an_empty_company_is_safe():
    assert employer_bonus(job("")) == 0.0


def test_the_bonus_cannot_outweigh_a_strong_keyword_match():
    """The point is reaching the rerank pool, not winning it outright."""
    from tailor.score import keyword_score

    weights = {"pytorch": 3, "cuda": 3, "distributed": 2, "c++": 3, "python": 2}
    strong = job("Nobody Ltd")
    strong.title = "PyTorch CUDA Distributed Systems Intern"
    strong.description = "python c++ pytorch cuda distributed"
    assert keyword_score(strong, weights) > config.PRIORITY_BONUS_TIER1


def test_terms_is_a_list_of_strings_not_the_infer_terms_tuple(monkeypatch):
    """infer_terms returns (terms, inferred) and was being assigned whole.

    Greenhouse, Lever and Ashby unpack it; this adapter did not, so a Workday
    posting carried ``(['Summer 2027'], True)`` into Job.terms - a list and a
    bool where a list of strings belongs. Every consumer joins that field, so
    the digest crashed on the first Workday posting whose text named no year.
    """
    pages = [_page(_listing("Software Intern", "Posted Today"))]
    source, _ = _source(monkeypatch, pages)
    job = source.scrape_board(BOARD)[0]

    assert isinstance(job.terms, list)
    assert all(isinstance(term, str) for term in job.terms)
    ", ".join(job.terms)          # would raise TypeError on the tuple
