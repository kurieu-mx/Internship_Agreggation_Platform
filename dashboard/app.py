"""A local dashboard: paste a posting URL, get a tailored resume and letter.

Why this is not just ``--apply-url`` with a web page on top
----------------------------------------------------------
It is exactly that, deliberately. The pipeline is ``apply_url.prepare``, the
same function the CLI calls, so a posting submitted here goes through the same
fetch, the same extraction, the same eligibility gates, the same scoring and
the same templates. The dashboard adds a form, a progress view, and links to
the two PDFs. It does not add a second way to build an application, which
would be a second thing to keep correct.

The one thing it does change is where the model calls go. Six calls per
posting on the API measures at ~$0.25-0.30, which is fine once a day and not
fine for an afternoon of pasting links. So this process sets
``LLM_BACKEND=cli`` for itself, and the calls run against the Max
subscription instead. Nothing else in the repo is affected - the digest and
its CI workflow keep the API default, because the runner has no claude.ai
login to use.

Running it::

    make dashboard          # or: uvicorn dashboard.app:app --port 8000

Submissions are held in memory. This is a single-user tool on a laptop, so a
restart losing the list of past runs is the correct trade against a schema to
migrate - the PDFs themselves are on disk under ``out/`` either way.
"""

import os

# Set before `config` is imported, since it reads the environment at import
# time. `load_dotenv` does not override variables already set, so this wins
# over the `.env` the digest uses. Overridable for anyone who wants to point
# the dashboard back at the API.
os.environ.setdefault("LLM_BACKEND", "cli")

import logging
import threading
import uuid
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import apply_url
import config
import llm

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="Internship application dashboard")


