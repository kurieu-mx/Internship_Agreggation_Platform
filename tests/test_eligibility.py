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


# -- graduate-degree requirements --------------------------------------------
#
# The distinction is not "does this mention a graduate degree" but "is an
# undergraduate eligible". Measured live: 34 postings list only PhD and 10 only
# Master's, but 114 list Bachelor's *and* Master's - those are either/or and
# must be kept, or the filter removes a quarter of the corpus.


def degreed(title="Software Engineer Intern", degrees=None, description=""):
    return Job(company="Acme", title=title, degrees=degrees or [],
               description=description)


@pytest.mark.parametrize("degrees", [["PhD"], ["Master's"], ["Master's", "PhD"], ["MBA"]])
def test_a_graduate_only_degrees_field_excludes(degrees):
    from eligibility import requires_graduate_degree

    assert requires_graduate_degree(degreed(degrees=degrees))


@pytest.mark.parametrize("degrees", [
    ["Bachelor's"],
    ["Bachelor's", "Master's"],
    ["Bachelor's", "Master's", "PhD"],
])
def test_a_degrees_field_accepting_bachelors_is_kept(degrees):
    from eligibility import requires_graduate_degree

    assert not requires_graduate_degree(degreed(degrees=degrees))


def test_an_empty_degrees_field_falls_through_to_the_title():
    from eligibility import requires_graduate_degree

    assert requires_graduate_degree(degreed(title="Quantitative Research Intern - PhD"))
    assert not requires_graduate_degree(degreed(title="Software Engineer Intern"))


@pytest.mark.parametrize("title", [
    "Quantitative Research Intern - PhD: Summer 2027",
    "PhD Quantitative Researcher Intern",
    "Master's Data Science Internship",
    "Software Engineering Intern, Masters",
    "Machine Learning PhD Software Engineer Intern",
])
def test_real_graduate_only_titles_are_excluded(title):
    """All five observed live."""
    from eligibility import requires_graduate_degree

    assert requires_graduate_degree(degreed(title=title))


@pytest.mark.parametrize("title", [
    "Quantitative Research Intern (BS/MS) - Summer 2027",
    "Software Engineer Intern, Bachelor's or Master's",
    "Undergraduate Research Intern",
])
def test_a_title_that_also_accepts_undergraduates_is_kept(title):
    from eligibility import requires_graduate_degree

    assert not requires_graduate_degree(degreed(title=title))


def test_a_stated_graduate_requirement_in_the_body_excludes():
    from eligibility import requires_graduate_degree

    assert requires_graduate_degree(
        degreed(description="Candidates must be pursuing a PhD in a quantitative field.")
    )


def test_an_either_or_requirement_in_the_body_is_kept():
    from eligibility import requires_graduate_degree

    assert not requires_graduate_degree(
        degreed(description="Pursuing a Bachelor's or Master's degree in Computer Science.")
    )


def test_a_genuine_requirement_survives_an_unrelated_bachelors_mention():
    """A long posting mentioning bachelor's elsewhere must not excuse 'must hold a PhD'."""
    from eligibility import requires_graduate_degree

    assert requires_graduate_degree(degreed(
        description="Our team has bachelor's graduates in other roles. "
                    "For this position you must be pursuing a PhD."
    ))


def test_the_structured_field_outranks_the_title():
    """A feed that filled the field in knows better than a title heuristic."""
    from eligibility import requires_graduate_degree

    assert not requires_graduate_degree(
        degreed(title="PhD Research Intern", degrees=["Bachelor's", "PhD"])
    )


def test_silence_means_eligible():
    from eligibility import requires_graduate_degree

    assert not requires_graduate_degree(degreed())


def test_the_batch_filter_splits_correctly():
    from eligibility import only_undergraduate_eligible

    jobs = [degreed(title="Open"), degreed(title="Closed", degrees=["PhD"])]
    assert [j.title for j in only_undergraduate_eligible(jobs)] == ["Open"]


