"""Apply to one posting by URL, for employers no source can reach.

Why this exists
---------------
Six of the employers worth applying to - IBM, Amazon, Google, Apple, Meta,
Microsoft - each run their own careers portal rather than an ATS with a public
API, and several sit behind bot protection. Writing six bespoke adapters was
judged not worth the maintenance, so those postings arrive the way a person
finds them: as a link.

This turns a link into the same output the daily digest produces - a tailored
resume, a grounded cover letter, and an email - through exactly the same code
path, so a hand-added posting is not a lesser application than a collected one.
It runs the same gates too: a link to a PhD-only role or one closed to
sponsorship is reported as such rather than quietly tailored for.

Fetching
--------
``requests`` is tried first because it is free and works for most sites. When
it returns nothing usable - IBM answers a plain request with HTTP 202 and an
empty body - the Composio fetcher is used, which renders the page. Neither
being able to read it is a clean failure with the reason stated, not a guess.

Reading the posting
-------------------
Portals share no markup, so the fields are extracted by one small model call
rather than a per-site parser. It is extraction, not judgement: every field it
returns is quoted from the fetched text, and the description it produces is
the text the tailoring and cover-letter steps then work from. Haiku, because
a stronger model buys nothing on a task whose output is copied rather than
composed.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import config
import llm
from models import Job

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string",
                    "description": "employer name as a person would say it, e.g. 'IBM'"},
        "title": {"type": "string", "description": "the job title, verbatim"},
        "locations": {"type": "array", "items": {"type": "string"},
                      "description": "each location as 'City, ST' where given"},
        "posted_date": {"type": "string",
                        "description": "ISO date the posting went live, or empty if not stated"},
        "term": {"type": "string",
                 "description": "e.g. 'Summer 2027', or empty if not stated"},
        "is_internship": {"type": "boolean"},
        "description": {"type": "string",
                        "description": "the full role description, responsibilities and "
                                       "requirements, as plain text copied from the page"},
    },
    "required": ["company", "title", "locations", "posted_date", "term",
                 "is_internship", "description"],
    "additionalProperties": False,
}

EXTRACTION_SYSTEM = """You read one job posting and return its fields.

This is extraction, not summarisation. Every field must come from the text you
are given:

- Copy the title verbatim. Do not tidy it.
- `company` is the employer, not the job board or the recruiting platform.
- `description` is the actual role content - responsibilities, requirements,
  qualifications, tech stack. Include all of it; the resume is tailored from
  this text, so anything you drop is invisible downstream. Leave out benefits
  boilerplate, equal-opportunity statements, and company culture filler.
- `posted_date` only if the page states one. Do not infer it from wording like
  "posted recently". Empty string if absent.
- `term` is the academic term an internship or co-op cohort is for, such as
  "Summer 2027". Leave it empty for a full-time or new-grad role: a start
  date, a graduation window, or a year on its own is not a term, and this
  field is printed on the cover letter, so a season invented for a full-time
  role puts a detail in front of a recruiter that the posting never claimed.
- If the page is not a job posting at all - a search page, a login wall, an
  error - set `is_internship` false and leave `description` empty."""


def _fetch_plain(url: str) -> str:
    """Fetch with requests. Returns visible text, or "" if that will not work."""
    import requests

    try:
        response = requests.get(
            url, timeout=25, allow_redirects=True,
            headers={"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/131.0 Safari/537.36"),
                     "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                     "Accept-Language": "en-US,en;q=0.9"},
        )
    except requests.RequestException as exc:
        log.debug("plain fetch failed: %s", exc)
        return ""

    # IBM answers a plain request with 202 and an empty body - a challenge
    # response, not a page. Anything this short is not a posting either.
    if not response.ok or len(response.content) < 2000:
        log.debug("plain fetch returned %s with %d bytes",
                  response.status_code, len(response.content))
        return ""

    body = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", response.text)
    text = re.sub(r"<[^>]+>", " ", body)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) > 800 else ""


def _fetch_rendered(url: str) -> str:
    """Fetch through Composio, which renders the page and passes bot checks."""
    from composio_gateway import available, execute
    from tailor.cover import _text_from

    if not available():
        log.warning("Composio is not configured, so a bot-protected page cannot be read")
        return ""
    try:
        return _text_from(execute(config.FETCH_URL_SLUG, {"url": url}))
    except Exception as exc:
        log.warning("rendered fetch failed (%s): %s", type(exc).__name__, exc)
        return ""


def fetch_posting(url: str) -> str:
    """The posting's text, by whichever route works. Cheapest first."""
    text = _fetch_plain(url)
    if text:
        log.info("fetched %d characters directly", len(text))
        return text

    log.info("direct fetch returned nothing usable - trying the rendered fetcher")
    text = _fetch_rendered(url)
    if text:
        log.info("fetched %d characters via Composio", len(text))
    return text


