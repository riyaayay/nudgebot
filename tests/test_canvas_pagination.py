"""Tests for Canvas scraper: Link header pagination, auth error vs transient error classification."""

from __future__ import annotations

import responses

from scrapers.base import PortalAuthError, PortalFetchError
from scrapers.canvas import CanvasScraper


@responses.activate
def test_canvas_pagination_traversal() -> None:
    api_base = "https://canvas.test.edu"

    # Mock login check /users/self
    responses.add(
        responses.GET,
        f"{api_base}/api/v1/users/self",
        json={"id": 1, "name": "Test Student"},
        status=200,
    )

    # Page 1 of courses with Link header pointing to page 2
    page1_url = f"{api_base}/api/v1/courses?enrollment_state=active&per_page=100"
    page2_url = f"{api_base}/api/v1/courses?enrollment_state=active&page=2&per_page=100"

    responses.add(
        responses.GET,
        page1_url,
        json=[{"id": 101, "name": "CS 101"}],
        headers={"Link": f'<{page2_url}>; rel="next", <{page1_url}>; rel="first"'},
        status=200,
    )
    responses.add(
        responses.GET,
        page2_url,
        json=[{"id": 102, "name": "MATH 201"}],
        status=200,
    )

    # Assignments for course 101
    responses.add(
        responses.GET,
        f"{api_base}/api/v1/courses/101/assignments",
        json=[{"id": 1001, "name": "HW1", "due_at": "2026-10-01T23:59:00Z", "points_possible": 10, "published": True}],
        status=200,
    )
    # Assignments for course 102
    responses.add(
        responses.GET,
        f"{api_base}/api/v1/courses/102/assignments",
        json=[
            {"id": 1002, "name": "Quiz 1", "due_at": "2026-10-05T23:59:00Z", "points_possible": 5, "published": True}
        ],
        status=200,
    )

    scraper = CanvasScraper(
        base_url=api_base,
        username=None,
        secret="mock_token",
        allow_insecure_http=True,
    )
    scraper.login()
    assignments = scraper.fetch_assignments()

    assert len(assignments) == 2
    assert assignments[0].course == "CS 101"
    assert assignments[0].title == "HW1"
    assert assignments[1].course == "MATH 201"
    assert assignments[1].title == "Quiz 1"


@responses.activate
def test_canvas_auth_error_classification() -> None:
    api_base = "https://canvas.test.edu"

    # 401 Unauthorized
    responses.add(
        responses.GET,
        f"{api_base}/api/v1/users/self",
        status=401,
    )

    scraper = CanvasScraper(
        base_url=api_base,
        username=None,
        secret="bad_token",
        allow_insecure_http=True,
    )
    import pytest

    with pytest.raises(PortalAuthError, match="rejected or expired"):
        scraper.login()


@responses.activate
def test_canvas_transient_error_classification() -> None:
    api_base = "https://canvas.test.edu"

    # 503 Service Unavailable
    responses.add(
        responses.GET,
        f"{api_base}/api/v1/users/self",
        status=503,
    )

    scraper = CanvasScraper(
        base_url=api_base,
        username=None,
        secret="valid_token",
        allow_insecure_http=True,
    )
    import pytest

    with pytest.raises(PortalFetchError, match="503"):
        scraper.login()
