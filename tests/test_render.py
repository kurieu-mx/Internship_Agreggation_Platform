"""The renderer and its guardrails.

The guardrails are the reason this pipeline can be trusted to email documents
with someone's name on them without a human reading each one first, so they
get tested harder than the rendering does. Each one must *fail the render*,
not warn: a resume that quietly went out with invented experience is a far
worse outcome than one that did not go out at all.
"""

from pathlib import Path

import pytest

from tailor.render import (
    RenderError,
    Selection,
    check_immutable,
    check_provenance,
    check_text_fidelity,
    full_selection,
    immutable_facts,
    load_profile,
    master_selection,
    page_count,
    pdf_text,
    render_resume,
    select_by_ids,
)

REAL_PROFILE = Path(__file__).resolve().parent.parent / "profile" / "profile.yml"


@pytest.fixture
def profile():
    return {
        "name": "Ada Lovelace",
        "default_summary": "sum-a",
        "contact": {"email": "ada@example.com", "phone": "+1 (555) 010-0000",
                    "links": [{"text": "site", "url": "https://example.com"}]},
        "summaries": [
            {"id": "sum-a", "tags": ["software"], "text": "A software summary."},
            {"id": "sum-b", "tags": ["ml"], "text": "An ML summary."},
        ],
        "education": [{"school": "Analytical Engine University", "location": "London",
                       "degree": "B.S. Mathematics", "dates": "1833 - 1837"}],
        "experience": [{
            "id": "role1", "title": "Engineer", "org": "Babbage Ltd",
            "location": "Remote", "dates": "1840 - 1842",
            "bullets": [
                {"id": "b1", "tags": ["software"],
                 "text": "Wrote the first published algorithm for a mechanical computer."},
                {"id": "b2", "tags": ["ml"],
                 "text": "Designed punch-card sequences for computing Bernoulli numbers."},
            ],
        }],
        "projects": [{
            "id": "p1", "name": "Notes", "stack": "Analysis",
            "bullets": [{"id": "b3", "tags": ["writing"],
                         "text": "Published extensive translator notes on the engine."}],
        }],
        "skills": [{"id": "s1", "label": "Languages", "tags": [], "items": ["Maths"]}],
    }


@pytest.fixture
def out(tmp_path):
    return tmp_path / "resume.pdf"


# -- selection ---------------------------------------------------------------


def test_full_selection_takes_the_whole_pool(profile):
    assert sorted(full_selection(profile).bullet_ids()) == ["b1", "b2", "b3"]


def test_selecting_by_id_keeps_only_those_bullets(profile):
    assert select_by_ids(profile, ["b2"]).bullet_ids() == ["b2"]


def test_a_role_with_no_selected_bullets_is_dropped_entirely(profile):
    """Otherwise you get a job heading with nothing under it."""
    selection = select_by_ids(profile, ["b3"])
    assert selection.experience == []
    assert len(selection.projects) == 1


def test_the_requested_bullet_order_is_honoured(profile):
    assert select_by_ids(profile, ["b2", "b1"]).bullet_ids() == ["b2", "b1"]


def test_a_summary_can_be_chosen_by_id(profile):
    assert select_by_ids(profile, ["b1"], summary_id="sum-b").summary == "An ML summary."


def test_the_default_summary_is_used_when_none_is_named(profile):
    assert select_by_ids(profile, ["b1"]).summary == "A software summary."


def test_skills_can_be_narrowed(profile):
    assert select_by_ids(profile, ["b1"], skill_ids=[]).skills == []


# -- guardrail: page count ---------------------------------------------------


def test_a_normal_resume_renders_to_one_page(profile, out):
    render_resume(profile, full_selection(profile), out)
    assert page_count(out) == 1


def test_an_overlong_resume_is_rejected(profile, out):
    """A tailored resume that spills to page two is a bug, not a variant."""
    long_bullets = [
        {"id": f"long{n}", "tags": [], "text": "Engineered a distributed system. " * 30}
        for n in range(40)
    ]
    profile["experience"][0]["bullets"].extend(long_bullets)

    with pytest.raises(RenderError, match="pages"):
        render_resume(profile, full_selection(profile), out)


def test_a_longer_document_can_be_allowed_explicitly(profile, out):
    profile["experience"][0]["bullets"].extend(
        [{"id": f"x{n}", "tags": [], "text": "Did a thing thoroughly. " * 30}
         for n in range(40)]
    )
    render_resume(profile, full_selection(profile), out, max_pages=5)
    assert page_count(out) > 1


