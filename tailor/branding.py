"""A company's logo and colours, for the cover letter's letterhead.

Everything here is derived from the company's own published assets - their
favicon and site - rather than invented. A wrong brand colour looks worse than
a neutral one, so every step degrades to a considered default instead of
guessing: no logo found means no logo shown, and an unreadable logo means the
letter uses a restrained slate accent.

The accent is picked from the logo's own pixels: the most *saturated* colour
present, not the most common. Most logos are mostly white or transparent
background, so the modal colour is almost always the background and the brand
colour is the minority one - which is exactly the one worth using.
"""

import base64
import colorsys
import io
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

import config

log = logging.getLogger(__name__)

CACHE = Path(__file__).resolve().parent.parent / "out" / ".branding"

# Clearbit's logo API is dead (connection refused). DuckDuckGo's icon service
# answers for every company tested and returns the largest images; Google's is
# the fallback and 404s on some domains.
_LOGO_SOURCES = [
    "https://icons.duckduckgo.com/ip3/{domain}.ico",
    "https://www.google.com/s2/favicons?domain={domain}&sz=128",
]

# Hosts that are an applicant-tracking system, not the employer.
_ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "simplify.jobs", "jobvite.com", "icims.com", "oraclecloud.com",
    "smartrecruiters.com", "workable.com", "bamboohr.com", "jobs.",
)

NEUTRAL_ACCENT = "#334155"      # slate - the "we could not tell" colour
NEUTRAL_INK = "#0f172a"


# Corporate suffixes that are part of a legal name but almost never part of a
# domain: "Quantbot Technologies" is quantbot.com, not quantbottechnologies.com.
# Deliberately narrow - "Applied Intuition" and "Two Sigma" must survive intact,
# so only words that are unambiguously corporate furniture are listed.
_CORPORATE_SUFFIXES = {
    "technologies", "technology", "tech", "inc", "incorporated", "llc", "ltd",
    "limited", "corp", "corporation", "co", "company", "group", "holdings",
    "partners", "ventures", "international", "worldwide", "global", "usa",
    "trading", "management", "capital", "securities", "systems", "solutions",
    "services", "consulting", "associates", "enterprises", "industries",
}


def company_domains(company: str) -> List[str]:
    """Plausible domains for a company name, best guess first.

    Emits the suffix-stripped form before the full one, because a company whose
    legal name carries corporate furniture almost always drops it in the
    domain. Both are returned - the caller tries them in order and the wrong
    one simply fails to resolve.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9]+", company.lower()) if w]
    if not words:
        return []

    trimmed = list(words)
    while len(trimmed) > 1 and trimmed[-1] in _CORPORATE_SUFFIXES:
        trimmed.pop()

    candidates = []
    if trimmed != words:
        candidates.append("".join(trimmed))
    candidates.append("".join(words))
    return [f"{c}.com" for c in dict.fromkeys(candidates) if c]


def domain_for(company: str, url: str = "") -> Optional[str]:
    """The employer's own domain, if we can work one out.

    Prefers the posting URL's host, but only when that host is the company's
    rather than an ATS's - fetching greenhouse.io's favicon for every company
    would put the same logo on every letter.
    """
    if url:
        try:
            host = (urlparse(url).hostname or "").lower().lstrip("www.")
            if host and not any(ats in host for ats in _ATS_HOSTS):
                return host
        except ValueError:
            pass

    candidates = company_domains(company)
    return candidates[0] if candidates else None


def fetch_logo(domain: str, timeout: int = 12) -> Optional[bytes]:
    """Download a logo for a domain, or return None."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / f"{re.sub(r'[^a-z0-9.]', '_', domain)}.img"
    if cached.exists() and cached.stat().st_size > 0:
        return cached.read_bytes()

    for template in _LOGO_SOURCES:
        try:
            response = requests.get(
                template.format(domain=domain), timeout=timeout,
                headers={"User-Agent": config.USER_AGENT},
            )
        except requests.RequestException:
            continue
        # A 16x16 grey placeholder is what these services return when they have
        # nothing; too small to be a real logo and not worth printing.
        if response.ok and len(response.content) > 700:
            cached.write_bytes(response.content)
            return response.content

    log.debug("no logo found for %s", domain)
    return None


def _saturation(rgb: Tuple[int, int, int]) -> float:
    r, g, b = (c / 255 for c in rgb)
    return colorsys.rgb_to_hsv(r, g, b)[1]


def _luminance(rgb: Tuple[int, int, int]) -> float:
    r, g, b = rgb
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def palette_from(image_bytes: bytes, count: int = 2) -> List[str]:
    """The brand colours in a logo, most brand-like first.

    Scores by saturation weighted by frequency, then drops anything too pale or
    too dark to read as an accent against white. Returns hex strings.
    """
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        if getattr(image, "n_frames", 1) > 1:      # multi-size .ico
            image.seek(image.n_frames - 1)          # the largest is usually last
        image = image.convert("RGBA").resize((64, 64))
    except Exception as exc:
        log.debug("could not read a logo image: %s", exc)
        return []

    counts: Dict[Tuple[int, int, int], int] = {}
    for r, g, b, a in image.getdata():
        if a < 128:                                 # transparent
            continue
        key = (r // 24 * 24, g // 24 * 24, b // 24 * 24)   # quantise
        counts[key] = counts.get(key, 0) + 1

    if not counts:
        return []

    total = sum(counts.values())
    scored = []
    for rgb, n in counts.items():
        lum, sat = _luminance(rgb), _saturation(rgb)
        if lum > 0.93 or lum < 0.06:                # white / black background
            continue
        if sat < 0.18 and not (0.15 < lum < 0.45):  # dull, and not a dark ink
            continue
        # Frequency matters, but a logo is mostly background: weight saturation
        # heavily so the brand colour beats the backdrop.
        scored.append(((sat * 2 + 0.4) * (n / total) ** 0.35, rgb))

    scored.sort(reverse=True)
    return ["#%02x%02x%02x" % rgb for _, rgb in scored[:count]]


def data_uri(image_bytes: bytes) -> Optional[str]:
    """A PNG data URI, so WeasyPrint can embed the logo with no network."""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        if getattr(image, "n_frames", 1) > 1:
            image.seek(image.n_frames - 1)
        image = image.convert("RGBA")
        if max(image.size) > 256:
            image.thumbnail((256, 256))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    except Exception as exc:
        log.debug("could not convert a logo: %s", exc)
        return None


class Branding:
    def __init__(self, logo: Optional[str] = None, accent: str = NEUTRAL_ACCENT,
                 secondary: str = NEUTRAL_INK, domain: Optional[str] = None):
        self.logo = logo
        self.accent = accent
        self.secondary = secondary
        self.domain = domain


def for_company(company: str, url: str = "") -> Branding:
    """Logo and colours for one employer. Never raises."""
    domain = domain_for(company, url)
    if not domain:
        return Branding()

    raw = fetch_logo(domain)
    if not raw:
        return Branding(domain=domain)

    colours = palette_from(raw)
    accent = colours[0] if colours else NEUTRAL_ACCENT
    secondary = colours[1] if len(colours) > 1 else NEUTRAL_INK

    # Guard against an accent so pale the letterhead text disappears.
    if _luminance(tuple(int(accent[i:i + 2], 16) for i in (1, 3, 5))) > 0.8:
        accent, secondary = NEUTRAL_ACCENT, accent

    return Branding(logo=data_uri(raw), accent=accent,
                    secondary=secondary, domain=domain)
