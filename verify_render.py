"""Prove the renderer reproduces master.pdf, rather than merely resembling it.

Since only a PDF of the resume exists, the layout had to be rebuilt in HTML and
CSS from measurements taken off the original. "It looks about right" is not a
standard that survives contact with a real application, so this renders the
exact selection that appears on the master and diffs it against the original,
word for word and page for page.

Run it after any change to the template or stylesheet:

    make verify-render
"""

import difflib
import logging
import re
from pathlib import Path

from tailor.render import (
    ROOT,
    check_immutable,
    html_to_pdf,
    load_profile,
    master_selection,
    page_count,
    pdf_text,
    render_html,
)

log = logging.getLogger(__name__)

MASTER = ROOT / "profile" / "master.pdf"
OUTPUT = ROOT / "out" / "_verify_master.pdf"


def _words(text: str) -> list:
    """Comparable word stream: layout differences must not register as drift."""
    text = text.replace("–", "-").replace("—", "-").replace("’", "'")
    return re.findall(r"[A-Za-z0-9@.+#/()',&-]+", text.lower())


def run() -> int:
    logging.getLogger("weasyprint").setLevel(logging.ERROR)
    logging.getLogger("fontTools").setLevel(logging.ERROR)

    if not MASTER.exists():
        print(f"  no master at {MASTER}")
        print("  -> copy your resume PDF there as profile/master.pdf")
        return 1

    profile = load_profile()
    selection = master_selection(profile)
    rebuilt = html_to_pdf(render_html(profile, selection), OUTPUT)

    master_pages, rebuilt_pages = page_count(MASTER), page_count(rebuilt)
    master_words, rebuilt_words = _words(pdf_text(MASTER)), _words(pdf_text(rebuilt))

    print(f"\n  master:  {master_pages} page(s), {len(master_words)} words")
    print(f"  rebuilt: {rebuilt_pages} page(s), {len(rebuilt_words)} words  ({rebuilt})")

    problems = []

    if rebuilt_pages != master_pages:
        problems.append(f"page count differs: {rebuilt_pages} vs {master_pages}")

    missing = check_immutable(rebuilt, profile)
    if missing:
        problems.append(f"facts missing from the rebuild: {', '.join(missing)}")

    matcher = difflib.SequenceMatcher(None, master_words, rebuilt_words)
    similarity = matcher.ratio()
    print(f"  text similarity: {similarity:.1%}")

    only_master, only_rebuilt = [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            only_master.extend(master_words[i1:i2])
        if tag in ("replace", "insert"):
            only_rebuilt.extend(rebuilt_words[j1:j2])

    if only_master:
        print(f"\n  in master but not the rebuild ({len(only_master)}):")
        print(f"    {' '.join(only_master[:40])}")
    if only_rebuilt:
        print(f"\n  in rebuild but not the master ({len(only_rebuilt)}):")
        print(f"    {' '.join(only_rebuilt[:40])}")

    # The threshold is not 100%: the master's contact line abbreviates URLs
    # that profile.yml stores in full, and that difference is intentional.
    if similarity < 0.97:
        problems.append(f"text similarity {similarity:.1%} is below 97%")

    if problems:
        print("\n  FAILED:")
        for problem in problems:
            print(f"    - {problem}")
        return 1

    print("\n  The rebuild reproduces the master.")
    return 0
