from datetime import datetime, timedelta, timezone

import pytest

from models import Job
from store import Store, open_store

NOW = datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    with open_store(":memory:") as opened:
        yield opened


def job(company="Acme", title="SWE Intern", **kwargs):
    return Job(company=company, title=title, **kwargs)


def test_first_sighting_is_reported_as_new(store):
    assert store.mark_seen([job()], now=NOW) == {"acme::swe intern"}


def test_second_sighting_is_not_new(store):
    store.mark_seen([job()], now=NOW)
    assert store.mark_seen([job()], now=NOW + timedelta(hours=1)) == set()


def test_first_seen_survives_across_runs(store):
    """A posting we already knew keeps its original sighting, not this one."""
    store.mark_seen([job()], now=NOW)

    later = job()
    store.mark_seen([later], now=NOW + timedelta(days=3))

    assert later.first_seen == NOW


def test_first_seen_is_now_for_a_brand_new_posting(store):
    fresh = job()
    store.mark_seen([fresh], now=NOW)
    assert fresh.first_seen == NOW


def test_mark_seen_tolerates_an_empty_batch(store):
    assert store.mark_seen([], now=NOW) == set()


def test_mark_seen_handles_more_than_one_query_chunk(store):
    """The IN-clause chunking must not drop postings past the chunk boundary."""
    batch = [job(title=f"Intern {n}") for n in range(1200)]
    assert len(store.mark_seen(batch, now=NOW)) == 1200
    assert store.mark_seen(batch, now=NOW) == set()


def test_backfills_a_url_that_arrives_later(store):
    store.mark_seen([job()], now=NOW)
    store.mark_seen([job(url="https://example.com/apply")], now=NOW)

    row = store.db.execute("SELECT url FROM seen").fetchone()
    assert row["url"] == "https://example.com/apply"


def test_a_later_empty_url_does_not_erase_a_known_one(store):
    store.mark_seen([job(url="https://example.com/apply")], now=NOW)
    store.mark_seen([job()], now=NOW)

    row = store.db.execute("SELECT url FROM seen").fetchone()
    assert row["url"] == "https://example.com/apply"


def test_nothing_is_sent_until_it_is_recorded(store):
    assert store.already_sent(["acme::swe intern"]) == set()


def test_recorded_sends_are_remembered(store):
    store.record_sent(["acme::swe intern"], digest_id="2026-08-11", now=NOW)
    assert store.already_sent(["acme::swe intern", "other::role"]) == {"acme::swe intern"}


def test_recording_the_same_send_twice_is_harmless(store):
    store.record_sent(["k"], digest_id="a", now=NOW)
    store.record_sent(["k"], digest_id="b", now=NOW)
    assert store.db.execute("SELECT COUNT(*) c FROM sent").fetchone()["c"] == 1


def test_already_sent_tolerates_an_empty_batch(store):
    assert store.already_sent([]) == set()


def test_runs_are_logged_with_their_outcome(store):
    run_id = store.start_run(now=NOW)
    store.finish_run(run_id, "ok", counts={"sent": 3}, now=NOW)

    run = store.recent_runs()[0]
    assert run["status"] == "ok"
    assert '"sent": 3' in run["counts"]
    assert run["error"] is None


def test_a_failed_run_keeps_its_error(store):
    run_id = store.start_run(now=NOW)
    store.finish_run(run_id, "failed", error="feed unreachable", now=NOW)
    assert store.recent_runs()[0]["error"] == "feed unreachable"
