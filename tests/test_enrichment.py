import requests

from enrichment import Enricher
from models import Job


def make_job(**kwargs):
    defaults = dict(company="Acme", title="Machine Learning Intern", field_category="AI / ML / Data")
    defaults.update(kwargs)
    return Job(**defaults)


class TestLabelMatching:
    def test_exact_label(self):
        assert Enricher._match_label("Machine Learning") == "Machine Learning"

    def test_label_with_preamble_and_punctuation(self):
        assert Enricher._match_label('  "Category: Machine Learning."  ') == "Machine Learning"

    def test_case_insensitive(self):
        assert Enricher._match_label("machine learning") == "Machine Learning"

    def test_unknown_label_returns_none(self):
        assert Enricher._match_label("Underwater Basket Weaving") is None

    def test_empty_reply_returns_none(self):
        assert Enricher._match_label("") is None


class TestAvailability:
    def test_unreachable_ollama_is_not_available(self, monkeypatch):
        def boom(*args, **kwargs):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(requests, "get", boom)
        assert Enricher().is_available() is False

    def test_availability_is_probed_only_once(self, monkeypatch):
        calls = []

        def boom(*args, **kwargs):
            calls.append(1)
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(requests, "get", boom)
        enricher = Enricher()
        enricher.is_available()
        enricher.is_available()
        assert len(calls) == 1


class TestClassification:
    def test_falls_back_to_feed_category_when_ollama_is_down(self, monkeypatch):
        enricher = Enricher()
        monkeypatch.setattr(enricher, "is_available", lambda: False)
        job = make_job()
        assert enricher.classify_role(job) == "AI / ML / Data"

    def test_uses_validated_model_output(self, monkeypatch):
        enricher = Enricher()
        monkeypatch.setattr(enricher, "is_available", lambda: True)
        monkeypatch.setattr(enricher, "_generate", lambda prompt: "Machine Learning")
        assert enricher.classify_role(make_job()) == "Machine Learning"

    def test_rejects_hallucinated_label(self, monkeypatch):
        enricher = Enricher()
        monkeypatch.setattr(enricher, "is_available", lambda: True)
        monkeypatch.setattr(enricher, "_generate", lambda prompt: "Astronaut")
        assert enricher.classify_role(make_job()) == "AI / ML / Data"

    def test_enrich_skips_entirely_when_unavailable(self, monkeypatch):
        enricher = Enricher()
        monkeypatch.setattr(enricher, "is_available", lambda: False)
        jobs = [make_job()]
        assert enricher.enrich(jobs)[0].field_category == "AI / ML / Data"

    def test_one_failing_job_does_not_abort_the_batch(self, monkeypatch):
        enricher = Enricher()
        monkeypatch.setattr(enricher, "is_available", lambda: True)

        def flaky(job):
            if job.company == "Bad":
                raise RuntimeError("model exploded")
            return "Software Engineering"

        monkeypatch.setattr(enricher, "classify_role", flaky)
        jobs = [make_job(company="Bad"), make_job(company="Good")]
        enricher.enrich(jobs)
        assert jobs[0].field_category == "AI / ML / Data"      # untouched
        assert jobs[1].field_category == "Software Engineering"
