from datetime import datetime, timezone

import pytest

from sources.ats import (
    AtsSource,
    Board,
    categorize_title,
    infer_terms,
    load_boards,
    looks_like_internship,
    parse_epoch_millis,
    parse_iso,
    strip_html,
)
from sources.base import FeedError


# -- is it an internship? ----------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer Intern",
        "Campus Quantitative Trader (Intern)",
        "Machine Learning Research Internship",
        "Engineering Co-op",
        "Software Engineering Coop",
        "Summer Analyst, Technology",
    ],
)
def test_internship_titles_are_recognised(title):
    assert looks_like_internship(title)


@pytest.mark.parametrize(
    "title",
    [
        "Internal Audit Manager",          # 'intern' as a prefix
        "International Sales Lead",        # ditto
        "Staff Software Engineer",
        "University Recruiter, Internship Program",
        "New Grad Software Engineer",
        "Software Engineer, Full-Time",
        "Senior IT Internal Auditor",
        "Senior, Internal Audit AI Enablement & Automation",
        "Staff Machine Learning Engineer",
        "Director of Engineering",
        "",
    ],
)
def test_non_internship_titles_are_rejected(title):
    assert not looks_like_internship(title)


def test_a_structured_hint_can_carry_the_decision():
    """Ashby's employmentType and Lever's commitment are more reliable than prose."""
    assert looks_like_internship("Summer Program 2027", employment_type="Intern")
    assert looks_like_internship("Campus Program", commitment="Internship")


def test_a_hint_does_not_match_intern_inside_another_word():
    """'Internal Audit' contains 'intern'; a substring check admits every auditor."""
    assert not looks_like_internship("IT Auditor", commitment="Internal Audit")
    assert not looks_like_internship("Audit Associate", employment_type="Internal")


def test_a_hint_cannot_override_a_disqualifying_title():
    assert not looks_like_internship("Senior Software Engineer", employment_type="Intern")


# -- which term? -------------------------------------------------------------


def test_a_year_in_the_title_is_read_not_inferred():
    terms, inferred = infer_terms("Software Engineer Intern - Summer 2027")
    assert terms == ["Summer 2027"]
    assert inferred is False


def test_the_title_outranks_a_stray_year_in_the_body():
    """Bodies mention graduation years; that must not dilute a clear title."""
    terms, inferred = infer_terms(
        "Quantitative Trader Intern - Summer 2027",
        description="Open to students graduating in 2028 or 2029.",
    )
    assert terms == ["Summer 2027"]
    assert inferred is False


def test_the_body_is_used_when_the_title_has_no_year():
    terms, inferred = infer_terms(
        "Campus Software Engineer (Intern)",
        description="This is our Summer 2027 internship programme.",
    )
    assert terms == ["Summer 2027"]
    assert inferred is False


def test_the_season_comes_from_the_title_when_the_title_names_one():
    terms, _ = infer_terms("Fall Software Intern", description="Our 2027 programme.")
    assert terms == ["Fall 2027"]


def test_a_late_year_posting_is_inferred_to_be_next_summer():
    posted = datetime(2026, 8, 11, tzinfo=timezone.utc)
    terms, inferred = infer_terms("Campus Software Engineer (Intern)", posted_at=posted)
    assert terms == ["Summer 2027"]
    assert inferred is True


def test_an_early_year_posting_is_inferred_to_be_this_summer():
    posted = datetime(2027, 2, 1, tzinfo=timezone.utc)
    terms, inferred = infer_terms("Campus Software Engineer (Intern)", posted_at=posted)
    assert terms == ["Summer 2027"]
    assert inferred is True


def test_a_title_naming_two_years_keeps_both():
    terms, inferred = infer_terms("Intern - Summer 2027 / Summer 2028")
    assert terms == ["Summer 2027", "Summer 2028"]
    assert inferred is False


def test_implausible_years_are_ignored():
    terms, inferred = infer_terms(
        "Intern", description="Founded in 1998.", posted_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    assert terms == ["Summer 2027"]
    assert inferred is True


# -- category ---------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Quantitative Trader Intern", "Quant"),
        ("Machine Learning Research Intern", "AI / ML / Data"),
        ("Data Engineer Intern", "AI / ML / Data"),
        ("FPGA Engineer Intern", "Hardware"),
        ("Product Management Intern", "Product"),
        ("Software Engineer Intern", "Software Engineering"),
        ("Backend Engineering Intern", "Software Engineering"),
        ("Site Reliability Engineer Intern", "Software Engineering"),
        ("Marketing Intern", "Other"),
    ],
)
def test_titles_map_onto_the_feed_vocabulary(title, expected):
    assert categorize_title(title) == expected


def test_quant_wins_over_software_when_both_appear():
    """Ordering matters: a quant dev is a quant role, not a generic SWE one."""
    assert categorize_title("Quantitative Developer Intern (Software)") == "Quant"


def test_the_department_is_a_tiebreak_when_the_title_is_bare():
    assert categorize_title("Summer Intern", department="Hardware") == "Hardware"


