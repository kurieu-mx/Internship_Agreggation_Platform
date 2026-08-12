"""Rank the day's postings against the profile.

Two stages, for cost and for honesty about what each is good at.

**Stage one is deterministic.** Term, geography, category and work
authorisation are facts, not judgements, and a model adds nothing to checking
them except latency and expense. A cheap keyword-overlap score then orders
what survives, so the expensive stage only ever sees a shortlist. On a normal
day that is ~300 postings down to ~15.

**Stage two is a single model call.** Keyword overlap cannot tell that a
"Quantitative Developer" posting wanting C++ and low-latency systems is a
better fit than a "Data Analyst" posting that happens to say "Python" four
times. That judgement is the whole reason to spend money here, and it is asked
once for the whole batch rather than once per posting.

If the model is unavailable the deterministic order stands. A digest ranked by
keyword overlap is worse than one ranked by Claude, and far better than none.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

import config
import llm
from models import Job

log = logging.getLogger(__name__)

# How many survive the cheap stage and get the expensive one. Comfortably more
# than TOP_N so the model has genuine choices to make, not a rubber stamp.
RERANK_LIMIT = 15

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "rankings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "integer", "description": "the [n] label of the posting"},
                    "score": {"type": "integer", "description": "fit, 0-100"},
                    "why": {"type": "string", "description": "one sentence, concrete"},
                    "gaps": {"type": "string", "description": "what the profile lacks, or ''"},
                },
                "required": ["ref", "score", "why", "gaps"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rankings"],
    "additionalProperties": False,
}

SYSTEM = """You rank internship postings for one candidate.

Score 0-100 for how well this specific candidate fits, and how worth applying
the posting is for them. Be discriminating: a flat list of 80s is useless. Use
the whole range, and reserve above 85 for postings that genuinely play to the
candidate's demonstrated strengths.

Weigh, roughly in order:
  - overlap between what the role needs and what the candidate has actually built
  - whether the role is the kind of work they have done before
  - seniority fit: this is a student seeking a summer internship
  - employer scale and recognition (see below)
  - how concrete the posting is about the work

On employer scale: large, established employers count for something real here,
and it is not prestige for its own sake. This candidate needs visa sponsorship,
and big companies file H-1B petitions as routine where small firms often cannot;
they also run structured internship programmes with actual return offers. So a
major employer is worth a genuine lift - roughly the difference between an 80
and an 85 on comparable roles.

It is a tiebreaker, not an override. A well-matched engineering role at a firm
nobody has heard of still beats a poorly-matched one at a famous company, and a
recognisable name attached to work outside the candidate's field - retail
management, generic business operations, sales - deserves no lift at all.

`why` is one concrete sentence naming the specific overlap - not "strong fit".
`gaps` names what the candidate would be stretching on, or is empty."""


def _profile_tags(profile: dict) -> Dict[str, int]:
    """Every tag in the pool, weighted by how often it appears.

    A tag on many bullets is something the candidate has done repeatedly, and
    a posting matching it is a better signal than one matching a tag that
    appears once.
    """
    weights: Dict[str, int] = {}
    sections = [
        *(b for role in profile.get("experience", []) for b in role["bullets"]),
        *(b for project in profile.get("projects", []) for b in project["bullets"]),
        *profile.get("skills", []),
        *profile.get("summaries", []),
    ]
    for entry in sections:
        for tag in entry.get("tags", []) or []:
            weights[tag.lower()] = weights.get(tag.lower(), 0) + 1

    # Skill item names are strong signals too - "PyTorch" in a posting means
    # more than the generic tag "ml".
    for group in profile.get("skills", []):
        for item in group.get("items", []) or []:
            cleaned = re.sub(r"\(.*?\)", "", str(item)).strip().lower()
            if len(cleaned) > 1:
                weights[cleaned] = weights.get(cleaned, 0) + 2
    return weights


def keyword_score(job: Job, weights: Dict[str, int]) -> float:
    """Cheap overlap score between a posting and the profile's vocabulary."""
    haystack = f"{job.title} {job.field_category} {job.description}".lower()
    if not haystack.strip():
        return 0.0

    hits = 0
    for tag, weight in weights.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(tag)}(?![a-z0-9])", haystack):
            hits += weight
    # Titles carry far more signal per word than a 4,000-character description.
    title = job.title.lower()
    for tag, weight in weights.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(tag)}(?![a-z0-9])", title):
            hits += weight * 3
    return float(hits)


def recency_bonus(job: Job, now: Optional[datetime] = None) -> float:
    """Newer postings rank higher: applying early measurably matters."""
    if job.posted_at is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    posted = job.posted_at if job.posted_at.tzinfo else job.posted_at.replace(tzinfo=timezone.utc)
    hours = max((now - posted).total_seconds() / 3600, 0)
    return max(0.0, 12.0 - hours / 2)


def employer_bonus(job: Job) -> float:
    """Extra points for a large, well-known employer.

    Matched on a word boundary rather than a substring: "hp" must not fire on
    "SharpSpring", and "target" must not fire on "Targeted Therapeutics". The
    company name is checked, never the title - a posting mentioning Google in
    its tech stack is not a Google posting.

    Sized to be smaller than a strong keyword match on purpose. This exists so
    a recognised name reaches the rerank pool, not so it wins it: the model
    still ranks on fit, and a famous logo on an unrelated role should still
    lose to a good match at a company nobody has heard of.
    """
    name = (job.company or "").lower()
    if not name.strip():
        return 0.0

    def names(candidates) -> bool:
        return any(re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", name)
                   for c in candidates)

    if names(config.PRIORITY_EMPLOYERS_TIER1):
        return config.PRIORITY_BONUS_TIER1
    if names(config.PRIORITY_EMPLOYERS_TIER2):
        return config.PRIORITY_BONUS_TIER2
    return 0.0


