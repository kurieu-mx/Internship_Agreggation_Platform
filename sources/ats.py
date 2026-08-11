"""Shared machinery for the direct-ATS adapters.

Why poll ATS boards at all, when the community feeds already aggregate them?
Latency and fidelity. A posting reaches Greenhouse the moment a recruiter hits
publish; it reaches an aggregator when a contributor notices and opens a PR.
For a digest whose whole premise is "posted in the last 24 hours", that gap is
the product. The boards also carry the full description, which the feeds do
not, and which the scoring step is much better with.

The cost is that these boards return *every* job a company has open, with no
notion of an academic term. So this module carries the two judgement calls the
feeds made for us:

* :func:`looks_like_internship` - is this an internship at all?
* :func:`infer_terms` - which academic term is it for?

Both are deliberately conservative, and :func:`infer_terms` reports when it is
guessing rather than reading, so downstream code can tell the difference.
"""

import html
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, NamedTuple, Optional

import requests

import config
from sources.base import FeedError

log = logging.getLogger(__name__)

# "Intern" alone is too loose - it matches "Internal Audit Manager" and
# "International Sales". Require a word boundary and one of the real forms.
_INTERN_RE = re.compile(
    r"\b(intern|internship|co-?op|industrial placement"
    # Finance names its internships differently: "Summer Analyst" is the
    # undergraduate one and "Summer Associate" the graduate one. Both are
    # internships, and excluding them would quietly drop most bank and trading
    # postings - which is a large share of what this pipeline is looking for.
    r"|summer (analyst|associate|scholar)"
    r"|(spring|winter|fall) (analyst|associate))\b",
    re.I,
)

# Things that read as internships but are not the thing we want. Two groups:
# words that merely *contain* "intern" (internal, international), and the
# roles on the other side of the table - someone whose job is to recruit or
# run an internship programme is not an intern, however the title is ordered.
_NOT_INTERN_RE = re.compile(
    r"\b(internal|international"
    r"|recruiter|recruiting|talent acquisition|university relations"
    r"|intern(ship)?\s+(program\s+)?(manager|coordinator|lead)"
    r"|returning intern|full[- ]time|new ?grad(uate)?"
    # Rotational graduate schemes read like internships and are not: they are
    # entry-level permanent hires. "Investment Analyst Program", "Technology
    # Development Program" - all seen leaking in from the feeds.
    r"|(analyst|associate|leadership|development|rotational)\s+program"
    r"|senior|staff|principal|director|head of|vice president|\bvp\b)\b",
    re.I,
)

_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_SEASON_RE = re.compile(r"\b(summer|fall|autumn|winter|spring)\b", re.I)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKS_RE = re.compile(r"\n{3,}")


def strip_html(text: Optional[str]) -> str:
    """Turn a board's HTML description into readable plain text.

    Greenhouse double-escapes its ``content`` field, so unescape first, then
    drop tags. Block-level tags become newlines so the text keeps the shape a
    model can read rather than collapsing into one paragraph.
    """
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _BLANKS_RE.sub("\n\n", text)
    return text.strip()


def looks_like_internship(title: str, employment_type: str = "",
                          commitment: str = "") -> bool:
    """Best-effort check that a posting is a student internship.

    Pass only genuine employment-type fields as hints - Ashby's
    ``employmentType``, Lever's ``categories.commitment``. A department or team
    name is **not** one of those and must not be passed here: "Internal Audit"
    is a department, and treating it as a hint admits every auditor on the
    board as an intern.

    The title is checked for disqualifiers first either way, so a hint can
    never override a title that plainly says "Senior".
    """
    if title and _NOT_INTERN_RE.search(title):
        return False

    # Word-boundary, not substring: "Internal" contains "intern".
    for hint in (employment_type, commitment):
        if hint and _INTERN_RE.search(hint) and not _NOT_INTERN_RE.search(hint):
            return True

    if not title:
        return False
    return bool(_INTERN_RE.search(title))


def infer_terms(title: str, description: str = "",
                posted_at: Optional[datetime] = None,
                now: Optional[datetime] = None) -> tuple:
    """Work out which academic term a posting is for.

    Returns ``(terms, inferred)``. ``inferred`` is True when we had to reason
    from the posting date rather than read a year off the text - callers should
    treat those as candidates, not facts.

    The **title** is authoritative. Only if it names no year do we look at the
    description, and then only its first paragraphs. This ordering matters:
    posting bodies routinely mention adjacent years for unrelated reasons -
    graduation windows ("graduating in 2028"), program history, start dates -
    and letting those outvote a title that plainly says "Summer 2027" turns one
    unambiguous term into a scattershot list.

    Absent any year at all, an internship published in the second half of a
    calendar year is recruiting for the *following* summer, the dominant
    pattern in US tech: 2027 postings start appearing around July 2026.
    """

    def _read(text: str):
        years = {int(y) for y in _YEAR_RE.findall(text) if 2020 <= int(y) <= 2035}
        season_match = _SEASON_RE.search(text)
        season = season_match.group(1).lower() if season_match else "summer"
        return years, ("fall" if season == "autumn" else season)

    title_years, title_season = _read(title)
    if title_years:
        return ([f"{title_season.title()} {year}" for year in sorted(title_years)], False)

    body_years, body_season = _read(description[:2000])
    if body_years:
        season = title_season if _SEASON_RE.search(title) else body_season
        return ([f"{season.title()} {year}" for year in sorted(body_years)], False)

    reference = posted_at or now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    # Published July or later -> recruiting for next summer.
    year = reference.year + 1 if reference.month >= 7 else reference.year
    return ([f"Summer {year}"], True)


