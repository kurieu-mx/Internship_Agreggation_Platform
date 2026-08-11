"""A hard daily ceiling on API spend.

A digest costs about $0.54. The reason for a cap is not that number - it is the
failure modes that would break it. A retry loop that does not terminate, a
``TOP_N`` typo, a workflow that fires repeatedly, a source that suddenly
returns ten thousand postings: any of these turns a 54-cent job into an
open-ended one, and the first you would hear of it is a bill.

So spend is metered against real token counts from each response, accumulated
in the same SQLite file as everything else, and checked **before** each call.
Once the day's total reaches the cap, further calls are refused and the
pipeline degrades exactly as it does when the model is unreachable: the
deterministic ranking stands, resumes fall back to the untailored master, and
the digest still goes out.

Persisting the total in the store rather than in memory is the point. A cap
that resets when the process restarts does not cap a crash-loop.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

import config

log = logging.getLogger(__name__)

# $ per million tokens. Cached reads bill at 10% of input, writes at 125%.
PRICES: Dict[str, Dict[str, float]] = {
    "claude-opus-5": {"in": 5.00, "out": 25.00},
    "claude-fable-5": {"in": 10.00, "out": 50.00},
    "claude-opus-4-8": {"in": 5.00, "out": 25.00},
    "claude-sonnet-5": {"in": 3.00, "out": 15.00},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
}
CACHE_READ = 0.10
CACHE_WRITE = 1.25

# Unknown model: price it as the most expensive one we know. Guessing low on a
# spend cap defeats the cap.
_FALLBACK = {"in": 10.00, "out": 50.00}


class BudgetExceeded(RuntimeError):
    """The day's spend ceiling has been reached."""


def cost_of(model: str, usage) -> float:
    """Dollar cost of one response, from its own reported token counts."""
    rates = PRICES.get(model, _FALLBACK)
    if model not in PRICES:
        log.warning("unknown model %r - pricing it at the highest known rate", model)

    def _get(name: str) -> int:
        return int(getattr(usage, name, 0) or 0)

    return (
        _get("input_tokens") * rates["in"]
        + _get("cache_read_input_tokens") * rates["in"] * CACHE_READ
        + _get("cache_creation_input_tokens") * rates["in"] * CACHE_WRITE
        + _get("output_tokens") * rates["out"]
    ) / 1_000_000


def _today(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def spent_today(now: Optional[datetime] = None) -> float:
    """What today's runs have cost so far, across every process."""
    from store import open_store

    try:
        with open_store(config.DB_PATH) as store:
            row = store.db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM spend WHERE day = ?",
                (_today(now),),
            ).fetchone()
            return float(row["total"] or 0.0)
    except Exception as exc:
        # A store that cannot be read must not silently disable the cap.
        log.warning("could not read today's spend (%s) - treating it as at the cap", exc)
        return float(config.DAILY_BUDGET_USD)


def record(model: str, usage, now: Optional[datetime] = None) -> float:
    """Meter one response. Returns what it cost."""
    from store import open_store

    cost = cost_of(model, usage)
    try:
        with open_store(config.DB_PATH) as store:
            store.record_spend(_today(now), model, usage, cost)
    except Exception as exc:
        log.warning("could not record spend (%s)", exc)
    return cost


def check(headroom: float = 0.0, now: Optional[datetime] = None) -> None:
    """Raise BudgetExceeded if another call would breach the cap.

    ``headroom`` is a rough estimate of what the next call costs, so the cap
    binds *before* the call rather than one call late. It is deliberately not
    precise - the exact cost is unknowable until the response arrives, and
    stopping slightly early is the right direction to be wrong in.
    """
    cap = config.DAILY_BUDGET_USD
    if cap <= 0:
        return
    spent = spent_today(now)
    if spent + headroom >= cap:
        raise BudgetExceeded(
            f"daily API budget reached: ${spent:.2f} of ${cap:.2f} spent today"
        )


def status(now: Optional[datetime] = None) -> str:
    cap = config.DAILY_BUDGET_USD
    spent = spent_today(now)
    if cap <= 0:
        return f"${spent:.2f} spent today (no cap set)"
    return f"${spent:.2f} of ${cap:.2f} spent today ({100 * spent / cap:.0f}%)"
