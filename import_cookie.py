"""Extract a session cookie from a DevTools "Copy as cURL" dump.

Hunting for the right row in the Network tab is the fiddly, error-prone part of
setting Handshake up: a single-page app fires dozens of analytics beacons for
every request of its own, and the cookie header has to be copied whole or it
silently fails. Both mistakes look identical afterwards - a rejected session.

"Copy as cURL" sidesteps all of it. Right-click any request to the site, copy,
paste into a file, and this pulls the cookie out and writes it to ``.env``. The
only judgement call left is picking a request to the right domain, which this
verifies rather than trusts.
"""

import re
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent

# DevTools emits `-H 'cookie: ...'` (Chrome) or `-b '...'` (some variants), in
# single quotes on Unix and double on Windows. `--cookie` is the long form.
_COOKIE_PATTERNS = [
    re.compile(r"""-H\s+(['"])\s*cookie\s*:\s*(?P<value>.*?)\1""", re.I | re.S),
    re.compile(r"""(?:-b|--cookie)\s+(['"])(?P<value>.*?)\1""", re.I | re.S),
]

_URL_RE = re.compile(r"""curl\s+(['"]?)(?P<url>https?://[^\s'"]+)\1""", re.I)


def extract(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(cookie, url)`` from a cURL command, either possibly None."""
    url_match = _URL_RE.search(text)
    url = url_match.group("url") if url_match else None

    for pattern in _COOKIE_PATTERNS:
        match = pattern.search(text)
        if match:
            # Windows-style ^" line continuations survive some copies.
            value = match.group("value").replace("^", "").strip()
            value = re.sub(r"\s*\\\s*\n\s*", "", value)
            return value, url

    return None, url


def _write_env(key: str, value: str, path: Path) -> None:
    """Set one key in .env, preserving everything else and quoting the value.

    Single quotes matter: a cookie is full of ``;`` and ``=``, which dotenv
    will otherwise read as a value terminator and silently truncate.
    """
    quoted = f"'{value}'"
    lines = path.read_text().splitlines() if path.exists() else []

    for index, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[index] = f"{key}={quoted}"
            break
    else:
        lines.append(f"{key}={quoted}")

    path.write_text("\n".join(lines) + "\n")


def run(source: str, key: str = "HANDSHAKE_COOKIE",
        expect_host: str = "joinhandshake.com") -> int:
    """Read a cURL dump from ``source`` and store its cookie in .env."""
    path = Path(source)
    if not path.exists():
        print(f"  no such file: {source}")
        print("\n  Paste the 'Copy as cURL' output into a file first, e.g.:")
        print("    (right-click the request -> Copy -> Copy as cURL)")
        print("    cat > /tmp/hs.txt   # paste, then press Ctrl-D")
        return 1

    cookie, url = extract(path.read_text())

    if url:
        print(f"  request URL: {url[:90]}")
    if url and expect_host not in url:
        print(f"\n  That request is not to {expect_host}.")
        print("  You have almost certainly copied an analytics beacon - those")
        print("  fire constantly and carry none of your session.")
        print(f"\n  -> In the Network tab, click the 'Doc' filter, reload, and copy")
        print(f"     the top row instead.")
        return 1

    if not cookie:
        print("\n  No cookie header found in that file.")
        print("  Make sure you used 'Copy as cURL' (not 'Copy link address'),")
        print("  and that you were logged in when the request was made.")
        return 1

    names = sorted({c.split("=", 1)[0].strip() for c in cookie.split(";") if "=" in c})
    print(f"  found {len(names)} cookie(s): {', '.join(names[:8])}"
          + (" ..." if len(names) > 8 else ""))

    if not any("session" in n.lower() for n in names):
        print("\n  Warning: no session cookie among those. This may not authenticate.")

    env_path = ROOT / ".env"
    _write_env(key, cookie, env_path)
    print(f"\n  wrote {key} to {env_path} ({len(cookie)} chars)")
    print("\n  Now run:  make handshake")
    return 0
