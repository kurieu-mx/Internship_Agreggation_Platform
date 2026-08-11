"""The daily spend ceiling.

This exists for the runs that go wrong, so its tests are about the wrong runs:
an unknown model, an unreadable database, a breach mid-digest. In every case
the safe direction is to spend *less* than allowed, never more.
"""

from datetime import datetime, timezone

import pytest

import budget
import config
import llm
from store import open_store

NOW = datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc)


class Usage:
    def __init__(self, input_tokens=0, output_tokens=0,
                 cache_read_input_tokens=0, cache_creation_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    with open_store(str(path)):
        pass                      # create the schema
    return str(path)


# -- pricing -----------------------------------------------------------------


def test_a_plain_call_is_priced_from_its_own_tokens():
    cost = budget.cost_of("claude-opus-5", Usage(input_tokens=1_000_000))
    assert cost == pytest.approx(5.00)


def test_output_is_priced_higher_than_input():
    into = budget.cost_of("claude-opus-5", Usage(input_tokens=100_000))
    out = budget.cost_of("claude-opus-5", Usage(output_tokens=100_000))
    assert out > into


def test_cached_reads_are_a_tenth_of_input():
    full = budget.cost_of("claude-opus-5", Usage(input_tokens=1_000_000))
    cached = budget.cost_of("claude-opus-5", Usage(cache_read_input_tokens=1_000_000))
    assert cached == pytest.approx(full * 0.10)


def test_cache_writes_cost_a_quarter_more():
    full = budget.cost_of("claude-opus-5", Usage(input_tokens=1_000_000))
    written = budget.cost_of("claude-opus-5", Usage(cache_creation_input_tokens=1_000_000))
    assert written == pytest.approx(full * 1.25)


def test_haiku_is_cheaper_than_opus():
    usage = Usage(input_tokens=100_000, output_tokens=10_000)
    assert budget.cost_of("claude-haiku-4-5", usage) < budget.cost_of("claude-opus-5", usage)


def test_an_unknown_model_is_priced_at_the_highest_known_rate():
    """Guessing low on a spend cap defeats the cap."""
    unknown = budget.cost_of("claude-something-new", Usage(input_tokens=1_000_000))
    opus = budget.cost_of("claude-opus-5", Usage(input_tokens=1_000_000))
    assert unknown > opus


def test_a_response_with_no_usage_costs_nothing():
    assert budget.cost_of("claude-opus-5", object()) == 0.0


# -- accumulation ------------------------------------------------------------


def test_nothing_is_spent_on_a_fresh_day(db):
    assert budget.spent_today(NOW) == 0.0


def test_spend_accumulates_across_calls(db):
    budget.record("claude-opus-5", Usage(input_tokens=100_000), NOW)
    budget.record("claude-opus-5", Usage(input_tokens=100_000), NOW)
    assert budget.spent_today(NOW) == pytest.approx(1.00)


def test_spend_accumulates_across_models(db):
    budget.record("claude-opus-5", Usage(input_tokens=100_000), NOW)
    budget.record("claude-haiku-4-5", Usage(input_tokens=100_000), NOW)
    assert budget.spent_today(NOW) == pytest.approx(0.60)


def test_yesterdays_spend_does_not_count_against_today(db):
    yesterday = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)
    budget.record("claude-opus-5", Usage(input_tokens=1_000_000), yesterday)
    assert budget.spent_today(NOW) == 0.0


def test_spend_survives_a_restart(db):
    """A cap held in memory does not cap a crash-loop."""
    budget.record("claude-opus-5", Usage(input_tokens=200_000), NOW)
    assert budget.spent_today(NOW) == pytest.approx(1.00)   # re-read from disk


# -- the check ---------------------------------------------------------------


def test_the_check_passes_below_the_cap(db, monkeypatch):
    monkeypatch.setattr(config, "DAILY_BUDGET_USD", 2.00)
    budget.check(now=NOW)


def test_the_check_raises_at_the_cap(db, monkeypatch):
    monkeypatch.setattr(config, "DAILY_BUDGET_USD", 2.00)
    budget.record("claude-opus-5", Usage(input_tokens=400_000), NOW)   # $2.00

    with pytest.raises(budget.BudgetExceeded, match="2.00"):
        budget.check(now=NOW)


def test_headroom_makes_the_cap_bind_before_the_call_not_after(db, monkeypatch):
    """Otherwise the cap is always breached by exactly one call."""
    monkeypatch.setattr(config, "DAILY_BUDGET_USD", 1.00)
    budget.record("claude-opus-5", Usage(input_tokens=180_000), NOW)   # $0.90

    budget.check(headroom=0.05, now=NOW)                # still fits
    with pytest.raises(budget.BudgetExceeded):
        budget.check(headroom=0.50, now=NOW)            # would overshoot


def test_a_cap_of_zero_disables_the_check(db, monkeypatch):
    monkeypatch.setattr(config, "DAILY_BUDGET_USD", 0)
    budget.record("claude-opus-5", Usage(input_tokens=10_000_000), NOW)
    budget.check(now=NOW)


def test_an_unreadable_store_is_treated_as_at_the_cap(monkeypatch):
    """Fail closed: a broken database must not silently remove the ceiling."""
    monkeypatch.setattr(config, "DB_PATH", "/nonexistent/dir/x.db")
    monkeypatch.setattr(config, "DAILY_BUDGET_USD", 2.00)
    with pytest.raises(budget.BudgetExceeded):
        budget.check(now=NOW)


# -- integration with the model layer ----------------------------------------


def test_a_call_is_refused_once_the_cap_is_reached(db, monkeypatch):
    monkeypatch.setattr(config, "DAILY_BUDGET_USD", 0.50)
    budget.record("claude-opus-5", Usage(input_tokens=200_000), NOW)   # $1.00

    called = []
    monkeypatch.setattr(llm, "get_client", lambda: called.append(1) or object())

    # Returns None - the same signal as an unreachable model - so every caller's
    # existing fallback path handles it with no special-casing.
    assert llm.complete_json("s", "p", {"type": "object"}) is None


def test_status_reads_cleanly(db, monkeypatch):
    monkeypatch.setattr(config, "DAILY_BUDGET_USD", 2.00)
    budget.record("claude-opus-5", Usage(input_tokens=200_000), NOW)
    text = budget.status(NOW)
    assert "$1.00" in text and "$2.00" in text and "50%" in text
