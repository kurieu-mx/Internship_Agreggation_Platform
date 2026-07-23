"""Normalisation helpers for the messy fields in the upstream feed.

The upstream data is community-maintained, so the same concept shows up under
several spellings: category is both "Software" and "Software Engineering",
locations range from "NYC" to "Carlsbad, Ca" to "Toronto, ON, Canada". These
helpers collapse that into a small, predictable set of values.
"""

import re
from typing import Iterable, List, Optional

US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR",
}

US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "puerto rico",
}

# Bare city names the feed uses without a state.
US_CITY_ALIASES = {"nyc", "sf", "la", "bay area", "silicon valley"}

# Explicit country markers that mean "not US", checked before anything else so
# that e.g. "Ontario, CA, Canada" is not mistaken for California.
NON_US_MARKERS = {
    "canada", "uk", "united kingdom", "england", "scotland", "wales",
    "ireland", "germany", "france", "spain", "italy", "netherlands",
    "switzerland", "sweden", "norway", "denmark", "finland", "poland",
    "portugal", "belgium", "austria", "czech republic", "romania", "hungary",
    "india", "china", "japan", "singapore", "taiwan", "south korea", "korea",
    "australia", "new zealand", "israel", "mexico", "brazil", "argentina",
    "chile", "colombia", "united arab emirates", "uae", "saudi arabia",
    "turkey", "egypt", "south africa", "nigeria", "kenya", "vietnam",
    "thailand", "indonesia", "philippines", "malaysia", "hong kong",
}

CATEGORY_MAP = {
    "software": "Software Engineering",
    "software engineering": "Software Engineering",
    "ai/ml/data": "AI / ML / Data",
    "data science, ai & machine learning": "AI / ML / Data",
    "hardware": "Hardware",
    "product": "Product",
    "quant": "Quant",
    "quantitative finance": "Quant",
}

SPONSORSHIP_MAP = {
    "offers sponsorship": "Yes",
    "does not offer sponsorship": "No",
    "u.s. citizenship is required": "US citizens only",
    "other": "Unknown",
}

_REMOTE_RE = re.compile(r"\bremote\b", re.I)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)


def _tail(location: str) -> str:
    """Last comma-separated component, lowercased."""
    return location.rsplit(",", 1)[-1].strip().lower()


def is_us_location(location: str) -> bool:
    """Best-effort check that a single location string is in the US.

    Conservative: anything we cannot positively identify as US is rejected,
    which keeps foreign postings out of a database that is explicitly US-only.
    """
    if not location:
        return False

    text = location.strip()
    lowered = text.lower()

    # A non-US country name anywhere in the string is decisive.
    for marker in NON_US_MARKERS:
        if re.search(rf"(^|[,\s]){re.escape(marker)}$", lowered) or f", {marker}" in lowered:
            return False

    if "united states" in lowered or re.search(r"\bu\.?s\.?a?\b", lowered):
        return True

    tail = _tail(text)
    if tail.upper() in US_STATE_CODES:
        return True
    if tail in US_STATE_NAMES:
        return True
    if lowered in US_CITY_ALIASES:
        return True
    if _REMOTE_RE.search(lowered) and "in usa" in lowered:
        return True

    return False


def filter_us_locations(locations: Iterable[str]) -> List[str]:
    """Keep only the US entries from a posting's location list."""
    return [loc for loc in locations or [] if is_us_location(loc)]


def normalize_category(raw: Optional[str]) -> str:
    if not raw:
        return "Other"
    return CATEGORY_MAP.get(raw.strip().lower(), raw.strip())


def normalize_sponsorship(raw: Optional[str]) -> str:
    """Map the upstream sponsorship enum to Yes / No / US citizens only / Unknown.

    ~99% of postings are "Other", i.e. genuinely unknown. Those are the ones
    worth spending an LLM call on; the rest are already authoritative.
    """
    if not raw:
        return "Unknown"
    return SPONSORSHIP_MAP.get(raw.strip().lower(), "Unknown")


def infer_work_mode(locations: Iterable[str]) -> str:
    """Derive Remote / Hybrid / On-site from location strings alone.

    Cheap and deterministic; the LLM is only asked when this returns Unknown.
    """
    joined = " ".join(locations or [])
    if not joined.strip():
        return "Unknown"
    if _HYBRID_RE.search(joined):
        return "Hybrid"
    if _REMOTE_RE.search(joined):
        return "Remote"
    return "On-site"
