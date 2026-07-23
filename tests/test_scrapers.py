import json
from pathlib import Path

import pytest

import config
from models import Job
from scrapers import ListingsFeedScraper, deduplicate

FIXTURE = Path(__file__).parent / "fixtures" / "listings_sample.json"


@pytest.fixture
def raw_listings():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def scraper():
    return ListingsFeedScraper(url="https://example.invalid/listings.json")


@pytest.fixture(autouse=True)
def default_filters(monkeypatch):
    """Pin the module-level filters so tests do not depend on the environment."""
    monkeypatch.setattr(config, "TERM_FILTER", "Summer 2026")
    monkeypatch.setattr(config, "ACTIVE_ONLY", True)


def test_parse_keeps_only_active_us_summer_postings(scraper, raw_listings):
    jobs = scraper.parse(raw_listings)
    companies = {job.company for job in jobs}

    assert "TikTok" in companies          # active, Summer 2026, San Jose CA
    assert "Bosch" in companies           # multi-term list including Summer 2026
    assert "Paramount Global" not in companies  # active=False
    assert "Waabi" not in companies       # Canada, and Spring-only
    assert "Astranis" not in companies    # Fall 2026 only
    assert "Anthropic" not in companies   # terms are N/A


def test_parse_strips_foreign_locations_from_kept_postings(scraper, raw_listings):
    monkey = [dict(raw_listings[0])]
    monkey[0]["locations"] = ["London, UK", "Austin, TX", "Toronto, ON, Canada"]
    job = scraper.parse(monkey)[0]
    assert job.locations == ["Austin, TX"]


def test_parse_includes_closed_postings_when_active_only_is_off(
    scraper, raw_listings, monkeypatch
):
    monkeypatch.setattr(config, "ACTIVE_ONLY", False)
    companies = {job.company for job in scraper.parse(raw_listings)}
    assert "Paramount Global" in companies


def test_term_filter_can_be_disabled(scraper, raw_listings, monkeypatch):
    monkeypatch.setattr(config, "TERM_FILTER", "")
    companies = {job.company for job in scraper.parse(raw_listings)}
    assert "Astranis" in companies  # Fall 2026 now allowed


def test_parse_populates_normalised_fields(scraper, raw_listings):
    bosch = next(j for j in scraper.parse(raw_listings) if j.company == "Bosch")
    assert bosch.field_category == "AI / ML / Data"   # from the long upstream name
    assert bosch.sponsorship == "Yes"
    assert bosch.work_mode == "On-site"
    assert bosch.posted_at is not None
    assert bosch.url.startswith("http")


def test_parse_skips_malformed_entries(scraper):
    junk = [
        "not a dict",
        {"company_name": "", "title": "Intern", "locations": ["SF"], "terms": ["Summer 2026"]},
        {"company_name": "Acme", "title": "", "locations": ["SF"], "terms": ["Summer 2026"]},
    ]
    assert scraper.parse(junk) == []


def test_parse_tolerates_empty_feed(scraper):
    assert scraper.parse([]) == []


def test_deduplicate_merges_locations_for_the_same_posting():
    jobs = [
        Job(company="Acme", title="SWE Intern", locations=["SF"]),
        Job(company="acme", title="swe intern", locations=["NYC"]),  # case-insensitive
        Job(company="Acme", title="ML Intern", locations=["SF"]),
    ]
    result = deduplicate(jobs)
    assert len(result) == 2
    swe = next(j for j in result if j.title == "SWE Intern")
    assert swe.locations == ["SF", "NYC"]


def test_deduplicate_does_not_duplicate_identical_locations():
    jobs = [
        Job(company="Acme", title="SWE Intern", locations=["SF"]),
        Job(company="Acme", title="SWE Intern", locations=["SF"]),
    ]
    assert deduplicate(jobs)[0].locations == ["SF"]
