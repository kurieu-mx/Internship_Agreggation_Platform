from datetime import datetime, timedelta, timezone

from freshness import cutoff, filter_fresh, is_fresh
from models import Job

NOW = datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc)


def dated(hours_ago, **kwargs):
    return Job(
        company="Acme",
        title=f"Intern {hours_ago}h",
        posted_at=NOW - timedelta(hours=hours_ago),
        **kwargs,
    )


def undated(title="Undated Intern", **kwargs):
    return Job(company="Acme", title=title, **kwargs)


# -- dated postings: an exact comparison ------------------------------------


def test_a_posting_from_this_morning_is_fresh():
    assert is_fresh(dated(4), window_hours=24, now=NOW)


def test_a_posting_from_last_week_is_not():
    assert not is_fresh(dated(24 * 7), window_hours=24, now=NOW)


def test_the_window_edge_is_inclusive():
    assert is_fresh(dated(24), window_hours=24, now=NOW)


def test_just_past_the_edge_is_excluded():
    assert not is_fresh(dated(25), window_hours=24, now=NOW)


def test_a_naive_timestamp_is_read_as_utc():
    job = Job(company="A", title="B", posted_at=(NOW - timedelta(hours=2)).replace(tzinfo=None))
    assert is_fresh(job, window_hours=24, now=NOW)


def test_a_wider_window_admits_more():
    job = dated(48)
    assert not is_fresh(job, window_hours=24, now=NOW)
    assert is_fresh(job, window_hours=168, now=NOW)


# -- undated postings: first sighting, and only the first --------------------


def test_an_undated_posting_seen_for_the_first_time_is_fresh():
    job = undated()
    assert is_fresh(job, window_hours=24, now=NOW, new_keys={job.key})


def test_an_undated_posting_we_already_knew_is_not_fresh():
    """Otherwise a dateless source re-qualifies its whole catalogue every run."""
    job = undated()
    assert not is_fresh(job, window_hours=24, now=NOW, new_keys=set())


def test_without_the_new_key_set_an_undated_posting_falls_back_to_first_seen():
    recent = undated(first_seen=NOW - timedelta(hours=2))
    stale = undated(title="Old", first_seen=NOW - timedelta(days=5))
    assert is_fresh(recent, window_hours=24, now=NOW)
    assert not is_fresh(stale, window_hours=24, now=NOW)


def test_an_undated_posting_with_no_history_at_all_is_not_fresh():
    assert not is_fresh(undated(), window_hours=24, now=NOW)


def test_a_publication_date_beats_the_first_seen_fallback():
    """A dated posting is judged on its date even if this run just found it."""
    job = dated(48)
    assert not is_fresh(job, window_hours=24, now=NOW, new_keys={job.key})


# -- the batch filter --------------------------------------------------------


def test_filter_keeps_only_what_is_inside_the_window():
    jobs = [dated(2), dated(50), undated(), undated(title="Known")]
    kept = filter_fresh(jobs, window_hours=24, now=NOW, new_keys={undated().key})

    assert [job.title for job in kept] == ["Intern 2h", "Undated Intern"]


def test_filter_tolerates_an_empty_batch():
    assert filter_fresh([], window_hours=24, now=NOW) == []


def test_cutoff_is_the_window_before_now():
    assert cutoff(window_hours=24, now=NOW) == NOW - timedelta(hours=24)
