"""Cover letters that say something true about the company.

A cover letter is only worth sending if it could not have been sent to anyone
else. That means naming something specific and real about the employer - which
is exactly the thing a language model will happily invent when it has nothing
to go on.

So this never asks a model what a company does. It fetches the company's own
pages, extracts claims *from that text*, and then **validates every claim back
against the source** before any of it reaches a letter. A claim that cannot be
traced to fetched text is dropped, not softened. If nothing survives, the
letter is written without a company-specific hook rather than with a
fabricated one - the same discipline ``enrichment.py`` applies to Ollama
output and ``render.py`` applies to resume bullets.

The research step runs on the cheap model (see ``config.MODEL_RESEARCH``): it
is extraction, and its output is checked. The letter itself runs on the good
one, because a human reads it.
"""

import logging
import re
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import config
import llm
from models import Job

log = logging.getLogger(__name__)

RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string",
                              "description": "one specific factual sentence about the company"},
                    "evidence": {"type": "string",
                                 "description": "the exact phrase from the source that supports it"},
                },
                "required": ["claim", "evidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["facts"],
    "additionalProperties": False,
}

RESEARCH_SYSTEM = """You extract verifiable facts about a company from its own pages.

Return only claims the supplied text directly supports. For each, quote the
exact phrase from the source that supports it in `evidence` - copied verbatim,
not paraphrased. A claim you cannot quote support for must not be returned.

Prefer specifics an applicant could reference credibly: what the company
builds, the technical problems it works on, how its engineering is organised.
Skip marketing language, awards, and benefits.

Return at most four facts. Returning none is correct if the text supports none."""

LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {
            "type": "string",
            "description": "One or two sentences. The opening, and the sharpest "
                           "thing you can say about why this specific role.",
        },
        "why_company": {
            "type": "string",
            "description": "Two to four sentences. Something specific and true "
                           "about the company, connected to this posting's work.",
        },
        "what_i_bring": {
            # No minItems/maxItems: the API rejects array length constraints
            # above 1. The count is asked for in the prompt and enforced in
            # code once the response is back - see write().
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string",
                              "description": "3-6 words naming the capability"},
                    "detail": {"type": "string",
                               "description": "One or two sentences of real evidence"},
                },
                "required": ["title", "detail"],
                "additionalProperties": False,
            },
        },
        "selected_work": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string",
                            "description": "id of the project or role it comes from"},
                    "name": {"type": "string", "description": "its name"},
                    "detail": {"type": "string",
                               "description": "One or two sentences, relevant to this posting"},
                },
                "required": ["ref", "name", "detail"],
                "additionalProperties": False,
            },
        },
        "closing": {"type": "string", "description": "One or two sentences."},
    },
    "required": ["hook", "why_company", "what_i_bring", "selected_work", "closing"],
    "additionalProperties": False,
}

LETTER_SYSTEM = """You write one cover letter for one internship application.

It is laid out as sections rather than prose paragraphs, so each section must
carry its own weight - no throat-clearing, no restating the section before it.

The whole document must read like it could not have been sent to any other
company. Generic enthusiasm is worse than nothing.

SECTIONS

`hook` - open with substance. Never "I am writing to express my interest".
Name the work, not the feeling.

`why_company` - the section that makes this letter specific. Use the supplied
verified facts and connect one of them to *this posting's* actual work. If no
facts are supplied, write about the problem the role addresses instead - do not
speculate about the company, and do not praise it in the abstract.

`what_i_bring` - exactly three. Each `title` names a capability the posting
actually asks for; each `detail` gives real evidence from the candidate's
experience, with the specific system, language, or number. Order them by how
much this posting cares, not by how proud the candidate is.

`selected_work` - exactly two projects or roles, the two most relevant to this
posting. `ref` must be the id of a real project or role from the profile.
`detail` explains it in terms of what this posting needs.

`closing` - brief and concrete. No "I look forward to hearing from you".

RULES

- Every factual claim about the candidate must come from the profile. No
  invented projects, metrics, scale, or familiarity with a technology.
- Every factual claim about the company must come from the supplied facts.
- Specific beats impressive. A number, a system name, a real constraint.
- No superlatives about the company. No "passionate", "excited to", "thrilled".
- It must fit on one page, so be economical: this is a page with structure,
  not a page with more words.

Address the hiring team, not a named individual."""


