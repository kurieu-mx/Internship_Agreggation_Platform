"""The dashboard: routing, the file endpoint, and the backend default.

The pipeline itself is covered by the tests for the modules it is built from -
this file is only about the web layer, so `apply_url.prepare` is stubbed
throughout. The two things worth pinning down are that the dashboard defaults
itself onto the CLI backend (the entire reason it exists) and that the file
endpoint cannot be talked into serving something it should not.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import apply_url  # noqa: E402
from dashboard import app as dash  # noqa: E402
from models import Job  # noqa: E402


@pytest.fixture(autouse=True)
def clean_submissions():
    dash._submissions.clear()
    yield
    dash._submissions.clear()


@pytest.fixture
def client():
    return TestClient(dash.app)


def _job():
    return Job(company="IBM", title="Software Engineer Intern",
               locations=["Armonk, NY"], url="https://example.com/job",
               description="Build things.", field_category="AI / ML / Data",
               terms=["Summer 2027"], source="manual")


def _prepared(tmp_path, tailored=True, cover=True):
    prepared = apply_url.Prepared(_job())
    resume = tmp_path / "Resume_ibm.pdf"
    resume.write_bytes(b"%PDF-1.7 resume")
    prepared.resume, prepared.tailored = resume, tailored
    if cover:
        letter = tmp_path / "Cover_ibm.pdf"
        letter.write_bytes(b"%PDF-1.7 cover")
        prepared.cover = letter
    prepared.gates = [("warn", "closed to applicants needing sponsorship — F-1")]
    return prepared


# --- the whole point --------------------------------------------------------

def test_the_dashboard_defaults_itself_to_the_cli_backend():
    """Importing the app must have selected the subscription-billed backend.

    If this ever regresses, every pasted link silently costs ~$0.25-0.30
    again, which is the exact failure the dashboard was built to avoid.
    """
    import os

    assert os.environ["LLM_BACKEND"] == "cli"


# --- routing ----------------------------------------------------------------

def test_the_index_renders_with_no_submissions(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Application builder" in response.text
    # Nothing in flight, so the page must not be polling itself.
    assert "http-equiv=\"refresh\"" not in response.text


def test_a_submitted_url_is_queued_and_redirects(client, monkeypatch, tmp_path):
    monkeypatch.setattr(apply_url, "prepare",
                        lambda url, out_dir, **kw: _prepared(tmp_path))

    response = client.post("/apply", data={"url": "https://example.com/job"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    body = client.get("/").text
    assert "IBM" in body and "Software Engineer Intern" in body


def test_a_non_http_url_is_rejected_without_running_the_pipeline(client, monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("the pipeline must not run for a bad URL")

    monkeypatch.setattr(apply_url, "prepare", explode)

    client.post("/apply", data={"url": "file:///etc/passwd"}, follow_redirects=False)
    assert "does not look like a URL" in client.get("/").text


def test_an_unreadable_page_becomes_a_failed_submission(client, monkeypatch):
    monkeypatch.setattr(apply_url, "prepare", lambda url, out_dir, **kw: None)

    client.post("/apply", data={"url": "https://example.com/x"}, follow_redirects=False)
    body = client.get("/").text
    assert "could not be read" in body
    assert "Could not build an application" in body


def test_a_raising_pipeline_does_not_500_the_server(client, monkeypatch):
    def boom(url, out_dir, **kw):
        raise RuntimeError("weasyprint exploded")

    monkeypatch.setattr(apply_url, "prepare", boom)

    client.post("/apply", data={"url": "https://example.com/x"}, follow_redirects=False)
    response = client.get("/")
    assert response.status_code == 200
    assert "weasyprint exploded" in response.text


# --- rendering --------------------------------------------------------------

def test_gates_are_surfaced_on_the_card(client, monkeypatch, tmp_path):
    monkeypatch.setattr(apply_url, "prepare",
                        lambda url, out_dir, **kw: _prepared(tmp_path))
    client.post("/apply", data={"url": "https://example.com/job"},
                follow_redirects=False)
    assert "closed to applicants needing sponsorship" in client.get("/").text


def test_an_untailored_resume_is_labelled(client, monkeypatch, tmp_path):
    monkeypatch.setattr(apply_url, "prepare",
                        lambda url, out_dir, **kw: _prepared(tmp_path, tailored=False))
    client.post("/apply", data={"url": "https://example.com/job"},
                follow_redirects=False)
    body = client.get("/").text
    assert "untailored" in body


def test_company_names_are_escaped(client, monkeypatch, tmp_path):
    """A posting title is untrusted text lifted off someone else's page."""
    prepared = _prepared(tmp_path)
    prepared.job.company = "<script>alert(1)</script>"
    monkeypatch.setattr(apply_url, "prepare", lambda url, out_dir, **kw: prepared)

    client.post("/apply", data={"url": "https://example.com/job"},
                follow_redirects=False)
    body = client.get("/").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


