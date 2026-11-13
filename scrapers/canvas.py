"""Canvas LMS integration using the official, documented REST API
(https://canvas.instructure.com/doc/api/).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

from models import Assignment, Grade
from scrapers.base import PortalAuthError, PortalFetchError, PortalScraper
from security_utils import create_resilient_session, validate_portal_url

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 15
MAX_PAGES = 10  # Safety cap against runaway pagination loops


class CanvasScraper(PortalScraper):
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
        super().__init__(
            base_url=base_url,
            username=username,
            secret=secret,
            config=config,
            user_agent_contact=user_agent_contact,
            allowed_domains=allowed_domains,
            allow_insecure_http=allow_insecure_http,
        )
        if not base_url:
            raise ValueError("Canvas scraper requires base_url, e.g. https://canvas.myschool.edu")

        # Validate URL for SSRF protection
        validated_base = validate_portal_url(
            base_url,
            allowed_domains=self.allowed_domains,
            allow_insecure_http=self.allow_insecure_http,
        )
        self._api_root = validated_base.rstrip("/") + "/api/v1"

        ua = f"NudgeBot/1.0 (Canvas API client; contact: {self.user_agent_contact})"
        self._session = create_resilient_session(
            user_agent=ua,
            allowed_domains=self.allowed_domains,
            allow_insecure_http=self.allow_insecure_http,
        )
        self._session.headers["Authorization"] = f"Bearer {secret}"

    def login(self) -> None:
        """Canvas has no separate login step — the bearer token is the credential.
        We validate that the token works against /users/self."""
        try:
            resp = self._session.get(f"{self._api_root}/users/self", timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise PortalFetchError(f"Could not reach Canvas server: {exc}") from exc

        if resp.status_code in (401, 403):
            raise PortalAuthError("Canvas personal access token was rejected or expired. Please generate a new one.")
        if resp.status_code >= 500:
            raise PortalFetchError(f"Canvas server returned error {resp.status_code}")
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise PortalFetchError(f"Canvas error: {exc}") from exc

    def _get_paginated(self, url: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Traverses Canvas RFC 5988 Link headers to fetch all pages up to MAX_PAGES."""
        all_records: list[dict[str, Any]] = []
        next_url: str | None = url
        current_params = params
        pages = 0

        while next_url and pages < MAX_PAGES:
            pages += 1
            try:
                resp = self._session.get(next_url, params=current_params, timeout=REQUEST_TIMEOUT_SECONDS)
            except requests.RequestException as exc:
                logger.warning("Canvas request failed for %s: %s", next_url, exc)
                break

            if resp.status_code in (401, 403):
                raise PortalAuthError("Canvas token rejected during fetch.")
            if resp.status_code >= 500:
                raise PortalFetchError(f"Canvas returned server error {resp.status_code}")
            if resp.status_code != 200:
                logger.debug("Canvas GET %s returned %s, stopping pagination", next_url, resp.status_code)
                break

            try:
                data = resp.json()
            except Exception:
                break

            if isinstance(data, list):
                all_records.extend(data)
            else:
                break

            # Check next link in Link header
            next_link = None
            if resp.links and "next" in resp.links:
                next_link = resp.links["next"].get("url")
            elif "Link" in resp.headers:
                match = re.search(r'<([^>]+)>;\s*rel="next"', resp.headers["Link"])
                if match:
                    next_link = match.group(1)

            if next_link:
                # Validate URL before requesting next page
                validate_portal_url(
                    next_link,
                    allowed_domains=self.allowed_domains,
                    allow_insecure_http=self.allow_insecure_http,
                )
                next_url = next_link
                current_params = None  # URL already includes query params
            else:
                next_url = None

        return all_records

    def _active_courses(self) -> list[dict[str, Any]]:
        raw_courses = self._get_paginated(
            f"{self._api_root}/courses",
            params={"enrollment_state": "active", "per_page": 100},
        )
        return [c for c in raw_courses if not c.get("access_restricted_by_date")]

    def fetch_assignments(self) -> list[Assignment]:
        out: list[Assignment] = []
        for course in self._active_courses():
            course_id = course["id"]
            course_name = course.get("course_code") or course.get("name", f"Course {course_id}")
            records = self._get_paginated(
                f"{self._api_root}/courses/{course_id}/assignments",
                params={"per_page": 100, "order_by": "due_at"},
            )
            for a in records:
                out.append(
                    Assignment(
                        course=course_name,
                        title=a.get("name", "Untitled assignment"),
                        key=f"{course_id}:{a['id']}",
                        due_date=a.get("due_at"),
                        points=str(a.get("points_possible")) if a.get("points_possible") is not None else None,
                        status="published" if a.get("published") else "unpublished",
                        url=a.get("html_url"),
                    )
                )
        return out

    def fetch_grades(self) -> list[Grade]:
        out: list[Grade] = []
        for course in self._active_courses():
            course_id = course["id"]
            course_name = course.get("course_code") or course.get("name", f"Course {course_id}")
            records = self._get_paginated(
                f"{self._api_root}/courses/{course_id}/assignments",
                params={"per_page": 100, "include[]": "submission"},
            )
            for a in records:
                submission = a.get("submission") or {}
                score = submission.get("score")
                if score is None:
                    continue  # not graded yet — nothing to report
                out.append(
                    Grade(
                        course=course_name,
                        title=a.get("name", "Untitled assignment"),
                        key=f"{course_id}:{a['id']}",
                        score=str(score),
                        max_score=str(a.get("points_possible")) if a.get("points_possible") is not None else None,
                        letter=submission.get("grade"),
                        url=a.get("html_url"),
                    )
                )
        return out

    def close(self) -> None:
        self._session.close()
