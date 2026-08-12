"""Claude access for the scoring and tailoring steps.

Deliberately *not* named ``anthropic.py`` - a module of that name shadows the
SDK package and breaks its own import, which this project already learned once
the hard way with ``composio_client.py``.

Three things live here because all three callers need them identically:

* **A cached profile prefix.** ``profile.yml`` is ~4.8k tokens and rides on 17
  of the 25 calls a digest makes. Sent as a cache-controlled system block it
  bills at 10% after the first write, which is most of the difference between
  $0.54 a day and $2.
* **Structured output.** Every call here wants JSON with a known shape, so the
  schema is enforced by the API rather than parsed hopefully out of prose.
* **Fail-soft.** A model call that errors returns None. The caller then falls
  back - to deterministic ordering, or to the untailored master resume - which
  is always better than no digest.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import config

log = logging.getLogger(__name__)

_client = None


def using_cli() -> bool:
    """True when calls should go through the Claude Code CLI instead of the API.

    Selected by ``LLM_BACKEND``. The default stays ``api`` so the digest and
    its CI workflow are unaffected - GitHub Actions has no interactive login,
    so the CLI backend is not available there. The dashboard sets it to
    ``cli`` for itself.
    """
    return config.LLM_BACKEND == "cli"


def get_client():
    """Cached Anthropic client, or None if the key is missing or bad."""
    global _client
    if _client is not None:
        return _client
    if not config.ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY is not set - scoring and tailoring will be skipped")
        return None
    try:
        import anthropic

        _client = anthropic.Anthropic()
    except Exception as exc:
        log.warning("could not create the Anthropic client: %s", exc)
        return None
    return _client


def available() -> bool:
    if using_cli():
        import llm_cli

        return llm_cli.available()
    return get_client() is not None


# Models that accept `output_config.effort`. Haiku 4.5 and the 4.5-generation
# Sonnet reject it with a 400 rather than ignoring it, so this is an allowlist
# and anything unrecognised is assumed not to support it - a missing effort
# setting costs a little quality, a rejected request costs the whole call.
_EFFORT_MODELS = (
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5", "claude-mythos-5",
)


def _supports_effort(model: str) -> bool:
    return any(model.startswith(known) for known in _EFFORT_MODELS)


def _estimated_cost(model: str, max_tokens: int) -> float:
    """A rough upper bound on the next call, for the pre-flight budget check.

    Assumes the response uses a quarter of ``max_tokens`` and the prompt is
    comparable - crude, but it only has to be the right order of magnitude to
    stop the cap being breached by one large call.
    """
    import budget

    rates = budget.PRICES.get(model, budget._FALLBACK)
    return (max_tokens * 0.25 * rates["out"] + 8000 * rates["in"]) / 1_000_000


def complete_json(system: str, prompt: str, schema: Dict[str, Any],
                  model: Optional[str] = None,
                  cached_prefix: str = "",
                  max_tokens: int = 16000,
                  effort: Optional[str] = None) -> Optional[Any]:
    """One structured call. Returns the parsed object, or None on any failure.

    ``cached_prefix`` is sent as its own system block with ``cache_control``,
    so it must be byte-identical between calls to be reused - which is why it
    is passed separately rather than concatenated into ``system`` by the
    caller, where a stray timestamp could silently defeat the cache.
    """
    # Checked first so the budget gate below is not consulted for calls that
    # cannot spend anything. Letting dashboard traffic accumulate against the
    # daily cap would starve the digest of the calls the cap exists to protect.
    if using_cli():
        import llm_cli

        return llm_cli.complete_json(
            system=system, prompt=prompt, schema=schema, model=model,
            cached_prefix=cached_prefix, max_tokens=max_tokens, effort=effort,
        )

    client = get_client()
    if client is None:
        return None

    # Checked before the call, so the cap binds rather than being noticed one
    # call late. A breach is not an error to propagate - the caller's fallback
    # (deterministic ranking, untailored master) is exactly the right response.
    import budget

    try:
        budget.check(headroom=_estimated_cost(model or config.MODEL_SCORING, max_tokens))
    except budget.BudgetExceeded as exc:
        log.warning("%s - skipping this call", exc)
        return None

    # `effort` is not universal: Haiku 4.5 rejects it outright with a 400, and
    # sending it there silently disabled every research call - the fetch
    # succeeded, the extraction 400'd, and each cover letter was written with
    # zero company facts. The failure was invisible because "no facts" is also
    # a legitimate outcome.
    output_config: Dict[str, Any] = {
        "format": {"type": "json_schema", "schema": schema}
    }
    if _supports_effort(model or config.MODEL_SCORING):
        output_config["effort"] = effort or config.MODEL_EFFORT

    blocks: List[Dict[str, Any]] = []
    if cached_prefix:
        blocks.append({
            "type": "text",
            "text": cached_prefix,
            "cache_control": {"type": "ephemeral"},
        })
    blocks.append({"type": "text", "text": system})

    try:
        response = client.messages.create(
            model=model or config.MODEL_SCORING,
            max_tokens=max_tokens,
            system=blocks,
            messages=[{"role": "user", "content": prompt}],
            output_config=output_config,
        )
    except Exception as exc:
        log.warning("model call failed (%s): %s", type(exc).__name__, exc)
        return None

    # Metered from the response's own token counts, before anything else can
    # go wrong with parsing - a call that cost money must be recorded as
    # having cost money even if its output turns out to be unusable.
    spent = budget.record(model or config.MODEL_SCORING, response.usage)
    log.debug("call cost $%.4f (%s)", spent, budget.status())

    # Safety classifiers can decline a request: HTTP 200, empty content.
    if response.stop_reason == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", None)
        log.warning("model declined the request (%s)", category or "no category given")
        return None

    if response.stop_reason == "max_tokens":
        log.warning("response hit max_tokens and is probably truncated")

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        log.warning("model returned no text")
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("model returned unparseable JSON: %s", exc)
        return None


def log_usage(response) -> None:
    """Report cache effectiveness. Zero reads across a run means it broke."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    log.debug(
        "tokens: %s new, %s cache-read, %s cache-write, %s out",
        getattr(usage, "input_tokens", "?"),
        getattr(usage, "cache_read_input_tokens", "?"),
        getattr(usage, "cache_creation_input_tokens", "?"),
        getattr(usage, "output_tokens", "?"),
    )