# -- HTML to text ------------------------------------------------------------


def test_greenhouse_double_escaping_is_undone():
    assert strip_html("&lt;p&gt;Build &amp; ship&lt;/p&gt;") == "Build & ship"


def test_list_items_survive_as_readable_bullets():
    assert "- Python" in strip_html("<ul><li>Python</li><li>Go</li></ul>")


def test_block_tags_become_line_breaks_not_run_on_text():
    assert strip_html("<p>One</p><p>Two</p>") == "One\nTwo"


def test_empty_html_is_empty_text():
    assert strip_html(None) == ""
    assert strip_html("") == ""


# -- timestamps --------------------------------------------------------------


def test_greenhouse_offsets_are_parsed():
    assert parse_iso("2026-06-22T16:14:59-04:00").year == 2026


def test_a_trailing_z_is_accepted():
    assert parse_iso("2026-04-07T17:12:35.753Z").tzinfo is not None


def test_a_naive_timestamp_is_assumed_utc():
    assert parse_iso("2026-04-07T17:12:35").tzinfo == timezone.utc


@pytest.mark.parametrize("value", [None, "", "not-a-date", 12345])
def test_junk_timestamps_become_none(value):
    assert parse_iso(value) is None


def test_lever_reports_milliseconds_not_seconds():
    """Reading Lever's createdAt as seconds lands in 1970 and breaks the window."""
    assert parse_epoch_millis(1779223091267).year == 2026


@pytest.mark.parametrize("value", [None, "", "abc", float("nan")])
def test_junk_epochs_become_none(value):
    assert parse_epoch_millis(value) is None


# -- companies.yml -----------------------------------------------------------


def test_a_bare_token_gets_a_derived_display_name(tmp_path):
    path = tmp_path / "companies.yml"
    path.write_text("greenhouse:\n  - applied-intuition\n")
    assert load_boards("greenhouse", str(path)) == [
        Board("applied-intuition", "Applied Intuition")
    ]


def test_an_explicit_name_wins_over_the_derived_one(tmp_path):
    path = tmp_path / "companies.yml"
    path.write_text("lever:\n  - {token: matchgroup, name: Match Group}\n")
    assert load_boards("lever", str(path)) == [Board("matchgroup", "Match Group")]


def test_both_forms_can_be_mixed(tmp_path):
    path = tmp_path / "companies.yml"
    path.write_text("ashby:\n  - modal\n  - {token: openai, name: OpenAI}\n")
    assert load_boards("ashby", str(path)) == [
        Board("modal", "Modal"),
        Board("openai", "OpenAI"),
    ]


def test_a_missing_file_means_no_boards_not_a_crash(tmp_path):
    assert load_boards("greenhouse", str(tmp_path / "nope.yml")) == []


def test_malformed_yaml_means_no_boards_not_a_crash(tmp_path):
    path = tmp_path / "companies.yml"
    path.write_text("greenhouse: [unclosed\n")
    assert load_boards("greenhouse", str(path)) == []


def test_an_unexpected_shape_is_skipped(tmp_path):
    path = tmp_path / "companies.yml"
    path.write_text("greenhouse:\n  token: janestreet\n")
    assert load_boards("greenhouse", str(path)) == []


def test_an_absent_ats_key_yields_nothing(tmp_path):
    path = tmp_path / "companies.yml"
    path.write_text("greenhouse:\n  - janestreet\n")
    assert load_boards("lever", str(path)) == []


def test_the_shipped_companies_file_parses():
    """A typo here silently halves the pipeline's reach, so assert it loads."""
    tokens = {kind: load_boards(kind) for kind in ("greenhouse", "lever", "ashby")}
    assert all(tokens.values()), "every ATS should have boards configured"
    for boards in tokens.values():
        assert all(board.token and board.name for board in boards)


# -- fail-soft board fetching ------------------------------------------------


class _OneBadBoard(AtsSource):
    name = "test-ats"
    kind = "test"

    def board_url(self, board):
        return f"https://example.invalid/{board.token}"

    def fetch_json(self, url):
        if "broken" in url:
            raise FeedError("board not found (404)")
        return {"ok": True}

    def parse_board(self, board, payload):
        return [f"job-from-{board.token}"]


def test_one_dead_board_does_not_take_down_the_others():
    source = _OneBadBoard(boards=[Board("good", "Good"), Board("broken", "Broken"),
                                  Board("alsogood", "Also Good")])
    assert source.scrape() == ["job-from-good", "job-from-alsogood"]


def test_no_boards_configured_is_an_empty_result():
    assert _OneBadBoard(boards=[]).scrape() == []


class _ExplodingParser(_OneBadBoard):
    def parse_board(self, board, payload):
        raise KeyError("upstream changed shape")


def test_an_unexpected_shape_change_is_contained():
    source = _ExplodingParser(boards=[Board("a", "A"), Board("b", "B")])
    assert source.scrape() == []
