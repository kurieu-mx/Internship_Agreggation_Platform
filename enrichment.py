"""Optional LLM enrichment via a local Ollama instance.

Scope
-----
The feed already carries authoritative values for most fields, so calling a
model on all of them would be slow and would risk overwriting good data with a
hallucination. Enrichment is therefore deliberately narrow:

* ``role`` - the upstream ``category`` is coarse ("AI/ML/Data" covers data
  analytics, ML research and ML infra alike). The title usually disambiguates,
  so the model maps the title onto a finer, fixed taxonomy.

Everything else is derived deterministically in ``normalize.py``.

Every model response is validated against the allowed label set before it is
used; anything unrecognised falls back to the deterministic value. If Ollama is
not running the pipeline logs one warning and continues unenriched, so the
project stays runnable with no local model installed.
"""

import logging
from typing import List, Optional

import requests

import config
from models import Job

log = logging.getLogger(__name__)

ROLE_LABELS = [
    "Software Engineering",
    "Machine Learning",
    "Data Science",
    "Data Engineering",
    "Hardware",
    "Security",
    "Site Reliability / Infrastructure",
    "Product",
    "Quant",
    "Research",
    "Other",
]

_PROMPT = """Classify the internship role below into exactly one category.

Categories: {labels}

Company: {company}
Title: {title}
Upstream category: {category}

Reply with the category name only, nothing else."""


class Enricher:
    """Thin, fail-soft wrapper around the Ollama generate API."""

    def __init__(self, base_url: str = None, model: str = None):
        self.base_url = (base_url or config.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or config.OLLAMA_MODEL
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        """Check once whether Ollama is reachable; cache the answer."""
        if self._available is not None:
            return self._available
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=5)
            response.raise_for_status()
            self._available = True
        except requests.RequestException as exc:
            log.warning(
                "Ollama unreachable at %s (%s) - continuing without enrichment",
                self.base_url,
                exc,
            )
            self._available = False
        return self._available

    def _generate(self, prompt: str) -> str:
        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": config.OLLAMA_TEMPERATURE},
                },
                timeout=config.OLLAMA_TIMEOUT,
            )
            response.raise_for_status()
            return (response.json().get("response") or "").strip()
        except (requests.RequestException, ValueError) as exc:
            log.debug("Ollama call failed: %s", exc)
            return ""

    @staticmethod
    def _match_label(reply: str) -> Optional[str]:
        """Resolve a free-text reply to a known label, or None.

        Small models like to answer "Category: Machine Learning" or wrap the
        answer in quotes, so match on containment rather than equality.
        """
        if not reply:
            return None
        cleaned = reply.strip().strip("\"'`.").lower()
        for label in ROLE_LABELS:
            if cleaned == label.lower():
                return label
        for label in ROLE_LABELS:
            if label.lower() in cleaned:
                return label
        return None

    def classify_role(self, job: Job) -> str:
        """Return a refined role label, falling back to the feed's category."""
        if not self.is_available():
            return job.field_category

        reply = self._generate(
            _PROMPT.format(
                labels=", ".join(ROLE_LABELS),
                company=job.company,
                title=job.title,
                category=job.field_category,
            )
        )
        return self._match_label(reply) or job.field_category

    def enrich(self, jobs: List[Job], progress_every: int = 25) -> List[Job]:
        """Enrich in place. A model failure degrades one field, never the run."""
        if not jobs or not self.is_available():
            return jobs

        log.info("enriching %d jobs with %s", len(jobs), self.model)
        for index, job in enumerate(jobs, start=1):
            try:
                job.field_category = self.classify_role(job)
            except Exception as exc:  # defensive: never lose a run to one job
                log.debug("enrichment failed for %s - %s: %s", job.company, job.title, exc)
            if progress_every and index % progress_every == 0:
                log.info("  enriched %d/%d", index, len(jobs))
        return jobs