def _parse_date(value: str) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    for form in (None, "%Y-%m-%d", "%d-%b-%Y", "%m/%d/%Y", "%B %d, %Y"):
        try:
            parsed = (datetime.fromisoformat(text) if form is None
                      else datetime.strptime(text, form))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _terms_for(result: dict, title: str, description: str,
               source: str = "") -> List[str]:
    """Which academic term this posting is for, or nothing if we cannot tell.

    A stated term is checked against ``source`` before it is believed. Asking
    the model not to invent one is not enough - told plainly that a start date
    is not a term, it still answered "Summer 2027" for a full-time new-grad
    role whose text says only "starting in 2027", and answered "2027" on
    another run of the same posting. This is the same problem ``validate_facts``
    solves for company research, and the same answer: the term has to appear in
    the text it was read from, or it did not come from there.

    Three things were wrong with reading it straight off ``infer_terms``.

    It returns ``(terms, inferred)``, and assigning that tuple to ``Job.terms``
    produced ``(['Summer 2027'], True)`` - a list and a bool where a list of
    strings belongs. Every consumer joins that field, so a posting whose page
    stated no term crashed the CLI, the store row, the dashboard card and the
    cover-letter header. The collected sources unpack it correctly; this path
    and Workday did not.

    Its fallback is also specific to internships: absent any year, it assumes a
    posting published in the second half of the year recruits for the following
    summer, which its own docstring scopes to US tech internships. That
    reasoning does not hold for a new-grad or full-time role, so an inferred
    term is kept only when the title actually reads as an internship.

    And the guess is worse here than in the digest. A collected posting that
    guesses wrong is one row in a ranking; a hand-added one has its term
    printed in the cover letter header, so guessing "Summer 2027" onto a
    new-grad application puts a fabricated detail in front of a recruiter.
    Saying nothing is the honest failure.
    """
    from sources.ats import infer_terms, looks_like_internship

    stated = (result.get("term") or "").strip()
    if stated:
        # Compared on collapsed whitespace and case only. Anything cleverer
        # starts guessing at what the model meant, which is the failure being
        # defended against.
        haystack = " ".join(f"{source} {title} {description}".lower().split())
        if " ".join(stated.lower().split()) in haystack:
            return [stated]
        log.info("dropping term %r - it does not appear in the posting text", stated)

    # Past this point every candidate is reasoning rather than reading, and
    # that reasoning is about internships: infer_terms supplies a *season* it
    # never saw, defaulting to summer, whether it took the year off the title
    # or out of the body. On a full-time posting that turns "starting in 2027"
    # into "Summer 2027". So a role that does not read as an internship keeps a
    # term only when one was literally written down, which the branch above
    # already handled.
    if not looks_like_internship(title):
        return []

    terms, _inferred = infer_terms(title, description)
    return list(terms)


