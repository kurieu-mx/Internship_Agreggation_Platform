"""Choose which bullets go on one resume, for one posting.

The model's job here is narrow on purpose: **select, order, and lightly
reword** entries that already exist in the pool. It is not writing a resume. It
cannot add a role, invent a metric, or claim a technology that is not already
somewhere in ``profile.yml``.

That narrowness is enforced downstream rather than trusted here.
``render.py`` rejects any bullet whose ``id`` is not in the pool, and any whose
wording has drifted so far from its source that it no longer describes the same
work. So the worst a bad response can do is fail validation and fall back to
the untailored master - never quietly ship a fabricated resume.

The page is the real constraint. One page fits roughly 11-13 bullets at this
layout, so the model is asked for a budget it can actually spend, and the
caller retries with a smaller budget if the render comes out long.
"""

import logging
from typing import List, Optional

import config
import llm
from models import Job
from tailor.render import RenderError, Selection, select_by_ids

log = logging.getLogger(__name__)

# What fits on one page at this layout. Starting point, not a guarantee -
# render_resume is the authority and the caller steps down on failure.
DEFAULT_BUDGET = 12
MIN_BUDGET = 8

SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_id": {"type": "string", "description": "id of the best-fitting summary"},
        "skill_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "skill group ids to show, most relevant first, max 3",
        },
        "bullets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "id of a bullet in the pool"},
                    "text": {"type": "string",
                             "description": "the bullet, optionally reworded for this posting"},
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary_id", "skill_ids", "bullets"],
    "additionalProperties": False,
}

SYSTEM = """You tailor one resume for one job posting.

You are given a pool of the candidate's real experience. Your job is to choose
which of it to show, in what order, and to reword it for this posting.

Hard rules, in order of importance:

1. Never invent. Every bullet you return must carry the `id` of a pool entry,
   and its text must describe the same work that entry describes. You may
   rephrase, re-emphasise, and trim. You may not add a technology, a metric, a
   scale, or an outcome that is not already in the source bullet.
2. Stay inside the budget. Returning more bullets than asked for makes the
   resume spill to a second page, and it will be rejected.
3. Lead with what this posting cares about. Within each role, the most
   relevant bullet goes first.
4. Keep every role that still has a bullet. Do not drop a job to make room -
   drop its weakest bullet instead. An employment gap reads worse than a
   thinner entry.
5. Rewording should be surgical. If a bullet already fits, return it
   unchanged. Prefer swapping emphasis over rewriting a sentence.

Choose the summary and skill groups that fit the posting, by id."""


def _pool_block(profile: dict) -> str:
    """The pool, rendered for the prompt. Sent as the cached prefix."""
    lines = ["SUMMARIES:"]
    for summary in profile.get("summaries", []):
        lines.append(f"  {summary['id']} [{', '.join(summary.get('tags', []))}]: "
                     f"{summary['text'][:200]}")

    lines.append("\nSKILL GROUPS:")
    for group in profile.get("skills", []):
        lines.append(f"  {group['id']}: {group['label']} — "
                     f"{', '.join(str(i) for i in group.get('items', []))}")

    lines.append("\nEXPERIENCE BULLETS:")
    for role in profile.get("experience", []):
        lines.append(f"  {role['title']} at {role['org']} ({role['dates']}):")
        for bullet in role["bullets"]:
            lines.append(f"    {bullet['id']} [{', '.join(bullet.get('tags', []))}]: "
                         f"{bullet['text']}")

    lines.append("\nPROJECT BULLETS:")
    for project in profile.get("projects", []):
        lines.append(f"  {project['name']} ({project['stack']}):")
        for bullet in project["bullets"]:
            lines.append(f"    {bullet['id']} [{', '.join(bullet.get('tags', []))}]: "
                         f"{bullet['text']}")
    return "\n".join(lines)


