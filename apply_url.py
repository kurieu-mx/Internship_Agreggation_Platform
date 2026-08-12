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
from typing import Optional
from urllib.parse import urlparse

import config
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

    term = (result.get("term") or "").strip()

    return Job(
        company=company,
        title=title,
        locations=us_locations,
        url=url,
        description=description,
        field_category=categorize_title(title),
        terms=[term] if term else infer_terms(title, description),
        work_mode=infer_work_mode(us_locations),
        posted_at=_parse_date(result.get("posted_date") or ""),
        external_id=(urlparse(url).query or "")[:60],
        source="manual",
        provider_rank=5,          # hand-added: outranks every collected source
    )


def _report_gates(job: Job) -> None:
    """Run the same filters the digest runs, and say what they found.

    Reported rather than enforced. You asked for this specific posting, so the
    decision to apply anyway is yours - but you should know before the letter
    is written, not after the interview.
    """
    from eligibility import detect_restriction, requires_graduate_degree
    from sources.ats import looks_like_internship

    if not looks_like_internship(job.title):
        print("  ! this title does not read as an internship")

    if requires_graduate_degree(job):
        print("  ! this posting appears to require a graduate degree")

    status, reason = detect_restriction(job)
    job.sponsorship = status
    if status in config.EXCLUDE_SPONSORSHIP:
        print(f"  ! closed to applicants needing sponsorship — {reason}")
    elif status == "Yes":
        print(f"  · sponsorship offered — {reason}")


def run(url: str, dry_run: bool = False, to: Optional[str] = None,
        skip_cover: bool = False) -> int:
    """Fetch, tailor, and send one posting. Returns a process exit code."""
    import budget
    import store
    from delivery.email import DigestItem, send
    from digest import _slug, load_profile
    from tailor.cover import cover_letter
    from tailor.keywords import align
    from tailor.resume import tailored_resume
    from tailor.score import rerank

    if not urlparse(url).scheme.startswith("http"):
        log.error("that does not look like a URL: %s", url)
        return 2

    text = fetch_posting(url)
    if not text:
        log.error("could not read that page by any route")
        return 1

    job = extract(url, text)
    if job is None:
        return 1

    now = datetime.now(timezone.utc)
    out_dir = ROOT / "out" / now.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  {job.company} — {job.title}")
    print(f"  {', '.join(job.locations) or 'location not stated'} · "
          f"{job.field_category} · {', '.join(job.terms) or 'term not stated'}")
    if job.posted_at:
        print(f"  posted {job.posted_at:%Y-%m-%d}")
    _report_gates(job)

    profile = load_profile()
    profile_text = (ROOT / "profile" / "profile.yml").read_text()

    spend_before = budget.spent_today()

    # Scored for the same reason the digest scores: the email prints it, and a
    # posting showing 0/100 reads as a bad match rather than an unscored one.
    rerank([job], profile, profile_text)

    brief = align(job, profile)
    if brief:
        print(f"  keywords: {len(brief.matched)} matched"
              + (f", missing {', '.join(brief.missing[:6])}" if brief.missing else ""))

    stem = f"{_slug(job.company)}_{_slug(job.title)}"
    item = DigestItem(job)

    try:
        resume, tailored = tailored_resume(job, profile, out_dir / f"Resume_{stem}.pdf")
        item.resume, item.tailored = resume, tailored
    except Exception as exc:
        log.warning("no resume (%s): %s", type(exc).__name__, exc)

    if not skip_cover:
        item.cover = cover_letter(job, profile, out_dir / f"Cover_{stem}.pdf")

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
    print(f"  cost   : ${budget.spent_today() - spend_before:.4f}")

    return 0 if (delivered or dry_run) else 1