def extract(url: str, text: str) -> Optional[Job]:
    """One model call, turning fetched text into a Job."""
    import llm
    from normalize import filter_us_locations, infer_work_mode
    from sources.ats import categorize_title, infer_terms

    if not llm.available():
        log.error("no Anthropic key, so the posting cannot be read")
        return None

    result = llm.complete_json(
        system=EXTRACTION_SYSTEM,
        prompt=f"URL: {url}\n\nPAGE TEXT:\n{text[:24000]}",
        schema=EXTRACTION_SCHEMA,
        model=config.MODEL_RESEARCH,
        max_tokens=8000,
    )
    if not result:
        return None

    title = (result.get("title") or "").strip()
    company = (result.get("company") or "").strip()
    if not title or not company:
        log.error("could not read a company and title from that page")
        return None

    if not result.get("is_internship"):
        log.warning("that page does not look like an internship posting")

    description = (result.get("description") or "").strip()
    locations = [str(x) for x in (result.get("locations") or []) if str(x).strip()]
    us_locations = filter_us_locations(locations) or locations

    return Job(
        company=company,
        title=title,
        locations=us_locations,
        url=url,
        description=description,
        field_category=categorize_title(title),
        terms=_terms_for(result, title, description, source=text),
        work_mode=infer_work_mode(us_locations),
        posted_at=_parse_date(result.get("posted_date") or ""),
        external_id=(urlparse(url).query or "")[:60],
        source="manual",
        provider_rank=5,          # hand-added: outranks every collected source
    )


def check_gates(job: Job) -> List[Tuple[str, str]]:
    """Run the same filters the digest runs and return what they found.

    Reported rather than enforced. You asked for this specific posting, so the
    decision to apply anyway is yours - but you should know before the letter
    is written, not after the interview.

    Returns ``(level, message)`` pairs. ``warn`` means you may be ineligible;
    ``note`` is an observation about what the posting is. The split matters
    because this path is used for roles the digest would never surface -
    new-grad, co-op, full-time - and flagging every one of those as a warning
    would put an orange box on every card until you stopped reading them,
    including the sponsorship one that can actually waste an application.

    Sets ``job.sponsorship`` as a side effect, which the email body and the
    dashboard both read.
    """
    from eligibility import detect_restriction, requires_graduate_degree
    from sources.ats import looks_like_internship

    findings: List[Tuple[str, str]] = []

    if not looks_like_internship(job.title):
        findings.append(("note", "this title does not read as an internship"))

    if requires_graduate_degree(job):
        findings.append(("warn", "this posting appears to require a graduate degree"))

    status, reason = detect_restriction(job)
    job.sponsorship = status
    if status in config.EXCLUDE_SPONSORSHIP:
        findings.append(("warn", f"closed to applicants needing sponsorship — {reason}"))
    elif status == "Yes":
        findings.append(("note", f"sponsorship offered — {reason}"))

    return findings


class Prepared:
    """Everything one posting produced, before anything is done with it.

    ``run`` emails it and the dashboard renders it. Splitting the pipeline
    here rather than duplicating it keeps the guarantee the module docstring
    makes - that a hand-added posting goes through exactly the same path as a
    collected one - true for the dashboard too, not just the CLI.
    """

    def __init__(self, job: Job):
        self.job = job
        self.resume: Optional[Path] = None
        self.cover: Optional[Path] = None
        self.tailored = False
        self.gates: List[Tuple[str, str]] = []
        self.brief = None
        self.cost = 0.0
        self.pasted = False


