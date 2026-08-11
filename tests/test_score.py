"""Ranking: the cheap deterministic stage, and the model stage's failure modes.

The model call itself is stubbed throughout. What matters in tests is that the
pipeline behaves when the model is absent, truncated, or returns something
unexpected - because on those days the digest still has to go out.
"""

from datetime import datetime, timedelta, timezone

import pytest

import llm
from models import Job
from tailor.score import (
    _profile_tags,
    keyword_score,
    prefilter,
    recency_bonus,
    rerank,
    shortlist,
)

NOW = datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def profile():
    return {
        "summaries": [{"id": "s1", "tags": ["software"], "text": "..."}],
        "experience": [{
            "id": "r1", "title": "Engineer", "org": "Acme", "location": "Remote",
            "dates": "2026",
            "bullets": [
                {"id": "b1", "tags": ["go", "distributed", "robotics"], "text": "..."},
                {"id": "b2", "tags": ["pytorch", "ml"], "text": "..."},
                {"id": "b3", "tags": ["go"], "text": "..."},
            ],
        }],
        "projects": [{
            "id": "p1", "name": "Proj", "stack": "x",
            "bullets": [{"id": "b4", "tags": ["ml", "cuda"], "text": "..."}],
        }],
        "skills": [{"id": "sk1", "label": "Languages", "tags": ["software"],
                    "items": ["Go (Golang)", "PyTorch"]}],
    }


def job(title="Software Engineer Intern", category="Software Engineering",
        description="", hours_ago=1, company="Globex", **kwargs):
    return Job(
        company=company, title=title, field_category=category,
        description=description, posted_at=NOW - timedelta(hours=hours_ago),
        locations=["NYC"], **kwargs,
    )


# -- the profile's vocabulary ------------------------------------------------


def test_tags_are_weighted_by_how_often_they_appear(profile):
    """A tag on three bullets is a stronger signal than one on a single bullet."""
    weights = _profile_tags(profile)
    assert weights["go"] > weights["distributed"]


def test_skill_item_names_become_searchable_terms(profile):
    weights = _profile_tags(profile)
    assert "pytorch" in weights
    assert "go" in weights          # parenthetical stripped from "Go (Golang)"


# -- keyword scoring ---------------------------------------------------------


def test_a_matching_posting_outscores_an_unrelated_one(profile):
    weights = _profile_tags(profile)
    match = job(title="Go Distributed Systems Intern")
    other = job(title="Marketing Intern")
    assert keyword_score(match, weights) > keyword_score(other, weights)


def test_a_title_match_counts_more_than_a_description_match(profile):
    """A word in the title describes the role; in the body it may be incidental."""
    weights = _profile_tags(profile)
    in_title = job(title="PyTorch Engineer Intern")
    in_body = job(title="Analyst Intern", description="We use PyTorch somewhere.")
    assert keyword_score(in_title, weights) > keyword_score(in_body, weights)


def test_substrings_do_not_count_as_matches(profile):
    """'go' must not match 'algorithm', 'ongoing' or 'Django'.

    The category is blanked deliberately: it is part of the haystack, so
    "Software Engineering" would legitimately match the `software` tag and
    mask what this test is actually asking about.
    """
    weights = _profile_tags(profile)
    bare = Job(company="Globex", title="Django Ongoing Algorithms", field_category="")
    assert keyword_score(bare, weights) == 0.0


def test_the_category_itself_is_a_signal(profile):
    """A posting filed under Software Engineering matches the `software` tag."""
    weights = _profile_tags(profile)
    categorised = Job(company="G", title="Analyst", field_category="Software Engineering")
    uncategorised = Job(company="G", title="Analyst", field_category="")
    assert keyword_score(categorised, weights) > keyword_score(uncategorised, weights)


def test_an_empty_posting_scores_zero(profile):
    assert keyword_score(Job(company="A", title=""), _profile_tags(profile)) == 0.0


# -- recency -----------------------------------------------------------------


def test_a_fresher_posting_gets_a_bigger_bonus():
    assert recency_bonus(job(hours_ago=1), NOW) > recency_bonus(job(hours_ago=20), NOW)


def test_an_old_posting_gets_no_bonus_rather_than_a_penalty():
    assert recency_bonus(job(hours_ago=500), NOW) == 0.0


def test_an_undated_posting_gets_no_bonus():
    assert recency_bonus(Job(company="A", title="B"), NOW) == 0.0


# -- prefilter ---------------------------------------------------------------


def test_postings_outside_the_target_categories_are_dropped(profile):
    jobs = [job(category="Software Engineering"), job(category="Hardware"),
            job(category="Product")]
    kept = prefilter(jobs, profile, categories=["Software Engineering"], now=NOW)
    assert [j.field_category for j in kept] == ["Software Engineering"]


def test_no_category_filter_keeps_everything(profile):
    jobs = [job(category="Hardware"), job(category="Product")]
    assert len(prefilter(jobs, profile, categories=[], now=NOW)) == 2


def test_the_shortlist_is_capped(profile):
    jobs = [job(title=f"Go Engineer Intern {n}") for n in range(40)]
    assert len(prefilter(jobs, profile, limit=15, now=NOW)) == 15


