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

# Bare city names, used without a state. The community feeds mostly write
# "Austin, TX", but ATS boards very often write just "Chicago" - and a
# conservative matcher that rejects every bare city silently drops most of what
# the direct-board adapters find. This list is the allowlist that makes bare
# cities usable without opening the door to "London" or "Bengaluru".
#
# Deliberately limited to unambiguous, high-volume US locations. Names shared
# with a larger foreign city (Vancouver, Ontario, Birmingham, Cambridge) are
# left out: a missed US posting costs one line in a digest, a Canadian one
# wrongly kept costs trust in the whole filter.
US_CITY_ALIASES = {
    "nyc", "sf", "la", "bay area", "silicon valley", "sfo", "nyc metro",
    "new york", "new york city", "brooklyn", "queens", "manhattan",
    "san francisco", "south san francisco", "palo alto", "mountain view",
    "menlo park", "redwood city", "sunnyvale", "santa clara", "san jose",
    "cupertino", "berkeley", "oakland", "san mateo", "foster city",
    "los angeles", "santa monica", "pasadena", "irvine", "san diego",
    "culver city", "el segundo", "long beach", "sacramento", "fremont",
    "seattle", "bellevue", "redmond", "kirkland", "tacoma", "spokane",
    "portland", "beaverton", "hillsboro", "eugene",
    "chicago", "evanston", "naperville", "schaumburg",
    "austin", "dallas", "houston", "san antonio", "fort worth", "plano",
    "richardson", "irving", "el paso", "arlington",
    "boston", "cambridge, ma", "somerville", "waltham", "burlington, ma",
    "boxborough", "andover", "lexington, ma", "needham",
    "denver", "boulder", "colorado springs", "fort collins", "louisville, co",
    "atlanta", "alpharetta", "marietta", "savannah",
    "miami", "orlando", "tampa", "jacksonville", "fort lauderdale",
    "washington dc", "washington, d.c.", "arlington, va", "mclean",
    "reston", "herndon", "tysons", "alexandria", "bethesda", "rockville",
    "baltimore", "annapolis", "college park",
    "philadelphia", "pittsburgh", "harrisburg", "king of prussia",
    "detroit", "ann arbor", "grand rapids", "dearborn", "warren, mi",
    "minneapolis", "st paul", "saint paul", "st. paul", "rochester, mn",
    "milwaukee", "madison, wi",
    "phoenix", "tempe", "scottsdale", "chandler, az", "mesa", "tucson",
    "salt lake city", "provo", "lehi", "park city",
    "las vegas", "reno", "henderson",
    "nashville", "memphis", "knoxville", "chattanooga",
    "charlotte", "raleigh", "durham", "chapel hill", "cary",
    "research triangle park", "greensboro", "winston-salem",
    "st louis", "st. louis", "saint louis", "kansas city", "omaha",
    "des moines", "indianapolis", "columbus", "cleveland", "cincinnati",
    "dayton", "toledo", "akron",
    "new orleans", "baton rouge", "little rock", "oklahoma city", "tulsa",
    "boise", "albuquerque", "santa fe", "anchorage", "honolulu",
    "hartford", "stamford", "new haven", "greenwich",
    "princeton", "newark", "jersey city", "hoboken", "trenton",
    "buffalo", "rochester, ny", "albany", "syracuse", "ithaca",
    "richmond", "virginia beach", "norfolk", "charlottesville", "blacksburg",
    "columbia, sc", "charleston", "greenville",
    "birmingham, al", "huntsville", "montgomery",
    "wilmington, de", "providence", "burlington, vt", "portland, me",
    "manchester, nh", "nashua", "morrisville", "bentonville", "fayetteville",
    # Explicitly-US remote phrasings only. A bare "Remote" or "Multiple
    # Locations" names no country, so it stays rejected.
    "remote - us", "us remote", "remote us", "remote (us)",
    "united states", "usa", "u.s.", "u.s.a.",
}