class Submission:
    """One pasted URL and whatever became of it."""

    def __init__(self, url: str, description: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.description = description
        self.state = "running"          # running | done | failed
        self.error: Optional[str] = None
        self.prepared: Optional[apply_url.Prepared] = None
        self.started = datetime.now(timezone.utc)


# Guarded because the worker thread writes what the request thread reads.
_submissions: Dict[str, Submission] = {}
_lock = threading.Lock()


def _record(submission: Submission) -> None:
    with _lock:
        _submissions[submission.id] = submission


def _get(submission_id: str) -> Optional[Submission]:
    with _lock:
        return _submissions.get(submission_id)


def _recent(limit: int = 20) -> List[Submission]:
    with _lock:
        return sorted(_submissions.values(), key=lambda s: s.started, reverse=True)[:limit]


def _process(submission_id: str) -> None:
    """Run the pipeline for one submission. Never raises into the server.

    A posting that cannot be read is a normal outcome here - a login wall, a
    dead link, a search page pasted by mistake - so it becomes a failed
    submission with a reason on it rather than a 500.
    """
    submission = _get(submission_id)
    if submission is None:
        return

    out_dir = ROOT / "out" / datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        prepared = apply_url.prepare(submission.url, out_dir,
                                     description=submission.description)
    except Exception as exc:
        log.exception("submission %s failed", submission_id)
        submission.error = f"{type(exc).__name__}: {exc}"
        submission.state = "failed"
        return

    if prepared is None:
        # Two different failures, and the fix differs. An unreadable page is
        # recoverable by pasting the text; unreadable pasted text is not.
        submission.error = (
            "That text could not be read as a job posting."
            if submission.description else
            "That page could not be read — it may be behind a login. "
            "Paste the description below and submit it with the same URL."
        )
        submission.state = "failed"
        return

    submission.prepared = prepared
    submission.state = "done"


# --- Rendering --------------------------------------------------------------
# Inline rather than templated: there is one page and one fragment, and a
# template directory for two documents is more moving parts than it saves.

STYLE = """
:root { color-scheme: light dark; --fg:#101418; --bg:#fbfaf7; --muted:#5c6570;
        --line:#e2ded6; --card:#fff; --warn:#8a4b1d; --warn-bg:#fdf2e7;
        --ok:#1f5c3d; --accent:#1a4f8a; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8e6e3; --bg:#14171a; --muted:#9aa3ad; --line:#2a2f35;
          --card:#1c2025; --warn:#e0a878; --warn-bg:#2e2216;
          --ok:#7fc9a0; --accent:#7fb0e8; } }
* { box-sizing:border-box; }
body { margin:0; padding:2.5rem 1.25rem; background:var(--bg); color:var(--fg);
       font:16px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif; }
main { max-width:52rem; margin:0 auto; }
h1 { font-size:1.4rem; margin:0 0 .25rem; letter-spacing:-.01em; }
.sub { color:var(--muted); font-size:.9rem; margin:0 0 1.75rem; }
form { display:flex; gap:.5rem; margin-bottom:2rem; flex-wrap:wrap; }
input[type=url] { flex:1 1 22rem; padding:.7rem .85rem; font-size:1rem;
       border:1px solid var(--line); border-radius:8px; background:var(--card);
       color:var(--fg); }
button { padding:.7rem 1.2rem; font-size:1rem; font-weight:550; cursor:pointer;
       border:0; border-radius:8px; background:var(--accent); color:#fff; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
       padding:1.1rem 1.25rem; margin-bottom:.85rem; }
.card h2 { font-size:1.05rem; margin:0 0 .3rem; }
.meta { color:var(--muted); font-size:.87rem; margin:0 0 .6rem;
       overflow-wrap:anywhere; }
.gate { font-size:.87rem; padding:.4rem .6rem; border-radius:6px;
       margin:.3rem 0; background:var(--warn-bg); color:var(--warn); }
.gate.note { background:transparent; color:var(--ok); padding-left:0; }
.files a { display:inline-block; margin:.45rem .5rem .1rem 0; padding:.45rem .8rem;
       border:1px solid var(--line); border-radius:7px; text-decoration:none;
       color:var(--accent); font-size:.9rem; }
.tag { display:inline-block; font-size:.75rem; padding:.15rem .5rem;
       border-radius:20px; background:var(--warn-bg); color:var(--warn);
       margin-left:.4rem; vertical-align:middle; }
.err { color:var(--warn); font-size:.9rem; margin:0; }
.foot { color:var(--muted); font-size:.8rem; margin-top:2rem;
       border-top:1px solid var(--line); padding-top:.9rem; }
details { flex:1 1 100%; margin-top:.2rem; }
summary { cursor:pointer; color:var(--muted); font-size:.87rem; }
.hint { color:var(--muted); font-size:.82rem; margin:.5rem 0 .4rem; }
textarea { width:100%; padding:.7rem .85rem; font-size:.92rem; font-family:inherit;
       border:1px solid var(--line); border-radius:8px; background:var(--card);
       color:var(--fg); resize:vertical; }
code { background:var(--warn-bg); color:var(--warn); padding:.1rem .35rem;
       border-radius:4px; font-size:.85em; }
"""


def _gate_html(gates: List[Tuple[str, str]]) -> str:
    return "".join(
        f'<p class="gate {"" if level == "warn" else "note"}">'
        f'{"⚠ " if level == "warn" else "· "}{escape(message)}</p>'
        for level, message in gates
    )


def _card(submission: Submission) -> str:
    url = escape(submission.url)

    if submission.state == "running":
        return (f'<div class="card"><h2>Working…</h2>'
                f'<p class="meta">{url}</p>'
                f'<p class="meta">Reading the posting, scoring it, then writing. '
                f'Usually under a minute.</p></div>')

    if submission.state == "failed":
        return (f'<div class="card"><h2>Could not build an application</h2>'
                f'<p class="meta">{url}</p>'
                f'<p class="err">{escape(submission.error or "unknown error")}</p></div>')

    prepared = submission.prepared
    assert prepared is not None
    job = prepared.job

    where = ", ".join(job.locations) or "location not stated"
    terms = ", ".join(job.terms) or "term not stated"
    score = f"{job.score:.0f}/100" if getattr(job, "score", None) is not None else "unscored"

    files = ""
    if prepared.resume:
        label = "Resume" if prepared.tailored else "Resume (untailored fallback)"
        files += f'<a href="/file/{submission.id}/resume">{label}</a>'
    if prepared.cover:
        files += f'<a href="/file/{submission.id}/cover">Cover letter</a>'
    if not files:
        files = '<p class="err">Nothing was produced for this posting.</p>'

    keywords = ""
    if prepared.brief:
        missing = ", ".join(prepared.brief.missing[:6])
        keywords = (f'<p class="meta">{len(prepared.brief.matched)} keywords matched'
                    + (f" · missing {escape(missing)}" if missing else "") + "</p>")

    stale = "" if prepared.tailored else '<span class="tag">untailored</span>'
    source = '<span class="tag">from pasted text</span>' if prepared.pasted else ""

    return (
        f'<div class="card">'
        f'<h2>{escape(job.company)} — {escape(job.title)}{stale}{source}</h2>'
        f'<p class="meta">{escape(where)} · {escape(job.field_category)} · '
        f'{escape(terms)} · match {score}</p>'
        f'<p class="meta"><a href="{url}" target="_blank" rel="noopener">{url}</a></p>'
        f'{_gate_html(prepared.gates)}'
        f'{keywords}'
        f'<div class="files">{files}</div>'
        f'</div>'
    )


def _page(body: str, refresh: bool) -> str:
    backend = ("Claude Code CLI — billed to your Max subscription, no API spend"
               if llm.using_cli() else
               f"Anthropic API — this costs roughly $0.25–0.30 per posting")
    meta = '<meta http-equiv="refresh" content="3">' if refresh else ""
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'{meta}<title>Application builder</title><style>{STYLE}</style></head><body><main>'
        f'<h1>Application builder</h1>'
        f'<p class="sub">Paste a posting URL. Same pipeline as the daily digest — '
        f'same eligibility gates, same templates.</p>'
        f'<form method="post" action="/apply">'
        f'<input type="url" name="url" placeholder="https://…" required autofocus>'
        f'<button type="submit">Build</button>'
        f'<details><summary>Paste the description instead</summary>'
        f'<p class="hint">For pages behind a login, or a posting that arrived '
        f'by email. The URL above is still used as the apply link. When this '
        f'is filled in, nothing is fetched.</p>'
        f'<textarea name="description" rows="10" '
        f'placeholder="Responsibilities, requirements, qualifications…"></textarea>'
        f'</details></form>'
        f'{body}'
        f'<p class="foot">{escape(backend)}.</p>'
        f'</main></body></html>'
    )


