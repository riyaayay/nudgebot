"""Shared data shapes. Every scraper (Canvas, generic HTML, or a future one)
returns these, so nothing downstream needs to know where the data came from.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from date_utils import normalize_due_date


class ItemType(StrEnum):
    ASSIGNMENT = "assignment"
    GRADE = "grade"
    ANNOUNCEMENT = "announcement"


@dataclass
class PortalItem:
    """Base shape every scraped item shares. `key` must be stable across polls
    (e.g. 'course_id:assignment_id') so the diff engine can track it over time.
    `mutable_fields` are the values that, if changed, should trigger a nudge
    (due date moved, grade posted, points changed, etc.)."""

    item_type: ItemType
    key: str
    course: str
    title: str
    mutable_fields: dict[str, Any] = field(default_factory=dict)
    url: str | None = None

    def content_hash(self) -> str:
        """Stable hash of the fields that matter for change detection.
        Excludes cosmetic differences like whitespace and non-significant keys."""
        normalized: dict[str, Any] = {}
        for k, v in sorted(self.mutable_fields.items()):
            if isinstance(v, str):
                normalized[k] = v.strip()
            else:
                normalized[k] = v
        payload = json.dumps(normalized, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Assignment(PortalItem):
    def __init__(
        self,
        course: str,
        title: str,
        key: str,
        due_date: str | None = None,
        points: str | None = None,
        status: str | None = None,
        url: str | None = None,
        default_tz: str = "Asia/Kolkata",
    ):
        norm_due = normalize_due_date(due_date, default_tz_name=default_tz) if due_date else None
        super().__init__(
            item_type=ItemType.ASSIGNMENT,
            key=key.strip(),
            course=course.strip(),
            title=title.strip(),
            mutable_fields={
                "due_date": norm_due,
                "points": str(points).strip() if points is not None else None,
                "status": status.strip() if status else None,
            },
            url=url.strip() if url else None,
        )


@dataclass
class Grade(PortalItem):
    def __init__(
        self,
        course: str,
        title: str,
        key: str,
        score: str | None = None,
        max_score: str | None = None,
        letter: str | None = None,
        url: str | None = None,
    ):
        super().__init__(
            item_type=ItemType.GRADE,
            key=key.strip(),
            course=course.strip(),
            title=title.strip(),
            mutable_fields={
                "score": str(score).strip() if score is not None else None,
                "max_score": str(max_score).strip() if max_score is not None else None,
                "letter": letter.strip() if letter else None,
            },
            url=url.strip() if url else None,
        )


@dataclass
class Announcement(PortalItem):
    def __init__(
        self,
        course: str,
        title: str,
        key: str,
        body_preview: str | None = None,
        posted_at: str | None = None,
        url: str | None = None,
    ):
        super().__init__(
            item_type=ItemType.ANNOUNCEMENT,
            key=key.strip(),
            course=course.strip(),
            title=title.strip(),
            mutable_fields={
                "body_preview": body_preview.strip() if body_preview else None,
                "posted_at": posted_at.strip() if posted_at else None,
            },
            url=url.strip() if url else None,
        )


@dataclass
class DiffResult:
    """One detected change: either an item we've never seen (is_new=True),
    a previously-seen item whose mutable fields changed (is_new=False),
    or an item removed from the portal (is_removed=True)."""

    item: PortalItem
    is_new: bool
    is_removed: bool = False
    previous_fields: dict[str, Any] | None = None  # only set when is_new=False or is_removed=True
