"""Cover-letter grounding, and the delivery layer's fallbacks.

The claim-validation test is the important one here. A cover letter that names
something specific about a company is the whole point of sending one, and it
is also exactly where a model will confabulate if left unchecked - so the
check that every claim traces back to fetched text gets tested hardest.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import llm
from delivery.email import DigestItem, build_body, send, subject
from models import Job
from tailor.cover import _candidate_urls, accent_for, monogram, validate_facts

NOW = datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc)

SOURCE = (
    "Quantbot Technologies is a systematic trading firm. Our engineers build "
    "low-latency execution systems in C++ and research pipelines in Python. "
    "We run our own research cluster and publish internal tooling."
)


def job(company="Quantbot Technologies", title="ML Research Intern", **kwargs):
    return Job(company=company, title=title, locations=["NYC"],
               field_category="Quant", posted_at=NOW - timedelta(hours=3), **kwargs)


# -- claim validation --------------------------------------------------------


def test_a_claim_with_real_supporting_evidence_survives():
    facts = [{"claim": "They build low-latency execution systems in C++.",
              "evidence": "build low-latency execution systems in C++"}]
    assert validate_facts(facts, SOURCE) == ["They build low-latency execution systems in C++."]


def test_an_invented_claim_is_dropped():
    """The failure this exists to prevent: a plausible fact nobody said."""
    facts = [{"claim": "They were founded in 2009 by three MIT physicists.",
              "evidence": "founded in 2009 by three MIT physicists"}]
    assert validate_facts(facts, SOURCE) == []


def test_a_claim_with_no_evidence_at_all_is_dropped():
    assert validate_facts([{"claim": "They are an excellent employer.", "evidence": ""}],
                          SOURCE) == []


def test_quoted_evidence_survives_light_reformatting():
    """A model reproducing a quote may normalise punctuation; that is not lying."""
    facts = [{"claim": "They run their own research cluster.",
              "evidence": "We run our own research cluster!"}]
    assert len(validate_facts(facts, SOURCE)) == 1


def test_a_mix_keeps_only_what_is_supported():
    facts = [
        {"claim": "Real.", "evidence": "research pipelines in Python"},
        {"claim": "Invented.", "evidence": "we operate offices in nineteen countries"},
    ]
    assert validate_facts(facts, SOURCE) == ["Real."]


def test_no_source_text_means_no_facts():
    """No research is a fine outcome; a letter without a hook beats a false one."""
    facts = [{"claim": "Anything.", "evidence": "anything at all"}]
    assert validate_facts(facts, "") == []


def test_an_empty_fact_list_is_fine():
    assert validate_facts([], SOURCE) == []


# -- letterhead --------------------------------------------------------------


def test_the_accent_is_stable_for_a_company():
    assert accent_for("Quantbot Technologies") == accent_for("quantbot technologies")


def test_different_companies_generally_differ():
    accents = {accent_for(c) for c in
               ("Stripe", "Ramp", "Figma", "ByteDance", "Citadel", "Anthropic")}
    assert len(accents) > 1


@pytest.mark.parametrize("company,expected", [
    ("Quantbot Technologies", "QT"),
    ("Stripe", "ST"),
    ("ByteDance", "BY"),
    ("", "?"),
])
def test_monograms(company, expected):
    assert monogram(company) == expected


def test_research_urls_skip_the_ats_host():
    """boards.greenhouse.io is not the company's site, so never fetch its root."""
    urls = _candidate_urls(job(url="https://boards.greenhouse.io/quantbot/jobs/1"))
    assert not any(u.rstrip("/").endswith("greenhouse.io") for u in urls)
    assert any("quantbot.com" in u for u in urls)


def test_a_posting_we_already_have_the_text_of_is_not_refetched():
    """ATS postings carry their description; fetching them again learns nothing."""
    with_text = job(url="https://www.quantbot.com/careers/1", description="Full posting text.")
    assert "https://www.quantbot.com/careers/1" not in _candidate_urls(with_text)


def test_a_posting_with_no_text_falls_back_to_its_own_url():
    """Feeds carry no description, so the posting page is the only source."""
    without = job(url="https://www.quantbot.com/careers/1")
    assert "https://www.quantbot.com/careers/1" in _candidate_urls(without)


def test_the_recruiting_subdomain_is_stripped():
    """apply.deloitte.com is a job-application host with no company content."""
    urls = _candidate_urls(job(company="Deloitte",
                               url="https://apply.deloitte.com/en_US/careers/JobDetail/x/1"))
    assert any(u.startswith("https://deloitte.com") for u in urls)


