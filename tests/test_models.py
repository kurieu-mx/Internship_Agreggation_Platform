from datetime import datetime, timezone

import pytest
import requests

import config
from models import Job, epoch_to_datetime
from scrapers import FeedError, ListingsFeedScraper


def test_row_length_matches_headers():
    job = Job(company="Acme", title="SWE Intern")
    assert len(job.to_row()) == len(config.COLUMN_HEADERS)


def test_to_dict_is_keyed_by_header():
    job = Job(company="Acme", title="SWE Intern", locations=["SF", "NYC"])
    row = job.to_dict()
    assert row["Company"] == "Acme"
    assert row["Location"] == "SF; NYC"


def test_posted_date_is_formatted_and_missing_date_is_blank():
    stamped = Job(company="A", title="B", posted_at=datetime(2026, 3, 4, tzinfo=timezone.utc))
    assert stamped.to_row()[config.COLUMN_HEADERS.index("Posted")] == "2026-03-04"
    assert Job(company="A", title="B").to_row()[config.COLUMN_HEADERS.index("Posted")] == ""


def test_key_ignores_case_and_surrounding_whitespace():
    assert Job(company=" Acme ", title="SWE Intern").key == Job(company="acme", title="swe intern").key


@pytest.mark.parametrize("value", [None, "", "not-a-number", float("nan")])
def test_bad_timestamps_become_none(value):
    assert epoch_to_datetime(value) is None


def test_valid_timestamp_parses():
    assert epoch_to_datetime(1764088142).year == 2025


class _Response:
    def __init__(self, payload=None, exc=None):
        self._payload = payload
        self._exc = exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc

    def json(self):
        return self._payload


class _Session:
    """Minimal stand-in for requests.Session that replays scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.headers = {}

    def get(self, url, timeout=None):
        self.calls += 1
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("sources.simplify.time.sleep", lambda seconds: None)


def test_fetch_retries_then_succeeds():
    session = _Session([requests.ConnectionError("boom"), _Response(payload=[{"a": 1}])])
    scraper = ListingsFeedScraper(url="https://example.invalid/x.json", session=session)
    assert scraper.fetch() == [{"a": 1}]
    assert session.calls == 2


def test_fetch_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(config, "MAX_RETRIES", 3)
    session = _Session([requests.ConnectionError("boom")] * 3)
    scraper = ListingsFeedScraper(url="https://example.invalid/x.json", session=session)
    with pytest.raises(FeedError):
        scraper.fetch()
    assert session.calls == 3


def test_fetch_rejects_non_list_payload(monkeypatch):
    monkeypatch.setattr(config, "MAX_RETRIES", 1)
    session = _Session([_Response(payload={"jobs": []})])
    scraper = ListingsFeedScraper(url="https://example.invalid/x.json", session=session)
    with pytest.raises(FeedError):
        scraper.fetch()


# -- de-duplication key normalisation ----------------------------------------
#
# Sources transcribe the same title inconsistently. An exact-match key lets the
# same role through twice, and in a top-eight shortlist that costs a slot a
# different company should have had.


def test_ampersand_and_the_word_and_are_the_same_posting():
    """Observed live: ByteDance's data-lake role arrived spelled both ways."""
    a = Job(company="ByteDance", title="Data Lake Infrastructure & Data Analytics Intern")
    b = Job(company="ByteDance", title="Data Lake Infrastructure and Data Analytics Intern")
    assert a.key == b.key


def test_punctuation_differences_do_not_split_a_posting():
    a = Job(company="Acme, Inc.", title="Software Engineer Intern - Platform")
    b = Job(company="Acme Inc", title="Software Engineer Intern, Platform")
    assert a.key == b.key


def test_a_trailing_term_suffix_is_ignored():
    a = Job(company="Acme", title="Software Engineer Intern (Summer 2027)")
    b = Job(company="Acme", title="Software Engineer Intern")
    assert a.key == b.key


def test_a_trailing_requisition_id_is_ignored():
    a = Job(company="Acme", title="Software Engineer Intern (Req 12345)")
    b = Job(company="Acme", title="Software Engineer Intern")
    assert a.key == b.key


def test_genuinely_different_roles_still_differ():
    """The normalisation must not be so eager that distinct roles collide."""
    assert Job(company="Acme", title="Software Engineer Intern").key != \
           Job(company="Acme", title="Machine Learning Intern").key
    assert Job(company="Acme", title="SWE Intern").key != \
           Job(company="Globex", title="SWE Intern").key
    # A meaningful qualifier in the middle is not noise.
    assert Job(company="Acme", title="Backend Engineer Intern").key != \
           Job(company="Acme", title="Frontend Engineer Intern").key
