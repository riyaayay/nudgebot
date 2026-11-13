"""Configurable requests + BeautifulSoup scraper for portals without a public API.
Supports form login, selector extraction for assignments, grades, announcements,
and SSRF validation.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag

from models import Announcement, Assignment, Grade
from scrapers.base import PortalAuthError, PortalFetchError, PortalScraper
from security_utils import create_resilient_session, validate_generic_config, validate_portal_url

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 20


def _extract_field(row: Tag, field_cfg: dict[str, Any]) -> str | None:
    selector = field_cfg.get("selector")
    if not selector:
        return None
    el = row.select_one(selector)
    if el is None:
        return None
    attr = field_cfg.get("attr", "text")
    if attr == "text":
        return el.get_text(strip=True)
    val = el.get(attr)
    return str(val).strip() if val is not None else None


class GenericHTMLScraper(PortalScraper):
    def __init__(
        self,
        base_url: str | None,
        username: str | None,
        secret: str,
        config: dict[str, Any] | None = None,
        user_agent_contact: str = "portal owner",
        allowed_domains: list[str] | None = None,
        allow_insecure_http: bool = False,
        default_tz: str = "Asia/Kolkata",
    ):
        super().__init__(
            base_url=base_url,
            username=username,
            secret=secret,
            config=config,
            user_agent_contact=user_agent_contact,
            allowed_domains=allowed_domains,
            allow_insecure_http=allow_insecure_http,
        )
        if not self.config:
            raise ValueError("GenericHTMLScraper requires a selector config — see README.")

        # Validate config and URLs
        self.config = validate_generic_config(
            self.config,
            allowed_domains=self.allowed_domains,
            allow_insecure_http=self.allow_insecure_http,
        )
        self.default_tz = default_tz

        ua = f"NudgeBot/1.0 (Personal deadline reminder; contact: {self.user_agent_contact})"
        self._session = create_resilient_session(
            user_agent=ua,
            allowed_domains=self.allowed_domains,
            allow_insecure_http=self.allow_insecure_http,
        )

    def login(self) -> None:
        cfg = self.config
        login_url = cfg.get("login_url")
        if not login_url:
            return  # Some portals require no explicit login or rely on basic auth

        login_url = validate_portal_url(
            login_url,
            allowed_domains=self.allowed_domains,
            allow_insecure_http=self.allow_insecure_http,
        )

        fields_cfg = cfg.get("login_fields", {})
        payload = {
            fields_cfg.get("username_field", "username"): self.username,
            fields_cfg.get("password_field", "password"): self.secret,
        }
        payload.update(cfg.get("login_extra_fields", {}))

        method = cfg.get("login_method", "POST").upper()
        try:
            if method == "POST":
                resp = self._session.post(login_url, data=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            else:
                resp = self._session.get(login_url, params=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise PortalFetchError(f"Could not reach portal login page: {exc}") from exc

        if resp.status_code in (401, 403):
            raise PortalAuthError("Portal rejected login credentials.")
        if resp.status_code >= 500:
            raise PortalFetchError(f"Portal returned {resp.status_code} during login.")

        fail_marker = cfg.get("login_failure_text_marker")
        if fail_marker and fail_marker in resp.text:
            raise PortalAuthError("Portal login page indicates invalid credentials.")

    def _get_soup(self, url: str) -> BeautifulSoup:
        validated_url = validate_portal_url(
            url,
            allowed_domains=self.allowed_domains,
            allow_insecure_http=self.allow_insecure_http,
        )
        try:
            resp = self._session.get(validated_url, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise PortalFetchError(f"Could not reach {validated_url}: {exc}") from exc

        if resp.status_code in (401, 403):
            raise PortalAuthError(f"Session expired or access forbidden for {validated_url}")
        if resp.status_code >= 500:
            raise PortalFetchError(f"{validated_url} returned {resp.status_code}")
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise PortalFetchError(f"HTTP error for {validated_url}: {exc}") from exc

        return BeautifulSoup(resp.text, "html.parser")

    def fetch_assignments(self) -> list[Assignment]:
        cfg = self.config
        url = cfg.get("assignments_url")
        if not url:
            return []
        soup = self._get_soup(url)
        rows = soup.select(cfg["assignment_row_selector"])
        fields_cfg = cfg.get("fields", {})
        out: list[Assignment] = []
        for i, row in enumerate(rows):
            course = _extract_field(row, fields_cfg.get("course", {})) or "Unknown course"
            title = _extract_field(row, fields_cfg.get("title", {})) or f"Untitled #{i}"
            due_date = _extract_field(row, fields_cfg["due_date"]) if "due_date" in fields_cfg else None
            points = _extract_field(row, fields_cfg["points"]) if "points" in fields_cfg else None
            key = row.get(cfg.get("row_id_attr", "id")) or f"{course}::{title}"
            item_url = None
            if "url" in fields_cfg:
                item_url = _extract_field(row, fields_cfg["url"])
            out.append(
                Assignment(
                    course=course,
                    title=title,
                    key=str(key),
                    due_date=due_date,
                    points=points,
                    url=item_url,
                    default_tz=self.default_tz,
                )
            )
        return out

    def fetch_grades(self) -> list[Grade]:
        cfg = self.config
        url = cfg.get("grades_url")
        if not url:
            return []
        soup = self._get_soup(url)
        rows = soup.select(cfg["grade_row_selector"])
        fields_cfg = cfg.get("grade_fields", {})
        out: list[Grade] = []
        for i, row in enumerate(rows):
            course = _extract_field(row, fields_cfg.get("course", {})) or "Unknown course"
            title = _extract_field(row, fields_cfg.get("title", {})) or f"Item #{i}"
            score = _extract_field(row, fields_cfg["score"]) if "score" in fields_cfg else None
            max_score = _extract_field(row, fields_cfg["max_score"]) if "max_score" in fields_cfg else None
            letter = _extract_field(row, fields_cfg["letter"]) if "letter" in fields_cfg else None
            key = row.get(cfg.get("row_id_attr", "id")) or f"{course}::{title}"
            item_url = None
            if "url" in fields_cfg:
                item_url = _extract_field(row, fields_cfg["url"])
            out.append(
                Grade(
                    course=course,
                    title=title,
                    key=str(key),
                    score=score,
                    max_score=max_score,
                    letter=letter,
                    url=item_url,
                )
            )
        return out

    def fetch_announcements(self) -> list[Announcement]:
        cfg = self.config
        url = cfg.get("announcements_url")
        selector = cfg.get("announcement_row_selector")
        if not url or not selector:
            return []
        soup = self._get_soup(url)
        rows = soup.select(selector)
        fields_cfg = cfg.get("announcement_fields", {})
        out: list[Announcement] = []
        for i, row in enumerate(rows):
            course = _extract_field(row, fields_cfg.get("course", {})) or "Unknown course"
            title = _extract_field(row, fields_cfg.get("title", {})) or f"Announcement #{i}"
            body = _extract_field(row, fields_cfg["body"]) if "body" in fields_cfg else None
            key = row.get(cfg.get("row_id_attr", "id")) or f"{course}::{title}"
            out.append(Announcement(course=course, title=title, key=str(key), body_preview=body))
        return out

    def close(self) -> None:
        self._session.close()