def test_the_posting_host_is_tried_when_it_is_the_company_site():
    urls = _candidate_urls(job(url="https://www.quantbot.com/careers/123"))
    assert any("quantbot.com" in u for u in urls)


# -- the digest email --------------------------------------------------------


def test_the_subject_counts_postings_and_companies():
    items = [DigestItem(job(company="A")), DigestItem(job(company="A")),
             DigestItem(job(company="B"))]
    assert "3 matches at 2 companies" in subject(items, NOW)


def test_an_empty_day_says_so():
    assert "nothing new" in subject([], NOW)


def test_the_body_carries_the_apply_link():
    item = DigestItem(job(url="https://example.com/apply"))
    assert 'href="https://example.com/apply"' in build_body([item], [], NOW)


def test_the_body_reports_the_score_and_reason():
    j = job()
    j.score, j.score_reason = 87.0, "Maps onto his transformer work."
    assert "87/100" in build_body([DigestItem(j)], [], NOW)
    assert "Maps onto his transformer work." in build_body([DigestItem(j)], [], NOW)


def test_an_untailored_fallback_is_disclosed(tmp_path):
    """You should know when a resume went out untailored, not discover it later."""
    resume = tmp_path / "Resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    body = build_body([DigestItem(job(), resume=resume, tailored=False)], [], NOW)
    assert "untailored" in body


def test_a_tailored_resume_is_not_flagged(tmp_path):
    resume = tmp_path / "Resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    body = build_body([DigestItem(job(), resume=resume, tailored=True)], [], NOW)
    assert "untailored" not in body


def test_a_posting_with_no_documents_says_so():
    assert "no documents could be generated" in build_body([DigestItem(job())], [], NOW)


def test_the_also_ranked_list_is_included():
    also = job(company="Runner Up")
    also.score = 40.0
    body = build_body([DigestItem(job())], [also], NOW)
    assert "Also posted (1)" in body
    assert "Runner Up" in body


def test_html_in_a_posting_cannot_break_the_email():
    """Titles come from third parties; they are escaped, not trusted."""
    body = build_body([DigestItem(job(title="<script>alert(1)</script> Intern"))], [], NOW)
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_an_ampersand_in_a_company_name_is_escaped():
    body = build_body([DigestItem(job(company="Sargent & Lundy"))], [], NOW)
    assert "Sargent &amp; Lundy" in body


# -- sending -----------------------------------------------------------------


def test_a_dry_run_sends_nothing(capsys):
    assert send([DigestItem(job())], [], to="x@example.com", now=NOW, dry_run=True) is False
    assert "subject:" in capsys.readouterr().out


def test_without_composio_nothing_is_sent(monkeypatch):
    """And it must report failure, so the postings are not marked delivered."""
    monkeypatch.setattr("delivery.email.available", lambda: False)
    assert send([DigestItem(job())], [], to="x@example.com", now=NOW) is False


def test_a_successful_send_reports_success(monkeypatch):
    monkeypatch.setattr("delivery.email.available", lambda: True)
    monkeypatch.setattr("delivery.email.execute", lambda slug, args: {"id": "1"})
    assert send([DigestItem(job())], [], to="x@example.com", now=NOW) is True


def test_a_failed_send_falls_back_to_a_draft(monkeypatch):
    """Eight tailored PDFs must not be thrown away by an expired token."""
    calls = []

    def _execute(slug, args):
        calls.append(slug)
        return None if slug == "GMAIL_SEND_EMAIL" else {"id": "draft-1"}

    monkeypatch.setattr("delivery.email.available", lambda: True)
    monkeypatch.setattr("delivery.email.execute", _execute)

    # False, deliberately: a draft is not delivery, so the postings stay unsent
    # and tomorrow's run retries them.
    assert send([DigestItem(job())], [], to="x@example.com", now=NOW) is False
    assert calls == ["GMAIL_SEND_EMAIL", "GMAIL_CREATE_EMAIL_DRAFT"]


def test_attachments_are_passed_as_paths(monkeypatch, tmp_path):
    captured = {}

    def _execute(slug, args):
        captured.update(args)
        return {"id": "1"}

    monkeypatch.setattr("delivery.email.available", lambda: True)
    monkeypatch.setattr("delivery.email.execute", _execute)

    resume = tmp_path / "Resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    cover = tmp_path / "Cover.pdf"
    cover.write_bytes(b"%PDF-1.4")

    send([DigestItem(job(), resume=resume, cover=cover)], [], to="x@example.com", now=NOW)
    assert len(captured["attachment"]) == 2
    assert all(str(resume.parent) in p for p in captured["attachment"])