# Cities that are decisively not in the US. Needed because the country name is
# usually absent when a board writes a bare city ("London", "Bengaluru"), so
# the country-marker check never fires and the string would otherwise fall
# through to "unrecognised" - which is the right default, but logging it as an
# explicit rejection makes the filter's behaviour auditable.
NON_US_CITIES = {
    "london", "manchester", "birmingham", "bristol", "edinburgh", "glasgow",
    "cambridge", "oxford", "leeds", "belfast", "dublin", "cork",
    "paris", "lyon", "toulouse", "berlin", "munich", "hamburg", "frankfurt",
    "amsterdam", "rotterdam", "eindhoven", "brussels", "zurich", "geneva",
    "madrid", "barcelona", "lisbon", "porto", "milan", "rome", "turin",
    "vienna", "prague", "warsaw", "krakow", "budapest", "bucharest",
    "belgrade", "zagreb", "sofia", "athens", "istanbul",
    "stockholm", "gothenburg", "oslo", "copenhagen", "helsinki", "tallinn",
    "toronto", "vancouver", "montreal", "ottawa", "calgary", "waterloo",
    "mexico city", "guadalajara", "monterrey", "sao paulo", "rio de janeiro",
    "buenos aires", "santiago", "bogota", "lima",
    "tokyo", "osaka", "kyoto", "seoul", "beijing", "shanghai", "shenzhen",
    "hangzhou", "guangzhou", "hong kong", "taipei", "singapore",
    "bengaluru", "bangalore", "hyderabad", "mumbai", "delhi", "new delhi",
    "pune", "chennai", "gurgaon", "gurugram", "noida", "kolkata",
    "tel aviv", "haifa", "jerusalem", "dubai", "abu dhabi", "doha", "riyadh",
    "cairo", "lagos", "nairobi", "cape town", "johannesburg",
    "sydney", "melbourne", "brisbane", "perth", "auckland", "wellington",
    "manila", "jakarta", "bangkok", "hanoi", "ho chi minh city", "kuala lumpur",
}

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


# Separators that unambiguously mean "several places", as opposed to the comma,
# which is overloaded: "San Francisco, CA" is one place but "New York, Seattle"
# is two. Real examples from the boards: "Chicago; New York",
# "Singapore / Hong Kong", "San Francisco, CA - New York, NY",
# "Dublin OR London", "SF - NYC - Remote".
_LOCATION_SPLIT_RE = re.compile(r"\s*(?:[;|•·\n]|\bor\b|/)\s*", re.I)

# Trailing internal office codes: "London - UK2", "San Francisco - SF9".
# Requires a digit so real place names ("Dubai - United Arab Emirates") survive.
_OFFICE_CODE_RE = re.compile(r"\s+-\s+[A-Za-z]*\d+[A-Za-z]*\s*$")


def _has_place_suffix(text: str) -> bool:
    """Does this string end in a state or country, i.e. is it one place?

    "San Francisco, CA" and "Chicago, United States" do; "New York, Seattle"
    does not. This is what decides whether a comma is joining a city to its
    state or separating two cities.
    """
    tail = _tail(text)
    return (
        tail.upper() in US_STATE_CODES
        or tail in US_STATE_NAMES
        or tail in NON_US_MARKERS
        or tail in {"united states", "usa", "u.s.", "u.s.a."}
    )


def split_locations(raw: str) -> List[str]:
    """Break one location field into the individual places it names.

    Boards routinely pack several locations into a single string, and treating
    the whole thing as one place is how "Chicago; New York" - two of the
    largest US tech markets - gets classified as non-US and dropped.

    The comma is only treated as a separator when the string does not already
    end in a state or country, which is what distinguishes "New York, Seattle"
    (two places) from "San Francisco, CA" (one).
    """
    if not raw or not raw.strip():
        return []

    places: List[str] = []
    for chunk in _LOCATION_SPLIT_RE.split(raw):
        chunk = _OFFICE_CODE_RE.sub("", chunk).strip().strip(",").strip()
        if not chunk:
            continue
        if "," in chunk and not _has_place_suffix(chunk):
            places.extend(part.strip() for part in chunk.split(",") if part.strip())
        else:
            places.append(chunk)

    # Preserve order, drop repeats.
    return list(dict.fromkeys(places))


def is_us_location(location: str) -> bool:
    """Best-effort check that a single location string is in the US.

    Conservative: anything we cannot positively identify as US is rejected,
    which keeps foreign postings out of a database that is explicitly US-only.
    """
    if not location:
        return False

    text = _OFFICE_CODE_RE.sub("", location.strip()).strip()
    lowered = text.lower()

    # A non-US country name anywhere in the string is decisive.
    for marker in NON_US_MARKERS:
        if re.search(rf"(^|[,\s]){re.escape(marker)}$", lowered) or f", {marker}" in lowered:
            return False

    # So is a bare foreign city, which carries no country name to match on.
    if lowered in NON_US_CITIES:
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
    """Keep only the US places named anywhere in a posting's location list.

    Each entry is first split into the individual places it names, so a field
    like "Toronto, New York, San Francisco" contributes its two US cities
    rather than being rejected wholesale on its Canadian one.
    """
    kept: List[str] = []
    for entry in locations or []:
        if not entry:
            continue
        for place in split_locations(entry) or [entry]:
            if is_us_location(place) and place not in kept:
                kept.append(place)
    return kept


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