# Title keywords -> the same category vocabulary normalize.py produces for the
# community feeds, so both kinds of source land in one namespace. Ordered:
# the first match wins, so the more specific patterns come first.
_CATEGORY_RULES = [
    ("Quant", r"\bquant(itative)?\b|\btrading\b|\btrader\b|\bmarket maker\b"),
    ("AI / ML / Data", r"\b(machine learning|ml|ai|deep learning|nlp|computer vision"
                       r"|research scientist|data scien|data engineer|data analyst"
                       r"|applied scien)\b"),
    ("Hardware", r"\b(hardware|electrical|firmware|asic|fpga|silicon|mechanical|rf)\b"),
    ("Product", r"\bproduct manage|\bpm intern|\bproduct design"),
    ("Software Engineering", r"\b(software|swe|backend|back-end|frontend|front-end"
                             r"|full[- ]?stack|infrastructure|platform|systems|devops"
                             r"|site reliability|security|mobile|ios|android|web)\b"),
]
_COMPILED_CATEGORY_RULES = [(label, re.compile(pattern, re.I))
                            for label, pattern in _CATEGORY_RULES]


def categorize_title(title: str, department: str = "") -> str:
    """Map an ATS posting onto the feed's category vocabulary.

    The boards have no shared taxonomy - one company's ``department`` is
    "Engineering", another's is "Trading Technology" - so the title carries
    most of the signal, with the department as a tiebreak. Anything we cannot
    place is "Other", which the target-category filter then drops; that is the
    right failure direction for a digest you actually want to read.
    """
    haystack = f"{title} {department}"
    for label, pattern in _COMPILED_CATEGORY_RULES:
        if pattern.search(haystack):
            return label
    return "Other"


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, tolerating a trailing Z and junk."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_epoch_millis(value: Any) -> Optional[datetime]:
    """Lever reports timestamps in milliseconds, unlike everyone else."""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


class Board(NamedTuple):
    """One company's job board on one ATS."""

    token: str
    name: str  # display name, for when the board's payload omits one


def _display_name(token: str) -> str:
    """A readable fallback for boards that do not name themselves.

    ``janestreet`` -> ``Janestreet``, ``applied-intuition`` -> ``Applied
    Intuition``. Imperfect for compound lowercase tokens, which is exactly why
    ``companies.yml`` lets you override it.
    """
    return re.sub(r"[-_]+", " ", token).strip().title()


def load_boards(kind: str, path: Optional[str] = None) -> List[Board]:
    """Read the board tokens for one ATS out of ``companies.yml``.

    Accepts either form under each ATS key, so you can start with a bare list
    and add display names only where the derived one reads badly::

        greenhouse:
          - janestreet                  # name derived
          - token: hudson-river-trading
            name: Hudson River Trading  # name given

    A missing or malformed file means "no boards configured for this ATS",
    logged and skipped - never a crashed run.
    """
    import yaml

    location = Path(path or config.COMPANIES_FILE)
    if not location.is_absolute():
        location = Path(__file__).resolve().parent.parent / location

    if not location.exists():
        log.warning("%s not found - no %s boards configured", location, kind)
        return []

    try:
        data = yaml.safe_load(location.read_text()) or {}
    except yaml.YAMLError as exc:
        log.warning("could not parse %s: %s", location, exc)
        return []

    entries = data.get(kind) or []
    if not isinstance(entries, list):
        log.warning("%s: expected a list under %r, got %s",
                    location, kind, type(entries).__name__)
        return []

    boards: List[Board] = []
    for entry in entries:
        if isinstance(entry, dict):
            token = str(entry.get("token") or "").strip()
            name = str(entry.get("name") or "").strip()
        else:
            token, name = str(entry).strip(), ""
        if not token:
            continue
        boards.append(Board(token=token, name=name or _display_name(token)))

    return boards


class AtsSource:
    """Base for the per-board ATS adapters.

    Each board is fetched independently: one company's board being renamed,
    rate-limited, or deleted drops that company from the run, not the source.
    A 404 in particular is expected in normal operation - board tokens change
    when companies rebrand - so it is logged at debug and moved past.
    """

    name = "ats"
    rank = 10
    kind = ""

    def __init__(self, boards: Optional[List[Board]] = None,
                 session: Optional[requests.Session] = None):
        self.boards = boards if boards is not None else load_boards(self.kind)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})

    def board_url(self, board: Board) -> str:
        raise NotImplementedError

    def parse_board(self, board: Board, payload: Any) -> List:
        raise NotImplementedError

    def fetch_json(self, url: str) -> Any:
        """GET with the same retry/backoff policy as the feed scraper."""
        last_error = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                response = self.session.get(url, timeout=config.REQUEST_TIMEOUT)
                if response.status_code == 404:
                    raise FeedError("board not found (404)")
                response.raise_for_status()
                return response.json()
            except FeedError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < config.MAX_RETRIES:
                    time.sleep(config.RETRY_BACKOFF * attempt)
        raise FeedError(f"could not fetch {url}: {last_error}") from last_error

    def scrape(self) -> List:
        if not self.boards:
            log.info("%s: no boards configured", self.name)
            return []

        jobs: List = []
        failures = 0
        for board in self.boards:
            try:
                payload = self.fetch_json(self.board_url(board))
                jobs.extend(self.parse_board(board, payload))
            except FeedError as exc:
                failures += 1
                log.debug("%s board %s unavailable: %s", self.name, board.token, exc)
            except Exception as exc:  # unexpected shape change
                failures += 1
                log.warning("%s board %s failed to parse: %s",
                            self.name, board.token, exc)

        log.info("%s: %d postings from %d/%d boards",
                 self.name, len(jobs), len(self.boards) - failures, len(self.boards))
        return jobs
