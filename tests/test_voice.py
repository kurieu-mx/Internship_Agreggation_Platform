"""The five machine-writing tells, and the check that catches them.

These five were counted across 31 letters this pipeline produced, not taken
from a list of things people say about AI prose. That distinction shows up in
what is *not* here: "excited", "passionate", "leverage", "robust", "delve" and
"I am writing to express my interest" appeared zero times across 14,384 words,
because the letter prompt had banned them from the beginning. Testing for them
would be testing a problem this project does not have.
"""

import pytest

from tailor import voice

CLEAN = (
    "I built the onboarding pipeline for a bookkeeping platform last summer. "
    "It took the product from 3 customers to 10. The work was Python on GCP, "
    "with validation around every model call. I want more of that."
)


# -- em dashes ---------------------------------------------------------------


def test_a_single_em_dash_is_flagged():
    """Zero, not "few". Allowed any, the model reliably produces several."""
    assert any("em dash" in p for p in voice.problems(
        "I built the pipeline — Python on GCP — last summer. It worked well."))


def test_en_dashes_count_too():
    assert voice.count_tells("I built it – then shipped it.")["em_dashes"] == 1


def test_the_fix_forbids_swapping_one_mark_for_another():
    """Banning dashes alone just moves the habit onto colons."""
    problem = next(p for p in voice.problems("I built it — it worked.")
                   if "em dash" in p)
    assert "colons" in problem


def test_clean_prose_has_no_dash_problem():
    assert not any("em dash" in p for p in voice.problems(CLEAN))


# -- colon reveals -----------------------------------------------------------


def test_repeated_colon_reveals_are_flagged():
    text = ("The problem was ownership: nobody owned it. "
            "The fix was simple: one service. "
            "The result was clear: fewer pages.")
    assert any("colon" in p for p in voice.problems(text))


def test_one_colon_is_allowed():
    text = ("The problem was ownership: nobody owned the pipeline. "
            "We fixed it in a week. Then it held.")
    assert not any("colon" in p for p in voice.problems(text))


def test_a_colon_before_a_capital_is_not_a_reveal():
    """"Stack: Python" is a label, not a rhetorical turn."""
    assert voice.count_tells("Stack: Python and Go.")["colon_reveals"] == 0


# -- tricolons ---------------------------------------------------------------


def test_repeated_three_item_lists_are_flagged():
    text = ("I worked on autonomy, edge inference, and command-and-control. "
            "The stack was Python, Go, and C++. "
            "It ran on drones, servers, and laptops.")
    assert any("three-item" in p for p in voice.problems(text))


def test_one_three_item_list_is_allowed():
    text = "The stack was Python, Go, and C++. I shipped it in June. It held."
    assert not any("three-item" in p for p in voice.problems(text))


def test_a_two_item_list_is_not_a_tricolon():
    assert voice.count_tells("I used Python and Go.")["tricolons"] == 0


# -- the pivot formula -------------------------------------------------------


@pytest.mark.parametrize("text", [
    "That is the same shape of problem as the bookkeeping platform.",
    "That's the same kind of work I did last summer.",
    "That is exactly the gap automation can close.",
    "That is what I want out of this internship.",
])
def test_the_pivot_formula_is_flagged(text):
    """13 of 31 letters reached for this identical turn."""
    assert any("same shape" in p for p in voice.problems(text))


def test_one_instance_is_already_too_many():
    """It is a template, not a frequency problem - any use is the tell."""
    assert voice.count_tells("That is the same shape of problem.")["pivots"] == 1


def test_ordinary_uses_of_that_are_not_flagged():
    assert voice.count_tells("That project taught me to instrument first.")["pivots"] == 0


# -- sentence rhythm ---------------------------------------------------------


def test_a_very_long_sentence_is_flagged():
    long_one = "I built " + " ".join(f"thing{i}" for i in range(45)) + "."
    assert any("longest sentence" in p for p in voice.problems(long_one))


def test_uniformly_long_prose_is_flagged():
    """The measured median was 33 words per sentence across 31 letters."""
    text = " ".join(
        "I built the onboarding pipeline in Python on Google Cloud Platform with "
        "validation stages wrapped carefully around every single model call over "
        "the whole of last summer, and it held up in production." for _ in range(4))
    assert any("average" in p for p in voice.problems(text))


def test_prose_with_no_short_sentences_is_flagged():
    text = " ".join(
        "I built the data pipeline in Python and shipped it to production."
        for _ in range(5))
    assert any("same length" in p for p in voice.problems(text))


def test_varied_rhythm_passes():
    assert not any("length" in p or "average" in p for p in voice.problems(CLEAN))


# -- scoring and selection ---------------------------------------------------


def test_score_counts_broken_rules():
    assert voice.score(CLEAN) == 0
    assert voice.score("I built it — and that is the same shape of problem.") >= 2


def test_a_revision_that_trades_one_fault_for_another_does_not_win():
    """The reason the revision is compared rather than trusted.

    Removing dashes while doubling sentence length is not an improvement, and
    editing prose to a rule sometimes produces exactly that.
    """
    before = "I built it — it worked. Then I shipped it. It held."
    after = " ".join(
        "I built the pipeline in Python on Google Cloud with validation wrapped "
        "around every model call and shipped it in June." for _ in range(4))
    assert voice.score(after) >= voice.score(before)


# -- reading a letter --------------------------------------------------------


def test_prose_extraction_reads_every_section():
    letter = {
        "hook": "I built the pipeline.",
        "why_company": "You run it at scale.",
        "what_i_bring": [{"title": "Backend", "detail": "Python on GCP."}],
        "selected_work": [{"ref": "x", "name": "X", "detail": "Shipped it."}],
        "closing": "Happy to walk through it.",
    }
    prose = voice.letter_prose(letter)
    for expected in ("I built the pipeline.", "You run it at scale.",
                     "Python on GCP.", "Shipped it.", "Happy to walk through it."):
        assert expected in prose


def test_section_titles_are_not_measured_as_prose():
    """"Backend services across languages" is a label; it has no rhythm to judge."""
    letter = {"hook": "", "why_company": "", "closing": "",
              "what_i_bring": [{"title": "Cloud, data, and CI practice",
                                "detail": "I ran services on GCP."}],
              "selected_work": []}
    assert "Cloud, data, and CI" not in voice.letter_prose(letter)


def test_an_empty_letter_yields_no_problems():
    assert voice.problems(voice.letter_prose({})) == []


def test_the_prompt_rules_and_the_check_agree():
    """They live in one module so they cannot drift apart."""
    assert "No em dashes" in voice.VOICE_RULES
    assert "three-item list" in voice.VOICE_RULES
    assert "same shape of problem" in voice.VOICE_RULES


def test_the_letter_prompt_carries_the_rules():
    from tailor.cover import LETTER_SYSTEM

    assert voice.VOICE_RULES in LETTER_SYSTEM
