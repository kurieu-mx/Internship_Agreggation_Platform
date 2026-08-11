"""Keyword alignment, and the line it must not cross.

Two properties matter here, and they pull against each other. The module has to
find enough of a posting's vocabulary to be worth running, and it must never
put a term in front of the model that the candidate cannot back up. Most of
these tests are about the second one, because that is the failure that ends up
in front of an interviewer.
"""

import pytest

from models import Job
from tailor.keywords import (KeywordBrief, align, coverage, posting_terms,
                             prioritise_skills, profile_vocabulary,
                             rank_skill_groups)
from tailor.render import Selection

PROFILE = {
    "summaries": [{"id": "sum-swe", "tags": ["software"],
                   "text": "Systems-minded engineer."}],
    "skills": [
        {"id": "skills-lang", "label": "Languages", "tags": ["software"],
         "items": ["C++", "Python", "Go (Golang)", "SQL", "TypeScript"]},
        {"id": "skills-ml", "label": "ML & AI", "tags": ["deep learning", "ml"],
         "items": ["PyTorch", "CUDA", "ONNX"]},
    ],
    "experience": [{
        "title": "SWE Intern", "org": "Swarmos", "dates": "2025",
        "bullets": [
            {"id": "swarm-consensus", "tags": ["distributed", "concurrency"],
             "text": "Fixed leader-election duel loops across a multi-node swarm."},
            {"id": "swarm-ros", "tags": ["robotics"],
             "text": "Built ROS 2 telemetry bridges over MAVLink."},
        ],
    }],
    "projects": [{
        "name": "AdHoc-LM", "stack": "PyTorch, Python",
        "bullets": [{"id": "lm-transformer", "tags": ["transformers", "deep learning"],
                     "text": "Implemented a 7.5M-parameter decoder-only transformer "
                             "from scratch, including byte-level BPE."}],
    }],
}


def job(title="Software Engineer Intern", description=""):
    return Job(company="Acme", title=title, locations=["NYC"],
               field_category="Software", description=description)


# -- the guardrail: only what the profile can back ---------------------------


def test_a_term_the_profile_cannot_evidence_is_never_suggested():
    """The whole design in one test. Rust is a real requirement and a real gap."""
    brief = align(job(description="Required: Rust, Kubernetes, and Python."), PROFILE)
    assert "Python" in brief.terms
    assert not any("rust" in t.lower() for t in brief.terms)
    assert not any("kubernetes" in t.lower() for t in brief.terms)


def test_unsupported_terms_are_reported_as_gaps_not_dropped():
    """They are worth knowing about; they are just never worth claiming."""
    brief = align(job(description="Required: Rust and Kubernetes."), PROFILE)
    assert "rust" in brief.missing
    assert "kubernetes" in brief.missing


def test_the_prompt_block_names_the_prohibition_beside_the_gap():
    brief = align(job(description="We use Python, Rust and Kubernetes."), PROFILE)
    block = brief.prompt_block()
    assert "never claim these" in block
    assert "rust" in block.split("never claim these")[1]


def test_the_prompt_block_carries_evidence_for_every_term_it_suggests():
    """A term with no stated evidence is a term the model has to guess about."""
    brief = align(job(description="Deep experience with PyTorch and CUDA."), PROFILE)
    block = brief.prompt_block()
    for _, surface, _ in brief.matched:
        assert surface in block
    assert "evidenced in:" in block


def test_the_suggestion_list_is_capped():
    """A long list invites stuffing, which reads worse than no optimisation."""
    brief = KeywordBrief([(f"t{i}", f"t{i}", "somewhere") for i in range(40)])
    assert brief.prompt_block(limit=5).count("evidenced in:") == 5


# -- tags as evidence --------------------------------------------------------


def test_a_tag_evidences_a_term_the_bullet_never_names():
    """The transformer bullet is deep-learning work without saying so.

    Literal matching alone would report the candidate's strongest area as a
    gap, and then instruct the model not to mention it.
    """
    vocabulary = profile_vocabulary(PROFILE)
    assert "deep learning" in vocabulary
    assert "transformers" in vocabulary


def test_evidence_points_at_where_it_came_from():
    vocabulary = profile_vocabulary(PROFILE)
    assert "AdHoc-LM" in vocabulary["transformers"]


# -- extraction quality ------------------------------------------------------


def test_boilerplate_does_not_become_a_keyword():
    """Frequency ranking surfaced "salary" and "base" on a real posting."""
    terms = posting_terms(job(description=(
        "The base salary range for this position may vary. "
        "Our environment is fast-paced and our markets are global."
    )))
    assert terms == {}


def test_an_alias_matches_and_reports_the_postings_own_wording():
    """An ATS matches the string the posting used, not our canonical form."""
    brief = align(job(description="Strong Golang experience required."), PROFILE)
    assert "Golang" in brief.terms


