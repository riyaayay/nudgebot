"""Tests for database migrations, backward compatibility with legacy DB, and pruning."""

from __future__ import annotations

import os
import sqlite3
import tempfile

from db import Database


def test_migrations_fresh_db() -> None:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path)
        with db.cursor() as cur:
            # Check schema version
            cur.execute("SELECT MAX(version) FROM schema_version")
            assert cur.fetchone()[0] == 5

            # Check new columns exist on users table
            cur.execute("PRAGMA table_info(users)")
            cols = {r["name"] for r in cur.fetchall()}
            assert "timezone" in cols
            assert "consecutive_failures" in cols
            assert "last_notified_at" in cols

            # Check new tables exist
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r["name"] for r in cur.fetchall()}
            assert "events" in tables
            assert "deadline_reminders" in tables
            assert "feedback" in tables
        db.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_migrations_legacy_v1_upgrade() -> None:
    """Simulates an existing v1 SQLite database without schema_version table."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # Create legacy schema directly
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_chat_id TEXT UNIQUE NOT NULL,
                telegram_username TEXT,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                portal_type TEXT,
                portal_base_url TEXT,
                portal_username_enc TEXT,
                portal_secret_enc TEXT,
                portal_config_json TEXT,
                poll_interval_minutes INTEGER NOT NULL DEFAULT 10,
                last_poll_at TEXT,
                last_poll_status TEXT
            );
            INSERT INTO users (telegram_chat_id, telegram_username, created_at)
            VALUES ('legacy_user_1', 'old_user', '2026-09-01T00:00:00Z');
            """
        )
        conn.commit()
        conn.close()

        # Opening with Database should recognize existing users table,
        # register version 1, and apply migrations 2..5 cleanly without data loss.
        db = Database(path)
        with db.cursor() as cur:
            cur.execute("SELECT MAX(version) FROM schema_version")
            assert cur.fetchone()[0] == 5

            cur.execute("SELECT id, telegram_chat_id, timezone FROM users WHERE telegram_chat_id='legacy_user_1'")
            row = cur.fetchone()
            assert row is not None
            assert row["telegram_chat_id"] == "legacy_user_1"
            assert row["timezone"] == "Asia/Kolkata"  # Default from migration 2

        db.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_online_backup_api() -> None:
    fd1, src_path = tempfile.mkstemp(suffix=".db")
    os.close(fd1)
    fd2, dst_path = tempfile.mkstemp(suffix=".db")
    os.close(fd2)
    try:
        db = Database(src_path)
        db.get_or_create_user("backup_test_chat", "backup_user")
        db.backup_to(dst_path)
        db.close()

        # Verify dst_path has the data
        backup_db = Database(dst_path)
        user = backup_db.get_user_by_chat_id("backup_test_chat")
        assert user is not None
        assert user.telegram_username == "backup_user"
        backup_db.close()
    finally:
        for p in (src_path, dst_path):
            if os.path.exists(p):
                os.remove(p)
