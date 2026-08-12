"""The CLI backend: schema validation, the retry, and the API-key strip.

The last of those is the one worth a test. A set ``ANTHROPIC_API_KEY`` makes
the CLI bill credits instead of the subscription, and the failure is silent -
the call still succeeds, it just costs money. Nothing else in the output would
tell you, so the environment is asserted here rather than trusted.
"""

import json
import subprocess

import llm_cli


def _envelope(result, is_error=False):
    """A stand-in for what `claude -p --output-format json` writes."""
    return subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"is_error": is_error, "result": result,
                           "total_cost_usd": 0.01}),
        stderr="",
    )


SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "is_internship": {"type": "boolean"},
        "locations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["company", "is_internship", "locations"],
    "additionalProperties": False,
}

GOOD = {"company": "IBM", "is_internship": True, "locations": ["Armonk, NY"]}


# --- the API-key strip ------------------------------------------------------

def test_api_key_is_removed_from_the_subprocess(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-used")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "also-not")
    monkeypatch.setenv("PATH", "/usr/bin")          # something else must survive

    seen = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs["env"])
        return _envelope(json.dumps(GOOD))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(llm_cli, "available", lambda: True)

    llm_cli.complete_json("sys", "prompt", SCHEMA)

    assert "ANTHROPIC_API_KEY" not in seen
    assert "ANTHROPIC_AUTH_TOKEN" not in seen
    assert seen["PATH"] == "/usr/bin"


def test_the_ambient_environment_is_not_mutated(monkeypatch):
    """Stripping the key must not unset it for the digest running in-process."""
    import os

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-live")
    llm_cli._environment()
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-live"


# --- parsing ----------------------------------------------------------------

def test_a_bare_object_parses():
    assert llm_cli._extract_json('{"a": 1}') == {"a": 1}


def test_a_fenced_object_parses():
    """A code fence is a formatting slip, not a wrong answer."""
    assert llm_cli._extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_a_preamble_before_the_object_parses():
    assert llm_cli._extract_json('Here you go:\n{"a": {"b": 2}}') == {"a": {"b": 2}}


def test_nested_braces_survive():
    assert llm_cli._extract_json('{"a": {"b": 1}, "c": 2}') == {"a": {"b": 1}, "c": 2}


def test_prose_with_no_object_is_none():
    assert llm_cli._extract_json("I could not do that.") is None


# --- validation -------------------------------------------------------------

def test_a_conforming_object_has_no_problems():
    assert llm_cli._validate(GOOD, SCHEMA) == []


def test_a_missing_required_property_is_caught():
    problems = llm_cli._validate({"company": "IBM", "is_internship": True}, SCHEMA)
    assert any("locations" in p for p in problems)


def test_a_wrong_type_is_caught():
    bad = dict(GOOD, locations="Armonk, NY")
    assert any("locations" in p for p in llm_cli._validate(bad, SCHEMA))


def test_a_boolean_is_not_accepted_as_an_integer():
    """bool subclasses int, so a naive isinstance check would let this pass."""
    schema = {"type": "object", "properties": {"n": {"type": "integer"}},
              "required": ["n"]}
    assert llm_cli._validate({"n": True}, schema)


def test_an_array_schema_rejects_an_object():
    assert llm_cli._validate({"a": 1}, {"type": "array"})


# --- the retry --------------------------------------------------------------

def test_a_malformed_first_answer_is_retried_and_the_faults_named(monkeypatch):
    prompts = []
    answers = iter([json.dumps({"company": "IBM"}), json.dumps(GOOD)])

    def fake_run(command, **kwargs):
        prompts.append(command[command.index("-p") + 1])
        return _envelope(next(answers))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(llm_cli, "available", lambda: True)

    assert llm_cli.complete_json("sys", "prompt", SCHEMA) == GOOD
    assert len(prompts) == 2
    # The retry must say what was wrong; a bare repeat reproduces the mistake.
    assert "rejected" in prompts[1] and "is_internship" in prompts[1]


def test_two_bad_answers_return_none_rather_than_retrying_forever(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(1)
        return _envelope("not json at all")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(llm_cli, "available", lambda: True)

    assert llm_cli.complete_json("sys", "prompt", SCHEMA) is None
    assert len(calls) == 2


# --- failing soft -----------------------------------------------------------

def test_a_missing_cli_returns_none(monkeypatch):
    monkeypatch.setattr(llm_cli.shutil, "which", lambda _: None)
    assert llm_cli.available() is False
    assert llm_cli.complete_json("sys", "prompt", SCHEMA) is None


def test_a_timeout_returns_none(monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(llm_cli, "available", lambda: True)
    assert llm_cli.complete_json("sys", "prompt", SCHEMA) is None


def test_a_nonzero_exit_returns_none(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess([], 1, "", "not logged in"))
    monkeypatch.setattr(llm_cli, "available", lambda: True)
    assert llm_cli.complete_json("sys", "prompt", SCHEMA) is None


def test_a_reported_error_returns_none(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs:
                        _envelope(None, is_error=True))
    monkeypatch.setattr(llm_cli, "available", lambda: True)
    assert llm_cli.complete_json("sys", "prompt", SCHEMA) is None


# --- the command ------------------------------------------------------------

def test_the_model_and_tool_restrictions_reach_the_command(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["argv"] = command
        return _envelope(json.dumps(GOOD))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(llm_cli, "available", lambda: True)

    llm_cli.complete_json("sys", "prompt", SCHEMA, model="claude-haiku-4-5")

    argv = captured["argv"]
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"
    assert "--disallowed-tools" in argv and "Bash" in argv
    assert argv[argv.index("--output-format") + 1] == "json"


def test_the_cached_prefix_reaches_the_system_prompt(monkeypatch):
    """No cache_control on this path, so the prefix must still get through."""
    captured = {}

    def fake_run(command, **kwargs):
        captured["argv"] = command
        return _envelope(json.dumps(GOOD))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(llm_cli, "available", lambda: True)

    llm_cli.complete_json("sys", "prompt", SCHEMA, cached_prefix="PROFILE-TEXT")

    argv = captured["argv"]
    assert "PROFILE-TEXT" in argv[argv.index("--system-prompt") + 1]
