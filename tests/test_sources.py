import pytest

from models import Job
from sources import build_sources
from sources.base import collect, deduplicate


class _Source:
    def __init__(self, name, rank, jobs=None, error=None):
        self.name = name
        self.rank = rank
        self._jobs = jobs or []
        self._error = error

    def scrape(self):
        if self._error:
            raise self._error
        return list(self._jobs)


def job(company="Acme", title="SWE Intern", **kwargs):
    return Job(company=company, title=title, **kwargs)


# -- fan-out -----------------------------------------------------------------


def test_every_source_contributes():
    jobs = collect([
        _Source("a", 10, [job(title="One")]),
        _Source("b", 20, [job(title="Two"), job(title="Three")]),
    ])
    assert len(jobs) == 3


def test_a_failing_source_costs_only_its_own_postings():
    """The whole point of the fan-out: LinkedIn breaking must not stop the run."""
    jobs = collect([
        _Source("healthy", 10, [job(title="Kept")]),
        _Source("broken", 20, error=RuntimeError("upstream on fire")),
        _Source("also-healthy", 30, [job(title="Also kept")]),
    ])
    assert [j.title for j in jobs] == ["Kept", "Also kept"]


def test_every_source_failing_is_an_empty_run_not_an_exception():
    jobs = collect([
        _Source("a", 10, error=RuntimeError("boom")),
        _Source("b", 20, error=ValueError("bang")),
    ])
    assert jobs == []


def test_collect_stamps_the_source_name_and_rank():
    jobs = collect([_Source("greenhouse", 10, [job()])])
    assert jobs[0].source == "greenhouse"
    assert jobs[0].provider_rank == 10


def test_collect_does_not_overwrite_a_source_that_named_itself():
    """Simplify reports the original board ('Lever', 'Workday'); keep it."""
    jobs = collect([_Source("simplify", 50, [job(source="Workday")])])
    assert jobs[0].source == "Workday"
    assert jobs[0].provider_rank == 50


def test_no_sources_is_an_empty_run():
    assert collect([]) == []


# -- de-duplication ----------------------------------------------------------


def test_the_more_authoritative_source_wins():
    """An ATS board is the system of record; an aggregator's copy defers."""
    ats = job(url="https://boards.greenhouse.io/x", provider_rank=10,
              description="full posting text")
    feed = job(url="https://simplify.jobs/x", provider_rank=50)

    survivor = deduplicate([feed, ats])[0]
    assert survivor.url == "https://boards.greenhouse.io/x"
    assert survivor.description == "full posting text"


def test_the_winner_is_the_same_whichever_order_they_arrive_in():
    ats = job(url="ats", provider_rank=10)
    feed = job(url="feed", provider_rank=50)
    assert deduplicate([ats, feed])[0].url == "ats"
    assert deduplicate([feed, ats])[0].url == "ats"


def test_locations_from_both_copies_are_merged():
    ats = job(locations=["NYC"], provider_rank=10)
    feed = job(locations=["Chicago", "NYC"], provider_rank=50)
    assert deduplicate([ats, feed])[0].locations == ["NYC", "Chicago"]


def test_the_loser_backfills_fields_the_winner_lacks():
    """Merging must never lose data the aggregator happened to have."""
    ats = job(provider_rank=10, url="")
    feed = job(provider_rank=50, url="https://simplify.jobs/x",
               terms=["Summer 2027"], degrees=["Bachelor's"])

    survivor = deduplicate([ats, feed])[0]
    assert survivor.url == "https://simplify.jobs/x"
    assert survivor.terms == ["Summer 2027"]
    assert survivor.degrees == ["Bachelor's"]


def test_a_populated_field_is_not_overwritten_by_the_loser():
    ats = job(provider_rank=10, url="ats-url")
    feed = job(provider_rank=50, url="feed-url")
    assert deduplicate([ats, feed])[0].url == "ats-url"


def test_a_missing_timestamp_is_backfilled():
    from datetime import datetime, timezone

    stamp = datetime(2026, 8, 11, tzinfo=timezone.utc)
    ats = job(provider_rank=10, posted_at=None)
    feed = job(provider_rank=50, posted_at=stamp)
    assert deduplicate([ats, feed])[0].posted_at == stamp


def test_different_postings_are_left_alone():
    jobs = deduplicate([job(title="SWE Intern"), job(title="ML Intern")])
    assert len(jobs) == 2


# -- registry ----------------------------------------------------------------


def test_known_sources_are_built():
    built = build_sources(["simplify", "vansh"])
    assert [s.name for s in built] == ["simplify", "vansh"]


def test_an_unknown_source_is_skipped_not_fatal():
    """A config typo should cost one source, not the day's digest."""
    built = build_sources(["simplify", "nonesuch"])
    assert [s.name for s in built] == ["simplify"]


def test_an_empty_source_list_is_allowed():
    assert build_sources([]) == []


def test_every_configured_default_source_can_be_built():
    import config

    built = build_sources(config.SOURCES)
    assert len(built) == len(config.SOURCES), "a default source failed to initialise"