def _candidate_urls(job: Job) -> List[str]:
    """Where to look for something true about this company.

    The posting URL's own host is the best source: an ATS-hosted board sits on
    the company's domain often enough to be worth trying, and when it does not
    the fetch simply fails and we move on.
    """
    urls: List[str] = []
    if job.url:
        try:
            host = (urlparse(job.url).hostname or "").lower()
        except ValueError:
            host = ""
        if host and not any(
            ats in host for ats in ("greenhouse.io", "lever.co", "ashbyhq.com",
                                    "myworkdayjobs.com", "simplify.jobs",
                                    "icims.com", "jobvite.com", "oraclecloud.com")
        ):
            # Strip the recruiting subdomain: apply.deloitte.com is a job
            # application host and says nothing about the company, while
            # deloitte.com does. Keep the registrable domain and try its
            # about page first, which is where the substance lives.
            bare = host[4:] if host.startswith("www.") else host
            parts = bare.split(".")
            root = ".".join(parts[-2:]) if len(parts) > 2 else bare
            urls += [f"https://{root}/about", f"https://{root}/",
                     f"https://www.{root}/"]

    # Suffix-stripped first: "Quantbot Technologies" is quantbot.com, not
    # quantbottechnologies.com. See branding.company_domains.
    from tailor.branding import company_domains

    for domain in company_domains(job.company):
        if not any(domain in existing for existing in urls):
            urls += [f"https://{domain}/about", f"https://{domain}/"]

    # The posting page itself, last, and only when we do not already hold its
    # text. It is not "about the company", but it describes the team and the
    # problem in the company's own words - and it is the one URL guaranteed to
    # exist when a marketing site sits behind a redirect the fetcher cannot
    # follow (deloitte.com/about returns nothing; www2.deloitte.com is where
    # their content lives).
    #
    # Skipped when `description` is already populated, which is the case for
    # every ATS posting: re-fetching a page whose text is already in memory
    # spends a Composio call to learn nothing.
    if job.url and not (job.description or "").strip():
        urls.append(job.url)

    # Preserve order, drop repeats.
    return list(dict.fromkeys(urls))


def fetch_pages(job: Job, max_chars: int = 12000) -> str:
    """Fetch the company's own text via Composio, or return empty.

    Empty is a perfectly acceptable outcome - it means the letter goes out
    without a company hook, which is far better than one with an invented fact.
    """
    from composio_gateway import available, execute

    if not available():
        log.debug("no Composio - skipping research for %s", job.company)
        return ""

    collected: List[str] = []
    for url in _candidate_urls(job):
        payload = execute(config.FETCH_URL_SLUG, {"url": url})
        if not payload:
            continue
        text = _text_from(payload)
        if text.strip():
            collected.append(re.sub(r"\s+", " ", text))
        if sum(len(c) for c in collected) >= max_chars:
            break

    return " ".join(collected)[:max_chars]


def _text_from(payload) -> str:
    """Pull page text out of whatever shape the fetch tool returned.

    Composio's fetch wraps pages in ``{"results": [{"text": ..., "title": ...}]}``
    rather than putting the body at the top level, and it has used flatter
    shapes before. Reading only the top level silently produced zero facts on
    every company - the fetch succeeded and the text was simply never found.
    """
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""

    for key in ("content", "text", "markdown", "body", "raw"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    collected = []
    for key in ("results", "data", "items", "pages"):
        entries = payload.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                title = entry.get("title") or ""
                body = _text_from(entry)
                if body:
                    collected.append(f"{title}\n{body}" if title else body)
            elif isinstance(entry, str):
                collected.append(entry)
    return "\n\n".join(collected)


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())


def validate_facts(facts: List[dict], source: str, min_overlap: float = 0.7) -> List[str]:
    """Keep only claims whose quoted evidence really appears in the source.

    Checked by word overlap rather than exact substring: a model reproducing a
    quote will occasionally normalise whitespace or punctuation, and failing
    those would throw away good facts. It will not, however, reconstruct a
    sentence that was never there - so a low overlap means the evidence was
    invented, and the claim goes with it.
    """
    haystack = set(_normalise(source).split())
    if not haystack:
        return []

    kept: List[str] = []
    for fact in facts:
        claim = (fact.get("claim") or "").strip()
        evidence = (fact.get("evidence") or "").strip()
        if not claim or not evidence:
            continue

        words = [w for w in _normalise(evidence).split() if len(w) > 2]
        if not words:
            continue
        overlap = sum(1 for w in words if w in haystack) / len(words)
        if overlap >= min_overlap:
            kept.append(claim)
        else:
            log.info("dropped an unsupported claim about the company "
                     "(evidence %.0f%% traceable): %s", overlap * 100, claim[:80])
    return kept


