"""Backwards-compatible re-exports.

The single-feed scraper that used to live here now sits behind the ``Source``
protocol in ``sources/``, alongside the ATS and search adapters. This module
stays so that existing imports - and the test suite written against them -
keep working unchanged.

New code should import from ``sources`` directly.
"""

from sources.base import FeedError, deduplicate
from sources.simplify import ListingsFeedScraper

__all__ = ["FeedError", "ListingsFeedScraper", "deduplicate"]