def test_a_missing_file_is_not_attached(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr("delivery.email.available", lambda: True)
    monkeypatch.setattr("delivery.email.execute",
                        lambda slug, args: captured.update(args) or {"id": "1"})

    send([DigestItem(job(), resume=tmp_path / "gone.pdf")], [], to="x@example.com", now=NOW)
    assert "attachment" not in captured


# -- deriving a company's domain ---------------------------------------------


@pytest.mark.parametrize("company,expected_first", [
    # Corporate furniture is dropped: this is quantbot.com, and guessing
    # quantbottechnologies.com meant the research fetch found nothing.
    ("Quantbot Technologies", "quantbot.com"),
    ("Altamira Technologies", "altamira.com"),
    ("LPL Financial Holdings", "lplfinancial.com"),
    # ...but a real two-word name must survive intact.
    ("Applied Intuition", "appliedintuition.com"),
    ("Two Sigma", "twosigma.com"),
    ("Jane Street", "janestreet.com"),
    ("Deloitte", "deloitte.com"),
    ("Sargent & Lundy", "sargentlundy.com"),
])
def test_company_domains_strip_only_corporate_suffixes(company, expected_first):
    from tailor.branding import company_domains

    assert company_domains(company)[0] == expected_first


def test_the_unstripped_form_is_kept_as_a_fallback():
    """Some companies really do use the long form; try it second, not never."""
    from tailor.branding import company_domains

    assert "quantbottechnologies.com" in company_domains("Quantbot Technologies")


def test_a_one_word_name_is_never_emptied():
    from tailor.branding import company_domains

    assert company_domains("Trading") == ["trading.com"]


def test_an_unusable_name_yields_nothing():
    from tailor.branding import company_domains

    assert company_domains("") == []


# -- subject lines for postings the digest did not find -----------------------


def _item(company, title, terms):
    from delivery.email import DigestItem
    from models import Job

    return DigestItem(Job(company=company, title=title, terms=list(terms),
                          locations=["NY"], field_category="Software Engineering"))


def test_a_digest_of_one_term_is_named_after_it():
    from delivery.email import subject

    line = subject([_item("Citadel", "SWE Intern", ["Summer 2027"]),
                    _item("Jane Street", "SWE Intern", ["Summer 2027"])], NOW)
    assert line.startswith("Summer 2027 — 2 matches at 2 companies")


def test_a_single_termless_posting_is_named_after_the_employer():
    """A new-grad role has no term; heading it "Summer 2027" is just wrong."""
    from delivery.email import subject

    line = subject([_item("Stripe", "Software Engineer, New Grad", [])], NOW)
    assert line.startswith("Stripe — 1 match at 1 company")
    assert "Summer 2027" not in line


def test_mixed_terms_fall_back_to_the_configured_filter():
    from delivery.email import subject

    line = subject([_item("Citadel", "SWE Intern", ["Summer 2027"]),
                    _item("Ramp", "Co-op", ["Fall 2027"])], NOW)
    assert line.startswith("Summer 2027 — 2 matches")


def test_a_posting_with_its_own_term_uses_that_term():
    from delivery.email import subject

    line = subject([_item("Ramp", "Software Co-op", ["Fall 2027"])], NOW)
    assert line.startswith("Fall 2027 — 1 match at 1 company")


def test_an_empty_digest_still_names_the_filter():
    from delivery.email import subject

    assert subject([], NOW).startswith("Summer 2027 — nothing new")


def test_a_missing_recipient_stops_the_send(monkeypatch, tmp_path):
    """Attachments carry a real name and phone number - never guess a mailbox."""
    import config
    from delivery.email import send

    monkeypatch.setattr(config, "DIGEST_TO", "")

    def explode(*a, **kw):
        raise AssertionError("nothing may be sent without a recipient")

    monkeypatch.setattr("delivery.email.execute", explode)
    monkeypatch.setattr("delivery.email.available", lambda: True)

    assert send([_item("Stripe", "SWE", [])], [], now=NOW) is False


def test_an_explicit_recipient_still_works(monkeypatch):
    """The guard must not break the --to override."""
    import config
    from delivery.email import send

    monkeypatch.setattr(config, "DIGEST_TO", "")
    sent = {}
    monkeypatch.setattr("delivery.email.available", lambda: True)
    monkeypatch.setattr("delivery.email.execute",
                        lambda slug, payload: sent.update(payload) or {"successful": True})

    send([_item("Stripe", "SWE", [])], [], to="someone@example.com", now=NOW)
    assert sent.get("recipient_email") == "someone@example.com"
