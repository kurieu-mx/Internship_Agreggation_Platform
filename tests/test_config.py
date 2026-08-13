"""Reading configuration out of the environment.

All of these are about one distinction: a variable that is *absent* versus one
that is *present and empty*. ``os.getenv(name, default)`` treats only the first
as missing, and CI produces the second.

That cost a full CI run. GitHub Actions expands an undefined repository
variable to the empty string rather than omitting it, so
``MODEL_TAILORING: ${{ vars.MODEL_TAILORING }}`` set the model name to "".
Every model call returned 400, tailoring fell back to the untailored master
for every posting, no cover letters were produced - and the workflow reported
success, because falling back is the designed response to a failed call. The
only visible trace was one line in an email body reading "untailored".
"""

import importlib

import pytest

import config


@pytest.fixture
def env(monkeypatch):
    """Set variables, then re-import config so it re-reads them."""
    def _set(**values):
        for name, value in values.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return importlib.reload(config)
    yield _set
    importlib.reload(config)


# -- the helper --------------------------------------------------------------


def test_an_absent_variable_falls_back(monkeypatch):
    monkeypatch.delenv("SOME_SETTING", raising=False)
    assert config._env("SOME_SETTING", "fallback") == "fallback"


def test_an_empty_variable_also_falls_back(monkeypatch):
    """The case os.getenv gets wrong, and the one CI actually produces."""
    monkeypatch.setenv("SOME_SETTING", "")
    assert config._env("SOME_SETTING", "fallback") == "fallback"


def test_a_whitespace_only_variable_falls_back(monkeypatch):
    monkeypatch.setenv("SOME_SETTING", "   ")
    assert config._env("SOME_SETTING", "fallback") == "fallback"


def test_a_real_value_is_returned_unchanged(monkeypatch):
    monkeypatch.setenv("SOME_SETTING", "actual")
    assert config._env("SOME_SETTING", "fallback") == "actual"


def test_a_value_that_looks_falsy_is_still_a_value(monkeypatch):
    """"0" and "false" are settings, not absences."""
    monkeypatch.setenv("SOME_SETTING", "0")
    assert config._env("SOME_SETTING", "fallback") == "0"


# -- the numeric readers -----------------------------------------------------


def test_an_empty_numeric_falls_back_rather_than_raising(monkeypatch):
    """float("") raises. This one would have crashed the run, not degraded it."""
    monkeypatch.setenv("DAILY_BUDGET_USD", "")
    assert config._env_float("DAILY_BUDGET_USD", 2.0) == 2.0


def test_an_empty_integer_falls_back(monkeypatch):
    monkeypatch.setenv("WINDOW_HOURS", "")
    assert config._env_int("WINDOW_HOURS", 24) == 24


def test_a_non_numeric_value_falls_back(monkeypatch):
    monkeypatch.setenv("WINDOW_HOURS", "soon")
    assert config._env_int("WINDOW_HOURS", 24) == 24


def test_real_numbers_are_read(monkeypatch):
    monkeypatch.setenv("WINDOW_HOURS", "48")
    monkeypatch.setenv("DAILY_BUDGET_USD", "5.50")
    assert config._env_int("WINDOW_HOURS", 24) == 48
    assert config._env_float("DAILY_BUDGET_USD", 2.0) == 5.50


# -- the setting that actually broke -----------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("MODEL_SCORING", "claude-opus-5"),
    ("MODEL_TAILORING", "claude-sonnet-5"),
    ("MODEL_LETTER", "claude-opus-5"),
    ("MODEL_RESEARCH", "claude-haiku-4-5"),
])
def test_a_blank_model_name_never_reaches_the_api(env, name, expected):
    """An empty model is a 400 on every call, and the pipeline hides it."""
    reloaded = env(**{name: ""})
    assert getattr(reloaded, name) == expected


def test_a_configured_model_still_overrides(env):
    assert env(MODEL_TAILORING="claude-opus-5").MODEL_TAILORING == "claude-opus-5"


def test_blank_list_settings_fall_back_to_their_defaults(env):
    reloaded = env(SOURCES="", TARGET_CATEGORIES="")
    assert "greenhouse" in reloaded.SOURCES
    assert "Quant" in reloaded.TARGET_CATEGORIES


def test_a_blank_timezone_does_not_become_utc(env):
    """The 3pm guard reads this. An empty TZ silently means UTC."""
    assert env(DIGEST_TIMEZONE="").DIGEST_TIMEZONE == "America/Chicago"


def test_a_blank_recipient_stays_blank(env):
    """There is no safe address to fall back to.

    This used to default to the author's, which is harmless in the original
    repo and a data leak in a fork: an owner who sets every other credential
    but forgets this one mails their own resume - name, phone, work history -
    to a stranger, with the run reporting success.
    """
    assert env(DIGEST_TO="").DIGEST_TO == ""