def prepare(url: str, out_dir: Path, skip_cover: bool = False,
            description: str = "") -> Optional[Prepared]:
    """Fetch, read, score and tailor one posting. Returns None if unreadable.

    ``description`` is the posting's text, pasted rather than fetched. The
    fetchers handle most of the public web, but the postings this tool exists
    for are disproportionately the ones they cannot reach: portals behind a
    login, applications that arrived by email, a PDF a recruiter sent. Without
    somewhere to paste, those are exactly the applications you cannot build,
    which is the wrong way round. When it is supplied the fetch is skipped
    entirely - no request is made, so a dead or gated link costs nothing.

    No delivery and no store writes - those belong to the caller, because
    "produced a letter" and "sent it" are different outcomes and only the
    caller knows which one it wants.
    """
    import budget
    from digest import _slug, load_profile
    from tailor.cover import cover_letter
    from tailor.keywords import align
    from tailor.resume import tailored_resume
    from tailor.score import rerank

    text = description.strip()
    if text:
        log.info("using %d pasted characters, skipping the fetch", len(text))
    else:
        text = fetch_posting(url)
    if not text:
        log.error("could not read that page by any route")
        return None

    job = extract(url, text)
    if job is None:
        return None

    prepared = Prepared(job)
    prepared.pasted = bool(description.strip())
    prepared.gates = check_gates(job)

    profile = load_profile()
    profile_text = (ROOT / "profile" / "profile.yml").read_text()

    spend_before = budget.spent_today()

    # Scored for the same reason the digest scores: the email prints it, and a
    # posting showing 0/100 reads as a bad match rather than an unscored one.
    rerank([job], profile, profile_text)

    prepared.brief = align(job, profile)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{_slug(job.company)}_{_slug(job.title)}"

    try:
        resume, tailored = tailored_resume(job, profile, out_dir / f"Resume_{stem}.pdf")
        prepared.resume, prepared.tailored = resume, tailored
    except Exception as exc:
        log.warning("no resume (%s): %s", type(exc).__name__, exc)

    if not skip_cover:
        prepared.cover = cover_letter(job, profile, out_dir / f"Cover_{stem}.pdf")

    prepared.cost = budget.spent_today() - spend_before
    return prepared


def run(url: str, dry_run: bool = False, to: Optional[str] = None,
        skip_cover: bool = False, description_file: Optional[str] = None) -> int:
    """Fetch, tailor, and send one posting. Returns a process exit code."""
    import store
    from delivery.email import DigestItem, send

    if not urlparse(url).scheme.startswith("http"):
        log.error("that does not look like a URL: %s", url)
        return 2

    description = ""
    if description_file:
        try:
            description = Path(description_file).read_text()
        except OSError as exc:
            log.error("could not read %s: %s", description_file, exc)
            return 2
        if not description.strip():
            log.error("%s is empty", description_file)
            return 2

    now = datetime.now(timezone.utc)
    out_dir = ROOT / "out" / now.strftime("%Y-%m-%d")

    prepared = prepare(url, out_dir, skip_cover=skip_cover,
                       description=description)
    if prepared is None:
        return 1

    job = prepared.job

    print(f"\n  {job.company} — {job.title}")
    print(f"  {', '.join(job.locations) or 'location not stated'} · "
          f"{job.field_category} · {', '.join(job.terms) or 'term not stated'}")
    if job.posted_at:
        print(f"  posted {job.posted_at:%Y-%m-%d}")
    for level, message in prepared.gates:
        print(f"  {'!' if level == 'warn' else '·'} {message}")

    brief = prepared.brief
    if brief:
        print(f"  keywords: {len(brief.matched)} matched"
              + (f", missing {', '.join(brief.missing[:6])}" if brief.missing else ""))

    item = DigestItem(job, resume=prepared.resume, cover=prepared.cover,
                      tailored=prepared.tailored)

    delivered = send([item], [], to=to or config.DIGEST_TO, now=now, dry_run=dry_run)

    # Recorded only on a real send, exactly as the digest does it, so a failed
    # send leaves the posting unsent rather than silently marked delivered.
    if delivered:
        with store.open_store() as db:
            db.record_sent([job.key], digest_id=now.strftime("%Y-%m-%d-manual"), now=now)

    print(f"\n  resume : {item.resume or 'none'}"
          f"{'' if item.tailored else ' (untailored fallback)'}")
    print(f"  cover  : {item.cover or 'none'}")
    print(f"  {'sent to ' + (to or config.DIGEST_TO) if delivered else 'not sent'}")
    print(f"  cost   : ${prepared.cost:.4f}"
          + ("  (subscription — no API spend)" if llm.using_cli() else ""))

    return 0 if (delivered or dry_run) else 1
