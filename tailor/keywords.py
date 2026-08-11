"""Aligning a tailored resume's vocabulary with the posting's.

Why this exists
---------------
Most applications are read by software before a human sees them. Applicant
tracking systems match the posting's terms against the resume's, and a resume
that describes the same work in different words scores as though it described
different work. "Built inference pipelines in PyTorch" and "productionised deep
learning models" are the same sentence to a person and unrelated strings to a
keyword matcher.

So the tailoring pass is told, explicitly, which of the posting's words it
should be using.

The line this must not cross
----------------------------
Keyword optimisation collapses into lying the moment it inserts a term the
candidate cannot support. The defence is structural rather than a warning in a
prompt: **the vocabulary is derived from the profile.** A term is only ever
suggested to the model if some pool entry, skill group, or project stack
already evidences it, so "use the posting's word for this" can only ever
re-label real work.

Terms the posting asks for and the profile cannot evidence are extracted too,
but they travel in a separate list that the model is told to avoid. They are
worth knowing - they are the honest gaps - and they are never worth claiming.

Why not just count words
------------------------
Measured on a real IMC posting, the top terms by frequency were "markets",
"environment", "may", "base", "salary". Frequency finds the boilerplate,
because boilerplate is what gets repeated. Matching against a known technical
vocabulary finds the requirements.
"""

import logging
import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

log = logging.getLogger(__name__)

# Surface forms that mean the same thing. Written posting-side -> canonical,
# because the posting is where the variation lives: a profile says "Go
# (Golang)" once, and postings say "Go", "Golang", and "Go programming".
ALIASES: Dict[str, Tuple[str, ...]] = {
    "go": ("golang", "go programming", "go lang"),
    "python": ("python3", "cpython"),
    "c++": ("cpp", "c/c++", "modern c++", "c++11", "c++14", "c++17", "c++20"),
    "javascript": ("js", "es6", "ecmascript"),
    "typescript": ("ts",),
    "postgresql": ("postgres", "psql"),
    "kubernetes": ("k8s",),
    "machine learning": ("ml", "statistical learning"),
    "deep learning": ("dl", "neural networks", "neural nets"),
    "natural language processing": ("nlp",),
    "computer vision": ("cv", "image processing", "vision"),
    "large language models": ("llm", "llms", "foundation models",
                              "generative ai", "genai"),
    "reinforcement learning": ("rl",),
    "pytorch": ("torch",),
    "tensorflow": ("tf", "keras"),
    "gcp": ("google cloud", "google cloud platform"),
    "aws": ("amazon web services",),
    "ci/cd": ("continuous integration", "continuous delivery", "cicd"),
    "rest": ("restful", "rest api", "rest apis"),
    "distributed systems": ("distributed computing", "distributed"),
    "low latency": ("low-latency", "latency-sensitive", "latency sensitive",
                    "high performance", "high-performance"),
    "data structures": ("data structures and algorithms", "dsa"),
    "object oriented": ("object-oriented", "oop"),
    "linux": ("unix", "posix"),
    "sql": ("relational databases",),
    "docker": ("containers", "containerisation", "containerization"),
    "ros 2": ("ros2", "ros", "robot operating system"),
    "cuda": ("gpu programming", "gpu kernels"),
    "opencv": ("open cv",),
    "time series": ("time-series", "timeseries"),
    "quantitative research": ("quant research", "systematic research"),
    "statistics": ("statistical", "statistical modeling", "statistical modelling"),
    "probability": ("probabilistic",),
    "microservices": ("micro-services", "service oriented", "service-oriented"),
    "api design": ("api development", "building apis"),
    "concurrency": ("concurrent", "multithreading", "multi-threading",
                    "parallel programming", "parallelism"),
    "systems programming": ("systems engineering", "low level", "low-level"),
    "data pipelines": ("etl", "data engineering", "elt"),
    "model deployment": ("mlops", "productionising models", "productionizing models",
                         "model serving", "inference serving"),
    "backend": ("back-end", "back end", "server-side", "server side"),
    "frontend": ("front-end", "front end", "client-side"),
    "full stack": ("full-stack", "fullstack"),
    "version control": ("git", "github", "gitlab"),
    "agile": ("scrum", "sprint"),
    "testing": ("unit testing", "unit tests", "test coverage", "automated testing"),
    "algorithms": ("algorithmic", "algorithm design"),
    "embedded": ("embedded systems", "firmware", "bare metal"),
    "autonomy": ("autonomous systems", "autonomous vehicles", "self-driving"),
    "simulation": ("simulations", "simulated environments"),
    "optimization": ("optimisation", "optimizing", "optimising"),
    "data analysis": ("data analytics", "analytics", "exploratory analysis"),
    "numpy": ("scipy",),
    "pandas": ("dataframes",),
}