def test_the_batch_filter_tolerates_an_empty_list():
    from eligibility import only_undergraduate_eligible

    assert only_undergraduate_eligible([]) == []


# -- company names that are really title fragments ---------------------------
#
# The search-backed sources split a posting heading on a dash and treat what
# follows as the employer. Observed live: LinkedIn returned
# title="Quantitative Research Internship", company="Master's: Summer 2027".
# The degree requirement was on the page, in the wrong field, so the
# undergraduate filter passed a Master's-only quant role - and a cover letter
# would have been addressed to a company that does not exist.


from eligibility import (drop_malformed, looks_like_title_fragment,
                         requires_graduate_degree)


@pytest.mark.parametrize("company", [
    "Master's: Summer 2027",
    "Elevate/Data Science [UG/Masters]",
    "Summer 2027",
    "Software Engineering Internship",
    "Quantitative Research Intern",
])
def test_a_title_fragment_is_recognised(company):
    assert looks_like_title_fragment(company)


@pytest.mark.parametrize("company", [
    "Susquehanna International Group (SIG)",
    "Masters Gallery Foods",          # a real company with a degree word in it
    "Jane Street",
    "Sargent & Lundy",
    "IBM",
    "Applied Intuition",
    "",
])
def test_a_real_company_name_is_left_alone(company):
    assert not looks_like_title_fragment(company)


def test_a_degree_in_a_fragment_company_still_disqualifies():
    """The bug this fixes: the requirement was read out of the wrong field."""
    job = Job(company="Master's: Summer 2027",
              title="Quantitative Research Internship",
              locations=["New York, NY"], field_category="Quant")
    assert requires_graduate_degree(job)


def test_a_degree_word_in_a_real_company_name_does_not_disqualify():
    """"Masters Gallery Foods" is an employer, not a requirement."""
    job = Job(company="Masters Gallery Foods", title="Software Engineer Intern",
              locations=["WI"], field_category="Software Engineering")
    assert not requires_graduate_degree(job)


def test_a_ug_or_masters_fragment_is_still_undergraduate_eligible():
    """"[UG/Masters]" is either/or - the same rule as "Bachelor's or Master's"."""
    job = Job(company="Elevate/Data Science [UG/Masters]",
              title="Summer 2027 Intern", locations=["NY"],
              field_category="AI / ML / Data")
    assert not requires_graduate_degree(job)


def test_malformed_postings_are_dropped_from_the_run():
    good = Job(company="Susquehanna International Group (SIG)",
               title="Trading System Engineering Intern",
               locations=["Philadelphia, PA"], field_category="Software Engineering")
    bad = Job(company="Master's: Summer 2027", title="Quantitative Research Internship",
              locations=["New York, NY"], field_category="Quant")
    assert drop_malformed([good, bad]) == [good]


def test_dropping_nothing_is_fine():
    good = Job(company="IBM", title="Software Developer Intern",
               locations=["Austin, TX"], field_category="Software Engineering")
    assert drop_malformed([good]) == [good]


# -- co-ops, and terms that are not the one being searched for ---------------
#
# Both of these reached a real digest. "Machine Learning Intern/Co-op (Winter
# 2027)" was sent under a Summer 2027 filter because the only term check
# compared the *year* and nothing looked at the season. "Full Stack Developer
# Co-op" was sent because the seasonal fallback assigned it Summer 2027 and
# looks_like_internship accepts co-op as a match.


from eligibility import (exclude_coops, is_coop_only, names_a_different_term,
                         only_target_term)


@pytest.mark.parametrize("title", [
    "Full Stack Developer Co-op",
    "Analog IC Design Co-op",
    "Software Engineering Coop",
])
def test_a_coop_is_recognised(title):
    assert is_coop_only(Job(company="X", title=title, locations=["NY"],
                            field_category="Software Engineering"))