def _posting_block(job: Job) -> str:
    parts = [
        f"COMPANY: {job.company}",
        f"ROLE: {job.title}",
        f"LOCATION: {', '.join(job.locations) or 'unspecified'}",
        f"CATEGORY: {job.field_category}",
    ]
    if job.description:
        parts.append(f"\nPOSTING:\n{job.description[:6000]}")
    if job.score_reason:
        parts.append(f"\nWHY THIS WAS SHORTLISTED: {job.score_reason}")
    return "\n".join(parts)


def choose(job: Job, profile: dict, budget: int = DEFAULT_BUDGET,
           model: Optional[str] = None) -> Optional[Selection]:
    """Ask the model which bullets to show. Returns None if it cannot.

    A None return is not an error condition the caller should abort on - it
    means "use the untailored master for this company", which is a perfectly
    good application.
    """
    if not llm.available():
        return None

    result = llm.complete_json(
        system=SYSTEM,
        prompt=(
            f"Tailor the resume for this posting. Return at most {budget} bullets "
            f"in total, and at most 3 skill groups.\n\n{_posting_block(job)}"
        ),
        schema=SELECTION_SCHEMA,
        model=model or config.MODEL_TAILORING,
        cached_prefix=_pool_block(profile),
    )
    if not result:
        return None

    bullets = [b for b in result.get("bullets", [])
               if isinstance(b, dict) and b.get("id") and b.get("text")]
    if not bullets:
        log.warning("tailoring for %s returned no usable bullets", job.company)
        return None

    if len(bullets) > budget:
        log.info("tailoring for %s returned %d bullets over a budget of %d - trimming",
                 job.company, len(bullets), budget)
        bullets = bullets[:budget]

    selection = select_by_ids(
        profile,
        bullet_ids=[b["id"] for b in bullets],
        summary_id=result.get("summary_id"),
        skill_ids=(result.get("skill_ids") or [])[:3] or None,
    )

    # Carry the model's rewording across onto the pool-ordered selection. Any
    # id it made up simply has nothing to attach to and is dropped here, before
    # the render even sees it.
    reworded = {b["id"]: b["text"].strip() for b in bullets}
    for group in list(selection.experience) + list(selection.projects):
        for bullet in group["bullets"]:
            if reworded.get(bullet["id"]):
                bullet["text"] = reworded[bullet["id"]]

    unknown = set(reworded) - set(selection.bullet_ids())
    if unknown:
        log.warning("tailoring for %s cited %d bullet id(s) not in the pool: %s",
                    job.company, len(unknown), ", ".join(sorted(unknown)))

    return selection


def tailored_resume(job: Job, profile: dict, destination,
                    model: Optional[str] = None):
    """Tailor, render, and validate - stepping down the budget if it spills.

    Returns ``(path, was_tailored)``. Falls back to the untailored master
    rather than failing: a company that scored into the top eight deserves an
    application even when the tailoring pass misbehaves.
    """
    from tailor.render import full_selection, master_selection, render_resume

    budget = DEFAULT_BUDGET
    last_error = None

    while budget >= MIN_BUDGET:
        selection = choose(job, profile, budget=budget, model=model)
        if selection is None:
            break
        try:
            return render_resume(profile, selection, destination), True
        except RenderError as exc:
            last_error = exc
            # A page overflow is worth one retry with less to fit; a
            # provenance or fidelity failure will not improve with a smaller
            # budget, so do not spend another call on it.
            if "pages" not in str(exc):
                log.warning("tailoring for %s rejected: %s", job.company, exc)
                break
            budget -= 2
            log.info("tailored resume for %s ran long - retrying with %d bullets",
                     job.company, budget)

    if last_error:
        log.warning("falling back to the untailored resume for %s (%s)",
                    job.company, last_error)
    else:
        log.info("falling back to the untailored resume for %s", job.company)

    fallback = master_selection(profile) if profile.get("master_layout") \
        else full_selection(profile)
    return render_resume(profile, fallback, destination), False
