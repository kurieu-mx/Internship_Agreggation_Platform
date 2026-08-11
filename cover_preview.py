"""Build one cover letter, for iterating on the design without a full run.

A full digest is ~25 model calls, three minutes, and most of a dollar. Tuning a
template against that loop is miserable, so this rebuilds a single posting from
the seen-store and renders just its letter.

    python main.py --cover-preview deloitte
    python main.py --cover-preview deloitte --no-research   # free, layout only

``--no-research`` skips the Composio fetch and the model call entirely and
renders with canned text, which is what you want when the thing you are
changing is CSS.
"""

import logging
from pathlib import Path
from typing import Optional

import config
from models import Job
from store import open_store

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent

# Enough structure to exercise every section of the layout, so a CSS change can
# be judged without spending a model call on prose.
_PLACEHOLDER = {
    "hook": "Your posting asks for someone who can take a model from a notebook "
            "to something that survives contact with production traffic. That is "
            "the arc of the last two internships I have done.",
    "why_company": "PLACEHOLDER — the researched paragraph goes here, tying a "
                   "verified fact about the company to the specific work in this "
                   "posting. Run without --no-research to generate the real one.",
    "what_i_bring": [
        {"title": "Production ML pipelines",
         "detail": "Built an edge perception pipeline on Raspberry Pi CM4 with "
                   "YOLO/ONNX and OpenCV, geo-projecting detections into GPS "
                   "coordinates fused with live flight telemetry."},
        {"title": "Distributed systems in Go",
         "detail": "Designed collision avoidance for an 8-drone swarm across "
                   "ROS 2, PX4 and ORCA, then debugged the leader-election duel "
                   "loops and state-machine races it surfaced."},
        {"title": "Data pipelines that hold up",
         "detail": "Processed 14,800 community-sourced postings into ~500 "
                   "validated records in under two seconds, with 71 tests and CI "
                   "across three Python versions."},
    ],
    "selected_work": [
        {"ref": "adhoc-lm", "name": "AdHoc-LM",
         "detail": "A 7.5M-parameter decoder-only transformer written from "
                   "scratch in PyTorch — BPE tokenisation, KV caching, "
                   "mixed-precision distributed training."},
        {"ref": "internship-platform", "name": "Internship Aggregation Platform",
         "detail": "An ingestion pipeline over 14,800 postings with "
                   "normalisation, dedup and multi-format export."},
    ],
    "closing": "Happy to talk through any of the above, or to send code.",
}


def find_job(needle: str) -> Optional[Job]:
    """Rebuild a Job from the seen-store by company substring, newest first."""
    with open_store(config.DB_PATH) as store:
        row = store.db.execute(
            """
            SELECT company, title, url, source, posted_at FROM seen
            WHERE lower(company) LIKE ?
            ORDER BY COALESCE(posted_at, first_seen) DESC LIMIT 1
            """,
            (f"%{needle.lower()}%",),
        ).fetchone()

    if row is None:
        return None

    from store import _parse

    return Job(
        company=row["company"], title=row["title"], url=row["url"] or "",
        source=row["source"] or "", posted_at=_parse(row["posted_at"]),
        terms=[config.TERM_FILTER] if config.TERM_FILTER else [],
        field_category="AI / ML / Data",
    )


def run(needle: str, no_research: bool = False,
        out: Optional[str] = None) -> int:
    from tailor.branding import for_company
    from tailor.cover import render_cover, research, write
    from tailor.render import load_profile

    job = find_job(needle)
    if job is None:
        print(f"\n  no posting matching {needle!r} in the store")
        print("  -> run `make preview` or a digest first so the store has something")
        return 1

    print(f"\n  {job.company} — {job.title}")
    print(f"  {job.url or 'no url'}")

    brand = for_company(job.company, job.url)
    print(f"  branding: domain={brand.domain} accent={brand.accent} "
          f"secondary={brand.secondary} logo={'yes' if brand.logo else 'none found'}")

    profile = load_profile()

    if no_research:
        letter = dict(_PLACEHOLDER)
        print("  letter:   placeholder (--no-research)")
    else:
        facts = research(job)
        print(f"  research: {len(facts)} verified fact(s)")
        for fact in facts:
            print(f"              - {fact[:96]}")
        letter = write(job, profile, facts)
        if not letter:
            print("\n  the model produced no letter")
            return 1

    destination = Path(out) if out else ROOT / "out" / "preview" / \
        f"Cover_{job.company.replace(' ', '_')}.pdf"

    try:
        pdf = render_cover(job, profile, letter, destination)
    except Exception as exc:
        print(f"\n  render failed: {type(exc).__name__}: {exc}")
        return 1

    print(f"\n  wrote {pdf}")
    print(f"  open with: xdg-open '{pdf}'")
    return 0
