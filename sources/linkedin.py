"""LinkedIn job postings, reached through public web search.

A note on the approach, because the obvious implementation is the wrong one.

LinkedIn has no public jobs API, its terms prohibit automated collection, and
it blocks scrapers aggressively - anything built against its HTML is broken by
design and stays broken. Composio's LinkedIn toolkit does not close that gap
either: it is oriented at profiles and posting content, not job search.

So this source does not touch LinkedIn. It searches the *public web*, scoped to
LinkedIn job URLs, using the same search toolkit as ``websearch.py``. Those
pages are the ones LinkedIn publishes for search engines to index, which is
exactly what a search engine is entitled to return. The trade is coverage: you
see the subset LinkedIn chooses to make public, not the logged-in feed.

Expect this leg to be thin and occasionally empty. It ranks last, contributes
nothing that the boards already have, and returns an empty list rather than
raising whenever search is unavailable.
"""

import logging
from typing import List

from sources.websearch import WebSearchSource

log = logging.getLogger(__name__)

_QUERIES = [
    "Summer 2027 software engineer intern",
    "Summer 2027 machine learning intern",
    "Summer 2027 quantitative intern",
]

_SITES = ["linkedin.com/jobs/view"]


class LinkedInSource(WebSearchSource):
    """Public LinkedIn job pages, via search. Never scrapes the site."""

    name = "linkedin"
    rank = 95  # below even generic web search

    def __init__(self, queries: List[str] = None, sites: List[str] = None, **kwargs):
        super().__init__(
            queries=queries if queries is not None else _QUERIES,
            sites=sites if sites is not None else _SITES,
            **kwargs,
        )

    def scrape(self) -> List:
        jobs = super().scrape()
        for job in jobs:
            job.source = "LinkedIn"
        return jobs
