"""Turn a content selection into a PDF, and refuse to emit a bad one.

The pipeline never edits a PDF. It regenerates one from ``profile.yml`` through
a template, which is what makes every tailored variant come out with identical
layout: same renderer, same stylesheet, same geometry, only the words differ.
Editing PDF text in place cannot do that - a replacement string of a different
width reflows the line, and from there the page.

Three guardrails run on every render, and each one *fails the render* rather
than warning. A resume is not a log file; a broken one is worse than a late
one, and the caller falls back to the untailored master.

1. **Page count.** A tailored resume that spills onto a second page is a bug,
   not a variant.
2. **Immutable fields.** Name, contact details, employers, dates and school
   must appear in the output byte-for-byte. Tailoring may reorder and reword
   *bullets*; it may not touch the facts of who you are and where you worked.
3. **Provenance.** Every bullet rendered must trace back to an ``id`` in the
   pool. This is the check that stops a model inventing experience and putting
   it on a document with your name at the top.
"""

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Path(__file__).resolve().parent / "templates"


class RenderError(RuntimeError):
    """A generated document failed validation and must not be used."""


@dataclass
class Selection:
    """What a tailoring pass decided to put on one resume.

    Bullets are carried as ``{id, text}`` so provenance survives into
    validation: ``id`` says which pool entry it came from, ``text`` is the
    possibly-reworded rendering of it.
    """

    summary: str = ""
    summary_id: str = ""
    experience: List[dict] = field(default_factory=list)
    projects: List[dict] = field(default_factory=list)
    skills: List[dict] = field(default_factory=list)

    def bullet_ids(self) -> List[str]:
        ids = [b["id"] for role in self.experience for b in role["bullets"]]
        ids += [b["id"] for project in self.projects for b in project["bullets"]]
        return ids


def _environment():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    return Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(profile: dict, selection: Selection,
                template: str = "resume.html.j2") -> str:
    return _environment().get_template(template).render(
        profile=profile,
        summary=selection.summary,
        experience=selection.experience,
        projects=selection.projects,
        skills=selection.skills,
    )


def html_to_pdf(html: str, destination: Path) -> Path:
    """Render HTML to PDF with WeasyPrint."""
    from weasyprint import HTML

    logging.getLogger("weasyprint").setLevel(logging.ERROR)
    logging.getLogger("fontTools").setLevel(logging.ERROR)

    destination.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(ROOT)).write_pdf(str(destination))
    return destination


# -- guardrails --------------------------------------------------------------


def page_count(pdf: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(pdf)).pages)


def pdf_text(pdf: Path) -> str:
    """Extract text, preferring pdftotext because its word spacing is saner.

    pypdf is the fallback so the guardrails still run in an environment
    without poppler installed - a missing binary must not silently disable a
    correctness check.
    """
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return result.stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf)).pages)


def _normalise(text: str) -> str:
    """Collapse whitespace so a line-wrap difference is not a false failure."""
    return re.sub(r"\s+", " ", text).strip().lower()


def immutable_facts(profile: dict) -> List[str]:
    """The strings that must survive tailoring unchanged.

    Deliberately excludes anything long enough to be re-wrapped across lines
    by the renderer - a wrapped URL is not a corrupted URL, and flagging it
    would make the check cry wolf until it got ignored.
    """
    facts = [profile["name"], profile["contact"]["email"], profile["contact"]["phone"]]
    for school in profile.get("education", []):
        facts += [school["school"], school["dates"]]
    for role in profile.get("experience", []):
        facts += [role["title"], role["org"], role["dates"]]
    return [f for f in facts if f]


def check_immutable(pdf: Path, profile: dict) -> List[str]:
    """Return the facts missing from the rendered PDF."""
    haystack = _normalise(pdf_text(pdf))
    return [fact for fact in immutable_facts(profile) if _normalise(fact) not in haystack]


def check_provenance(selection: Selection, pool_ids: Iterable[str]) -> List[str]:
    """Return bullet ids that do not exist in the pool."""
    known = set(pool_ids)
    return [bid for bid in selection.bullet_ids() if bid not in known]


def pool_bullet_ids(profile: dict) -> List[str]:
    ids = [b["id"] for role in profile.get("experience", []) for b in role["bullets"]]
    ids += [b["id"] for project in profile.get("projects", []) for b in project["bullets"]]
    return ids


def check_text_fidelity(selection: Selection, profile: dict,
                        max_drift: float = 0.6) -> List[str]:
    """Flag bullets reworded so heavily they no longer describe the same work.

    Provenance alone is not enough: a model can cite a real bullet id and then
    write something unrelated under it. This compares the rendered wording to
    the pool entry and rejects anything that shares too little vocabulary with
    its source.

    ``max_drift`` is the share of the *original*'s meaningful words allowed to
    disappear. Generous by design - rewording for a posting is the point, and
    the guardrail is there to catch substitution, not editing.
    """
    originals: Dict[str, str] = {}
    for role in profile.get("experience", []):
        originals.update({b["id"]: b["text"] for b in role["bullets"]})
    for project in profile.get("projects", []):
        originals.update({b["id"]: b["text"] for b in project["bullets"]})

    def _words(text: str) -> set:
        return {w for w in re.findall(r"[a-z0-9+#/.]+", text.lower()) if len(w) > 3}

    drifted = []
    for role in list(selection.experience) + list(selection.projects):
        for bullet in role["bullets"]:
            source = originals.get(bullet["id"])
            if not source:
                continue
            source_words = _words(source)
            if not source_words:
                continue
            kept = len(source_words & _words(bullet["text"])) / len(source_words)
            if kept < (1 - max_drift):
                drifted.append(
                    f"{bullet['id']} (only {kept:.0%} of the original wording remains)"
                )
    return drifted


