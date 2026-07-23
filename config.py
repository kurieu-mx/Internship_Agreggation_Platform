"""Configuration, loaded from the environment with sensible defaults.

Nothing in here is secret. Real credentials live in a .env file (git-ignored)
or in the process environment; see .env.example.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# --- Data source -----------------------------------------------------------
# The Pitt CSC / Simplify community repo publishes every listing as structured
# JSON. Scraping their rendered README (the original approach here) broke the
# moment they changed its layout, so we read the machine-readable file instead.
LISTINGS_URL = os.getenv(
    "LISTINGS_URL",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships"
    "/dev/.github/scripts/listings.json",
)

# Only keep postings whose term matches one of these (case-insensitive
# substring match). Empty string disables the filter.
TERM_FILTER = os.getenv("TERM_FILTER", "Summer 2026")

# Drop postings the upstream source has marked closed.
ACTIVE_ONLY = os.getenv("ACTIVE_ONLY", "true").lower() != "false"

# --- HTTP ------------------------------------------------------------------
REQUEST_TIMEOUT = _env_int("REQUEST_TIMEOUT", 30)
MAX_RETRIES = _env_int("MAX_RETRIES", 3)
RETRY_BACKOFF = _env_int("RETRY_BACKOFF", 2)  # seconds, doubled per attempt
USER_AGENT = os.getenv(
    "USER_AGENT", "internship-scraper (+https://github.com/kurieu-mx/internship-scraper)"
)

# --- Ollama (optional enrichment) ------------------------------------------
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
OLLAMA_TIMEOUT = _env_int("OLLAMA_TIMEOUT", 30)
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.1"))

# --- Google Sheets (optional sink) -----------------------------------------
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
SHEET_NAME = os.getenv("SHEET_NAME", "Internships")

COLUMN_HEADERS = [
    "Company",
    "Position",
    "Location",
    "Field",
    "Term",
    "Degrees",
    "Sponsorship",
    "Work Mode",
    "Posted",
    "Application Link",
    "Source",
    "Status",
]
