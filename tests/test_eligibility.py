import pytest

from eligibility import annotate, detect_restriction, filter_eligible
from models import Job


def job(title="Software Engineer Intern", description="", sponsorship="Unknown"):
    return Job(company="Acme", title=title, description=description,
               sponsorship=sponsorship)


# -- explicit refusals to sponsor --------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "We are unable to sponsor visas for this position.",
        "This role does not offer visa sponsorship.",
        "We cannot sponsor applicants for work authorization.",
        "Sponsorship is not available for this internship.",
        "No visa sponsorship is provided.",
        "Candidates must be authorized to work without the need for sponsorship.",
        "We will not sponsor employment visas.",
    ],
)
def test_a_stated_refusal_to_sponsor_is_detected(text):
    status, reason = detect_restriction(job(description=text))
    assert status == "No"
    assert reason


# -- citizenship requirements ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Applicants must be a U.S. citizen.",
        "US citizenship is required for this role.",
        "This position is restricted to US citizens.",
        "Open to U.S. persons only.",
        "Permanent resident status is required.",
    ],
)
def test_a_citizenship_requirement_is_detected(text):
    status, _ = detect_restriction(job(description=text))
    assert status == "US citizens only"


@pytest.mark.parametrize(
    "text",
    [
        "Must be able to obtain a security clearance.",
        "This role is subject to ITAR restrictions.",
        "Position requires export-control compliance.",
        "Active TS/SCI clearance preferred.",
        "Candidates must hold a Secret clearance.",
    ],
)
def test_clearance_and_export_control_count_as_a_citizenship_bar(text):
    """An F-1 student cannot hold a clearance, whatever the posting calls it."""
    status, reason = detect_restriction(job(description=text))
    assert status == "US citizens only"
    assert "clearance" in reason or "export" in reason.lower()


def test_a_restriction_in_the_title_is_caught_too():
    status, _ = detect_restriction(job(title="Software Engineer Intern (US Citizens Only)"))
    assert status == "US citizens only"


# -- not false positives -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "We are able to sponsor visas for exceptional candidates.",
        "We will sponsor H-1B visas after conversion.",
        "Visa sponsorship is available for this role.",
        "We sponsor candidates requiring work authorization.",
    ],
)
def test_an_offer_to_sponsor_is_not_read_as_a_refusal(text):
    """'able to sponsor' must not match the negation pattern."""
    status, _ = detect_restriction(job(description=text))
    assert status == "Yes"


def test_an_ordinary_posting_stays_unknown():
    status, reason = detect_restriction(
        job(description="Build distributed systems in Go. Strong CS fundamentals.")
    )
    assert status == "Unknown"
    assert reason == ""


def test_a_posting_with_no_text_stays_unknown():
    assert detect_restriction(job())[0] == "Unknown"


def test_a_status_the_source_stated_is_trusted_over_the_text():
    """Don't second-guess a source that filled the field in."""
    status, reason = detect_restriction(
        job(description="We cannot sponsor.", sponsorship="Yes")
    )
    assert status == "Yes"
    assert reason == "stated by the source"


def test_a_citizenship_bar_outranks_an_offer_to_sponsor():
    """A posting mentioning both is closed to you; the restriction wins."""
    status, _ = detect_restriction(
        job(description="We sponsor visas. However, this role requires US citizenship.")
    )
    assert status == "US citizens only"


# -- the filter --------------------------------------------------------------


def test_restricted_postings_are_dropped():
    jobs = [
        job(title="Open Role", description="Great team."),
        job(title="Cleared Role", description="Requires an active security clearance."),
        job(title="No Sponsor Role", description="We are unable to sponsor."),
    ]
    assert [j.title for j in filter_eligible(jobs)] == ["Open Role"]


def test_unknown_postings_are_kept():
    """Most sources say nothing; treating silence as a no would empty the digest."""
    jobs = [job(title=f"Role {n}") for n in range(5)]
    assert len(filter_eligible(jobs)) == 5


def test_a_posting_that_offers_sponsorship_is_kept():
    jobs = [job(description="Visa sponsorship is available.")]
    assert len(filter_eligible(jobs)) == 1


def test_the_filter_annotates_what_it_keeps():
    jobs = [job(description="Visa sponsorship is available.")]
    assert filter_eligible(jobs)[0].sponsorship == "Yes"


def test_a_dropped_posting_carries_its_reason():
    jobs = [job(description="Requires ITAR compliance.")]
    annotated = annotate(jobs)
    assert annotated[0].sponsorship == "US citizens only"
    assert "itar" in annotated[0].score_reason.lower()


def test_the_exclusion_set_is_configurable():
    """Someone who only wants explicit yes-sponsors can tighten it."""
    jobs = [job(title="Silent", description="Nothing stated.")]
    assert filter_eligible(jobs, exclude={"Unknown"}) == []


def test_an_empty_batch_is_fine():
    assert filter_eligible([]) == []


# -- the internship gate -----------------------------------------------------
#
# Applied to every source, including the community feeds. Those are named
# "Summer2027-Internships" and were trusted not to need it; measured live, six
# full-time and rotational-graduate roles were reaching the shortlist.


@pytest.mark.parametrize("title", [
    "Software Engineer",
    "Private Credit – Investment Analyst Program",
    "Technology, Operations, Digital and Data Development Program",
    "Quantitative Analyst Associate - Quantitative Technology",
    "Senior Machine Learning Engineer",
])
def test_non_internships_are_dropped(title):
    from eligibility import only_internships

    assert only_internships([Job(company="Acme", title=title)]) == []


@pytest.mark.parametrize("title", [
    "Software Engineer Intern",
    "Machine Learning Co-op",
    "AI and Data Engineering Summer Scholar Intern",
    # Finance names its internships differently; excluding these would drop
    # most bank and trading postings, which is much of the target.
    "Capital Markets Quant Summer Associate - Quantitative Technology",
    "Quantitative Summer Analyst",
])
def test_real_internships_are_kept(title):
    from eligibility import only_internships

    assert len(only_internships([Job(company="Acme", title=title)])) == 1


def test_the_gate_tolerates_an_empty_batch():
    from eligibility import only_internships

    assert only_internships([]) == []