# --- the file endpoint ------------------------------------------------------

def test_the_pdfs_are_served(client, monkeypatch, tmp_path):
    monkeypatch.setattr(apply_url, "prepare",
                        lambda url, out_dir, **kw: _prepared(tmp_path))
    client.post("/apply", data={"url": "https://example.com/job"},
                follow_redirects=False)

    submission_id = next(iter(dash._submissions))

    resume = client.get(f"/file/{submission_id}/resume")
    assert resume.status_code == 200
    assert resume.headers["content-type"] == "application/pdf"
    assert resume.content == b"%PDF-1.7 resume"

    assert client.get(f"/file/{submission_id}/cover").content == b"%PDF-1.7 cover"


def test_an_unknown_submission_is_404(client):
    assert client.get("/file/deadbeef/resume").status_code == 404


def test_an_unknown_role_is_404(client, monkeypatch, tmp_path):
    monkeypatch.setattr(apply_url, "prepare",
                        lambda url, out_dir, **kw: _prepared(tmp_path))
    client.post("/apply", data={"url": "https://example.com/job"},
                follow_redirects=False)
    submission_id = next(iter(dash._submissions))

    # Files are addressed by role, not by path, so there is nothing to
    # traverse - an arbitrary string simply does not name one of the two.
    assert client.get(f"/file/{submission_id}/../../etc/passwd").status_code == 404
    assert client.get(f"/file/{submission_id}/settings").status_code == 404


def test_a_missing_cover_is_404_rather_than_an_error(client, monkeypatch, tmp_path):
    monkeypatch.setattr(apply_url, "prepare",
                        lambda url, out_dir, **kw: _prepared(tmp_path, cover=False))
    client.post("/apply", data={"url": "https://example.com/job"},
                follow_redirects=False)
    submission_id = next(iter(dash._submissions))
    assert client.get(f"/file/{submission_id}/cover").status_code == 404


def test_a_deleted_file_is_404(client, monkeypatch, tmp_path):
    prepared = _prepared(tmp_path)
    monkeypatch.setattr(apply_url, "prepare", lambda url, out_dir, **kw: prepared)
    client.post("/apply", data={"url": "https://example.com/job"},
                follow_redirects=False)
    submission_id = next(iter(dash._submissions))

    Path(prepared.resume).unlink()
    assert client.get(f"/file/{submission_id}/resume").status_code == 404


# --- ordering ---------------------------------------------------------------

def test_the_newest_submission_is_listed_first(client, monkeypatch, tmp_path):
    monkeypatch.setattr(apply_url, "prepare",
                        lambda url, out_dir, **kw: _prepared(tmp_path))

    for name in ("Alpha", "Omega"):
        submission = dash.Submission(f"https://example.com/{name}")
        prepared = _prepared(tmp_path)
        prepared.job.company = name
        submission.prepared, submission.state = prepared, "done"
        submission.started = datetime(
            2026, 8, 12, 9 if name == "Alpha" else 17, tzinfo=timezone.utc)
        dash._record(submission)

    body = client.get("/").text
    assert body.index("Omega") < body.index("Alpha")