def prefilter(jobs: Iterable[Job], profile: dict,
              limit: int = RERANK_LIMIT,
              categories: Optional[List[str]] = None,
              now: Optional[datetime] = None) -> List[Job]:
    """Cheap, deterministic narrowing. Sets ``job.score`` as a provisional rank."""
    categories = categories if categories is not None else config.TARGET_CATEGORIES
    weights = _profile_tags(profile)

    wanted = [
        job for job in jobs
        if not categories or any(c.lower() in job.field_category.lower() for c in categories)
    ]
    dropped = len(list(jobs)) - len(wanted) if hasattr(jobs, "__len__") else None

    for job in wanted:
        job.score = (keyword_score(job, weights)
                     + recency_bonus(job, now)
                     + employer_bonus(job))

    wanted.sort(key=lambda j: -j.score)
    kept = wanted[:limit]
    promoted = sum(1 for job in kept if employer_bonus(job))
    log.info("prefilter: %d postings in target categories%s, taking top %d "
             "(%d at priority employers)",
             len(wanted), f" (dropped {dropped})" if dropped else "",
             min(limit, len(wanted)), promoted)
    return kept


def _posting_block(index: int, job: Job) -> str:
    """One posting, compact. Descriptions are truncated: the first ~1,200
    characters carry the role, the rest is benefits boilerplate."""
    parts = [
        f"[{index}] {job.company} — {job.title}",
        f"    location: {', '.join(job.locations) or 'unspecified'}",
        f"    category: {job.field_category} | sponsorship: {job.sponsorship}",
    ]
    if job.description:
        body = re.sub(r"\s+", " ", job.description)[:1200]
        parts.append(f"    description: {body}")
    return "\n".join(parts)


def rerank(jobs: List[Job], profile: dict, profile_text: str = "",
           model: Optional[str] = None) -> List[Job]:
    """Score the shortlist with one model call. Returns them ranked."""
    if not jobs:
        return []
    if not llm.available():
        log.info("rerank: no model available - keeping the deterministic order")
        return jobs

    prompt = "\n\n".join(_posting_block(n, job) for n, job in enumerate(jobs))
    result = llm.complete_json(
        system=SYSTEM,
        prompt=f"Rank these {len(jobs)} postings.\n\n{prompt}",
        schema=SCORE_SCHEMA,
        model=model or config.MODEL_SCORING,
        cached_prefix=profile_text,
    )

    if not result or "rankings" not in result:
        log.warning("rerank: no usable response - keeping the deterministic order")
        return jobs

    by_ref = {r["ref"]: r for r in result["rankings"] if isinstance(r.get("ref"), int)}
    scored = 0
    for index, job in enumerate(jobs):
        ranking = by_ref.get(index)
        if not ranking:
            continue
        job.score = float(max(0, min(100, ranking.get("score", 0))))
        job.score_reason = (ranking.get("why") or "").strip()
        if ranking.get("gaps"):
            job.score_reason += f" Gaps: {ranking['gaps'].strip()}"
        scored += 1

    if scored < len(jobs):
        log.warning("rerank: model scored %d of %d postings", scored, len(jobs))

    jobs.sort(key=lambda j: -j.score)
    log.info("rerank: scored %d postings, top score %.0f", scored, jobs[0].score if jobs else 0)
    return jobs


def diversify(ranked: List[Job], top_n: int,
              max_per_company: Optional[int] = None) -> tuple:
    """Take the best ``top_n``, but not all from one employer.

    Observed live: a single company posted five roles in one day and took four
    of eight tailoring slots. That is a bad trade even when the scores are
    honest - eight applications to six companies beats eight to three, because
    the marginal fifth application to one employer is worth much less than a
    first application to another.

    Overflow is not discarded; it drops to the front of the also-ranked list,
    so a genuinely exceptional third role at one company is still one click
    away in the digest.

    When too few companies posted to fill ``top_n``, the digest is **short**
    rather than padded. Backfilling with a third and fourth role at the same
    employer would spend real money re-tailoring for a company already applied
    to, which is precisely the trade the cap exists to refuse. Five
    applications to five companies beats eight to four.
    """
    cap = max_per_company if max_per_company is not None else config.MAX_PER_COMPANY
    if cap <= 0:
        return ranked[:top_n], ranked[top_n:]

    chosen: List[Job] = []
    overflow: List[Job] = []
    seen: Dict[str, int] = {}

    for job in ranked:
        company = job.company.strip().lower()
        if len(chosen) < top_n and seen.get(company, 0) < cap:
            seen[company] = seen.get(company, 0) + 1
            chosen.append(job)
        else:
            overflow.append(job)

    companies = len({j.company.strip().lower() for j in chosen})
    if len(chosen) < top_n:
        log.info("shortlist: %d postings across %d companies - short of the %d "
                 "slots because only %d companies cleared the filters (cap %d each)",
                 len(chosen), companies, top_n, companies, cap)
    else:
        log.info("shortlist: %d postings across %d companies (cap %d/company)",
                 len(chosen), companies, cap)
    return chosen, overflow


def shortlist(jobs: Iterable[Job], profile: dict, profile_text: str = "",
              top_n: Optional[int] = None,
              now: Optional[datetime] = None) -> tuple:
    """The full ranking pass: prefilter, rerank, spread across employers."""
    top_n = top_n if top_n is not None else config.TOP_N
    candidates = prefilter(jobs, profile, now=now)
    ranked = rerank(candidates, profile, profile_text)
    return diversify(ranked, top_n)
