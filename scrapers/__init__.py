"""Registry mapping portal_type -> scraper class. Add a new portal by
implementing PortalScraper elsewhere in this package and registering it here
— nothing else in the codebase needs to change.
"""

from scrapers.base import PortalAuthError, PortalFetchError, PortalScraper
from scrapers.canvas import CanvasScraper
from scrapers.generic_html import GenericHTMLScraper

SCRAPER_REGISTRY: dict[str, type[PortalScraper]] = {
    "canvas": CanvasScraper,
    "generic_html": GenericHTMLScraper,
}

__all__ = [
    "CanvasScraper",
    "GenericHTMLScraper",
    "PortalAuthError",
    "PortalFetchError",
    "PortalScraper",
    "SCRAPER_REGISTRY",
]
