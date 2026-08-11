"""The daily run, end to end.

    collect -> de-duplicate -> eligibility -> freshness -> score
            -> tailor -> render -> send -> record

Two properties matter more than any individual step here.

**Nothing is recorded as sent until it is sent.** ``record_sent`` runs only
after delivery succeeds, so a crash, an expired token, or a failed render
means tomorrow's run picks the same postings up again rather than losing them
silently.

**Every step degrades rather than aborts.** A source that dies costs its own
postings; a failed tailoring pass falls back to the untailored master; a failed
cover letter still leaves a resume and an apply link; a failed send leaves a
draft. The only outcome this refuses to produce is a digest that looks complete
but isn't.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import config
from delivery.email import DigestItem, send
from eligibility import (filter_eligible, only_internships,
                         only_undergraduate_eligible)
from freshness import filter_fresh
from sources import build_sources, collect, deduplicate
from store import open_store
from tailor.cover import cover_letter
from tailor.render import load_profile
from tailor.resume import tailored_resume
from tailor.score import shortlist

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:40] or "role"


def run(window_hours: Optional[int] = None, top_n: Optional[int] = None,
        dry_run: bool = False, to: Optional[str] = None,
        out_dir: Optional[Path] = None,
        skip_cover: bool = False) -> int:
    now = datetime.now(timezone.utc)
    window_hours = window_hours if window_hours is not None else config.WINDOW_HOURS
    top_n = top_n if top_n is not None else config.TOP_N
    out_dir = Path(out_dir) if out_dir else ROOT / "out" / now.strftime("%Y-%m-%d")

    profile = load_profile()
    profile_text = (ROOT / "profile" / "profile.yml").read_text()

    import budget

    with open_store(config.DB_PATH) as store:
        run_id = store.start_run(now)
        counts = {}
        # Spend is accumulated per day, so a run's own cost is only knowable as
        # a delta across it. Captured here and closed out in every exit path,
        # otherwise "what did that run cost" is unanswerable after the fact -
        # which it was, the first time it was asked.
        spend_before = budget.spent_today(now)
        try:
            jobs = deduplicate(collect(build_sources(config.SOURCES)))
            counts["collected"] = len(jobs)

            jobs = only_internships(jobs)
            counts["internships"] = len(jobs)

            if config.UNDERGRADUATE_ONLY:
                jobs = only_undergraduate_eligible(jobs)
                counts["undergrad_eligible"] = len(jobs)

            jobs = filter_eligible(jobs)
            counts["eligible"] = len(jobs)

            new_keys = store.mark_seen(jobs, now)
            fresh = filter_fresh(jobs, window_hours, now, new_keys)

            # Never re-send a posting, even if a source re-dates it.
            already = store.already_sent([job.key for job in fresh])
            fresh = [job for job in fresh if job.key not in already]
            counts["fresh"] = len(fresh)
            if already:
                log.info("skipping %d posting(s) already sent", len(already))

            if not fresh:
                log.info("nothing new inside the %dh window", window_hours)
                send([], [], to=to, now=now, dry_run=dry_run)
                counts["cost_usd"] = round(budget.spent_today() - spend_before, 4)
                store.finish_run(run_id, "ok", counts)
                return 0

            top, also = shortlist(fresh, profile, profile_text, top_n, now)
            counts["shortlisted"] = len(top)
            counts["also_ranked"] = len(also)

            items: List[DigestItem] = []
            for job in top:
                stem = f"{_slug(job.company)}_{_slug(job.title)}"
                item = DigestItem(job)

                try:
                    resume, tailored = tailored_resume(
                        job, profile, out_dir / f"Resume_{stem}.pdf"
                    )
                    item.resume, item.tailored = resume, tailored
                except Exception as exc:
                    log.warning("no resume for %s (%s): %s",
                                job.company, type(exc).__name__, exc)

                if not skip_cover:
                    item.cover = cover_letter(job, profile, out_dir / f"Cover_{stem}.pdf")

                items.append(item)

            counts["tailored"] = sum(1 for i in items if i.tailored)
            counts["covers"] = sum(1 for i in items if i.cover)

            delivered = send(items, also, to=to, now=now, dry_run=dry_run)
            counts["sent"] = int(delivered)

            # Only now, and only if it really went out.
            if delivered:
                store.record_sent([item.job.key for item in items],
                                  digest_id=now.strftime("%Y-%m-%d"), now=now)

            counts["cost_usd"] = round(budget.spent_today() - spend_before, 4)
            store.finish_run(run_id, "ok" if delivered or dry_run else "not_sent", counts)
            log.info("run complete: %s", counts)
            log.info("this run cost $%.2f (%s)", counts["cost_usd"], budget.status())
            return 0 if (delivered or dry_run) else 1

        except Exception as exc:
            log.exception("digest failed")
            counts["cost_usd"] = round(budget.spent_today() - spend_before, 4)
            store.finish_run(run_id, "failed", counts, error=f"{type(exc).__name__}: {exc}")
            return 1
