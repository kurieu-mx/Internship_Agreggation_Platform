"""Catching the habits that make a letter read as machine-written.

These five were not chosen from a list of things people say about AI prose.
They were counted across 31 letters this pipeline actually produced, and they
are the ones that were there. Several tells that get named constantly turned
out to be absent already, because the letter prompt had banned them from the
start - across 14,384 words there were zero instances of "excited", "thrilled",
"passionate", "align", "resonate", "leverage", "robust", "seamless", "delve",
and zero of "I am writing to express my interest".

What was left is subtler, and all of it is the kind of thing that reads as
good writing one sentence at a time and as machine writing over a page:

===========================================================================
  tell                          measured        why it reads as machine
===========================================================================
  em dash                    151  (4.9/letter)  punctuation as drama
  colon-led reveal            63  (2.0/letter)  same move, different mark
  rule-of-three list          60  (1.9/letter)  rhythm nobody speaks in
  sentences over ~34 words    median 33 words   no short sentences at all
  "That is the same X as Y"   13 of 31 letters  one formula, every letter
===========================================================================

The through-line is uniformity. A person writing five cover letters produces
five different rhythms; this produced one rhythm thirty-one times. The em dash
is the most visible symptom rather than the disease - banning it alone would
just push the same appositive habit onto commas and colons, which is why the
colon reveal is measured here too.

These are counted, not trusted to a prompt. The letter prompt states the rules
and this module checks whether they were followed, exactly as the resume's
provenance and fidelity checks do: the prompt asks, the guardrail verifies.
"""

import re
import statistics
from typing import Dict, List, Tuple

# One "—" is a choice; five is a habit. Zero is asked for because the model
# reliably produces several when allowed any, and a comma or a full stop does
# the same work without the tell.
MAX_EM_DASHES = 0

# A colon introducing an explanatory clause - "the real problem: nobody owns
# it". The same dramatic reveal as the em dash, so capping one and not the
# other just moves the habit.
MAX_COLON_REVEALS = 1

# "autonomy, edge inference, and distributed command-and-control". One reads
# as deliberate; two or more per page is the cadence people notice.
MAX_TRICOLONS = 1

# Measured median was 33 words with a third of sentences over 40. Human cover
# letters run 15-25. The cap matters less than the variety: a page where every
# sentence is the same length reads as generated whatever that length is.
MAX_SENTENCE_WORDS = 38
MAX_MEAN_SENTENCE_WORDS = 24
MIN_SHORT_SENTENCE_SHARE = 0.15      # at least this many under 12 words

_EM_DASH_RE = re.compile(r"[—–]")

# A colon followed by a lowercase word is an explanation, not a list label.
_COLON_REVEAL_RE = re.compile(r":\s+[a-z]")

# "a, b, and c" - three or more comma-separated items ending in a conjunction.
_TRICOLON_RE = re.compile(
    r"\b[\w()+#./-]+(?:\s+[\w()+#./-]+){0,3},\s+"
    r"[\w()+#./-]+(?:\s+[\w()+#./-]+){0,3},\s+(?:and|or)\s+",
    re.I,
)

# The pivot every letter reached for: state the company's problem, then turn
# with "That is the same shape of problem as...". Once it is a nice sentence;
# thirty-one times it is a template.
_PIVOT_RE = re.compile(
    r"\bthat(?:'s| is)\s+(?:the\s+)?(?:same|exactly|precisely|the kind of|"
    r"the sort of|the shape|what|where)\b"
    r"|\bthe same (?:shape|kind|sort|type) of (?:problem|work|thing)\b",
    re.I,
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def letter_prose(letter: dict) -> str:
    """Just the sentences a reader reads, with the scaffolding left out.

    Measuring a rendered PDF instead would mean measuring pdftotext's view of
    a two-column layout, where a section heading and the row beside it become
    one 90-word "sentence" and every statistic is wrong.
    """
    parts: List[str] = [str(letter.get("hook") or ""),
                        str(letter.get("why_company") or ""),
                        str(letter.get("closing") or "")]
    for item in letter.get("what_i_bring") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("detail") or ""))
    for item in letter.get("selected_work") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("detail") or ""))
    return "\n\n".join(p.strip() for p in parts if p.strip())


def sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text)
            if len(s.split()) >= 3]


def count_tells(text: str) -> Dict[str, float]:
    """The five, counted. Cheap enough to run on every letter."""
    lengths = [len(s.split()) for s in sentences(text)] or [0]
    return {
        "em_dashes": len(_EM_DASH_RE.findall(text)),
        "colon_reveals": len(_COLON_REVEAL_RE.findall(text)),
        "tricolons": len(_TRICOLON_RE.findall(text)),
        "pivots": len(_PIVOT_RE.findall(text)),
        "longest_sentence": max(lengths),
        "mean_sentence": statistics.mean(lengths),
        "short_share": sum(1 for n in lengths if n < 12) / len(lengths),
    }


