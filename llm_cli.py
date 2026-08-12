"""Claude access through the Claude Code CLI, billed to the Max subscription.

Why this exists
---------------
``llm.py`` talks to the Anthropic API, which bills per token. Measured from
the spend table, a digest day is ~80 calls and ~$3.00, and one hand-added
posting through ``--apply-url`` is six calls and ~$0.25-0.30. That is the
right trade for the 3pm digest, which runs once. It is the wrong trade for a
dashboard where you paste links all afternoon.

The Claude Code CLI in headless mode (``claude -p``) runs against the Max
subscription rather than API credits, so the same six calls cost nothing
marginal. This module is a drop-in backend for ``llm.complete_json`` - same
signature, same fail-soft ``None`` contract - so the six call sites in
``apply_url``, ``tailor.score``, ``tailor.resume`` and ``tailor.cover`` do not
know which one they are talking to.

Three things differ from the API backend, and all three are handled here:

* **An API key silently wins.** If ``ANTHROPIC_API_KEY`` is set, the CLI uses
  it and bills credits - the subscription login is ignored entirely. Since
  ``config`` loads ``.env``, which sets that key for the digest, the variable
  is stripped from the subprocess environment. This is the same class of
  problem as the ``PYTHONPATH`` unset in the Makefile: an inherited variable
  quietly changing what runs.
* **The schema is not enforced.** The API constrains output to the JSON schema;
  the CLI can only be asked nicely. So the response is validated here and one
  retry is spent naming what was wrong. Two failures return None, and the
  caller's existing fallback - deterministic ranking, the untailored master -
  is exactly the right response, as it already is when the model is
  unreachable.
* **Spend is not metered.** ``budget.py`` guards a real bill; these calls do
  not produce one. Recording them would let dashboard use exhaust the cap that
  protects the digest, so the spend table is left alone deliberately. The
  notional cost the CLI reports is logged for visibility, not billed.
"""

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

import config

log = logging.getLogger(__name__)

# Long enough for an Opus letter with thinking, short enough that a wedged
# subprocess surfaces as a failed call rather than a hung dashboard request.
TIMEOUT_SECONDS = 300

# Claude Code ships a large system prompt and a full tool set on every
# invocation. None of it is useful for a single structured extraction, and the
# tools are actively unwanted - this is one JSON answer, not an agent loop.
_DISABLED_TOOLS = (
    "Bash", "Read", "Write", "Edit", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "NotebookEdit",
)

_SCHEMA_RULES = (
    "You return exactly one JSON object and nothing else. No prose, no "
    "explanation, no markdown code fences. The object must match the JSON "
    "schema given in the message, including every required property."
)


def available() -> bool:
    """True if the CLI is installed and usable."""
    return shutil.which("claude") is not None


def _environment() -> Dict[str, str]:
    """The subprocess environment, with the API key removed.

    A set ``ANTHROPIC_API_KEY`` takes precedence over the claude.ai login, so
    leaving it in place would route these calls back through paid credits -
    silently, because the call still succeeds. The whole point of this backend
    is that it does not.
    """
    env = dict(os.environ)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(name, None)
    return env


def _validate(result: Any, schema: Dict[str, Any]) -> List[str]:
    """Shallow schema check. Returns the problems found, empty if it passes.

    Deliberately shallow: this catches the failures a prompted model actually
    makes - a missing property, a string where a list belongs, a stray wrapper
    object - and does not try to be a JSON Schema implementation. Anything
    subtler than this is the caller's business, exactly as it is with the API
    backend, whose guarantee also stops at the shape.
    """
    problems: List[str] = []

    expected = schema.get("type")
    if expected == "object" and not isinstance(result, dict):
        return [f"expected a JSON object, got {type(result).__name__}"]
    if expected == "array" and not isinstance(result, list):
        return [f"expected a JSON array, got {type(result).__name__}"]
    if not isinstance(result, dict):
        return problems

    for name in schema.get("required", []):
        if name not in result:
            problems.append(f"missing required property {name!r}")

    types = {
        "string": str, "boolean": bool, "array": list,
        "object": dict, "number": (int, float), "integer": int,
    }
    for name, spec in (schema.get("properties") or {}).items():
        if name not in result or not isinstance(spec, dict):
            continue
        wanted = types.get(spec.get("type"))
        # bool is a subclass of int, so an integer field would accept `true`.
        if wanted and not isinstance(result[name], wanted):
            problems.append(f"property {name!r} should be {spec['type']}")
        elif wanted is int and isinstance(result[name], bool):
            problems.append(f"property {name!r} should be integer")

    return problems