def test_the_best_match_survives_the_cap(profile):
    jobs = [job(title=f"Analyst Intern {n}") for n in range(30)]
    jobs.append(job(title="Go Distributed Robotics PyTorch Intern"))
    kept = prefilter(jobs, profile, limit=5, now=NOW)
    assert kept[0].title == "Go Distributed Robotics PyTorch Intern"


def test_prefilter_sets_a_provisional_score(profile):
    kept = prefilter([job(title="Go Intern")], profile, now=NOW)
    assert kept[0].score > 0


# -- rerank: the failure modes that matter -----------------------------------


def test_without_a_model_the_deterministic_order_stands(profile, monkeypatch):
    """A missing key must degrade the ranking, not cancel the digest."""
    monkeypatch.setattr(llm, "available", lambda: False)
    jobs = [job(title="A"), job(title="B")]
    assert [j.title for j in rerank(jobs, profile)] == ["A", "B"]


def test_a_failed_model_call_leaves_the_order_alone(profile, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", lambda **kw: None)
    jobs = [job(title="A"), job(title="B")]
    assert [j.title for j in rerank(jobs, profile)] == ["A", "B"]


def test_scores_are_applied_and_the_list_reordered(profile, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", lambda **kw: {"rankings": [
        {"ref": 0, "score": 20, "why": "weak", "gaps": ""},
        {"ref": 1, "score": 90, "why": "strong", "gaps": "no Spark"},
    ]})

    ranked = rerank([job(title="A"), job(title="B")], profile)
    assert [j.title for j in ranked] == ["B", "A"]
    assert ranked[0].score == 90
    assert "strong" in ranked[0].score_reason
    assert "no Spark" in ranked[0].score_reason


def test_a_posting_the_model_skipped_keeps_its_prefilter_score(profile, monkeypatch):
    """A partial response must not silently zero the postings it omitted."""
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", lambda **kw: {"rankings": [
        {"ref": 0, "score": 90, "why": "x", "gaps": ""},
    ]})

    jobs = [job(title="A"), job(title="B")]
    for j in jobs:
        j.score = 5.0
    ranked = rerank(jobs, profile)
    assert next(j for j in ranked if j.title == "B").score == 5.0


def test_out_of_range_scores_are_clamped(profile, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", lambda **kw: {"rankings": [
        {"ref": 0, "score": 999, "why": "x", "gaps": ""},
    ]})
    assert rerank([job()], profile)[0].score == 100


def test_a_malformed_ranking_entry_is_ignored(profile, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", lambda **kw: {"rankings": [
        {"ref": "not-an-int", "score": 90, "why": "x", "gaps": ""},
    ]})
    rerank([job()], profile)   # must not raise


def test_an_empty_shortlist_is_fine(profile):
    assert rerank([], profile) == []


# -- shortlist ---------------------------------------------------------------


def test_the_split_returns_the_top_n_and_the_remainder(profile, monkeypatch):
    """Everything that cleared the filters must be reachable, not truncated away.

    One posting per company, so the per-employer cap is not what is under test
    here - see the diversify tests below for that.
    """
    monkeypatch.setattr(llm, "available", lambda: False)
    jobs = [job(title=f"Go Intern {n}", company=f"Company{n}") for n in range(12)]

    top, rest = shortlist(jobs, profile, top_n=8, now=NOW)
    assert len(top) == 8
    assert len(rest) == 4


# -- spreading across employers ----------------------------------------------


def _scored(company, score):
    j = Job(company=company, title=f"{company} role {score}")
    j.score = score
    return j


def test_one_company_cannot_take_every_slot():
    """Observed live: ByteDance posted five roles and took four of eight slots."""
    ranked = [_scored("ByteDance", 90 - n) for n in range(5)] + \
             [_scored("Stripe", 60), _scored("Ramp", 55), _scored("Figma", 50)]

    from tailor.score import diversify
    top, rest = diversify(ranked, top_n=8, max_per_company=2)

    assert sum(1 for j in top if j.company == "ByteDance") == 2
    assert len({j.company for j in top}) == 4


def test_overflow_is_not_discarded():
    from tailor.score import diversify
    ranked = [_scored("ByteDance", 90 - n) for n in range(5)]
    top, rest = diversify(ranked, top_n=8, max_per_company=2)
    assert len(top) + len(rest) == 5


def test_a_short_digest_is_preferred_to_a_padded_one():
    """Too few companies means fewer applications, not repeats of one employer.

    Padding would spend real money re-tailoring for a company already applied
    to - the exact trade the cap exists to refuse.
    """
    from tailor.score import diversify
    ranked = [_scored("ByteDance", 90 - n) for n in range(6)]
    top, rest = diversify(ranked, top_n=5, max_per_company=2)
    assert len(top) == 2
    assert len(rest) == 4


def test_the_cap_can_be_disabled():
    from tailor.score import diversify
    ranked = [_scored("ByteDance", 90 - n) for n in range(5)]
    top, _ = diversify(ranked, top_n=5, max_per_company=0)
    assert len(top) == 5


def test_the_highest_scorer_is_always_kept():
    from tailor.score import diversify
    ranked = [_scored("ByteDance", 99), _scored("ByteDance", 98),
              _scored("ByteDance", 97), _scored("Stripe", 10)]
    top, _ = diversify(ranked, top_n=2, max_per_company=2)
    assert top[0].score == 99