def research(job: Job, model: Optional[str] = None) -> List[str]:
    """Facts about the company that its own pages support. Possibly none."""
    source = fetch_pages(job)
    if not source:
        return []

    result = llm.complete_json(
        system=RESEARCH_SYSTEM,
        prompt=f"Company: {job.company}\n\nSource text:\n{source}",
        schema=RESEARCH_SCHEMA,
        model=model or config.MODEL_RESEARCH,
        max_tokens=4000,
    )
    if not result:
        return []

    facts = validate_facts(result.get("facts", []), source)
    log.info("research: %d verified fact(s) about %s", len(facts), job.company)
    return facts


def write(job: Job, profile: dict, facts: List[str],
          model: Optional[str] = None) -> Optional[dict]:
    """Draft the letter. Returns its paragraphs, or None."""
    if not llm.available():
        return None

    fact_block = (
        "VERIFIED FACTS ABOUT THE COMPANY (use these; do not add others):\n"
        + "\n".join(f"  - {f}" for f in facts)
    ) if facts else (
        "NO VERIFIED COMPANY FACTS ARE AVAILABLE. Write about the role itself "
        "and do not speculate about the company."
    )

    from tailor.resume import _pool_block, _posting_block

    letter = llm.complete_json(
        system=LETTER_SYSTEM,
        prompt=(f"{_posting_block(job)}\n\n{fact_block}\n\n"
                "Return exactly 3 `what_i_bring` items and exactly 2 "
                "`selected_work` items."),
        schema=LETTER_SCHEMA,
        model=model or config.MODEL_TAILORING,
        cached_prefix=_pool_block(profile),
        max_tokens=8000,
    )
    if not letter:
        return None

    # The counts cannot be expressed in the schema (the API rejects array
    # length constraints above 1), so they are trimmed here. Too few is left
    # alone: the layout handles two rows or one card without breaking, and a
    # short section beats a padded one.
    letter["what_i_bring"] = (letter.get("what_i_bring") or [])[:3]
    letter["selected_work"] = (letter.get("selected_work") or [])[:2]
    return letter


def ground_selected_work(letter: dict, profile: dict) -> dict:
    """Replace the model's project names and stacks with the profile's own.

    The model picks *which* two pieces of work to show and writes why they are
    relevant; it does not get to name them or restate their tech stack, because
    those are facts. Anything whose ``ref`` does not resolve is dropped rather
    than printed under a made-up name.
    """
    known = {}
    for project in profile.get("projects", []):
        known[project["id"]] = (project["name"], project.get("stack", ""))
        known[project["name"].lower()] = (project["name"], project.get("stack", ""))
    for role in profile.get("experience", []):
        label = f"{role['title']}, {role['org']}"
        known[role["id"]] = (label, role.get("dates", ""))
        known[role["org"].lower()] = (label, role.get("dates", ""))
        # The model reasonably cites a *bullet* id when the bullet is the
        # relevant thing; resolve those to the role or project they belong to
        # rather than dropping an otherwise good entry.
        for bullet in role.get("bullets", []):
            known.setdefault(bullet["id"], (label, role.get("dates", "")))
    for project in profile.get("projects", []):
        for bullet in project.get("bullets", []):
            known.setdefault(bullet["id"],
                             (project["name"], project.get("stack", "")))

    grounded = []
    for item in letter.get("selected_work", []):
        ref = str(item.get("ref", "")).strip()
        match = known.get(ref) or known.get(ref.lower()) or \
            known.get(str(item.get("name", "")).lower())
        if not match:
            log.info("dropped a 'selected work' entry with no profile match: %r", ref)
            continue
        grounded.append({"name": match[0], "stack": match[1],
                         "detail": item.get("detail", "")})

    letter["selected_work"] = grounded
    return letter