# Terms whose surface form is too common in ordinary English to match on its
# own. "Go" appears in "go to market"; "R" appears everywhere. These match only
# via an alias or a language-context phrase.
AMBIGUOUS = {"go", "r", "c", "rest", "vision", "testing", "agile", "simulation"}

_LANGUAGE_CONTEXT = re.compile(
    r"(?:languages?|proficien\w+|experience|fluent|written|program\w*|coding|"
    r"such as|including|e\.g\.|like)\b[^.;\n]{0,90}$", re.I)

# Requirement vocabulary the profile may not cover. Extracted so the gap can be
# reported and so the model can be told what *not* to reach for. Deliberately
# not merged with the profile-derived list - the whole design rests on those
# two staying separate.
GENERAL_VOCABULARY = (
    "kubernetes", "terraform", "spark", "hadoop", "kafka", "airflow", "dbt",
    "snowflake", "redis", "mongodb", "postgresql", "graphql", "grpc", "rust",
    "java", "scala", "kotlin", "swift", "ruby", "php", "matlab", "julia",
    "react", "node.js", "django", "flask", "fastapi", "spring",
    "aws", "azure", "ci/cd", "microservices", "rest", "api design",
    "machine learning", "deep learning", "natural language processing",
    "computer vision", "large language models", "reinforcement learning",
    "tensorflow", "jax", "hugging face", "transformers", "scikit-learn",
    "distributed systems", "low latency", "concurrency", "systems programming",
    "data pipelines", "data structures", "algorithms", "object oriented",
    "statistics", "probability", "linear algebra", "calculus", "stochastic",
    "time series", "quantitative research", "derivatives", "market making",
    "trading systems", "portfolio", "risk management", "backtesting",
    "model deployment", "data analysis", "experimentation", "a/b testing",
    "version control", "testing", "agile", "embedded", "autonomy",
    "simulation", "optimization", "networking", "operating systems",
    "compilers", "databases", "security", "cryptography",
)

# Section headings that introduce what the employer actually wants. Terms found
# under one of these outrank terms found in the benefits blurb.
_REQUIREMENT_HEADING = re.compile(
    r"(what you'?ll (?:do|need|bring)|requirements?|qualifications?|"
    r"responsibilities|skills|about you|who you are|we'?re looking for|"
    r"you (?:have|will|should)|minimum|preferred|basic qualifications|"
    r"tech(?:nical)? (?:stack|skills)|experience with)", re.I)


def _surface_forms(canonical: str) -> Tuple[str, ...]:
    """Every string that counts as a mention of ``canonical``."""
    return (canonical,) + ALIASES.get(canonical, ())


def _pattern(term: str) -> re.Pattern:
    """Word-boundary matcher for one surface form.

    Two details that are easy to get wrong:

    * ``\\b`` does not work at the edge of ``c++`` - the boundary falls between
      ``c`` and ``+``, so ``\\bc\\+\\+\\b`` never matches anything. A term
      ending in punctuation gets a negative lookahead instead.
    * Multi-word terms must match across a hyphen as well as a space, because
      postings write both "low latency" and "low-latency".
    """
    # Not re.escape(term) wholesale: since 3.7 it leaves spaces alone, so
    # substituting an escaped space afterwards silently does nothing.
    parts = term.split(" ")
    # Plural tolerance on the final word only, and only when it is long enough
    # for a trailing "s" to be inflection rather than the word. A posting
    # saying "transformers" must match a profile saying "transformer", and
    # "aws" must not be allowed to match "aw".
    last = parts[-1]
    if last[-1].isalnum() and len(last) >= 5:
        stem = last[:-1] if last.endswith("s") else last
        parts[-1] = re.escape(stem) + "s?"
    else:
        parts[-1] = re.escape(last)
    escaped = r"[\s\-]+".join(
        [re.escape(part) for part in parts[:-1]] + [parts[-1]]
    )
    left = r"\b" if term[0].isalnum() else r"(?<![\w+#])"
    right = r"\b" if term[-1].isalnum() else r"(?![\w+#])"
    return re.compile(left + escaped + right, re.I)


_CACHE: Dict[str, re.Pattern] = {}


def _matcher(term: str) -> re.Pattern:
    if term not in _CACHE:
        _CACHE[term] = _pattern(term)
    return _CACHE[term]