def _extract_json(text: str) -> Optional[Any]:
    """Parse the model's answer, tolerating a code fence around it.

    The instruction says no fences and it is usually obeyed, but a fence is a
    formatting slip rather than a wrong answer - failing the call over one
    would spend a retry to get the same content back.
    """
    text = text.strip()
    if text.startswith("```"):
        body = text.split("\n", 1)[-1]
        text = body.rsplit("```", 1)[0].strip() if "```" in body else body.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # A leading sentence before the object is the other common slip. Take the
    # outermost braces rather than the first, so nested objects survive.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _invoke(system: str, prompt: str, model: str) -> Optional[str]:
    """One CLI call. Returns the model's text, or None if it did not answer."""
    command = [
        "claude", "-p", prompt,
        "--model", model,
        "--system-prompt", system,
        # Claude Code's dynamic sections describe a coding session - the git
        # state, the working directory. None of it belongs in a prompt that
        # reads a job posting, and it changes between calls.
        "--exclude-dynamic-system-prompt-sections",
        "--disallowed-tools", *_DISABLED_TOOLS,
        "--output-format", "json",
    ]

    try:
        completed = subprocess.run(
            command, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, env=_environment(),
        )
    except subprocess.TimeoutExpired:
        log.warning("the CLI did not answer within %ds", TIMEOUT_SECONDS)
        return None
    except OSError as exc:
        log.warning("could not run the CLI (%s): %s", type(exc).__name__, exc)
        return None

    if completed.returncode != 0:
        log.warning("the CLI exited %d: %s",
                    completed.returncode, (completed.stderr or "").strip()[:300])
        return None

    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError:
        log.warning("the CLI returned output that is not JSON")
        return None

    if envelope.get("is_error"):
        log.warning("the CLI reported an error: %s",
                    envelope.get("api_error_status") or envelope.get("subtype"))
        return None

    # Reported, never billed - these calls run against the subscription. Worth
    # logging so the dashboard's usage is visible, and so the saving against
    # the API backend stays measurable.
    log.debug("call would have cost $%.4f on the API",
              envelope.get("total_cost_usd") or 0.0)

    return envelope.get("result") or None


def complete_json(system: str, prompt: str, schema: Dict[str, Any],
                  model: Optional[str] = None,
                  cached_prefix: str = "",
                  max_tokens: int = 16000,
                  effort: Optional[str] = None) -> Optional[Any]:
    """One structured call. Returns the parsed object, or None on any failure.

    Signature matches ``llm.complete_json`` so the two are interchangeable.
    Two of its parameters have no CLI equivalent and are accepted rather than
    dropped, so callers need no backend-specific branches:

    ``cached_prefix`` is prepended to the system prompt instead of riding in
    its own cache-controlled block. On the API that separation is what makes
    the profile bill at 10%; here nothing is billed, so the distinction buys
    nothing and the text just needs to reach the model.

    ``max_tokens`` and ``effort`` are not exposed by the CLI. Both are
    ceilings and hints rather than requirements, and the calls that pass them
    already work without.
    """
    if not available():
        log.warning("the claude CLI is not installed - this call will be skipped")
        return None

    instructions = f"{cached_prefix}\n\n{system}" if cached_prefix else system
    instructions = f"{instructions}\n\n{_SCHEMA_RULES}"

    message = (
        f"{prompt}\n\n"
        f"Return one JSON object matching this schema:\n"
        f"{json.dumps(schema)}"
    )

    chosen = model or config.MODEL_SCORING

    for attempt in (1, 2):
        text = _invoke(instructions, message, chosen)
        if text is None:
            return None

        result = _extract_json(text)
        if result is None:
            problems = ["the response was not valid JSON"]
        else:
            problems = _validate(result, schema)
            if not problems:
                return result

        log.warning("attempt %d did not match the schema: %s",
                    attempt, "; ".join(problems))
        if attempt == 2:
            return None

        # Name the faults rather than repeating the request. A bare retry
        # tends to reproduce the same mistake.
        message = (
            f"{message}\n\n"
            f"Your previous answer was rejected: {'; '.join(problems)}. "
            f"Return the corrected JSON object only."
        )

    return None