def tagline_for(job, profile: dict) -> str:
    """The one-line descriptor under the name, angled at this posting.

    Short by design: it sits under a 17pt name and competes with a logo, so a
    full degree title with punctuation reads as clutter. Degree abbreviation
    plus school is enough - the resume carries the rest.
    """
    school = (profile.get("education") or [{}])[0]
    degree = str(school.get("degree", ""))

    level = "B.S.E. Data Science" if "B.S.E" in degree else (
        degree.split(",")[0].split(".")[0].strip() or "Undergraduate"
    )
    where = school.get("school", "")
    field = re.sub(r"\s*/\s*", " · ", (job.field_category or "").strip())

    parts = [p for p in (field, f"{level}, {where}".strip(", ")) if p]
    return "  ·  ".join(parts)


def relevant_skills(job, profile: dict, limit: int = 12) -> str:
    """A compact toolkit line, ordered by what this posting actually mentions.

    Drawn from the profile's own skill groups - nothing is added - and ordered
    so a reader scanning for their stack finds it in the first few items.
    """
    haystack = f"{job.title} {job.field_category} {job.description}".lower()

    scored = []
    for group in profile.get("skills", []):
        for item in group.get("items", []) or []:
            name = str(item)
            bare = re.sub(r"\(.*?\)", "", name).strip()
            hit = bool(bare) and bare.lower() in haystack
            scored.append((0 if hit else 1, bare or name))

    seen, ordered = set(), []
    for _, name in sorted(scored, key=lambda pair: pair[0]):
        if name.lower() not in seen:
            seen.add(name.lower())
            ordered.append(name)
    return "  ·  ".join(ordered[:limit])


# -- rendering ---------------------------------------------------------------

# A small, stable palette. The accent is derived from the company name so it is
# consistent for a given employer across runs, without pretending to be their
# actual brand colour - guessing that from a favicon looks worse when wrong
# than a considered neutral does.
_ACCENTS = [
    "#1f3a5f", "#2c5545", "#5a3a5f", "#6b3a2e", "#2e4a5a",
    "#4a3f6b", "#3f5a2e", "#5f4a1f", "#2f4f4f", "#4b2e4b",
]


def accent_for(company: str) -> str:
    return _ACCENTS[sum(ord(c) for c in company.lower()) % len(_ACCENTS)]


def monogram(company: str) -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", company) if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def render_cover(job: Job, profile: dict, letter: dict, destination: Path,
                 max_pages: int = 1, brand=None) -> Path:
    """Render the letter, enforcing the same one-page rule as the resume.

    Unlike the resume, the overflow fix here is typographic rather than
    editorial: the sections are all load-bearing, so a letter that runs a few
    lines long is set slightly tighter rather than having a section cut. Three
    steps, then it fails - past that the page stops being readable and a
    two-page letter would be the better outcome anyway.
    """
    from tailor.branding import for_company
    from tailor.render import RenderError, _environment, html_to_pdf, page_count

    letter = ground_selected_work(dict(letter), profile)
    brand = brand if brand is not None else for_company(job.company, job.url)
    template = _environment().get_template("cover.html.j2")

    pdf = None
    for scale in (1.0, 0.94, 0.88):
        html = template.render(
            profile=profile, job=job, letter=letter, brand=brand,
            tagline=tagline_for(job, profile),
            skills=relevant_skills(job, profile),
            scale=scale,
        )
        pdf = html_to_pdf(html, destination)
        pages = page_count(pdf)
        if pages <= max_pages:
            if scale < 1.0:
                log.info("%s: set at %.0f%% to fit one page",
                         destination.name, scale * 100)
            return pdf
        log.debug("%s: %d pages at %.0f%%", destination.name, pages, scale * 100)

    raise RenderError(
        f"{destination.name}: still {page_count(pdf)} pages at the tightest setting"
    )


def cover_letter(job: Job, profile: dict, destination: Path) -> Optional[Path]:
    """Research, write, render. Returns None if it could not be produced.

    A missing cover letter is survivable - the digest still carries the link
    and the tailored resume - so every failure here degrades rather than
    raising.
    """
    try:
        facts = research(job)
        letter = write(job, profile, facts)
        if not letter:
            log.warning("no cover letter produced for %s", job.company)
            return None
        return render_cover(job, profile, letter, destination)
    except Exception as exc:
        log.warning("cover letter for %s failed (%s): %s",
                    job.company, type(exc).__name__, exc)
        return None