# --- Routes -----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    recent = _recent()
    body = "".join(_card(s) for s in recent)
    # Only poll while something is actually in flight, so an idle dashboard
    # is not reloading itself every three seconds forever.
    refresh = any(s.state == "running" for s in recent)
    return HTMLResponse(_page(body, refresh))


@app.post("/apply")
def apply(background: BackgroundTasks, url: str = Form(...),
          description: str = Form("")) -> RedirectResponse:
    url = url.strip()
    submission = Submission(url, description.strip())

    if not urlparse(url).scheme.startswith("http"):
        # Still required even when the text is pasted: it is the apply link in
        # the email and on the card, and the branding lookup keys off its host.
        submission.error = "That does not look like a URL."
        submission.state = "failed"
        _record(submission)
        return RedirectResponse("/", status_code=303)

    _record(submission)
    background.add_task(_process, submission.id)
    return RedirectResponse("/", status_code=303)


@app.get("/file/{submission_id}/{which}")
def file(submission_id: str, which: str) -> Response:
    """Serve one of a submission's two PDFs.

    Addressed by submission and role rather than by path, so a user-supplied
    string never reaches the filesystem and there is nothing to traverse.
    """
    submission = _get(submission_id)
    if submission is None or submission.prepared is None:
        return Response("Not found", status_code=404)

    path = {"resume": submission.prepared.resume,
            "cover": submission.prepared.cover}.get(which)
    if path is None or not Path(path).is_file():
        return Response("Not found", status_code=404)

    return Response(
        Path(path).read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{Path(path).name}"'},
    )
