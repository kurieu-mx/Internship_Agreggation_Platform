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
from sheets import summarize
from sources import FeedError, build_sources, collect, deduplicate

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
        "--sources",
        help="comma-separated sources to run (default: %s)" % ",".join(config.SOURCES),
    )
    parser.add_argument(
        "--check-boards",
        action="store_true",
        help="verify every board token in companies.yml still resolves, then exit",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="report what is configured, what is missing, and how to fix it",
    )
    parser.add_argument(
        "--check-handshake",
        action="store_true",
        help="probe Handshake with your session cookie and report what came back",
    )
    parser.add_argument(
        "--import-cookie",
        metavar="FILE",
        help="extract a session cookie from a DevTools 'Copy as cURL' dump "
             "and write it to .env",
    )
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
        "--digest", action="store_true",
        help="run the full daily pipeline: score, tailor, render and send",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="with --digest: build everything and print it, but send nothing",
    )
    parser.add_argument("--window", type=int, metavar="HOURS",
                        help="with --digest: how far back a posting may have been posted")
    parser.add_argument("--top", type=int, metavar="N",
                        help="with --digest: how many postings get tailored documents")
    parser.add_argument("--to", metavar="EMAIL", help="with --digest: override the recipient")
    parser.add_argument("--skip-cover", action="store_true",
                        help="with --digest: skip cover letters (faster, cheaper)")
    parser.add_argument(
        "--apply-url", metavar="URL",
        help="tailor and email one posting by link, for employers no source "
             "reaches (IBM, Amazon, Google, Apple, Meta, Microsoft)")
    parser.add_argument("--cover-preview", metavar="COMPANY",
                        help="build one cover letter from a stored posting, for "
                             "iterating on the design without a full run")
    parser.add_argument("--no-research", action="store_true",
                        help="with --cover-preview: placeholder text, no API calls")
    parser.add_argument(
        "--schedule",
        metavar="HH:MM",
        help="run once now, then daily at this time (24h clock)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def gather(args) -> List[Job]:
    """Fetch from every configured source, filter and de-duplicate."""
    config.TERM_FILTER = args.term if args.term is not None else config.TERM_FILTER

    names = args.sources.split(",") if args.sources else config.SOURCES
    jobs = deduplicate(collect(build_sources([n.strip() for n in names if n.strip()])))

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


def check_boards() -> int:
    """Verify every token in companies.yml still resolves.

    Board tokens rot: companies rebrand, migrate ATS, or take their board
    private, and a dead token is silent at runtime by design. This makes the
    rot visible on demand rather than letting the file quietly decay.
    """
    from sources.ats import load_boards
    from sources.ashby import AshbySource
    from sources.greenhouse import GreenhouseSource
    from sources.lever import LeverSource

    sources = {
        "greenhouse": GreenhouseSource,
        "lever": LeverSource,
        "ashby": AshbySource,
    }

    dead = 0
    for kind, factory in sources.items():
        boards = load_boards(kind)
        source = factory(boards=[])
        print(f"\n{kind} ({len(boards)} boards)")
        for board in boards:
            try:
                payload = source.fetch_json(source.board_url(board))
                found = source.parse_board(board, payload)
                print(f"  ok    {board.token:28s} {len(found):3d} matching postings")
            except Exception as exc:
                dead += 1
                print(f"  DEAD  {board.token:28s} {type(exc).__name__}: {exc}")

    print(f"\n{dead} unreachable board(s).")
    return 1 if dead else 0


def run_once(args) -> int:
    try:
        jobs = gather(args)
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
    # These libraries log every HTTP round trip at INFO. A digest makes ~60 of
    # them, which buries the lines that actually say what the run decided.
    if not args.verbose:
        for noisy in ("httpx", "httpcore", "composio", "weasyprint",
                      "fontTools", "urllib3", "anthropic"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.doctor:
        import doctor

        return doctor.run()

    if args.apply_url:
        import apply_url

        return apply_url.run(args.apply_url, dry_run=args.dry_run,
                             to=args.to, skip_cover=args.skip_cover)

    if args.cover_preview:
        import cover_preview

        return cover_preview.run(args.cover_preview, no_research=args.no_research)

    if args.digest:
        import digest

        return digest.run(window_hours=args.window, top_n=args.top,
                          dry_run=args.dry_run, to=args.to,
                          skip_cover=args.skip_cover)

    if args.import_cookie:
        import import_cookie

        return import_cookie.run(args.import_cookie)

    if args.check_handshake:
        from sources.handshake import HandshakeSource

        return HandshakeSource().diagnose()

    if args.check_boards:
        return check_boards()

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
