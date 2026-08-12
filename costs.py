"""Measure what a day of digests actually costs.

Estimating tokens from character counts is guesswork; this counts them with
``messages.count_tokens`` against the real profile and a real posting, then
multiplies by the per-day call counts. ``count_tokens`` is free, so re-running
this after editing profile.yml or the prompts costs nothing.

    make costs

The prompt bodies below are representative rather than final - the scoring and
cover-letter steps are still being built - so treat the output as a close
bound, not a bill. The structure (which call, how many times, what is cached)
is what makes it accurate; the exact wording moves the total by a few percent.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional

import config

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent

# Anthropic list prices, $ per million tokens. Cached reads bill at 10% of
# input; a cache write bills at 125%.
PRICES: Dict[str, Dict[str, float]] = {
    "claude-opus-5":   {"in": 5.00, "out": 25.00},
    "claude-sonnet-5": {"in": 3.00, "out": 15.00},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
}

CACHE_READ = 0.10
CACHE_WRITE = 1.25


class Step:
    """One kind of call in a day's run."""

    def __init__(self, name: str, calls: int, prompt: str, cached_prompt: str = "",
                 output_tokens: int = 0, model: str = "claude-opus-5"):
        self.name = name
        self.calls = calls
        self.prompt = prompt
        self.cached_prompt = cached_prompt
        self.output_tokens = output_tokens
        self.model = model
        self.new_in = 0
        self.cached_in = 0


def _count(client, model: str, text: str) -> int:
    if not text:
        return 0
    return client.messages.count_tokens(
        model=model, messages=[{"role": "user", "content": text}]
    ).input_tokens


def build_steps(profile_text: str, posting: str, research_page: str,
                top_n: int) -> List[Step]:
    """The calls one digest makes, with representative prompt bodies."""
    rerank_batch = "\n\n".join(
        f"[{n}] Acme Corp | Software Engineer Intern | New York, NY | Software "
        f"Engineering | Summer 2027\n{posting[:600]}"
        for n in range(15)
    )

    return [
        Step(
            "rerank shortlist", 1,
            prompt=(
                "Score each posting 0-100 for fit against the candidate profile. "
                "Return JSON: [{id, score, why, gaps}].\n\n" + rerank_batch
            ),
            cached_prompt=profile_text,
            output_tokens=900,
            model=config.MODEL_SCORING,
        ),
        Step(
            "tailor resume", top_n,
            prompt=(
                "Select and lightly reword bullets from the pool for this posting. "
                "Return JSON: {summary_id, bullets:[{id, text}], skill_ids}. "
                "Do not invent experience.\n\n" + posting
            ),
            cached_prompt=profile_text,
            output_tokens=800,
            model=config.MODEL_TAILORING,
        ),
        Step(
            "company research", top_n,
            prompt=(
                "Extract verifiable facts about this company from the page below: "
                "what they build, recent notable work, engineering culture. "
                "Quote only what the text supports.\n\n" + research_page
            ),
            output_tokens=250,
            model=config.MODEL_RESEARCH,
        ),
        Step(
            "cover letter", top_n,
            prompt=(
                "Write a sectioned cover letter for this posting: hook, why-company, "
                "three what-I-bring items, two selected-work items, closing. Cite "
                "verified company facts.\n\n" + posting
            ),
            cached_prompt=profile_text,
            output_tokens=750,          # sectioned letter: measured ~700-800
            model=config.MODEL_LETTER,
        ),
    ]


def price(steps: List[Step], overrides: Optional[Dict[str, str]] = None) -> Dict[str, float]:
    """Total a day at list prices, honouring per-step model overrides."""
    overrides = overrides or {}
    totals = {"new_in": 0, "cached_in": 0, "out": 0, "cost": 0.0}

    for step in steps:
        model = overrides.get(step.name, step.model)
        rates = PRICES[model]

        new_in = step.new_in * step.calls
        cached_in = step.cached_in * step.calls
        out = step.output_tokens * step.calls

        cost = (
            new_in * rates["in"]
            + cached_in * rates["in"] * CACHE_READ
            + out * rates["out"]
        ) / 1_000_000
        # The cacheable prefix is written once per model per day, not per call.
        if step.cached_in:
            cost += step.cached_in * rates["in"] * CACHE_WRITE / 1_000_000

        totals["new_in"] += new_in
        totals["cached_in"] += cached_in
        totals["out"] += out
        totals["cost"] += cost

    return totals


def run(top_n: Optional[int] = None) -> int:
    import anthropic

    top_n = top_n or config.TOP_N
    model = "claude-opus-5"

    profile_text = (ROOT / "profile" / "profile.yml").read_text()

    # A real posting description, so the count reflects real inputs.
    posting = _sample_posting()
    # Careers pages vary wildly; 12k chars is a realistic full page.
    research_page = "Our engineering team builds distributed systems at scale. " * 210

    client = anthropic.Anthropic()
    steps = build_steps(profile_text, posting, research_page, top_n)

    print(f"\n  Measuring with {model}, TOP_N={top_n}\n")
    for step in steps:
        step.cached_in = _count(client, model, step.cached_prompt)
        step.new_in = _count(client, model, step.prompt)
        print(f"    {step.name:20s} x{step.calls:<2d}  "
              f"new {step.new_in:6,d}  cached {step.cached_in:6,d}  "
              f"out ~{step.output_tokens:,d}")

    print()
    scenarios = {
        "configured (.env)": {},
        "all Opus 5": {name: "claude-opus-5" for name in
                       ("rerank shortlist", "tailor resume",
                        "company research", "cover letter")},
        "Opus, research on Haiku": {"company research": "claude-haiku-4-5"},
        "Opus writing, Sonnet rest": {
            "rerank shortlist": "claude-sonnet-5",
            "company research": "claude-sonnet-5",
        },
        "all Sonnet 5": {name: "claude-sonnet-5" for name in
                         ("rerank shortlist", "tailor resume",
                          "company research", "cover letter")},
    }

    width = max(len(s) for s in scenarios)
    for label, overrides in scenarios.items():
        totals = price(steps, overrides)
        daily = totals["cost"]
        print(f"    {label.ljust(width)}   ${daily:5.2f}/day    "
              f"${daily * 30:6.2f}/month at 30 active days")

    totals = price(steps)
    print(f"\n    tokens/day: {totals['new_in']:,} new in, "
          f"{totals['cached_in']:,} cached in, {totals['out']:,} out")
    print(f"    calls/day:  {sum(s.calls for s in steps)}")
    print("\n  Quiet days cost nothing: no fresh postings means no tailoring calls.")
    print(f"  Cost scales linearly with TOP_N (currently {top_n}).\n")
    return 0


def _sample_posting() -> str:
    """A real posting description if one can be fetched, else a stand-in."""
    try:
        from sources.ats import Board
        from sources.greenhouse import GreenhouseSource

        jobs = GreenhouseSource(boards=[Board("imc", "IMC Trading")]).scrape()
        described = [j for j in jobs if j.description]
        if described:
            job = described[0]
            return f"{job.company} | {job.title} | {', '.join(job.locations)}\n\n{job.description}"
    except Exception as exc:
        log.debug("could not fetch a live posting (%s); using a stand-in", exc)
    return "Software Engineer Intern, Summer 2027. " * 90
