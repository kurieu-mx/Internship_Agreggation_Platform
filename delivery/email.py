"""Send the digest, with the generated PDFs attached.

Composio's ``GMAIL_SEND_EMAIL`` takes attachments as plain local file paths,
so there is no upload step to get wrong.

The important behaviour here is the fallback: if sending fails, this writes a
**draft** instead. A run that produced eight tailored resumes and then hit an
expired OAuth token should not throw them away - a draft sitting in Gmail is
recoverable by hand, and the store is only told the postings were delivered
when something actually was.
"""

import html
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import config
from composio_gateway import available, execute
from models import Job

log = logging.getLogger(__name__)


class Attachment:
    def __init__(self, path: Path, label: str):
        self.path = Path(path)
        self.label = label


class DigestItem:
    """One shortlisted posting and whatever was produced for it."""

    def __init__(self, job: Job, resume: Optional[Path] = None,
                 cover: Optional[Path] = None, tailored: bool = False):
        self.job = job
        self.resume = resume
        self.cover = cover
        self.tailored = tailored


def _hours_ago(job: Job, now: datetime) -> str:
    if job.posted_at is None:
        # Search and LinkedIn results carry no publication date, so these
        # qualify on first sighting rather than on age - which means one can
        # be genuinely old and still appear here the day we first see it.
        # Saying so is the difference between a surprise and a caveat.
        return "no date published, new to this digest"
    posted = job.posted_at if job.posted_at.tzinfo else job.posted_at.replace(tzinfo=timezone.utc)
    hours = (now - posted).total_seconds() / 3600
    if hours < 1:
        return "posted under an hour ago"
    if hours < 24:
        return f"posted {int(hours)}h ago"
    return f"posted {int(hours / 24)}d ago"


def subject(items: List[DigestItem], now: datetime) -> str:
    """The subject line, named after what is actually in the email.

    "Summer 2027" used to be hardcoded here, which is right for the digest and
    wrong for everything else: a hand-added new-grad or full-time posting went
    out under a heading naming a term it has nothing to do with. The prefix now
    follows the postings - their shared term when they have one, the employer
    when a single posting has none - and only falls back to the configured
    filter when neither applies.
    """
    if not items:
        return f"{config.TERM_FILTER or 'Internships'} — nothing new ({now:%b %-d})"

    terms = {term for item in items for term in item.job.terms}

    if len(terms) == 1:
        prefix = terms.pop()
    elif not terms and len(items) == 1:
        prefix = items[0].job.company
    else:
        prefix = config.TERM_FILTER or "Internships"

    companies = len({item.job.company for item in items})
    return (f"{prefix} — {len(items)} match"
            f"{'es' if len(items) != 1 else ''} at {companies} "
            f"compan{'ies' if companies != 1 else 'y'} ({now:%b %-d})")


def build_body(items: List[DigestItem], also: List[Job], now: datetime) -> str:
    """The email itself. HTML, because the apply links have to be clickable."""
    esc = html.escape
    parts = [
        "<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "font-size:14px;line-height:1.5;color:#111\">"
    ]

    if not items:
        parts.append("<p>No new Summer 2027 postings matched today.</p>")
    else:
        parts.append(f"<p><strong>{len(items)}</strong> worth applying to today.</p>")

    for index, item in enumerate(items, start=1):
        job = item.job
        parts.append("<div style=\"margin:0 0 22px 0;padding:0 0 16px 0;"
                     "border-bottom:1px solid #e3e3e3\">")
        parts.append(
            f"<div style=\"font-size:15px\"><strong>{index}. {esc(job.company)}"
            f"</strong> — {esc(job.title)}</div>"
        )
        parts.append(
            f"<div style=\"color:#555;margin:3px 0\">"
            f"{esc(', '.join(job.locations) or 'location unspecified')} &middot; "
            f"{esc(job.field_category)} &middot; {_hours_ago(job, now)} &middot; "
            f"score {job.score:.0f}/100</div>"
        )
        if job.sponsorship and job.sponsorship != "Unknown":
            parts.append(f"<div style=\"color:#555\">sponsorship: "
                         f"{esc(job.sponsorship)}</div>")
        if job.score_reason:
            parts.append(f"<div style=\"margin:6px 0;color:#333\">"
                         f"{esc(job.score_reason)}</div>")
        if job.url:
            parts.append(
                f"<div style=\"margin:8px 0\"><a href=\"{esc(job.url)}\" "
                f"style=\"color:#1155cc\"><strong>Apply →</strong></a></div>"
            )

        produced = []
        if item.resume:
            produced.append(esc(item.resume.name)
                            + ("" if item.tailored else " (untailored — tailoring failed)"))
        if item.cover:
            produced.append(esc(item.cover.name))
        if produced:
            parts.append("<div style=\"color:#555;font-size:13px\">attached: "
                         + ", ".join(produced) + "</div>")
        else:
            parts.append("<div style=\"color:#a00;font-size:13px\">"
                         "no documents could be generated for this one</div>")
        parts.append("</div>")

    if also:
        parts.append(f"<p style=\"margin-top:20px\"><strong>Also posted "
                     f"({len(also)})</strong> — ranked lower, no documents generated:</p><ul>")
        for job in also:
            link = (f"<a href=\"{esc(job.url)}\" style=\"color:#1155cc\">"
                    f"{esc(job.title)}</a>") if job.url else esc(job.title)
            parts.append(f"<li style=\"margin:4px 0\">{esc(job.company)} — {link} "
                         f"<span style=\"color:#888\">({job.score:.0f})</span></li>")
        parts.append("</ul>")

    parts.append("</div>")
    return "\n".join(parts)


def _attachments(items: List[DigestItem]) -> List[str]:
    paths: List[str] = []
    for item in items:
        for path in (item.resume, item.cover):
            if path and Path(path).exists():
                paths.append(str(Path(path).resolve()))
    return paths


def send(items: List[DigestItem], also: List[Job],
         to: Optional[str] = None, now: Optional[datetime] = None,
         dry_run: bool = False) -> bool:
    """Send the digest. Returns True only if something actually went out."""
    now = now or datetime.now(timezone.utc)
    to = to or config.DIGEST_TO
    body = build_body(items, also, now)
    line = subject(items, now)
    files = _attachments(items)

    if dry_run:
        print(f"\n--- to: {to}\n--- subject: {line}")
        print(f"--- attachments ({len(files)}):")
        for path in files:
            print(f"      {path}")
        print("---\n")
        print(body)
        return False

    if not available():
        log.error("Composio is not configured - cannot send. "
                  "The generated PDFs are still on disk.")
        return False

    arguments = {
        "recipient_email": to,
        "subject": line,
        "body": body,
        "is_html": True,
    }
    if files:
        arguments["attachment"] = files if len(files) > 1 else files[0]

    if execute("GMAIL_SEND_EMAIL", arguments) is not None:
        log.info("sent %d posting(s) with %d attachment(s) to %s",
                 len(items), len(files), to)
        return True

    # Sending failed. Leave a draft rather than discard the work.
    log.warning("send failed - falling back to a draft")
    if execute("GMAIL_CREATE_EMAIL_DRAFT", arguments) is not None:
        log.warning("saved the digest as a Gmail draft instead; "
                    "postings are NOT marked as sent, so tomorrow's run will retry")
        return False

    log.error("could not send or draft the digest. The PDFs remain in %s",
              files[0].rsplit("/", 1)[0] if files else "out/")
    return False