@pytest.mark.parametrize("title", [
    # Names both: an internship that also accepts co-op students.
    "Machine Learning Intern/Co-op (Winter 2027)",
    "Software Engineer Intern",
    "Summer 2027 Internship",
])
def test_an_internship_is_not_a_coop(title):
    assert not is_coop_only(Job(company="X", title=title, locations=["NY"],
                                field_category="Software Engineering"))


def test_coops_are_dropped_from_a_run():
    keep = Job(company="A", title="Software Engineer Intern", locations=["NY"],
               field_category="Software Engineering")
    drop = Job(company="B", title="Full Stack Developer Co-op", locations=["NY"],
               field_category="Software Engineering")
    assert exclude_coops([keep, drop]) == [keep]


def _termed(title, terms):
    return Job(company="X", title=title, locations=["NY"],
               field_category="Software Engineering", terms=terms)


@pytest.mark.parametrize("title,terms", [
    # The one that shipped: right year, wrong season.
    ("Machine Learning Intern/Co-op (Winter 2027)", ["Winter 2027"]),
    ("Fall 2027 Software Intern", ["Fall 2027"]),
    ("Spring 2027 Intern", ["Spring 2027"]),
    ("Summer 2026 Intern", ["Summer 2026"]),
])
def test_a_different_term_is_dropped(title, terms):
    assert names_a_different_term(_termed(title, terms), "Summer 2027")


@pytest.mark.parametrize("title,terms", [
    ("Software Engineer Intern - Summer 2027", ["Summer 2027"]),
    # Silence is kept, exactly as it is for sponsorship and degree: most ATS
    # postings state no term, and dropping those discards the corpus.
    ("Software Engineer Intern", []),
    ("Quantitative Developer Intern", []),
])
def test_a_matching_or_unstated_term_is_kept(title, terms):
    assert not names_a_different_term(_termed(title, terms), "Summer 2027")


def test_autumn_and_fall_are_the_same_term():
    assert not names_a_different_term(_termed("Autumn 2027 Intern", ["Autumn 2027"]),
                                      "Fall 2027")


def test_an_empty_filter_drops_nothing():
    jobs = [_termed("Winter 2027 Intern", ["Winter 2027"])]
    assert only_target_term(jobs, "") == jobs


def test_the_term_gate_reads_the_title_when_no_term_is_published():
    """ATS sources publish no term field; the season is in the title."""
    assert names_a_different_term(_termed("Winter 2027 Software Intern", []),
                                  "Summer 2027")


def test_the_title_overrides_a_contradicting_feed_term():
    """The community feed tagged "Engineer Intern - Spring 2027" as Summer 2027.

    Reading terms and title as one string found "summer" first and kept a
    spring posting. The title is the employer's own words; a feed term is a
    contributor's tag.
    """
    job = _termed("Engineer Intern - Spring 2027", ["Summer 2027"])
    assert names_a_different_term(job, "Summer 2027")


def test_a_posting_offering_several_terms_qualifies_on_any_of_them():
    """"Summer 2027, Fall 2027" is offering both, so it is a Summer posting."""
    job = _termed("DERMS Intern", ["Summer 2027", "Fall 2027"])
    assert not names_a_different_term(job, "Summer 2027")


def test_several_terms_that_all_conflict_are_dropped():
    job = _termed("DERMS Intern", ["Fall 2027", "Winter 2027"])
    assert names_a_different_term(job, "Summer 2027")


# -- the US gate, run after enrichment ---------------------------------------


def test_a_known_non_us_location_is_dropped():
    from eligibility import only_us

    keep = Job(company="A", title="Intern", locations=["Austin, TX"],
               field_category="Software Engineering")
    drop = Job(company="B", title="Intern", locations=["Bratislava"],
               field_category="Quant")
    assert only_us([keep, drop]) == [keep]


def test_a_posting_with_no_location_is_still_kept():
    """Silence is not evidence - the same rule as sponsorship and degree."""
    from eligibility import only_us

    job = Job(company="A", title="Intern", locations=[],
              field_category="Software Engineering")
    assert only_us([job]) == [job]
