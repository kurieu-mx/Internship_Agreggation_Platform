"""The 'Copy as cURL' cookie extractor.

The failure this guards against is the expensive one: silently extracting a
partial or wrong cookie, which is indistinguishable from an expired session
once it reaches Handshake.
"""

import pytest

from import_cookie import _write_env, extract, run

CHROME_LINUX = """curl 'https://umich.joinhandshake.com/stu/postings' \\
  -H 'accept: text/html,application/xhtml+xml' \\
  -H 'accept-language: en-US,en;q=0.9' \\
  -H 'cookie: _joinhandshake_session=abc123; hs_csrf=xyz789; ajs_user_id=42' \\
  -H 'user-agent: Mozilla/5.0' \\
  --compressed"""

CHROME_ANALYTICS_BEACON = """curl 'https://analytics.google.com/g/collect?v=2&tid=G-4M16ZMP2G5' \\
  -H 'accept: */*' \\
  -H 'content-type: text/plain;charset=UTF-8' \\
  --data-raw ''"""

BASH_B_FLAG = """curl 'https://umich.joinhandshake.com/explore' \\
  -b '_joinhandshake_session=abc123; other=1'"""

DOUBLE_QUOTED = '''curl "https://umich.joinhandshake.com/stu/postings" ^
  -H "cookie: _joinhandshake_session=abc123; hs_csrf=xyz789"'''


def test_the_cookie_header_is_extracted():
    cookie, url = extract(CHROME_LINUX)
    assert cookie == "_joinhandshake_session=abc123; hs_csrf=xyz789; ajs_user_id=42"
    assert url == "https://umich.joinhandshake.com/stu/postings"


def test_every_cookie_is_kept_not_just_the_session_one():
    """Handshake splits session state across several; a partial copy fails."""
    cookie, _ = extract(CHROME_LINUX)
    assert cookie.count(";") == 2


def test_the_short_b_flag_is_understood():
    cookie, _ = extract(BASH_B_FLAG)
    assert cookie == "_joinhandshake_session=abc123; other=1"


def test_double_quoted_windows_style_is_understood():
    cookie, _ = extract(DOUBLE_QUOTED)
    assert "_joinhandshake_session=abc123" in cookie
    assert "^" not in cookie


def test_an_analytics_beacon_yields_no_cookie():
    cookie, url = extract(CHROME_ANALYTICS_BEACON)
    assert cookie is None
    assert "analytics.google.com" in url


def test_junk_input_is_survivable():
    assert extract("not a curl command at all") == (None, None)
    assert extract("") == (None, None)


# -- the CLI wrapper ---------------------------------------------------------


def test_a_beacon_is_rejected_with_an_explanation(tmp_path, capsys):
    path = tmp_path / "curl.txt"
    path.write_text(CHROME_ANALYTICS_BEACON)

    assert run(str(path)) == 1
    assert "analytics beacon" in capsys.readouterr().out


def test_a_missing_file_is_reported(tmp_path, capsys):
    assert run(str(tmp_path / "nope.txt")) == 1
    assert "no such file" in capsys.readouterr().out


def test_a_good_dump_is_written_to_env(tmp_path, monkeypatch, capsys):
    import import_cookie

    monkeypatch.setattr(import_cookie, "ROOT", tmp_path)
    path = tmp_path / "curl.txt"
    path.write_text(CHROME_LINUX)

    assert run(str(path)) == 0
    written = (tmp_path / ".env").read_text()
    assert "HANDSHAKE_COOKIE='_joinhandshake_session=abc123;" in written


def test_the_value_is_quoted_so_semicolons_survive(tmp_path):
    """Unquoted, dotenv truncates the value at the first ';'."""
    env = tmp_path / ".env"
    _write_env("HANDSHAKE_COOKIE", "a=1; b=2", env)
    assert env.read_text().strip() == "HANDSHAKE_COOKIE='a=1; b=2'"


def test_an_existing_key_is_replaced_not_duplicated(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-x\nHANDSHAKE_COOKIE='old'\nTOP_N=8\n")

    _write_env("HANDSHAKE_COOKIE", "new=1", env)

    text = env.read_text()
    assert text.count("HANDSHAKE_COOKIE") == 1
    assert "new=1" in text
    assert "sk-ant-x" in text and "TOP_N=8" in text   # nothing else disturbed


def test_the_key_is_appended_when_absent(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TOP_N=8\n")
    _write_env("HANDSHAKE_COOKIE", "a=1", env)
    assert "TOP_N=8" in env.read_text()
    assert "HANDSHAKE_COOKIE='a=1'" in env.read_text()