def problems(text: str) -> List[str]:
    """Which rules this draft broke, phrased so the model can act on them.

    Returned as instructions rather than diagnoses - this list is handed
    straight back to the model as the revision brief, so "cut the em dashes"
    is more useful than "em_dashes=4".
    """
    counts = count_tells(text)
    found: List[str] = []

    if counts["em_dashes"] > MAX_EM_DASHES:
        found.append(
            f"Remove all {int(counts['em_dashes'])} em dashes. Do not swap them "
            f"for colons or parentheses - restructure into separate sentences, "
            f"or use a comma where the clause is genuinely subordinate."
        )
    if counts["colon_reveals"] > MAX_COLON_REVEALS:
        found.append(
            f"There are {int(counts['colon_reveals'])} colons introducing an "
            f"explanation. Keep at most one; write the others as plain sentences."
        )
    if counts["tricolons"] > MAX_TRICOLONS:
        found.append(
            f"There are {int(counts['tricolons'])} three-item lists. Keep at "
            f"most one. Name two things, or one specific thing."
        )
    if counts["pivots"]:
        found.append(
            "Cut the \"That is the same shape of problem as...\" turn. Connect "
            "the experience to the role by stating what you did, without "
            "announcing the parallel."
        )
    if counts["longest_sentence"] > MAX_SENTENCE_WORDS:
        found.append(
            f"The longest sentence is {int(counts['longest_sentence'])} words. "
            f"Split anything over {MAX_SENTENCE_WORDS}."
        )
    if counts["mean_sentence"] > MAX_MEAN_SENTENCE_WORDS:
        found.append(
            f"Sentences average {counts['mean_sentence']:.0f} words, which is "
            f"essay length, not letter length. Bring the average under "
            f"{MAX_MEAN_SENTENCE_WORDS}."
        )
    if counts["short_share"] < MIN_SHORT_SENTENCE_SHARE:
        found.append(
            "Every sentence is roughly the same length. Include a few short "
            "ones - under ten words - so the page has a pulse."
        )
    return found


def score(text: str) -> int:
    """How many rules a draft breaks. Lower is better; used to pick a winner.

    A revision is only kept when it beats the draft on this, because a rewrite
    that fixes the dashes and doubles the sentence length is not an
    improvement, and asking a model to edit its own prose sometimes produces
    exactly that.
    """
    return len(problems(text))


def summarise(text: str) -> str:
    """One log line describing a letter's voice."""
    counts = count_tells(text)
    return (f"{int(counts['em_dashes'])} dashes, "
            f"{int(counts['tricolons'])} triples, "
            f"{int(counts['colon_reveals'])} colon-reveals, "
            f"{counts['mean_sentence']:.0f}w mean sentence")


# The prompt-side statement of the same rules. Kept here so the instruction
# and the check that enforces it cannot drift apart.
VOICE_RULES = """VOICE - these are the habits that make writing read as
machine-generated. They were measured across this pipeline's own output, so
they are not hypothetical:

- **No em dashes.** Not one. They were the single most common tell. A comma,
  a full stop, or a rewritten clause does the same work.
- **At most one colon introducing an explanation.** A colon used for drama is
  an em dash wearing a different hat.
- **At most one three-item list** in the whole letter. "Autonomy, edge
  inference, and distributed command-and-control" is a cadence nobody speaks
  in. Name two things, or name one and be specific about it.
- **Vary sentence length.** Average under 24 words, nothing over 38, and
  include a few short sentences. Uniform length is what makes a page feel
  generated regardless of what it says.
- **Never announce a parallel before drawing it.** No sentence may begin
  "That is the same shape of problem as...", "That is the kind of work...",
  "That is exactly what...", or "That is what I want out of...". This was the
  single most repeated move across the letters measured, and it is the one
  that makes them feel written to a template. Trust the reader to see the
  connection you have just laid out.

      Instead of:  I built a validation pipeline around every model call.
                   That is the same kind of work this role describes.
      Write:       I built a validation pipeline around every model call,
                   which is what this role is asking for.
      Or better:   I built a validation pipeline around every model call.

  The third version is usually right. If the parallel needs stating, you have
  not made it clearly enough.

Write the way a competent engineer writes when they are being direct: plain
sentences, concrete nouns, no rhetorical scaffolding."""
