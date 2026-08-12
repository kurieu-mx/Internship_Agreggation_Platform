"""The single record type that flows through the pipeline."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config

# Trailing noise sources append to titles: requisition ids, office codes, and
# term suffixes that are already captured in `terms`.
_TITLE_NOISE_RE = re.compile(
    r"\s*[\(\[\-–—|]\s*(?:req(?:uisition)?\s*#?\s*\w+|20\d{2}|summer\s+20\d{2}"
    r"|fall\s+20\d{2}|spring\s+20\d{2})\s*[\)\]]?\s*$",
    re.I,
)


# A bare trailing year, with no bracket or dash introducing it. Workday
# tenants write "Supply Chain Data Analyst Internship 2027" where the
# community feeds write "Supply Chain Data Analyst Intern", so without this
# the same posting arrives twice and takes two shortlist slots. The season is
# deliberately left alone: "Intern Summer 2027" and "Intern Fall 2027" are
# genuinely different postings and must not collide.
_TRAILING_YEAR_RE = re.compile(r"\s+20\d{2}\s*$")

# Corporate furniture in a legal name. Workday reports the registered entity
# ("Motorola Solutions", "Sony Group") while aggregators use the short name.
# Narrow on purpose - "Applied Materials" and "Northrop Grumman" carry no
# suffix here and must survive whole.
_COMPANY_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "group", "holdings", "plc", "sa", "ag", "nv",
    "solutions", "technologies", "international", "worldwide", "global",
}


def _dedupe_key(value: str) -> str:
    """Normalise a company or title past differences that are not differences.

    Collapses ``&``/``and``, strips punctuation and repeated whitespace, and
    removes trailing requisition or term suffixes. Conservative on purpose:
    it only removes things that carry no meaning, so two genuinely different
    roles never collide.
    """
    text = (value or "").strip().lower()
    text = _TITLE_NOISE_RE.sub("", text)
    text = _TRAILING_YEAR_RE.sub("", text)
    # "Internship" and "Intern" name the same thing, and sources pick freely
    # between them for the same req.
    text = re.sub(r"\binternships?\b", "intern", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _company_key(value: str) -> str:
    """A company name reduced to the part that identifies the employer.

    Same normalisation as a title, then trailing corporate suffixes are
    dropped. Never empties a name: a company called "Systems" keeps its name
    rather than becoming a key that matches everything.
    """
    words = _dedupe_key(value).split()
    while len(words) > 1 and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


@dataclass
class Job:
    company: str
    title: str
    locations: List[str] = field(default_factory=list)
    field_category: str = "Other"
    terms: List[str] = field(default_factory=list)
    degrees: List[str] = field(default_factory=list)
    sponsorship: str = "Unknown"
    work_mode: str = "Unknown"
    url: str = ""
    source: str = ""
    posted_at: Optional[datetime] = None
    active: bool = True

    # --- digest pipeline fields ---------------------------------------------
    # None of these appear in to_row(); the spreadsheet schema is unchanged.

    # Full posting text where the source provides it (the ATS adapters do,
    # the aggregator feeds do not). Scoring reads this when present.
    description: str = ""
    deadline: Optional[datetime] = None

    # Stable per-source identifier, used as the seen-store primary key so a
    # posting that changes its title slightly is not re-sent as new.
    external_id: str = ""

    # When this run first laid eyes on the posting. Only consulted for sources
    # that publish no timestamp of their own - see freshness.py.
    first_seen: Optional[datetime] = None

    # Dedup precedence: lower wins. ATS boards are the system of record, so
    # they outrank aggregator feeds when the same posting appears in both.
    provider_rank: int = 100

    score: float = 0.0
    score_reason: str = ""

    @property
    def key(self) -> str:
        """Stable identity for de-duplication across sources.

        Company + title only; the same posting often appears with different
        location lists or URLs depending on which contributor added it.

        Both are normalised past cosmetic differences, because sources
        transcribe the same title inconsistently and an exact match misses
        them. Observed live: ByteDance's "Data Lake Infrastructure & Data
        Analytics" and "...Infrastructure and Data Analytics" arrived as two
        postings and cost two separate slots in a top-eight shortlist.
        """
        return f"{_company_key(self.company)}::{_dedupe_key(self.title)}"

    def to_row(self) -> List[str]:
        """Render as one spreadsheet/CSV row, ordered to match COLUMN_HEADERS."""
        return [
            self.company,
            self.title,
            "; ".join(self.locations),
            self.field_category,
            "; ".join(self.terms),
            "; ".join(self.degrees),
            self.sponsorship,
            self.work_mode,
            self.posted_at.strftime("%Y-%m-%d") if self.posted_at else "",
            self.url,
            self.source,
            "Active" if self.active else "Closed",
        ]

    def to_dict(self) -> Dict[str, Any]:
        return dict(zip(config.COLUMN_HEADERS, self.to_row()))


def epoch_to_datetime(value: Any) -> Optional[datetime]:
    """Upstream timestamps are Unix seconds; tolerate junk without raising."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
