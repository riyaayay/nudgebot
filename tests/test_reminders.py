"""Tests for deadline reminder logic, reminder windows, and duplicate prevention."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from telegram import Bot

from config import Settings
from db import Database
from diff_engine import persist_current_state
from models import Assignment
from scheduler import check_deadline_reminders


@pytest.fixture
def test_setup() -> tuple[Database, Settings, str]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    settings = Settings(
        telegram_bot_token="123456789:ABCdefGHIjklMNOpqrSTUvwxYZ123456789",
        encryption_keys=[key],
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
    yield db, settings, path
    db.close()
    if os.path.exists(path):
        os.remove(path)


@pytest.mark.asyncio
async def test_deadline_reminder_sent_once(test_setup: tuple[Database, Settings, str]) -> None:
    db, settings, _ = test_setup
    now = datetime.now(UTC)

    # Create linked user
    user = db.get_or_create_user("chat_reminder", "remind_me")
    db.link_portal(user.id, "canvas", "https://canvas.edu", None, "fake_enc", None)

    # Assignment due in exactly 24 hours
    due_24h = (now + timedelta(hours=24)).isoformat()
    a1 = Assignment(course="CS 3110", title="Prelim 1", key="c1:p1", due_date=due_24h)

    # Assignment due in 3 hours
    due_3h = (now + timedelta(hours=3)).isoformat()
    a2 = Assignment(course="ECON 201", title="Problem Set", key="c2:ps", due_date=due_3h)

    # Assignment due in 10 hours (outside 24h and 3h windows)
    due_10h = (now + timedelta(hours=10)).isoformat()
    a3 = Assignment(course="PHYS 110", title="Lab Report", key="c3:lab", due_date=due_10h)

    # Already submitted assignment due in 3 hours
    a4 = Assignment(course="CHEM 101", title="Quiz", key="c4:q", due_date=due_3h, status="submitted")

    persist_current_state(db, user.id, [a1, a2, a3, a4], [], [])

    mock_bot = AsyncMock(spec=Bot)
    mock_bot.send_message = AsyncMock()

    # 1. Run reminder check
    await check_deadline_reminders(db, mock_bot, settings)

    # Expected: 2 reminders sent (a1 for 24h, a2 for 3h; a3 outside window; a4 is submitted)
    assert mock_bot.send_message.call_count == 2
    assert db.has_deadline_reminder(user.id, "c1:p1", 24) is True
    assert db.has_deadline_reminder(user.id, "c2:ps", 3) is True
    assert db.has_deadline_reminder(user.id, "c3:lab", 24) is False
    assert db.has_deadline_reminder(user.id, "c4:q", 3) is False

    # 2. Run reminder check again immediately: should send ZERO duplicates!
    mock_bot.send_message.reset_mock()
    await check_deadline_reminders(db, mock_bot, settings)
    assert mock_bot.send_message.call_count == 0