def _mentions(text: str, canonical: str) -> Optional[str]:
    """The surface form ``text`` uses for ``canonical``, or None.

    Returns the posting's *own* wording rather than the canonical form, since
    the posting's wording is the thing an ATS is matching on.
    """
    for form in _surface_forms(canonical):
        match = _matcher(form).search(text)
        if not match:
            continue
        # An ambiguous term needs to look like it is naming a technology.
        # Checked on the surface form, not the canonical one: "vision" is an
        # alias of "computer vision", and "our vision is to..." must not count.
        if form in AMBIGUOUS:
            preceding = text[max(0, match.start() - 100):match.start()]
            if not _LANGUAGE_CONTEXT.search(preceding):
                continue
        return match.group(0)
    return None


# -- what the candidate can actually claim -----------------------------------


def profile_vocabulary(profile: dict) -> Dict[str, str]:
    """Canonical term -> where in the profile it is evidenced.

    This is the allowlist. Everything the tailoring pass is encouraged to say
    has to come through here, which is what stops keyword optimisation becoming
    fabrication: a term with no entry has no evidence, so it is never suggested.
    """
    corpus: List[Tuple[str, str]] = []

    def add(text: str, where: str, tags: Iterable[str] = ()) -> None:
        """Record one piece of evidence, its own tags included.

        Tags count. A bullet reading "implemented a 7.5M-parameter decoder-only
        transformer from scratch in PyTorch" is deep-learning work by any
        reading, but the words "deep learning" appear nowhere in it, so literal
        matching alone would report the candidate's strongest area as a gap.
        The tags are the candidate's own classification of their own work, and
        treating them as evidence is what lets the posting's vocabulary reach a
        bullet that demonstrates the skill without naming it.
        """
        corpus.append((f"{text} {' '.join(tags)}".strip(), where))

    for group in profile.get("skills", []):
        label = group.get("label", "skills")
        for item in group.get("items", []):
            add(str(item), f"skills: {label}", group.get("tags", []))

    for role in profile.get("experience", []):
        where = str(role.get("org", "experience"))
        for bullet in role.get("bullets", []):
            add(bullet.get("text", ""), where, bullet.get("tags", []))

    for project in profile.get("projects", []):
        where = str(project.get("name", "project"))
        add(project.get("stack", ""), f"{where} (stack)")
        for bullet in project.get("bullets", []):
            add(bullet.get("text", ""), where, bullet.get("tags", []))

    for summary in profile.get("summaries", []):
        add(summary.get("text", ""), "summary", summary.get("tags", []))

    # A term is claimable if any surface form of it appears anywhere above.
    # Checked against the profile text directly rather than against a fixed
    # list, so adding a skill to profile.yml widens the vocabulary with no
    # code change.
    vocabulary: Dict[str, str] = {}
    candidates = set(GENERAL_VOCABULARY) | set(ALIASES)
    for canonical in candidates:
        for text, where in corpus:
            if not text:
                continue
            if _mentions(text, canonical):
                vocabulary[canonical] = where
                break
    return vocabulary


# -- what the posting asks for -----------------------------------------------


def _requirement_region(description: str) -> str:
    """The part of a posting that states requirements, if it is separable.

    Everything after the first requirements-ish heading. Falls back to the
    whole text, because plenty of postings have no headings at all.
    """
    match = _REQUIREMENT_HEADING.search(description)
    return description[match.start():] if match else description


def posting_terms(job) -> Dict[str, str]:
    """Canonical term -> the posting's own wording for it.

    Reads the title and description. The title counts double in ranking terms
    but is merged here; ``align`` does the ordering.
    """
    text = f"{job.title}\n{job.description or ''}"
    if not text.strip():
        return {}

    found: Dict[str, str] = {}
    for canonical in set(GENERAL_VOCABULARY) | set(ALIASES):
        surface = _mentions(text, canonical)
        if surface:
            found[canonical] = surface
    return found