@pytest.mark.parametrize("text", [
    "Experience with C++ required.",
    "Proficiency in modern C++.",
    "We write C/C++ every day.",
])
def test_cpp_matches_despite_the_word_boundary_problem(text):
    r"""\bc\+\+\b never matches: the boundary falls between "c" and "+"."""
    assert "c++" in posting_terms(job(description=text))


def test_a_hyphenated_posting_matches_a_spaced_profile():
    assert "low latency" in posting_terms(job(description="Low-latency trading systems."))


def test_a_plural_in_the_posting_matches_a_singular_in_the_profile():
    assert "transformers" in posting_terms(job(description="Training large transformer models."))


def test_a_short_word_ending_in_s_is_not_stemmed_into_nonsense():
    """"aws" must not become "aw" and start matching anything."""
    assert "aws" not in posting_terms(job(description="He saw the raw data."))


def test_an_ambiguous_bare_term_needs_technical_context():
    """"Go" appears in ordinary English far more often than as a language."""
    assert "go" not in posting_terms(job(description="Ready to go to market fast."))
    assert "go" in posting_terms(job(description="Languages: Go, Python, C++."))


def test_an_ambiguous_alias_is_also_checked_for_context():
    """"vision" aliases "computer vision"; a mission statement is not that."""
    assert "computer vision" not in posting_terms(
        job(description="Our vision is to change how the world trades."))


def test_a_posting_with_no_text_yields_nothing():
    assert posting_terms(job(title="", description="")) == {}
    assert not align(job(title="", description=""), PROFILE)


# -- ranking -----------------------------------------------------------------


def test_a_term_in_the_title_outranks_one_buried_in_the_body():
    """The list is capped, so ordering decides what actually gets optimised."""
    brief = align(job(title="Deep Learning Intern",
                      description="Requirements: SQL. We also do deep learning."),
                  PROFILE)
    assert brief.matched[0][0] == "deep learning"


def test_a_requirement_outranks_the_benefits_blurb():
    brief = align(job(title="Intern", description=(
        "Perks: free lunch, and a Python-themed party.\n"
        "Requirements: strong C++ fundamentals."
    )), PROFILE)
    assert brief.matched[0][0] == "c++"


# -- the reorderings that need no validation ---------------------------------


def test_skill_items_lead_with_what_the_posting_asked_for():
    selection = Selection(skills=[dict(PROFILE["skills"][0])])
    brief = align(job(description="Languages: TypeScript and SQL."), PROFILE)
    prioritise_skills(selection, brief)
    assert set(selection.skills[0]["items"][:2]) == {"SQL", "TypeScript"}


def test_reordering_never_adds_or_drops_an_item():
    """It is only safe because it is only a permutation."""
    selection = Selection(skills=[dict(PROFILE["skills"][0])])
    before = set(selection.skills[0]["items"])
    prioritise_skills(selection, align(job(description="Python and Go."), PROFILE))
    assert set(selection.skills[0]["items"]) == before


def test_an_empty_brief_leaves_the_order_alone():
    selection = Selection(skills=[dict(PROFILE["skills"][0])])
    before = list(selection.skills[0]["items"])
    prioritise_skills(selection, KeywordBrief())
    assert selection.skills[0]["items"] == before


def test_a_group_with_no_matches_keeps_the_candidates_own_order():
    """Observed live: a name tiebreaker alphabetised every unmatched group,
    turning a curated "PyTorch, CUDA, ONNX" into "BigQuery, Docker, Firestore"
    and discarding ordering the candidate had chosen deliberately."""
    selection = Selection(skills=[dict(PROFILE["skills"][1])])   # ML group
    before = list(selection.skills[0]["items"])
    prioritise_skills(selection, align(job(description="We use SQL."), PROFILE))
    assert selection.skills[0]["items"] == before


def test_relevant_items_move_up_without_reshuffling_the_rest():
    selection = Selection(skills=[dict(PROFILE["skills"][0])])
    prioritise_skills(selection, align(job(description="SQL required."), PROFILE))
    # SQL leads; everything else holds its original relative order.
    assert selection.skills[0]["items"][0] == "SQL"
    assert selection.skills[0]["items"][1:] == ["C++", "Python", "Go (Golang)",
                                                "TypeScript"]


def test_skill_groups_rank_by_how_much_of_the_posting_they_carry():
    brief = align(job(description="We need PyTorch and CUDA experience."), PROFILE)
    assert rank_skill_groups(PROFILE, brief)[0] == "skills-ml"


# -- measurement -------------------------------------------------------------


def test_coverage_counts_what_the_page_actually_says():
    brief = align(job(description="Python, C++, and PyTorch."), PROFILE)
    landed, absent = coverage("Built things in Python and C++.", brief)
    assert 0 < landed < 1
    assert "PyTorch" in absent


def test_full_coverage_reports_no_gaps():
    brief = align(job(description="Python and C++."), PROFILE)
    landed, absent = coverage("Python and C++ work.", brief)
    assert landed == 1.0 and absent == []


def test_an_empty_brief_is_fully_covered_by_definition():
    assert coverage("anything", KeywordBrief()) == (1.0, [])