# -- guardrail: immutable facts ----------------------------------------------


def test_the_facts_that_must_not_change_are_enumerated(profile):
    facts = immutable_facts(profile)
    assert "Ada Lovelace" in facts
    assert "ada@example.com" in facts
    assert "Babbage Ltd" in facts
    assert "1840 - 1842" in facts


def test_a_faithful_render_keeps_every_fact(profile, out):
    render_resume(profile, full_selection(profile), out)
    assert check_immutable(out, profile) == []


def test_a_render_that_loses_an_employer_is_caught(profile, out):
    """Tailoring may reword bullets; it may not silently drop where you worked."""
    from tailor.render import html_to_pdf, render_html

    selection = full_selection(profile)
    selection.experience[0]["org"] = "Some Other Company"
    pdf = html_to_pdf(render_html(profile, selection), out)

    assert "Babbage Ltd" in check_immutable(pdf, profile)


def test_render_resume_refuses_when_a_fact_is_missing(profile, out):
    selection = full_selection(profile)
    selection.experience[0]["dates"] = "1999 - 2000"

    with pytest.raises(RenderError, match="facts missing"):
        render_resume(profile, selection, out)


# -- guardrail: provenance ---------------------------------------------------


def test_bullets_from_the_pool_pass(profile):
    assert check_provenance(full_selection(profile), ["b1", "b2", "b3"]) == []


def test_an_invented_bullet_is_caught(profile):
    selection = Selection(experience=[{
        "title": "Engineer", "org": "Babbage Ltd", "location": "Remote",
        "dates": "1840 - 1842",
        "bullets": [{"id": "fabricated", "text": "Led a team of 200 engineers."}],
    }])
    assert check_provenance(selection, ["b1", "b2", "b3"]) == ["fabricated"]


def test_render_resume_refuses_an_invented_bullet(profile, out):
    """The check that stops a model putting work you never did on your resume."""
    selection = full_selection(profile)
    selection.experience[0]["bullets"].append(
        {"id": "fabricated", "text": "Scaled the platform to ten million users."}
    )

    with pytest.raises(RenderError, match="not traceable to the pool"):
        render_resume(profile, selection, out)


# -- guardrail: text fidelity ------------------------------------------------


def test_light_rewording_is_allowed(profile):
    """Rewording for a posting is the point; the guardrail must not block it."""
    selection = full_selection(profile)
    selection.experience[0]["bullets"][0]["text"] = (
        "Published the first algorithm written for a mechanical computer."
    )
    assert check_text_fidelity(selection, profile) == []


def test_wholesale_substitution_under_a_real_id_is_caught(profile):
    """Citing a real bullet id while writing something unrelated under it."""
    selection = full_selection(profile)
    selection.experience[0]["bullets"][0]["text"] = (
        "Negotiated multi-million dollar vendor contracts across three continents."
    )
    drifted = check_text_fidelity(selection, profile)
    assert any("b1" in d for d in drifted)


def test_render_resume_refuses_a_substituted_bullet(profile, out):
    selection = full_selection(profile)
    selection.experience[0]["bullets"][0]["text"] = "Completely unrelated marketing copy."

    with pytest.raises(RenderError, match="reworded beyond recognition"):
        render_resume(profile, selection, out)


def test_an_unchanged_bullet_never_drifts(profile):
    assert check_text_fidelity(full_selection(profile), profile) == []


# -- the real profile --------------------------------------------------------


@pytest.mark.skipif(not REAL_PROFILE.exists(), reason="no profile.yml checked in")
def test_the_real_profile_renders_to_one_page(tmp_path):
    """The master layout must keep fitting as the pool is edited."""
    profile = load_profile()
    render_resume(profile, master_selection(profile), tmp_path / "real.pdf")


@pytest.mark.skipif(not REAL_PROFILE.exists(), reason="no profile.yml checked in")
def test_the_real_profile_keeps_its_facts(tmp_path):
    profile = load_profile()
    out = tmp_path / "real.pdf"
    render_resume(profile, master_selection(profile), out)

    text = pdf_text(out)
    assert "Eugenio Kuri Muzquiz" in text
    assert "Merlin Drones" in text
    assert "University of Michigan" in text
