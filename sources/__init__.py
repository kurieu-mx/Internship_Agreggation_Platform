"""Source registry.

Adapters are imported lazily, so a source whose optional dependency or
credentials are missing costs you that source rather than the whole run. An
unknown name in ``SOURCES`` is a warning, not a crash - the digest should still
go out when a config typo slips through.
"""

import logging
from typing import Callable, Dict, List

from sources.base import FeedError, Source, collect, deduplicate

log = logging.getLogger(__name__)

__all__ = ["FeedError", "Source", "collect", "deduplicate", "build_sources", "REGISTRY"]


def _simplify() -> Source:
    from sources.simplify import ListingsFeedScraper

    return ListingsFeedScraper()


def _vansh() -> Source:
    from sources.vansh import VanshFeedScraper

    return VanshFeedScraper()


def _greenhouse() -> Source:
    from sources.greenhouse import GreenhouseSource

    return GreenhouseSource()


def _lever() -> Source:
    from sources.lever import LeverSource

    return LeverSource()


def _ashby() -> Source:
    from sources.ashby import AshbySource

    return AshbySource()


def _workday() -> Source:
    from sources.workday import WorkdaySource

    return WorkdaySource()


def _websearch() -> Source:
    from sources.websearch import WebSearchSource

    return WebSearchSource()


def _linkedin() -> Source:
    from sources.linkedin import LinkedInSource

    return LinkedInSource()


def _handshake() -> Source:
    from sources.handshake import HandshakeSource

    return HandshakeSource()


REGISTRY: Dict[str, Callable[[], Source]] = {
    "simplify": _simplify,
    "vansh": _vansh,
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "workday": _workday,
    "websearch": _websearch,
    "linkedin": _linkedin,
    "handshake": _handshake,
}


def build_sources(names: List[str]) -> List[Source]:
    """Instantiate the named sources, skipping any that cannot be built."""
    built: List[Source] = []
    for name in names:
        factory = REGISTRY.get(name)
        if factory is None:
            log.warning("unknown source %r - skipping (known: %s)",
                        name, ", ".join(sorted(REGISTRY)))
            continue
        try:
            built.append(factory())
        except Exception as exc:  # missing dependency, bad config file, ...
            log.warning("could not initialise source %s: %s", name, exc)
    return built
