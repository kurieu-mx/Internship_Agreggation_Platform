#!/usr/bin/env python3
"""Command-line entry point.

Runs with no credentials by default: it fetches the feed and prints a table,
so the project can be cloned and exercised in one command. Google Sheets and
LLM enrichment are opt-in flags.
"""

import argparse
import csv
import logging
import sys
import time
from typing import List

import config
from enrichment import Enricher
from models import Job
from scrapers import FeedError, ListingsFeedScraper, deduplicate
from sheets import summarize

log = logging.getLogger("internship-scraper")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="internship-scraper",
        description="Collect US tech internship postings into a structured dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python main.py --limit 20                 preview 20 postings\n"
            "  python main.py --format csv --out jobs.csv    write a CSV\n"
            "  python main.py --enrich --format csv      refine roles with a local LLM\n"
            "  python main.py --sheets                   append to Google Sheets\n"
        ),
    )
    parser.add_argument("--limit", type=int, help="only output the first N postings")
    parser.add_argument(
        "--term",
        default=config.TERM_FILTER,
        help=f"term to keep, substring match (default: {config.TERM_FILTER!r}; "
             f"pass '' for all terms)",
    )
    parser.add_argument("--category", help="keep only this field category (substring match)")
    parser.add_argument(
        "--format",
        choices=["table", "csv", "json"],
        default="table",
        help="output format (default: table)",
    )
    parser.add_argument("--out", help="write output to this file instead of stdout")
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="refine role categories with a local Ollama model (skipped if unreachable)",
    )
    parser.add_argument(
        "--sheets", action="store_true", help="append results to the configured Google Sheet"
    )
    parser.add_argument(
        "--schedule",
        metavar="HH:MM",
        help="run once now, then daily at this time (24h clock)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def collect(args) -> List[Job]:
    """Fetch, filter and de-duplicate; enrich if requested."""
    config.TERM_FILTER = args.term if args.term is not None else config.TERM_FILTER

    jobs = deduplicate(ListingsFeedScraper().scrape())

    if args.category:
        needle = args.category.lower()
        jobs = [j for j in jobs if needle in j.field_category.lower()]

    jobs.sort(key=lambda j: (j.posted_at is None, -(j.posted_at.timestamp() if j.posted_at else 0)))

    if args.limit:
        jobs = jobs[: args.limit]

    if args.enrich:
        Enricher().enrich(jobs)

    return jobs


# --- output ---------------------------------------------------------------


def write_table(jobs: List[Job], stream) -> None:
    if not jobs:
        stream.write("No postings matched.\n")
        return

    columns = [
        ("Company", lambda j: j.company, 24),
        ("Position", lambda j: j.title, 46),
        ("Location", lambda j: ", ".join(j.locations), 26),
        ("Field", lambda j: j.field_category, 20),
        ("Sponsorship", lambda j: j.sponsorship, 16),
    ]

    header = "  ".join(name.ljust(width) for name, _, width in columns)
    stream.write(header.rstrip() + "\n")
    stream.write("-" * len(header) + "\n")

    for job in jobs:
        cells = []
        for _, getter, width in columns:
            value = getter(job) or ""
            if len(value) > width:
                value = value[: width - 1] + "…"
            cells.append(value.ljust(width))
        stream.write("  ".join(cells).rstrip() + "\n")

    stream.write(f"\n{len(jobs)} postings\n")
    for category, count in summarize(jobs).items():
        stream.write(f"  {category}: {count}\n")


def write_csv(jobs: List[Job], stream) -> None:
    writer = csv.writer(stream)
    writer.writerow(config.COLUMN_HEADERS)
    writer.writerows(job.to_row() for job in jobs)


def write_json(jobs: List[Job], stream) -> None:
    import json

    json.dump([job.to_dict() for job in jobs], stream, indent=2)
    stream.write("\n")


WRITERS = {"table": write_table, "csv": write_csv, "json": write_json}


def emit(jobs: List[Job], args) -> None:
    writer = WRITERS[args.format]
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as handle:
            writer(jobs, handle)
        log.info("wrote %d postings to %s", len(jobs), args.out)
    else:
        writer(jobs, sys.stdout)


def run_once(args) -> int:
    try:
        jobs = collect(args)
    except FeedError as exc:
        log.error("%s", exc)
        return 1

    emit(jobs, args)

    if args.sheets:
        from sheets import SheetsError, SheetsWriter

        try:
            sink = SheetsWriter().connect()
            added = sink.append(jobs)
            log.info("added %d rows - %s", added, sink.url)
        except SheetsError as exc:
            log.error("Google Sheets: %s", exc)
            return 1

    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    status = run_once(args)
    if not args.schedule:
        return status

    try:
        import schedule
    except ImportError:
        log.error("--schedule requires the 'schedule' package (pip install schedule)")
        return 1

    log.info("scheduled daily at %s; Ctrl-C to stop", args.schedule)
    schedule.every().day.at(args.schedule).do(run_once, args)
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
