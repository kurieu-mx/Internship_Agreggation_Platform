"""Durable memory for the digest: what we have seen, and what we have sent.

Two jobs, both of which the pipeline is wrong without:

**Freshness for sources that publish no timestamp.** The community feeds and
the ATS boards all carry a real posting date, so "posted in the last 24 hours"
is an exact question there. Search results and logged-in boards frequently do
not. For those, the first time we ever saw a posting is the best available
proxy - but only if it is recorded somewhere that outlives the process.

**Idempotency.** A scheduled job that can fire twice - a retry, a manual
``workflow_dispatch``, a DST edge - must not email the same posting twice. The
sent-set makes a re-run a no-op rather than a duplicate.

SQLite because it is in the standard library, needs no server, and the whole
database is one file that can be committed as a CI artifact or thrown away to
force a clean re-send.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set

import config
from models import Job

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    key         TEXT PRIMARY KEY,
    external_id TEXT,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT,
    source      TEXT,
    posted_at   TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sent (
    key       TEXT PRIMARY KEY,
    digest_id TEXT NOT NULL,
    sent_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    counts      TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS spend (
    day          TEXT NOT NULL,
    model        TEXT NOT NULL,
    calls        INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read   INTEGER NOT NULL DEFAULT 0,
    cache_write  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model)
);

CREATE INDEX IF NOT EXISTS seen_first_seen ON seen (first_seen);
"""


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Store:
    """Thin SQLite wrapper. Open it with :func:`open_store`."""

    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # -- seen ---------------------------------------------------------------

    def mark_seen(self, jobs: Iterable[Job], now: Optional[datetime] = None) -> Set[str]:
        """Record every posting, and report which ones we had never seen.

        Sets ``job.first_seen`` on each job as a side effect - for a posting we
        already knew about that is its *original* sighting, not this one, which
        is exactly what the freshness check needs.

        Returns the set of keys seen for the first time.
        """
        now = now or datetime.now(timezone.utc)
        stamp = _iso(now)
        jobs = list(jobs)
        if not jobs:
            return set()

        known: Dict[str, Optional[datetime]] = {}
        keys = [job.key for job in jobs]
        for chunk_start in range(0, len(keys), 500):
            chunk = keys[chunk_start:chunk_start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self.db.execute(
                f"SELECT key, first_seen FROM seen WHERE key IN ({placeholders})", chunk
            ).fetchall()
            for row in rows:
                known[row["key"]] = _parse(row["first_seen"])

        new_keys = {job.key for job in jobs if job.key not in known}

        for job in jobs:
            job.first_seen = known.get(job.key) or now

        self.db.executemany(
            """
            INSERT INTO seen (key, external_id, company, title, url, source,
                              posted_at, first_seen, last_seen)
            VALUES (:key, :external_id, :company, :title, :url, :source,
                    :posted_at, :first_seen, :last_seen)
            ON CONFLICT(key) DO UPDATE SET
                last_seen   = excluded.last_seen,
                url         = COALESCE(NULLIF(excluded.url, ''), seen.url),
                posted_at   = COALESCE(excluded.posted_at, seen.posted_at),
                external_id = COALESCE(NULLIF(excluded.external_id, ''), seen.external_id)
            """,
            [
                {
                    "key": job.key,
                    "external_id": job.external_id,
                    "company": job.company,
                    "title": job.title,
                    "url": job.url,
                    "source": job.source,
                    "posted_at": _iso(job.posted_at),
                    "first_seen": _iso(job.first_seen),
                    "last_seen": stamp,
                }
                for job in jobs
            ],
        )
        self.db.commit()

        log.info("%d/%d postings are new to the store", len(new_keys), len(jobs))
        return new_keys

    # -- sent ---------------------------------------------------------------

    def already_sent(self, keys: Iterable[str]) -> Set[str]:
        """Of the given keys, which have already gone out in some digest."""
        keys = list(keys)
        if not keys:
            return set()
        found: Set[str] = set()
        for chunk_start in range(0, len(keys), 500):
            chunk = keys[chunk_start:chunk_start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self.db.execute(
                f"SELECT key FROM sent WHERE key IN ({placeholders})", chunk
            ).fetchall()
            found.update(row["key"] for row in rows)
        return found

    def record_sent(self, keys: Iterable[str], digest_id: str,
                    now: Optional[datetime] = None) -> None:
        """Mark postings as delivered. Call this only after the send succeeds."""
        stamp = _iso(now or datetime.now(timezone.utc))
        self.db.executemany(
            "INSERT OR IGNORE INTO sent (key, digest_id, sent_at) VALUES (?, ?, ?)",
            [(key, digest_id, stamp) for key in keys],
        )
        self.db.commit()

    # -- spend --------------------------------------------------------------

    def record_spend(self, day: str, model: str, usage, cost: float) -> None:
        """Accumulate one response's tokens and cost against a day.

        Written immediately rather than batched at the end of a run: the
        failure this guards against is a process that never reaches its end.
        """
        def _get(name: str) -> int:
            return int(getattr(usage, name, 0) or 0)

        self.db.execute(
            """
            INSERT INTO spend (day, model, calls, input_tokens, cache_read,
                               cache_write, output_tokens, cost_usd)
            VALUES (?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(day, model) DO UPDATE SET
                calls         = spend.calls + 1,
                input_tokens  = spend.input_tokens + excluded.input_tokens,
                cache_read    = spend.cache_read + excluded.cache_read,
                cache_write   = spend.cache_write + excluded.cache_write,
                output_tokens = spend.output_tokens + excluded.output_tokens,
                cost_usd      = spend.cost_usd + excluded.cost_usd
            """,
            (day, model, _get("input_tokens"), _get("cache_read_input_tokens"),
             _get("cache_creation_input_tokens"), _get("output_tokens"), cost),
        )
        self.db.commit()

    def spend_by_day(self, limit: int = 14) -> List[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT day, SUM(calls) AS calls, SUM(cost_usd) AS cost_usd
            FROM spend GROUP BY day ORDER BY day DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    # -- runs ---------------------------------------------------------------

    def start_run(self, now: Optional[datetime] = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
            (_iso(now or datetime.now(timezone.utc)),),
        )
        self.db.commit()
        return cursor.lastrowid

    def finish_run(self, run_id: int, status: str, counts: Optional[dict] = None,
                   error: Optional[str] = None,
                   now: Optional[datetime] = None) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at = ?, status = ?, counts = ?, error = ? WHERE id = ?",
            (
                _iso(now or datetime.now(timezone.utc)),
                status,
                json.dumps(counts or {}, sort_keys=True),
                error,
                run_id,
            ),
        )
        self.db.commit()

    def recent_runs(self, limit: int = 10) -> List[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


@contextmanager
def open_store(path: Optional[str] = None):
    """Open the store at ``path`` (``:memory:`` works, and tests use it)."""
    connection = sqlite3.connect(path or config.DB_PATH)
    try:
        yield Store(connection)
    finally:
        connection.close()
