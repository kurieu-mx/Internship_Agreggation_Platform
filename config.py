"""Configuration, loaded from the environment with sensible defaults.

Nothing in here is secret. Real credentials live in a .env file (git-ignored)
or in the process environment; see .env.example.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    """An environment variable, treating blank as unset.

    ``os.getenv(name, default)`` returns the default only when the variable is
    *absent*. A variable that is present and empty returns an empty string,
    and the default never applies.

    That distinction is not academic. GitHub Actions expands an unset
    repository variable to the empty string rather than omitting it, so a
    workflow passing ``MODEL_TAILORING: ${{ vars.MODEL_TAILORING }}`` with no
    such variable defined sets it to "". Observed on the first CI run: every
    model call returned ``400 - model: String should have at least 1
    character``, tailoring fell back to the untailored master for every
    posting, no cover letters were produced, and the workflow still reported
    success - because falling back is the designed response to a failed call.

    The same shape would have crashed outright on the numeric settings, where
    ``float("")`` raises rather than degrades.
    """
    value = os.getenv(name)
    return value if value is not None and value.strip() else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


# --- Data source -----------------------------------------------------------
# The Pitt CSC / Simplify community repo publishes every listing as structured
# JSON. Scraping their rendered README (the original approach here) broke the
# moment they changed its layout, so we read the machine-readable file instead.
LISTINGS_URL = _env(
    "LISTINGS_URL",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships"
    "/dev/.github/scripts/listings.json",
)

# A second community feed with a leaner schema: it carries `season` ("Summer")
# rather than a `terms` list, and the year comes from the repo itself. Lower
# yield than the Simplify feed (~400 records vs ~14k), but it is maintained
# independently, so it occasionally lists postings the larger repo has missed.
VANSH_URL = _env(
    "VANSH_URL",
    "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships"
    "/dev/.github/scripts/listings.json",
)

# The year VANSH_URL's repo covers. Not present in the payload - it is only in
# the repo name - so it has to be stated here alongside the URL.
VANSH_YEAR = _env_int("VANSH_YEAR", 2027)

# Only keep postings whose term matches one of these (case-insensitive
# substring match). Empty string disables the filter.
TERM_FILTER = _env("TERM_FILTER", "Summer 2027")

# Drop postings the upstream source has marked closed.
ACTIVE_ONLY = _env("ACTIVE_ONLY", "true").lower() != "false"

# --- Digest ----------------------------------------------------------------
# How far back a posting may have been published and still count as "new".
WINDOW_HOURS = _env_int("WINDOW_HOURS", 24)

# How many of the highest-scoring postings get a tailored resume and cover
# letter. Everything else that cleared the filters is listed link-only, so a
# heavy posting day is never silently truncated.
TOP_N = _env_int("TOP_N", 10)

# Most tailored applications any one employer may take. One, deliberately: a
# second application to the same company the same day adds little, while a
# first application to a different one adds a lot. Without any cap a company
# that posts five roles takes most of the slots - observed live, where
# ByteDance took four of eight. Overflow drops to the also-ranked list rather
# than being discarded. Set to 0 to disable.
MAX_PER_COMPANY = _env_int("MAX_PER_COMPANY", 1)

# Sponsorship statuses to drop outright. For an applicant who needs visa
# sponsorship, a role requiring US citizenship or a security clearance is not
# an opportunity, so it should not occupy one of the TOP_N tailoring slots.
#
# Note what this can and cannot do: no source reliably publishes a sponsorship
# field (measured: 100% report "Unknown"), so eligibility.py derives the status
# from the posting text instead, and only the ATS sources publish any text.
# Postings that stay "Unknown" are kept - see eligibility.py for why.
EXCLUDE_SPONSORSHIP = [
    s.strip()
    for s in _env("EXCLUDE_SPONSORSHIP", "No,US citizens only").split(",")
    if s.strip()
]

# Drop postings that require a Master's or PhD. An internship asking for a
# degree the candidate does not have is not an opportunity, and it should not
# occupy a tailoring slot. Measured live: ~50 of 440 internships are
# graduate-only, mostly quant research roles.
#
# Postings open to "Bachelor's or Master's" are kept - that is either/or, not a
# requirement - and so is silence, which is most of them.
UNDERGRADUATE_ONLY = _env("UNDERGRADUATE_ONLY", "true").lower() != "false"

# Categories worth tailoring for. Matched against the normalised category.
TARGET_CATEGORIES = [
    c.strip()
    for c in _env(
        "TARGET_CATEGORIES", "Software Engineering,AI / ML / Data,Quant"
    ).split(",")
    if c.strip()
]

# Which source adapters to run. Unknown names are ignored with a warning
# rather than aborting the run.
#
# The credentialed sources are listed by default even though most setups have
# no credentials for them: each returns an empty list and logs one line when
# unconfigured, so leaving them in costs nothing and means they start
# contributing the moment a key is added, with no config change.
# `handshake` is deliberately absent: it needs a browser cookie that expires
# every few weeks, and the upkeep was judged not worth the extra coverage.
# Add it back here (and set HANDSHAKE_COOKIE) if that changes.
SOURCES = [
    s.strip()
    for s in _env(
        "SOURCES",
        "greenhouse,lever,ashby,workday,simplify,vansh,websearch,linkedin",
    ).split(",")
    if s.strip()
]

# Board tokens for the direct-ATS adapters.
COMPANIES_FILE = _env("COMPANIES_FILE", "companies.yml")

# Where the seen-postings / sent-digest database lives.
DB_PATH = _env("DB_PATH", "internships.db")

# --- Models -----------------------------------------------------------------
# The SDK reads ANTHROPIC_API_KEY from the environment itself; it is mirrored
# here only so the doctor and llm.py can check for it without importing the SDK.
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")

# Which backend the model calls go through: `api` or `cli`.
#
# `api` bills per token and is what the digest uses - a digest day measures at
# ~80 calls and ~$3.00, paid once. `cli` shells out to Claude Code in headless
# mode, which runs against the Max subscription and costs nothing marginal.
#
# The default is `api` deliberately. The CLI backend needs an interactive
# claude.ai login, which the GitHub Actions runner does not have, so flipping
# this default would break the 3pm workflow rather than make it free. The
# dashboard - where the same six calls per posting would otherwise cost
# ~$0.25-0.30 every time a link is pasted - sets `cli` for its own process.
LLM_BACKEND = _env("LLM_BACKEND", "api").strip().lower()

# Split by what the call actually does, not to shave the bill.
#
# Scoring decides which eight companies you apply to. It is the
# highest-leverage judgement in the pipeline and the one whose mistakes are
# invisible - a good posting ranked tenth simply never appears. Opus.
MODEL_SCORING = _env("MODEL_SCORING", "claude-opus-5")

# Resume tailoring. Sonnet, and the reason is the guardrails rather than the
# price: this step does not write a resume, it picks bullet ids out of a fixed
# pool and lightly rewords them, and four checks stand behind it - provenance,
# text fidelity, immutable facts, page count. A weaker model that chooses less
# well still cannot fabricate anything, because the checks reject it and the
# untailored master goes out instead.
#
# Measured share of a digest's cost before the switch: 31%, the largest single
# line. Whether the choices get worse is the open question, and the answer
# comes off a real run rather than a projection - the digest logs keyword
# coverage per posting, so a drop shows up without a special measurement.
MODEL_TAILORING = _env("MODEL_TAILORING", "claude-sonnet-5")

# The cover letter, kept on Opus deliberately.
#
# It shares nothing with resume tailoring except the word "tailoring". This is
# composition a human reads and judges, it is the only part of an application
# that distinguishes it from a template, and its guardrails check whether the
# facts are traceable - not whether the writing is any good. Nothing downstream
# would catch a duller letter, so the model is where that quality has to come
# from.
#
# It was one setting with MODEL_TAILORING until the two jobs were separated:
# "select from a validated pool" and "write something worth reading" have
# opposite tolerances for a cheaper model.
MODEL_LETTER = _env("MODEL_LETTER", "claude-opus-5")

# Company research is extraction: pull verifiable facts out of a careers page.
# No judgement, and the output is validated against the source text before it
# reaches a letter, so a weaker model cannot smuggle anything through. Haiku,
# which is ~23% of the daily cost for ~60% of the input tokens.
MODEL_RESEARCH = _env("MODEL_RESEARCH", "claude-haiku-4-5")

# Reasoning depth for the scoring and writing calls. `high` is the default;
# raise to `xhigh` if tailoring reads shallow, lower to `medium` to cut cost.
MODEL_EFFORT = _env("MODEL_EFFORT", "high")

# Hard ceiling on API spend per calendar day (UTC), in dollars. A normal digest
# costs ~$0.54; this exists for the abnormal ones - a retry loop, a TOP_N typo,
# a workflow firing repeatedly, a source that suddenly returns ten thousand
# postings. Once reached, further model calls are refused and the pipeline
# degrades exactly as it does when the model is unreachable, so the digest
# still goes out. Spend is tracked in the database, so it survives a restart
# and a crash-loop cannot reset it. Set to 0 to disable.
DAILY_BUDGET_USD = _env_float("DAILY_BUDGET_USD", 2.00)

# --- Employer prominence ----------------------------------------------------
# Postings at these employers get a bonus in the deterministic prefilter, so a
# recognised name is carried into the rerank pool rather than being cut by a
# keyword count. The model still decides the final shortlist - this widens who
# gets considered, it does not decide who wins.
#
# The bonus is deliberately smaller than a strong keyword match. A big name on
# a badly-fitting role is still a badly-fitting role, and a digest that leads
# with a retail management internship because the logo is famous would be
# worse than one that never mentioned it.
#
# There is a substantive reason beyond brand: an applicant who needs visa
# sponsorship is materially better served by large established employers, who
# file H-1B petitions routinely, than by small firms that often cannot.
PRIORITY_EMPLOYERS_TIER1 = [
    s.strip().lower()
    for s in _env(
        "PRIORITY_EMPLOYERS_TIER1",
        "google,alphabet,meta,facebook,amazon,apple,microsoft,netflix,nvidia,"
        "openai,anthropic,deepmind,tesla,spacex,stripe,databricks",
    ).split(",")
    if s.strip()
]

PRIORITY_EMPLOYERS_TIER2 = [
    s.strip().lower()
    for s in _env(
        "PRIORITY_EMPLOYERS_TIER2",
        "ibm,intel,salesforce,adobe,oracle,cisco,qualcomm,dell,hp,hewlett,"
        "broadcom,amd,micron,texas instruments,applied materials,analog devices,"
        "boeing,lockheed,northrop grumman,raytheon,rtx,honeywell,ge aerospace,"
        "jpmorgan,goldman sachs,morgan stanley,capital one,mastercard,visa,"
        "paypal,bloomberg,palantir,snowflake,workday,servicenow,vmware,"
        "uber,lyft,airbnb,doordash,linkedin,bytedance,tiktok,samsung,sony,"
        "siemens,bosch,intuit,cisco systems,comcast,motorola,autodesk,target",
    ).split(",")
    if s.strip()
]

# Points added in the prefilter. Sized against recency_bonus, which peaks at
# 12 for a posting that just went live.
PRIORITY_BONUS_TIER1 = _env_float("PRIORITY_BONUS_TIER1", 18.0)
PRIORITY_BONUS_TIER2 = _env_float("PRIORITY_BONUS_TIER2", 10.0)

# --- Composio (optional: web search + Gmail delivery) -----------------------
# Everything Composio-backed degrades to a no-op when the key is absent, so
# the public sources keep working with no configuration at all.
COMPOSIO_API_KEY = _env("COMPOSIO_API_KEY", "")
COMPOSIO_USER_ID = _env("COMPOSIO_USER_ID", "default")

# Which search tool to run. Composio's search toolkit fronts several providers;
# Tavily returns the cleanest title/url/content triples for this parser.
SEARCH_SLUG = _env("SEARCH_SLUG", "COMPOSIO_SEARCH_TAVILY")

# Used by the cover-letter research step to read a company's own pages.
FETCH_URL_SLUG = _env("FETCH_URL_SLUG", "COMPOSIO_SEARCH_FETCH_URL_CONTENT")

# --- Handshake (optional, needs your own session) ---------------------------
# Handshake has no public API and sits behind university SSO. This is your own
# authenticated session, exported from your browser - not a scraper working
# around a login. Unset means the source contributes nothing.
HANDSHAKE_COOKIE = _env("HANDSHAKE_COOKIE", "")
HANDSHAKE_HOST = _env("HANDSHAKE_HOST", "umich.joinhandshake.com")

# --- Delivery ---------------------------------------------------------------
# No default, deliberately. This used to fall back to the author's address,
# which is fine until someone forks the repo: a fork that sets every other
# credential but forgets this one mails its owner's tailored resume - name,
# phone, address, work history - to a stranger, and the run reports success.
# The doctor passed it too, because the address it was checking was a valid
# one. Empty is the honest default; the doctor and the sender both refuse.
DIGEST_TO = _env("DIGEST_TO", "")
DIGEST_TIMEZONE = _env("DIGEST_TIMEZONE", "America/Chicago")

# --- HTTP ------------------------------------------------------------------
REQUEST_TIMEOUT = _env_int("REQUEST_TIMEOUT", 30)
MAX_RETRIES = _env_int("MAX_RETRIES", 3)
RETRY_BACKOFF = _env_int("RETRY_BACKOFF", 2)  # seconds, doubled per attempt
USER_AGENT = _env(
    "USER_AGENT", "internship-scraper (+https://github.com/kurieu-mx/internship-scraper)"
)

# --- Ollama (optional enrichment) ------------------------------------------
OLLAMA_BASE_URL = _env("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = _env("OLLAMA_MODEL", "llama3.2")
OLLAMA_TIMEOUT = _env_int("OLLAMA_TIMEOUT", 30)
OLLAMA_TEMPERATURE = _env_float("OLLAMA_TEMPERATURE", 0.1)

# --- Google Sheets (optional sink) -----------------------------------------
GOOGLE_CREDENTIALS_FILE = _env("GOOGLE_CREDENTIALS_FILE", "credentials.json")
SPREADSHEET_ID = _env("SPREADSHEET_ID", "")
SHEET_NAME = _env("SHEET_NAME", "Internships")

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
