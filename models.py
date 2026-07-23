"""The single record type that flows through the pipeline."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config


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

    @property
    def key(self) -> str:
        """Stable identity for de-duplication across sources.

        Company + title only; the same posting often appears with different
        location lists or URLs depending on which contributor added it.
        """
        return f"{self.company.strip().lower()}::{self.title.strip().lower()}"

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