class KeywordBrief:
    """What to say, what not to reach for, and how well it landed.

    ``matched`` is the useful half: terms the posting asks for *and* the
    profile evidences, carrying the posting's own wording. ``missing`` is the
    honest half - real requirements with no support behind them.
    """

    def __init__(self, matched: Sequence[Tuple[str, str, str]] = (),
                 missing: Sequence[str] = ()):
        # (canonical, posting's wording, where the profile evidences it)
        self.matched = list(matched)
        self.missing = list(missing)

    @property
    def terms(self) -> List[str]:
        return [surface for _, surface, _ in self.matched]

    def __bool__(self) -> bool:
        return bool(self.matched)

    def prompt_block(self, limit: int = 14) -> str:
        """The instruction given to the tailoring model.

        Capped, because a long list invites stuffing. Names the evidence beside
        each term so the model can see the rewording it is allowed to make -
        and states the prohibition in the same breath, next to the terms it
        applies to, rather than as a general disclaimer elsewhere.
        """
        if not self.matched:
            return ""

        lines = ["KEYWORDS THIS POSTING MATCHES ON.",
                 "Each is already evidenced in the pool. Where a bullet describes",
                 "this work, use the posting's wording for it rather than a synonym.",
                 "Do not attach a term to a bullet that does not already support it,",
                 "and do not list a term twice to raise its count.",
                 ""]
        for _, surface, evidence in self.matched[:limit]:
            lines.append(f"  {surface}  (evidenced in: {evidence})")

        if self.missing:
            lines += ["", "ASKED FOR, BUT NOT IN THE POOL - never claim these:",
                      "  " + ", ".join(self.missing[:12])]
        return "\n".join(lines)


def align(job, profile: dict) -> KeywordBrief:
    """Intersect one posting's requirements with the profile's evidence.

    Ordering matters more than it looks: the prompt block is capped, so
    whatever sorts first is what the resume actually gets optimised for. Title
    terms rank above body terms, and body terms found in a requirements section
    rank above ones found in the benefits blurb.
    """
    asked = posting_terms(job)
    if not asked:
        return KeywordBrief()

    vocabulary = profile_vocabulary(profile)
    title = job.title or ""
    requirements = _requirement_region(job.description or "")

    def rank(canonical: str) -> tuple:
        in_title = bool(_mentions(title, canonical))
        in_requirements = bool(_mentions(requirements, canonical))
        # Longer canonical forms are more specific and less likely to be noise.
        return (not in_title, not in_requirements, -len(canonical))

    matched = sorted(
        ((c, asked[c], vocabulary[c]) for c in asked if c in vocabulary),
        key=lambda row: rank(row[0]),
    )
    missing = sorted((c for c in asked if c not in vocabulary),
                     key=rank)

    log.debug("keywords for %s: %d matched, %d missing",
              getattr(job, "company", "?"), len(matched), len(missing))
    return KeywordBrief(matched, missing)


# -- ordering the parts of the resume we can reorder honestly ----------------


def prioritise_skills(selection, brief: KeywordBrief) -> None:
    """Reorder skill items so posting-relevant ones read first. In place.

    The safest optimisation available: every item shown is one the candidate
    already listed, and only the order changes. A recruiter skims the first few
    entries of each line, and a keyword matcher does not care about order at
    all - so this costs nothing and can only help.
    """
    if not brief.matched:
        return

    wanted = {canonical for canonical, _, _ in brief.matched}

    def relevance(item: str) -> int:
        return 0 if any(_mentions(str(item), c) for c in wanted) else 1

    # Keyed on relevance alone, so Python's stable sort preserves the
    # candidate's own ordering within each tier. An earlier version broke the
    # tie on the item name, which silently alphabetised every group where
    # nothing matched - turning a curated "PyTorch, CUDA, ONNX" into
    # "BigQuery, Docker, Firestore, Git" and losing information the candidate
    # had deliberately encoded in the order.
    for group in selection.skills:
        group["items"] = sorted(group["items"], key=relevance)


def rank_skill_groups(profile: dict, brief: KeywordBrief,
                      limit: int = 3) -> List[str]:
    """Which skill groups carry the most of what this posting asks for.

    Used as a deterministic fallback when the model returns no skill ids, and
    as a sanity check when it does.
    """
    if not brief.matched:
        return []

    wanted = {canonical for canonical, _, _ in brief.matched}
    scored = []
    for group in profile.get("skills", []):
        text = " ".join(str(i) for i in group.get("items", []))
        hits = sum(1 for canonical in wanted if _mentions(text, canonical))
        if hits:
            scored.append((-hits, group["id"]))
    scored.sort()
    return [group_id for _, group_id in scored[:limit]]


# -- measuring the result ----------------------------------------------------


def coverage(text: str, brief: KeywordBrief) -> Tuple[float, List[str]]:
    """How much of the brief the rendered resume actually says.

    Returns ``(fraction, terms_still_missing)``. Reported rather than enforced:
    a term can legitimately fail to land because no bullet survived the page
    budget, and rejecting a good resume over a keyword would be optimising the
    wrong thing.
    """
    if not brief.matched:
        return 1.0, []

    landed, absent = 0, []
    for canonical, surface, _ in brief.matched:
        if _mentions(text, canonical):
            landed += 1
        else:
            absent.append(surface)
    return landed / len(brief.matched), absent
