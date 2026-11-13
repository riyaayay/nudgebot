"""Every portal integration implements this interface. The rest of the
pipeline (diff engine, summarizer, notifier) only ever talks to this, never
to Canvas or BeautifulSoup directly — that is what makes it easy to add a
new portal without touching anything else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from models import Announcement, Assignment, Grade


class PortalAuthError(Exception):
    """Raised when login/token validation fails — surfaced to the user as
    'your portal link expired, please /link again'."""


class PortalFetchError(Exception):
    """Raised for transient failures (timeouts, 5xx, layout changed). These
    get logged and retried next cycle rather than shown to the user."""


class PortalScraper(ABC):
    """One instance is created per poll cycle for a given user, then discarded.
    Implementations should be safe to construct cheaply and should not cache
    credentials beyond their own lifetime."""

    def __init__(
        self,
        base_url: str | None,
        username: str | None,
        secret: str,
        config: dict[str, Any] | None = None,
        user_agent_contact: str = "portal owner",
        allowed_domains: list[str] | None = None,
        allow_insecure_http: bool = False,
    ):
        self.base_url = base_url
        self.username = username
        self.secret = secret  # password or API token, already decrypted
        self.config = config or {}
        self.user_agent_contact = user_agent_contact
        self.allowed_domains = allowed_domains
        self.allow_insecure_http = allow_insecure_http

    @abstractmethod
    def login(self) -> None:
        """Establish an authenticated session. Raise PortalAuthError on bad
        credentials, PortalFetchError on network/transient issues."""

    @abstractmethod
    def fetch_assignments(self) -> list[Assignment]:
        """Fetch current list of assignments from portal."""

    @abstractmethod
    def fetch_grades(self) -> list[Grade]:
        """Fetch current list of grades from portal."""

    def fetch_announcements(self) -> list[Announcement]:
        """Optional — not every portal implementation needs to support this.
        Default is empty list rather than forcing every scraper to implement it."""
        return []

    def close(self) -> None:
        """Override to tear down sessions/connections. No-op by default."""
        return

    def fetch_all(self) -> tuple[list[Assignment], list[Grade], list[Announcement]]:
        self.login()
        try:
            return self.fetch_assignments(), self.fetch_grades(), self.fetch_announcements()
        finally:
            self.close()
