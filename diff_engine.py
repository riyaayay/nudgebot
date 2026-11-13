"""Diff engine: turns 'here is everything currently on the portal' into
'here is what is NEW, CHANGED, or REMOVED since last time' by comparing
content hashes against stored SQLite state.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from db import Database
from models import Announcement, Assignment, DiffResult, Grade, PortalItem

logger = logging.getLogger(__name__)


def compute_diff(
    db: Database,
    user_id: int,
    assignments: list[Assignment],
    grades: list[Grade],
    announcements: list[Announcement],
    detect_removed: bool = False,
) -> list[DiffResult]:
    """Compares scraped portal items against seen_items in the database for user_id.
    Returns DiffResults for any new or modified items (and optionally removed items)."""
    all_items: list[PortalItem] = [*assignments, *grades, *announcements]
    seen_hashes = db.get_seen_hashes(user_id)

    diffs: list[DiffResult] = []
    current_keys: set[str] = set()

    for item in all_items:
        composite_key = f"{item.item_type.value}:{item.key}"
        current_keys.add(composite_key)
        new_hash = item.content_hash()
        old_hash = seen_hashes.get(composite_key)

        if old_hash is None:
            diffs.append(DiffResult(item=item, is_new=True))
        elif old_hash != new_hash:
            previous = db.get_seen_fields(user_id, item.item_type.value, item.key)
            diffs.append(DiffResult(item=item, is_new=False, previous_fields=previous))
        # else: unchanged, do nothing

    if detect_removed:
        for seen_key, _ in seen_hashes.items():
            if seen_key not in current_keys:
                parts = seen_key.split(":", 1)
                item_type_str = parts[0]
                item_key = parts[1] if len(parts) > 1 else seen_key
                prev_fields = db.get_seen_fields(user_id, item_type_str, item_key) or {}
                # Create a placeholder PortalItem for the removed item
                from models import ItemType

                try:
                    itype = ItemType(item_type_str)
                except ValueError:
                    itype = ItemType.ASSIGNMENT
                dummy_item = PortalItem(
                    item_type=itype,
                    key=item_key,
                    course=prev_fields.get("course", "Unknown course"),
                    title=prev_fields.get("title", "Removed item"),
                    mutable_fields=prev_fields,
                )
                diffs.append(DiffResult(item=dummy_item, is_new=False, is_removed=True, previous_fields=prev_fields))

    return diffs


def persist_current_state(
    db: Database,
    user_id: int,
    assignments: list[Assignment],
    grades: list[Grade],
    announcements: list[Announcement],
) -> None:
    """Persists current items to seen_items. Should only be called after
    diffs are computed and any notifications have been successfully delivered."""
    for item in (*assignments, *grades, *announcements):
        # Store essential fields in raw_fields_json including url, title, and course for /deadlines
        fields: dict[str, Any] = dict(item.mutable_fields)
        fields["url"] = item.url
        fields["title"] = item.title
        fields["course"] = item.course
        db.upsert_seen_item(
            user_id=user_id,
            item_type=item.item_type.value,
            item_key=item.key,
            item_hash=item.content_hash(),
            title=item.title,
            course=item.course,
            raw_fields_json=json.dumps(fields, default=str),
        )
