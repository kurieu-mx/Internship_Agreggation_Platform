"""Lazy, fail-soft access to Composio.

Composio is the authenticated layer for the parts of this pipeline that need
somebody's credentials: sending the digest through Gmail, and the search
toolkits behind the web-search source. Everything else - the community feeds,
the ATS boards - is public and deliberately does not depend on this module.

That separation is the point. If ``COMPOSIO_API_KEY`` is unset, the package is
not installed, or a connection has expired, the functions here return "not
available" instead of raising, and the caller degrades: the search leg
contributes nothing, and the digest falls back to writing a draft. A missing
credential should cost you one capability, never the run.
"""

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# Composio scopes every call to a user. One key can serve several people, so
# the id is explicit rather than implied.
USER_ID = os.getenv("COMPOSIO_USER_ID", "default")

# The only directory the SDK may read files from when staging attachments.
OUTPUT_DIR = Path(__file__).resolve().parent / "out"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

_client = None
_client_error: Optional[str] = None
_versions: Dict[str, Optional[str]] = {}
_lock = threading.Lock()


def available() -> bool:
    """Is Composio usable right now? Never raises."""
    return get_client() is not None


def get_client():
    """Return a Composio client, or None if it cannot be built.

    Cached after the first attempt, including the failure - there is no point
    re-importing a missing package once per source.
    """
    global _client, _client_error

    with _lock:
        if _client is not None or _client_error is not None:
            return _client

        if not os.getenv("COMPOSIO_API_KEY"):
            _client_error = "COMPOSIO_API_KEY is not set"
            log.info("Composio unavailable: %s - skipping Composio-backed steps",
                     _client_error)
            return None

        try:
            from composio import Composio
        except ImportError as exc:
            _client_error = f"composio package not installed ({exc})"
            log.info("Composio unavailable: %s", _client_error)
            return None

        try:
            _client = Composio(
                # Required for `attachment` to accept file paths at all. Without
                # it the SDK passes the path through as a bare string and the
                # API rejects it, asking for a FileUploadable - which is exactly
                # what this flag makes the SDK construct for you.
                dangerously_allow_auto_upload_download_files=True,
                # ...and this is what makes that safe. "Dangerously" above means
                # the SDK will read any path appearing in a tool argument;
                # confining it to out/ means a malformed or injected argument
                # cannot exfiltrate a file from anywhere else on the machine.
                # Note the parameter name: the constructor takes **kwargs, so a
                # misspelling is silently ignored and leaves the allowlist
                # empty - which fails closed, but confusingly.
                file_upload_dirs=[OUTPUT_DIR],
            )
        except Exception as exc:
            _client_error = f"{type(exc).__name__}: {exc}"
            log.warning("Composio client could not be created: %s", _client_error)
            return None

        return _client


def tool_version(slug: str) -> Optional[str]:
    """The concrete version of a tool's toolkit, e.g. gmail -> '20260721_00'.

    Resolved at runtime and cached, rather than pinned in config, because a
    pinned date rots silently. It has to be resolved at all because of a
    catch-22 in the SDK: leaving the version unset resolves it to "latest",
    "latest" is refused unless you pass ``dangerously_skip_version_check``, and
    that flag *also* skips the file-upload substitution - so attachments and an
    unpinned version are mutually exclusive.
    """
    if slug in _versions:
        return _versions[slug]

    client = get_client()
    if client is None:
        return None
    try:
        version = client.tools.get_raw_composio_tool_by_slug(slug).version
    except Exception as exc:
        log.warning("could not resolve a version for %s: %s", slug, exc)
        version = None
    _versions[slug] = version
    return version


def execute(slug: str, arguments: dict, user_id: Optional[str] = None) -> Optional[Any]:
    """Run one Composio tool. Returns its payload, or None on any failure.

    Composio wraps results as ``{"successful": bool, "data": ..., "error": ...}``
    on most toolkits but not all, so both shapes are unwrapped here rather than
    at every call site.
    """
    client = get_client()
    if client is None:
        return None

    version = tool_version(slug)
    try:
        result = client.tools.execute(
            slug,
            user_id=user_id or USER_ID,
            arguments=arguments,
            # A concrete version, never `dangerously_skip_version_check` - see
            # tool_version() for why the two cannot both be avoided.
            **({"version": version} if version else
               {"dangerously_skip_version_check": True}),
        )
    except Exception as exc:
        log.warning("Composio tool %s failed: %s: %s", slug, type(exc).__name__, exc)
        return None

    if isinstance(result, dict):
        if result.get("successful") is False:
            log.warning("Composio tool %s reported failure: %s",
                        slug, result.get("error"))
            return None
        if "data" in result:
            return result["data"]
    return result