def render_resume(profile: dict, selection: Selection, destination: Path,
                  max_pages: int = 1) -> Path:
    """Render and validate. Raises RenderError rather than emit a bad PDF."""
    html = render_html(profile, selection)
    pdf = html_to_pdf(html, destination)

    problems: List[str] = []

    pages = page_count(pdf)
    if pages > max_pages:
        problems.append(f"{pages} pages, expected at most {max_pages}")

    missing = check_immutable(pdf, profile)
    if missing:
        problems.append(f"facts missing from the output: {', '.join(missing)}")

    invented = check_provenance(selection, pool_bullet_ids(profile))
    if invented:
        problems.append(f"bullets not traceable to the pool: {', '.join(invented)}")

    drifted = check_text_fidelity(selection, profile)
    if drifted:
        problems.append(f"bullets reworded beyond recognition: {'; '.join(drifted)}")

    if problems:
        raise RenderError(f"{destination.name}: " + "; ".join(problems))

    log.info("rendered %s (%d page)", destination.name, pages)
    return pdf


def full_selection(profile: dict, summary_id: Optional[str] = None) -> Selection:
    """Everything in the pool - the untailored fallback, and the setup baseline.

    Used to prove the renderer reproduces the master before any tailoring is
    layered on, and used again at runtime whenever a tailored render fails
    validation and the company still deserves an application.
    """
    summary_id = summary_id or profile.get("default_summary")
    summary = next(
        (s["text"] for s in profile.get("summaries", []) if s["id"] == summary_id), ""
    )
    return Selection(
        summary=summary,
        summary_id=summary_id or "",
        experience=[
            {
                "title": role["title"], "org": role["org"],
                "location": role["location"], "dates": role["dates"],
                "bullets": [{"id": b["id"], "text": b["text"]} for b in role["bullets"]],
            }
            for role in profile.get("experience", [])
        ],
        projects=[
            {
                "name": project["name"], "stack": project["stack"],
                "bullets": [{"id": b["id"], "text": b["text"]} for b in project["bullets"]],
            }
            for project in profile.get("projects", [])
        ],
        skills=list(profile.get("skills", [])),
    )


def select_by_ids(profile: dict, bullet_ids: Iterable[str],
                  summary_id: Optional[str] = None,
                  skill_ids: Optional[Iterable[str]] = None) -> Selection:
    """Build a Selection from pool ids, preserving the pool's own ordering.

    Roles and projects with no surviving bullets are dropped entirely rather
    than rendered as a bare heading.
    """
    wanted = list(bullet_ids)
    order = {bid: index for index, bid in enumerate(wanted)}
    summary_id = summary_id or profile.get("default_summary")
    summary = next(
        (s["text"] for s in profile.get("summaries", []) if s["id"] == summary_id), ""
    )

    def _pick(bullets):
        chosen = [b for b in bullets if b["id"] in order]
        chosen.sort(key=lambda b: order[b["id"]])
        return [{"id": b["id"], "text": b["text"]} for b in chosen]

    experience = []
    for role in profile.get("experience", []):
        picked = _pick(role["bullets"])
        if picked:
            experience.append({
                "title": role["title"], "org": role["org"],
                "location": role["location"], "dates": role["dates"],
                "bullets": picked,
            })

    projects = []
    for project in profile.get("projects", []):
        picked = _pick(project["bullets"])
        if picked:
            projects.append({
                "name": project["name"], "stack": project["stack"], "bullets": picked,
            })

    groups = profile.get("skills", [])
    if skill_ids is not None:
        wanted_skills = list(skill_ids)
        groups = sorted(
            (g for g in groups if g["id"] in wanted_skills),
            key=lambda g: wanted_skills.index(g["id"]),
        )

    return Selection(
        summary=summary, summary_id=summary_id or "",
        experience=experience, projects=projects, skills=list(groups),
    )


def master_selection(profile: dict) -> Selection:
    """Exactly what appears on master.pdf - the fidelity baseline."""
    layout = profile.get("master_layout") or {}
    if not layout:
        return full_selection(profile)
    return select_by_ids(
        profile,
        bullet_ids=layout.get("bullets", []),
        summary_id=layout.get("summary"),
        skill_ids=layout.get("skills"),
    )


def load_profile(path: Optional[Path] = None) -> dict:
    import yaml

    path = path or ROOT / "profile" / "profile.yml"
    return yaml.safe_load(Path(path).read_text())
