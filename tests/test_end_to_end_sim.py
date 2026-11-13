"""End-to-end simulation test verifying:
link -> baseline (no spam) -> new assignment (1 notification) ->
due date change (1 notification) -> deadline reminder fires once (no duplicates).
"""

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
from scheduler import check_deadline_reminders, poll_one_user


@pytest.mark.asyncio
async def test_complete_end_to_end_lifecycle() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    from cryptography.fernet import Fernet

    fernet_key = Fernet.generate_key().decode()
    cipher = CredentialCipher(fernet_key)
    db = Database(db_path)

    settings = Settings(
        telegram_bot_token="123456789:ABCdefGHIjklMNOpqrSTUvwxYZ123456789",
        encryption_keys=[fernet_key],
        anthropic_api_key=None,  # deterministic templates
        anthropic_model="claude-3-5-sonnet-latest",
        default_poll_interval_minutes=10,
        min_poll_interval_minutes=5,
        database_path=db_path,
        log_level="INFO",
        log_format="text",
        admin_telegram_id=None,
        allowed_telegram_ids=None,
        invite_code=None,
        max_users=100,
        allowed_portal_domains=None,
        health_port=8080,
        user_agent_contact="portal owner",
        default_timezone="UTC",
        reminder_hours=[24, 3],
        max_daily_llm_calls=50,
    )

    mock_bot = AsyncMock(spec=Bot)
    mock_bot.send_message = AsyncMock()

    try:
        # -------------------------------------------------------------------
        # Step 1: User links portal
        # -------------------------------------------------------------------
        chat_id = "simulated_student_123"
        user = db.get_or_create_user(chat_id, "student_jane", default_timezone="UTC")
        token_enc = cipher.encrypt("canvas_access_token_abc")
        db.link_portal(user.id, "canvas", "https://canvas.school.edu", None, token_enc, None)
        user = db.get_user_by_id(user.id)
        assert user is not None
        assert user.is_linked is True

        # -------------------------------------------------------------------
        # Step 2: First poll -> silent baseline (zero spam, 1 friendly confirmation)
        # -------------------------------------------------------------------
        now = datetime.now(UTC)
        initial_due = (now + timedelta(days=5)).isoformat()
        current_portal_assignments = [
            Assignment(course="CS 3110", title="Problem Set 1", key="c1:ps1", due_date=initial_due),
            Assignment(course="ECON 201", title="Reading Quiz", key="c2:rq1", due_date=initial_due),
        ]
        current_portal_grades = [
            Grade(course="CS 3110", title="Diagnostic Quiz", key="c1:diag", score="10", max_score="10")
        ]

        with patch("scheduler._instantiate_scraper") as mock_inst:
            mock_scraper = AsyncMock()
            mock_scraper.fetch_all = lambda: (list(current_portal_assignments), list(current_portal_grades), [])
            mock_inst.return_value = mock_scraper

            await poll_one_user(db, cipher, mock_bot, user, settings)

        # Verification of baseline
        assert mock_bot.send_message.call_count == 1
        baseline_msg = mock_bot.send_message.call_args[1]["text"]
        assert "Linked ✅ Tracking 2 assignments and 1 grades" in baseline_msg
        assert len(db.get_seen_hashes(user.id)) == 3
        stats = db.get_stats()
        assert stats["total_onboarded"] == 1
        assert stats["notifications_sent"] == 0  # no notification diffs sent during baseline!

        # -------------------------------------------------------------------
        # Step 3: Second poll -> A new assignment appears on the portal
        # -------------------------------------------------------------------
        mock_bot.send_message.reset_mock()
        user = db.get_user_by_id(user.id)

        # 24 hours until due
        due_24h = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
        new_assignment = Assignment(course="CS 3110", title="Problem Set 2", key="c1:ps2", due_date=due_24h)
        current_portal_assignments.append(new_assignment)

        with patch("scheduler._instantiate_scraper") as mock_inst:
            mock_scraper = AsyncMock()
            mock_scraper.fetch_all = lambda: (list(current_portal_assignments), list(current_portal_grades), [])
            mock_inst.return_value = mock_scraper

            await poll_one_user(db, cipher, mock_bot, user, settings)

        # Verification of new assignment alert
        assert mock_bot.send_message.call_count == 1
        new_item_msg = mock_bot.send_message.call_args[1]["text"]
        assert "Problem Set 2" in new_item_msg
        assert "🆕 New assignment" in new_item_msg
        stats = db.get_stats()
        assert stats["notifications_sent"] == 1
        assert stats["deadlines_caught"] == 1

        # -------------------------------------------------------------------
        # Step 4: Third poll -> Due date changes on Problem Set 2
        # -------------------------------------------------------------------
        mock_bot.send_message.reset_mock()
        user = db.get_user_by_id(user.id)

        # Moved to 23.9 hours from now
        changed_due = (datetime.now(UTC) + timedelta(hours=23.9)).isoformat()
        current_portal_assignments[2] = Assignment(
            course="CS 3110",
            title="Problem Set 2",
            key="c1:ps2",
            due_date=changed_due,
        )

        with patch("scheduler._instantiate_scraper") as mock_inst:
            mock_scraper = AsyncMock()
            mock_scraper.fetch_all = lambda: (list(current_portal_assignments), list(current_portal_grades), [])
            mock_inst.return_value = mock_scraper

            await poll_one_user(db, cipher, mock_bot, user, settings)

        # Verification of changed notification
        assert mock_bot.send_message.call_count == 1
        changed_msg = mock_bot.send_message.call_args[1]["text"]
        assert "Problem Set 2" in changed_msg
        assert "Due date changed" in changed_msg or "updated" in changed_msg
        stats = db.get_stats()
        assert stats["notifications_sent"] == 2
        assert stats["deadlines_caught"] == 2  # due date change counted

        # -------------------------------------------------------------------
        # Step 5: Reminder tick -> 24-hour reminder fires once
        # -------------------------------------------------------------------
        mock_bot.send_message.reset_mock()

        # Run reminder sweep
        await check_deadline_reminders(db, mock_bot, settings)

        # Exactly 1 reminder sent for Problem Set 2 (due in ~24h)
        assert mock_bot.send_message.call_count == 1
        reminder_msg = mock_bot.send_message.call_args[1]["text"]
        assert "Upcoming Deadline Reminder" in reminder_msg
        assert "Problem Set 2" in reminder_msg
        assert "~24h" in reminder_msg
        assert db.has_deadline_reminder(user.id, "c1:ps2", 24) is True

        # Second reminder sweep: ZERO duplicate messages!
        mock_bot.send_message.reset_mock()
        await check_deadline_reminders(db, mock_bot, settings)
        assert mock_bot.send_message.call_count == 0

    finally:
        db.close()
        if os.path.exists(db_path):
            os.remove(db_path)
