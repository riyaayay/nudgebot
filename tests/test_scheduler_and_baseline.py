"""Tests for scheduler tick, per-user interval calculation, and first-poll silent baselining."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from telegram import Bot

from config import Settings
from crypto_utils import CredentialCipher
from db import Database
from models import Assignment, Grade
from scheduler import poll_one_user


@pytest.fixture
def test_setup() -> tuple[Database, CredentialCipher, Settings, str]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    from cryptography.fernet import Fernet

    fernet_key = Fernet.generate_key().decode()
    cipher = CredentialCipher(fernet_key)

    settings = Settings(
        telegram_bot_token="123456789:ABCdefGHIjklMNOpqrSTUvwxYZ123456789",
        encryption_keys=[fernet_key],
        anthropic_api_key=None,
        anthropic_model="claude-3-5-sonnet-latest",
        default_poll_interval_minutes=10,
        min_poll_interval_minutes=5,
        database_path=path,
        log_level="INFO",
        log_format="text",
        admin_telegram_id=None,
        allowed_telegram_ids=None,
        invite_code=None,
        max_users=100,
        allowed_portal_domains=None,
        health_port=8080,
        user_agent_contact="portal owner",
        default_timezone="Asia/Kolkata",
        reminder_hours=[24, 3],
        max_daily_llm_calls=50,
    )
    yield db, cipher, settings, path
    db.close()
    if os.path.exists(path):
        os.remove(path)


def test_per_user_poll_interval_due_check(test_setup: tuple[Database, CredentialCipher, Settings, str]) -> None:
    db, cipher, settings, _ = test_setup
    now = datetime.now(UTC)

    # User 1: polled 2 mins ago, interval 10 -> NOT DUE
    u1 = db.get_or_create_user("chat_1", "user1", default_poll_interval=10)
    db.link_portal(u1.id, "canvas", "https://canvas.school.edu", None, cipher.encrypt("tok1"), None)
    db.record_poll(u1.id, "ok", "poll")
    with db.cursor() as cur:
        cur.execute("UPDATE users SET last_poll_at = ? WHERE id = ?", ((now - timedelta(minutes=2)).isoformat(), u1.id))

    # User 2: polled 12 mins ago, interval 10 -> DUE
    u2 = db.get_or_create_user("chat_2", "user2", default_poll_interval=10)
    db.link_portal(u2.id, "canvas", "https://canvas.school.edu", None, cipher.encrypt("tok2"), None)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE users SET last_poll_at = ? WHERE id = ?", ((now - timedelta(minutes=12)).isoformat(), u2.id)
        )

    # User 3: interval 2m (violates 5m min), polled 3m ago -> NOT DUE (enforces 5m minimum)
    u3 = db.get_or_create_user("chat_3", "user3", default_poll_interval=2)
    db.link_portal(u3.id, "canvas", "https://canvas.school.edu", None, cipher.encrypt("tok3"), None)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE users SET last_poll_at = ?, poll_interval_minutes = 2 WHERE id = ?",
            ((now - timedelta(minutes=3)).isoformat(), u3.id),
        )

    # User 4: never polled -> DUE
    u4 = db.get_or_create_user("chat_4", "user4", default_poll_interval=10)
    db.link_portal(u4.id, "canvas", "https://canvas.school.edu", None, cipher.encrypt("tok4"), None)

    due = db.get_users_due_for_poll(min_interval_minutes=5)
    due_ids = {u.id for u in due}

    assert u1.id not in due_ids
    assert u2.id in due_ids
    assert u3.id not in due_ids
    assert u4.id in due_ids


@pytest.mark.asyncio
async def test_first_poll_silent_baseline(test_setup: tuple[Database, CredentialCipher, Settings, str]) -> None:
    db, cipher, settings, _ = test_setup
    user = db.get_or_create_user("chat_baseline", "baseline_tester")
    db.link_portal(user.id, "canvas", "https://canvas.myschool.edu", None, cipher.encrypt("valid_token"), None)
    user = db.get_user_by_id(user.id)
    assert user is not None

    # Mock scraper returning 3 assignments and 1 grade
    mock_assignments = [
        Assignment(course="CS 101", title=f"HW {i}", key=f"c1:hw{i}", due_date="2026-10-01") for i in range(1, 4)
    ]
    mock_grades = [Grade(course="CS 101", title="Quiz 1", key="c1:q1", score="90", max_score="100")]

    mock_bot = AsyncMock(spec=Bot)
    mock_bot.send_message = AsyncMock()

    with patch("scheduler._instantiate_scraper") as mock_inst:
        mock_scraper = AsyncMock()
        mock_scraper.fetch_all = lambda: (mock_assignments, mock_grades, [])
        mock_inst.return_value = mock_scraper

        # Run first poll
        await poll_one_user(db, cipher, mock_bot, user, settings)

    # 1. Verify items were persisted to seen_items
    seen = db.get_seen_hashes(user.id)
    assert len(seen) == 4

    # 2. Verify exactly ONE friendly baseline message was sent (no spam diffs)
    assert mock_bot.send_message.call_count == 1
    sent_text = mock_bot.send_message.call_args[1]["text"]
    assert "Linked ✅ Tracking 3 assignments and 1 grades" in sent_text

    # 3. Verify onboarded event was recorded
    with db.cursor() as cur:
        cur.execute("SELECT event_type FROM events WHERE user_id = ?", (user.id,))
        events = [r[0] for r in cur.fetchall()]
        assert "onboarded" in events

    # 4. Run second poll with 1 new item: should send a notification
    user = db.get_user_by_id(user.id)
    mock_assignments.append(Assignment(course="CS 101", title="HW 4", key="c1:hw4", due_date="2026-10-15"))

    mock_bot.send_message.reset_mock()
    with patch("scheduler._instantiate_scraper") as mock_inst:
        mock_scraper = AsyncMock()
        mock_scraper.fetch_all = lambda: (mock_assignments, mock_grades, [])
        mock_inst.return_value = mock_scraper

        await poll_one_user(db, cipher, mock_bot, user, settings)

    # Exactly one notification sent for the new assignment
    assert mock_bot.send_message.call_count == 1
    sent_text2 = mock_bot.send_message.call_args[1]["text"]
    assert "HW 4" in sent_text2
