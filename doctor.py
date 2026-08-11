"""Pre-flight check: what is configured, what is missing, and how to fix it.

Setting this pipeline up touches four separate systems - a Python environment,
two API keys, an OAuth connection, and a document toolchain - and every one of
them fails quietly in its own way. An unset key looks identical to a key with
no Gmail connected until the moment the digest tries to send.

So rather than a setup guide that goes stale, this reports the actual state of
the machine it runs on. Every check is independent, none of them raise, and
each failure carries the exact command that fixes it.

    python main.py --doctor
"""

import importlib
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import config

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT / "profile"

OK, WARN, FAIL = "ok", "warn", "fail"

_MARK = {OK: "\033[32m ok \033[0m", WARN: "\033[33mwarn\033[0m", FAIL: "\033[31mFAIL\033[0m"}


class Result:
    def __init__(self, status: str, label: str, detail: str = "", fix: str = ""):
        self.status = status
        self.label = label
        self.detail = detail
        self.fix = fix


def _module(name: str) -> Optional[str]:
    """Return an installed module's version, or None if it is not importable.

    WeasyPrint logs its stylesheet parsing at INFO on import, which would print
    three lines of noise into the middle of a report whose whole job is being
    readable at a glance.
    """
    for noisy in ("weasyprint", "fontTools", "httpx", "composio"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    try:
        mod = importlib.import_module(name)
    except Exception:
        return None
    return getattr(mod, "__version__", "installed")


# -- individual checks -------------------------------------------------------


def check_core_deps() -> Result:
    missing = [n for n in ("requests", "yaml", "dotenv") if _module(n) is None]
    if missing:
        return Result(FAIL, "core dependencies", f"missing: {', '.join(missing)}",
                      "pip install -r requirements.txt")
    return Result(OK, "core dependencies", "requests, yaml, dotenv")


def check_env_file() -> Result:
    path = ROOT / ".env"
    if not path.exists():
        return Result(WARN, ".env file", "not found (env vars may come from the shell)",
                      "cp .env.example .env   # then fill in the keys")
    return Result(OK, ".env file", str(path))


def check_anthropic() -> Result:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return Result(FAIL, "Anthropic API key", "ANTHROPIC_API_KEY is not set",
                      "add ANTHROPIC_API_KEY=sk-ant-... to .env "
                      "(get one at platform.claude.com)")
    if _module("anthropic") is None:
        return Result(FAIL, "Anthropic SDK", "the anthropic package is not installed",
                      "pip install -r requirements.txt")
    return Result(OK, "Anthropic API key", f"set ({key[:11]}...), SDK installed")


def check_composio() -> Result:
    key = os.getenv("COMPOSIO_API_KEY", "")
    if not key:
        return Result(WARN, "Composio API key", "COMPOSIO_API_KEY is not set - "
                      "web search and email delivery will be skipped",
                      "add COMPOSIO_API_KEY=... to .env (get one at app.composio.dev)")
    if _module("composio") is None:
        return Result(FAIL, "Composio SDK", "the composio package is not installed",
                      "pip install -r requirements.txt")

    import composio_gateway

    if not composio_gateway.available():
        return Result(FAIL, "Composio client", "key is set but the client would not build",
                      "check the key at app.composio.dev")
    return Result(OK, "Composio API key", f"set ({key[:8]}...), client builds")


def _toolkit_of(account) -> str:
    """Read the toolkit name off a connected account, whatever shape it has.

    The SDK has moved this field around between releases (``toolkit`` as a
    string, then as an object with ``.slug``, and ``app_name`` before that), so
    all the known spellings are tried rather than pinning one.
    """
    toolkit = getattr(account, "toolkit", None)
    for candidate in (
        getattr(toolkit, "slug", None),
        getattr(toolkit, "name", None),
        toolkit if isinstance(toolkit, str) else None,
        getattr(account, "app_name", None),
        getattr(account, "appName", None),
    ):
        if candidate:
            return str(candidate).lower()
    return ""


def _user_of(account) -> str:
    for attr in ("user_id", "userId", "entity_id", "entityId"):
        value = getattr(account, attr, None)
        if value:
            return str(value)
    return ""


def _list_accounts(client) -> list:
    """Every connected account on the key, across all users."""
    accounts = client.connected_accounts.list()
    for attr in ("items", "data"):
        items = getattr(accounts, attr, None)
        if items:
            return list(items)
    return list(accounts) if isinstance(accounts, (list, tuple)) else []


def check_gmail_connection() -> Result:
    """A key with no connected Gmail fails only at send time; surface it now.

    The common setup mistake is not a missing connection but a mismatched
    ``user_id``: Composio scopes every call to a user, the dashboard shows you
    an account id and an auth-config id but not that user, and a wrong value
    here looks exactly like "no Gmail connected". So when the configured user
    has no Gmail, this reports which user *does* - the fix is then a copy-paste
    rather than a hunt.
    """
    if not os.getenv("COMPOSIO_API_KEY"):
        return Result(WARN, "Gmail connection", "skipped - no Composio key")

    import composio_gateway

    client = composio_gateway.get_client()
    if client is None:
        return Result(WARN, "Gmail connection", "skipped - Composio client unavailable")

    try:
        accounts = _list_accounts(client)
    except Exception as exc:
        return Result(WARN, "Gmail connection", f"could not verify ({type(exc).__name__}: {exc})",
                      "check manually at app.composio.dev")

    if not accounts:
        return Result(FAIL, "Gmail connection", "no connected accounts on this key",
                      "connect Gmail at app.composio.dev")

    configured = config.COMPOSIO_USER_ID
    gmail_users = sorted({_user_of(a) for a in accounts if "gmail" in _toolkit_of(a)})

    if configured in gmail_users:
        return Result(OK, "Gmail connection", f"connected under user_id '{configured}'")

    if gmail_users:
        listed = ", ".join(f"'{u}'" for u in gmail_users if u)
        return Result(
            FAIL, "Gmail connection",
            f"Gmail is connected, but under {listed} - not '{configured}'",
            f"set COMPOSIO_USER_ID={gmail_users[0]} in .env",
        )

    others = sorted({_toolkit_of(a) for a in accounts if _toolkit_of(a)})
    return Result(FAIL, "Gmail connection",
                  f"no Gmail among {len(accounts)} connected account(s)"
                  + (f": {', '.join(others)}" if others else ""),
                  "connect Gmail at app.composio.dev")


def check_master_resume() -> Result:
    """Which renderer backend the supplied master implies - or that none was."""
    if not PROFILE_DIR.exists():
        return Result(FAIL, "master resume", "profile/ does not exist",
                      "mkdir -p profile   # then add your master resume")

    backends = {
        ".tex": "LaTeX, exact",
        ".docx": "LibreOffice, exact",
        ".html": "WeasyPrint, hand-matched",
        ".md": "WeasyPrint, hand-matched",
        ".pdf": "WeasyPrint, rebuilt from measurements",
    }
    found = [p for p in PROFILE_DIR.glob("master.*") if p.suffix.lower() in backends]
    if not found:
        others = [p.name for p in PROFILE_DIR.iterdir() if p.is_file()]
        return Result(
            FAIL, "master resume",
            "no profile/master.{tex,docx,html,pdf}"
            + (f" (profile/ contains: {', '.join(others)})" if others else ""),
            "copy your resume in as profile/master.pdf - see profile/README.md",
        )
    master = found[0]
    return Result(OK, "master resume", f"{master.name} ({backends[master.suffix.lower()]})")


def check_render_toolchain() -> Result:
    """The toolchain needed depends on the master's format, so check that first."""
    masters = list(PROFILE_DIR.glob("master.*")) if PROFILE_DIR.exists() else []
    suffix = masters[0].suffix.lower() if masters else ""

    if suffix == ".tex":
        for engine in ("tectonic", "latexmk", "pdflatex"):
            if shutil.which(engine):
                return Result(OK, "render toolchain", f"{engine} found")
        return Result(FAIL, "render toolchain", "no LaTeX engine on PATH",
                      "sudo apt install texlive-latex-recommended latexmk"
                      "   # or: cargo install tectonic")

    if suffix == ".docx":
        if shutil.which("libreoffice") or shutil.which("soffice"):
            return Result(OK, "render toolchain", "libreoffice found")
        return Result(FAIL, "render toolchain", "libreoffice not on PATH (needed for docx->pdf)",
                      "sudo apt install libreoffice-writer")

    if _module("weasyprint") is None:
        return Result(FAIL, "render toolchain", "weasyprint not importable",
                      "pip install -r requirements.txt")

    # For a PDF master the layout is rebuilt in HTML/CSS, so a metric-compatible
    # font decides whether the result matches or merely resembles the original.
    if suffix == ".pdf":
        try:
            listed = subprocess.run(["fc-list"], capture_output=True, text=True,
                                    timeout=15).stdout.lower()
        except Exception:
            listed = ""
        if "nimbus roman" in listed:
            return Result(OK, "render toolchain", "weasyprint + Nimbus Roman "
                                                  "(metric-identical to Times)")
        if "liberation serif" in listed:
            return Result(WARN, "render toolchain",
                          "weasyprint, but only Liberation Serif - close to Times, "
                          "not identical",
                          "sudo apt install fonts-urw-base35   # for Nimbus Roman")
        return Result(WARN, "render toolchain",
                      "weasyprint, but no Times-compatible font found",
                      "sudo apt install fonts-urw-base35")

    return Result(OK, "render toolchain", "weasyprint available")


def check_render_fidelity() -> Result:
    """Does the rebuild still reproduce the master?

    Only meaningful for a PDF master, where the layout was reconstructed rather
    than reused. Template or font changes can silently degrade it, and the
    first symptom would otherwise be a two-page resume going out to a company.
    """
    masters = list(PROFILE_DIR.glob("master.pdf")) if PROFILE_DIR.exists() else []
    if not masters:
        return Result(WARN, "render fidelity", "skipped - no PDF master to compare against")
    if not (PROFILE_DIR / "profile.yml").exists():
        return Result(WARN, "render fidelity", "skipped - no profile.yml yet")

    try:
        from tailor.render import (html_to_pdf, load_profile, master_selection,
                                   page_count, render_html)

        profile = load_profile()
        pdf = html_to_pdf(render_html(profile, master_selection(profile)),
                          ROOT / "out" / "_doctor_render.pdf")
        pages = page_count(pdf)
    except Exception as exc:
        return Result(FAIL, "render fidelity", f"{type(exc).__name__}: {exc}",
                      "make verify-render   # for the full diff")

    if pages != page_count(masters[0]):
        return Result(FAIL, "render fidelity",
                      f"rebuild is {pages} page(s), master is {page_count(masters[0])}",
                      "make verify-render   # for the full diff")
    return Result(OK, "render fidelity", f"rebuild matches the master at {pages} page(s)")


def check_profile_yaml() -> Result:
    path = PROFILE_DIR / "profile.yml"
    if not path.exists():
        return Result(WARN, "profile.yml", "not found - will be generated from your master",
                      "nothing to do yet; it is written during setup")
    try:
        import yaml

        data = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        return Result(FAIL, "profile.yml", f"does not parse: {exc}", "fix the YAML syntax")

    def _count(section):
        return sum(len(item.get("bullets", []) or [])
                   for item in (data.get(section) or []) if isinstance(item, dict))

    roles = len(data.get("experience") or [])
    projects = len(data.get("projects") or [])
    bullets = _count("experience") + _count("projects")
    return Result(OK, "profile.yml",
                  f"{roles} roles, {projects} projects, {bullets} bullets in the pool")


def check_companies() -> Result:
    try:
        from sources.ats import load_boards

        counts = {k: len(load_boards(k)) for k in ("greenhouse", "lever", "ashby")}
    except Exception as exc:
        return Result(FAIL, "companies.yml", f"{type(exc).__name__}: {exc}",
                      "check companies.yml syntax")
    total = sum(counts.values())
    if total == 0:
        return Result(FAIL, "companies.yml", "no boards configured",
                      "populate companies.yml")
    return Result(OK, "companies.yml",
                  f"{total} boards ("
                  + ", ".join(f"{k}={v}" for k, v in counts.items()) + ")")


def check_database() -> Result:
    try:
        from store import open_store

        with open_store(config.DB_PATH) as store:
            runs = len(store.recent_runs(limit=1))
        return Result(OK, "database", f"{config.DB_PATH} writable"
                      + (" (has run history)" if runs else " (empty)"))
    except Exception as exc:
        return Result(FAIL, "database", f"{type(exc).__name__}: {exc}",
                      f"check write permissions for {config.DB_PATH}")


def check_budget() -> Result:
    """Report the spend ceiling and how much of today it has already used."""
    import budget

    cap = config.DAILY_BUDGET_USD
    if cap <= 0:
        return Result(WARN, "spend cap", "DAILY_BUDGET_USD=0 - no ceiling set",
                      "set DAILY_BUDGET_USD=2.00 in .env to bound a runaway run")
    try:
        spent = budget.spent_today()
    except Exception as exc:
        return Result(WARN, "spend cap", f"cap ${cap:.2f}, but spend is unreadable ({exc})")

    if spent >= cap:
        return Result(FAIL, "spend cap",
                      f"${spent:.2f} of ${cap:.2f} already spent today - "
                      "further model calls will be refused",
                      "raise DAILY_BUDGET_USD, or wait for the UTC day to roll over")
    return Result(OK, "spend cap", f"${spent:.2f} of ${cap:.2f} used today "
                                   f"(a normal digest costs ~$0.54)")


def check_delivery_target() -> Result:
    if not config.DIGEST_TO or "@" not in config.DIGEST_TO:
        return Result(FAIL, "digest recipient", f"DIGEST_TO looks wrong: {config.DIGEST_TO!r}",
                      "set DIGEST_TO=you@example.com in .env")
    return Result(OK, "digest recipient", config.DIGEST_TO)


def check_handshake() -> Result:
    """Only report Handshake when it is switched on.

    A checklist that permanently warns about a capability you deliberately
    declined trains you to ignore its warnings, which is the one thing it
    cannot afford. Off by choice is a clean state, not a deficiency.
    """
    enabled = "handshake" in config.SOURCES

    if not enabled:
        return Result(OK, "Handshake", "off by choice (not in SOURCES)")
    if not config.HANDSHAKE_COOKIE:
        return Result(FAIL, "Handshake", "in SOURCES but HANDSHAKE_COOKIE is unset",
                      "python main.py --import-cookie /tmp/hs.txt   "
                      "(a 'Copy as cURL' dump), or drop it from SOURCES")
    return Result(OK, "Handshake", f"cookie set for {config.HANDSHAKE_HOST}")


CHECKS = [
    check_core_deps,
    check_env_file,
    check_anthropic,
    check_composio,
    check_gmail_connection,
    check_master_resume,
    check_render_toolchain,
    check_render_fidelity,
    check_profile_yaml,
    check_companies,
    check_database,
    check_budget,
    check_delivery_target,
    check_handshake,
]


def run() -> int:
    """Run every check. Exit code is 1 if anything is a hard failure."""
    results: List[Result] = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:  # a broken check must not hide the others
            results.append(
                Result(FAIL, check.__name__, f"check itself failed: {type(exc).__name__}: {exc}")
            )

    width = max(len(r.label) for r in results)
    print()
    for result in results:
        print(f"  [{_MARK[result.status]}] {result.label.ljust(width)}  {result.detail}")
        if result.fix and result.status != OK:
            print(f"         {' ' * width}  -> {result.fix}")

    failures = [r for r in results if r.status == FAIL]
    warnings = [r for r in results if r.status == WARN]

    print()
    if failures:
        print(f"  {len(failures)} blocking issue(s), {len(warnings)} optional.")
        print("  The digest cannot run until the blocking ones are resolved.")
        return 1
    if warnings:
        print(f"  Ready, with {len(warnings)} optional item(s) unconfigured.")
        return 0
    print("  Everything is configured.")
    return 0
