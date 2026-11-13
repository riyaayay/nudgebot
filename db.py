"""Thin SQLite wrapper with WAL mode, foreign keys, busy timeout,
and versioned schema migrations.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class User:
    id: int
    telegram_chat_id: str
    telegram_username: str | None
    is_active: bool
    portal_type: str | None
    portal_base_url: str | None
    portal_username_enc: str | None
    portal_secret_enc: str | None
    portal_config_json: str | None
    poll_interval_minutes: int
    last_poll_at: str | None
    last_poll_status: str | None
    timezone: str = "Asia/Kolkata"
    consecutive_failures: int = 0
    last_notified_at: str | None = None

    @property
    def is_linked(self) -> bool:
        return bool(self.portal_type and self.portal_secret_enc)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> User:
        keys = row.keys()
        return cls(
            id=row["id"],
            telegram_chat_id=row["telegram_chat_id"],
            telegram_username=row["telegram_username"],
            is_active=bool(row["is_active"]),
            portal_type=row["portal_type"],
            portal_base_url=row["portal_base_url"],
            portal_username_enc=row["portal_username_enc"],
            portal_secret_enc=row["portal_secret_enc"],
            portal_config_json=row["portal_config_json"],
            poll_interval_minutes=row["poll_interval_minutes"],
            last_poll_at=row["last_poll_at"],
            last_poll_status=row["last_poll_status"],
            timezone=row["timezone"] if "timezone" in keys and row["timezone"] else "Asia/Kolkata",
            consecutive_failures=row["consecutive_failures"]
            if "consecutive_failures" in keys and row["consecutive_failures"] is not None
            else 0,
            last_notified_at=row["last_notified_at"] if "last_notified_at" in keys else None,
        )


MIGRATIONS: list[str] = [
    # 001: Initial baseline schema
    """
    CREATE TABLE IF NOT EXISTS users (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_chat_id        TEXT UNIQUE NOT NULL,
        telegram_username       TEXT,
        created_at              TEXT NOT NULL,
        is_active               INTEGER NOT NULL DEFAULT 1,
        portal_type             TEXT,
        portal_base_url         TEXT,
        portal_username_enc     TEXT,
        portal_secret_enc       TEXT,
        portal_config_json      TEXT,
        poll_interval_minutes   INTEGER NOT NULL DEFAULT 10,
        last_poll_at            TEXT,
        last_poll_status        TEXT
    );

    CREATE TABLE IF NOT EXISTS seen_items (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        item_type       TEXT NOT NULL,
        item_key        TEXT NOT NULL,
        item_hash       TEXT NOT NULL,
        title           TEXT,
        course          TEXT,
        first_seen_at   TEXT NOT NULL,
        last_updated_at TEXT NOT NULL,
        raw_fields_json TEXT,
        UNIQUE(user_id, item_type, item_key)
    );

    CREATE TABLE IF NOT EXISTS poll_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        polled_at     TEXT NOT NULL,
        status        TEXT NOT NULL,
        detail        TEXT,
        new_items     INTEGER DEFAULT 0,
        changed_items INTEGER DEFAULT 0
    );
    """,
    # 002: Add timezone, consecutive failures, last notified to users
    """
    ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata';
    ALTER TABLE users ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE users ADD COLUMN last_notified_at TEXT;
    """,
    # 003: Events instrumentation table
    """
    CREATE TABLE IF NOT EXISTS events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
        event_type  TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        meta_json   TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_events_type_created ON events(event_type, created_at);
    CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);
    """,
    # 004: Deadline reminders tracking table
    """
    CREATE TABLE IF NOT EXISTS deadline_reminders (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        item_key        TEXT NOT NULL,
        reminder_hours  INTEGER NOT NULL,
        reminded_at     TEXT NOT NULL,
        UNIQUE(user_id, item_key, reminder_hours)
    );
    CREATE INDEX IF NOT EXISTS idx_reminders_user_key ON deadline_reminders(user_id, item_key);
    """,
    # 005: Feedback table
    """
    CREATE TABLE IF NOT EXISTS feedback (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at  TEXT NOT NULL,
        message     TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at);
    """,
]


class Database:
    """Thread-safe SQLite wrapper with WAL mode, foreign keys, busy timeout,
    and ordered schema migrations."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

        # Concurrency & reliability pragmas
        with self._lock:
            # WAL mode requires file-backed DB (in-memory ignores or uses memory journal)
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._run_migrations()

    def _run_migrations(self) -> None:
        """Applies versioned schema migrations sequentially."""
        cur = self._conn.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )"""
        )
        self._conn.commit()

        cur.execute("SELECT MAX(version) FROM schema_version")
        row = cur.fetchone()
        current_version = row[0] if row and row[0] is not None else 0

        # Check if legacy database (version 1 tables exist but schema_version was empty)
        if current_version == 0:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
            if cur.fetchone():
                current_version = 1
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (1, ?)",
                    (_now(),),
                )
                self._conn.commit()

        for idx, sql in enumerate(MIGRATIONS, start=1):
            if idx > current_version:
                logger.info("Applying database migration %03d...", idx)
                self._conn.executescript(sql)
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (idx, _now()),
                )
                self._conn.commit()
                current_version = idx
        cur.close()

    @contextmanager
    def cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    # ---- Users -----------------------------------------------------------

    def get_or_create_user(
        self,
        telegram_chat_id: str,
        telegram_username: str | None,
        default_timezone: str = "Asia/Kolkata",
        default_poll_interval: int = 10,
    ) -> User:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            )
            row = cur.fetchone()
            if row:
                # Update username if it changed
                if telegram_username and row["telegram_username"] != telegram_username:
                    cur.execute(
                        "UPDATE users SET telegram_username = ? WHERE id = ?",
                        (telegram_username, row["id"]),
                    )
                return User.from_row(row)

            now_str = _now()
            cur.execute(
                """INSERT INTO users (telegram_chat_id, telegram_username, created_at,
                   is_active, poll_interval_minutes, timezone, consecutive_failures)
                   VALUES (?, ?, ?, 1, ?, ?, 0)""",
                (
                    telegram_chat_id,
                    telegram_username,
                    now_str,
                    default_poll_interval,
                    default_timezone,
                ),
            )
            user_id = cur.lastrowid
            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            return User.from_row(cur.fetchone())

    def get_user_by_chat_id(self, telegram_chat_id: str) -> User | None:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            )
            row = cur.fetchone()
            return User.from_row(row) if row else None

    def get_user_by_id(self, user_id: int) -> User | None:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
            return User.from_row(row) if row else None

    def link_portal(
        self,
        user_id: int,
        portal_type: str,
        base_url: str | None,
        username_enc: str | None,
        secret_enc: str,
        config_json: str | None,
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                """UPDATE users SET
                   portal_type=?, portal_base_url=?, portal_username_enc=?,
                   portal_secret_enc=?, portal_config_json=?, consecutive_failures=0,
                   is_active=1
                   WHERE id=?""",
                (portal_type, base_url, username_enc, secret_enc, config_json, user_id),
            )

    def unlink_portal(self, user_id: int) -> None:
        with self.cursor() as cur:
            cur.execute(
                """UPDATE users SET
                   portal_type=NULL, portal_base_url=NULL, portal_username_enc=NULL,
                   portal_secret_enc=NULL, portal_config_json=NULL, consecutive_failures=0
                   WHERE id=?""",
                (user_id,),
            )
            cur.execute("DELETE FROM seen_items WHERE user_id = ?", (user_id,))
            cur.execute("DELETE FROM deadline_reminders WHERE user_id = ?", (user_id,))

    def set_active(self, user_id: int, is_active: bool) -> None:
        with self.cursor() as cur:
            cur.execute(
                "UPDATE users SET is_active=? WHERE id=?",
                (int(is_active), user_id),
            )

    def update_user_timezone(self, user_id: int, timezone: str) -> None:
        with self.cursor() as cur:
            cur.execute("UPDATE users SET timezone=? WHERE id=?", (timezone, user_id))

    def update_user_interval(self, user_id: int, poll_interval_minutes: int) -> None:
        with self.cursor() as cur:
            cur.execute(
                "UPDATE users SET poll_interval_minutes=? WHERE id=?",
                (poll_interval_minutes, user_id),
            )

    def record_poll(
        self,
        user_id: int,
        status: str,
        detail: str = "",
        new_items: int = 0,
        changed_items: int = 0,
    ) -> None:
        with self.cursor() as cur:
            now = _now()
            cur.execute(
                "UPDATE users SET last_poll_at=?, last_poll_status=? WHERE id=?",
                (now, status, user_id),
            )
            cur.execute(
                """INSERT INTO poll_log (user_id, polled_at, status, detail, new_items, changed_items)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, now, status, detail, new_items, changed_items),
            )

    def increment_consecutive_failures(self, user_id: int) -> int:
        with self.cursor() as cur:
            cur.execute(
                "UPDATE users SET consecutive_failures = consecutive_failures + 1 WHERE id = ?",
                (user_id,),
            )
            cur.execute("SELECT consecutive_failures FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
            return row["consecutive_failures"] if row else 1

    def reset_consecutive_failures(self, user_id: int) -> None:
        with self.cursor() as cur:
            cur.execute("UPDATE users SET consecutive_failures = 0 WHERE id = ?", (user_id,))

    def update_last_notified(self, user_id: int) -> None:
        with self.cursor() as cur:
            cur.execute("UPDATE users SET last_notified_at=? WHERE id=?", (_now(), user_id))

    def list_active_linked_users(self) -> list[User]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE is_active=1 AND portal_type IS NOT NULL AND portal_secret_enc IS NOT NULL"
            )
            return [User.from_row(r) for r in cur.fetchall()]

    def list_all_users(self) -> list[User]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY id ASC")
            return [User.from_row(r) for r in cur.fetchall()]

    def get_users_due_for_poll(self, min_interval_minutes: int = 5) -> list[User]:
        """Returns users who are active, linked, and whose elapsed time since
        last poll meets their configured poll_interval_minutes (capped at min_interval_minutes)."""
        active_users = self.list_active_linked_users()
        now_dt = datetime.now(UTC)
        due: list[User] = []
        for u in active_users:
            if not u.last_poll_at:
                due.append(u)
                continue
            try:
                last_dt = datetime.fromisoformat(u.last_poll_at)
                effective_minutes = max(u.poll_interval_minutes, min_interval_minutes)
                if (now_dt - last_dt).total_seconds() >= effective_minutes * 60:
                    due.append(u)
            except Exception:
                due.append(u)
        return due

    def get_user_count(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            return cur.fetchone()[0]

    def delete_user_data(self, user_id: int) -> None:
        """GDPR full erasure: permanently wipes user, credentials, seen items,
        logs, reminders, and feedback."""
        with self.cursor() as cur:
            cur.execute("DELETE FROM seen_items WHERE user_id=?", (user_id,))
            cur.execute("DELETE FROM poll_log WHERE user_id=?", (user_id,))
            cur.execute("DELETE FROM deadline_reminders WHERE user_id=?", (user_id,))
            cur.execute("DELETE FROM feedback WHERE user_id=?", (user_id,))
            cur.execute("DELETE FROM events WHERE user_id=?", (user_id,))
            cur.execute("DELETE FROM users WHERE id=?", (user_id,))

    # ---- Seen Items (Diff State) -----------------------------------------

    def get_seen_hashes(self, user_id: int) -> dict[str, str]:
        """Returns {f'{item_type}:{item_key}': item_hash} for fast lookup."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT item_type, item_key, item_hash FROM seen_items WHERE user_id=?",
                (user_id,),
            )
            return {f"{r['item_type']}:{r['item_key']}": r["item_hash"] for r in cur.fetchall()}

    def get_seen_fields(self, user_id: int, item_type: str, item_key: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            cur.execute(
                """SELECT raw_fields_json FROM seen_items
                   WHERE user_id=? AND item_type=? AND item_key=?""",
                (user_id, item_type, item_key),
            )
            row = cur.fetchone()
            return json.loads(row["raw_fields_json"]) if row and row["raw_fields_json"] else None

    def upsert_seen_item(
        self,
        user_id: int,
        item_type: str,
        item_key: str,
        item_hash: str,
        title: str,
        course: str,
        raw_fields_json: str,
    ) -> None:
        with self.cursor() as cur:
            now = _now()
            cur.execute(
                """INSERT INTO seen_items
                   (user_id, item_type, item_key, item_hash, title, course,
                    first_seen_at, last_updated_at, raw_fields_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, item_type, item_key) DO UPDATE SET
                     item_hash=excluded.item_hash,
                     title=excluded.title,
                     course=excluded.course,
                     last_updated_at=excluded.last_updated_at,
                     raw_fields_json=excluded.raw_fields_json""",
                (user_id, item_type, item_key, item_hash, title, course, now, now, raw_fields_json),
            )

    def get_stored_assignments(self, user_id: int) -> list[dict[str, Any]]:
        """Returns all stored assignments for a user, parsing raw_fields_json."""
        with self.cursor() as cur:
            cur.execute(
                """SELECT item_key, title, course, first_seen_at, last_updated_at, raw_fields_json
                   FROM seen_items WHERE user_id=? AND item_type='assignment'
                   ORDER BY course, title""",
                (user_id,),
            )
            rows = cur.fetchall()
            assignments: list[dict[str, Any]] = []
            for r in rows:
                fields = json.loads(r["raw_fields_json"]) if r["raw_fields_json"] else {}
                assignments.append(
                    {
                        "key": r["item_key"],
                        "title": r["title"] or "Untitled assignment",
                        "course": r["course"] or "Unknown course",
                        "due_date": fields.get("due_date"),
                        "points": fields.get("points"),
                        "status": fields.get("status"),
                        "url": fields.get("url"),
                    }
                )
            return assignments

    # ---- Reminders -------------------------------------------------------

    def has_deadline_reminder(self, user_id: int, item_key: str, reminder_hours: int) -> bool:
        with self.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM deadline_reminders
                   WHERE user_id=? AND item_key=? AND reminder_hours=?""",
                (user_id, item_key, reminder_hours),
            )
            return cur.fetchone() is not None

    def record_deadline_reminder(self, user_id: int, item_key: str, reminder_hours: int) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO deadline_reminders (user_id, item_key, reminder_hours, reminded_at)
                   VALUES (?, ?, ?, ?)""",
                (user_id, item_key, reminder_hours, _now()),
            )

    # ---- Feedback --------------------------------------------------------

    def record_feedback(self, user_id: int, message: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO feedback (user_id, created_at, message) VALUES (?, ?, ?)",
                (user_id, _now(), message),
            )

    def get_feedback_list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(
                """SELECT f.id, f.user_id, f.created_at, f.message, u.telegram_username, u.telegram_chat_id
                   FROM feedback f
                   JOIN users u ON f.user_id = u.id
                   ORDER BY f.id DESC LIMIT ?""",
                (limit,),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "created_at": r["created_at"],
                    "message": r["message"],
                    "username": r["telegram_username"],
                    "chat_id": r["telegram_chat_id"],
                }
                for r in rows
            ]

    # ---- Events & Metrics ------------------------------------------------

    def record_event(
        self,
        user_id: int | None,
        event_type: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Records an event for metrics and cohort retention tracking.
        event_type: onboarded | notification_sent | deadline_caught |
                    reminder_sent | feedback_received | command_used | unlinked | llm_call
        """
        meta_str = json.dumps(meta) if meta else None
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO events (user_id, event_type, created_at, meta_json) VALUES (?, ?, ?, ?)",
                (user_id, event_type, _now(), meta_str),
            )

    def record_llm_call(self, provider: str, meta: dict[str, Any] | None = None) -> None:
        """Records an LLM call event for daily quota tracking."""
        payload = {"provider": provider}
        if meta:
            payload.update(meta)
        self.record_event(user_id=None, event_type="llm_call", meta=payload)

    def get_daily_llm_call_count(self) -> int:
        """Returns the number of LLM calls made today (UTC)."""
        today_start = datetime.now(UTC).strftime("%Y-%m-%dT00:00:00")
        with self.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = 'llm_call' AND created_at >= ?",
                (today_start,),
            )
            row = cur.fetchone()
            return row[0] if row else 0

    def get_stats(self) -> dict[str, Any]:
        """Calculates production metrics:
        - total onboarded
        - currently active (linked, is_active=1, successful poll or interaction in last 7 days)
        - 4-week cohort retention (% of users onboarded >= 28 days ago who are currently active)
        - total deadlines caught
        - notifications sent
        - error rate (poll_log error % over last 7 days)
        - feedback count
        """
        now = datetime.now(UTC)
        seven_days_ago = (now - timedelta(days=7)).isoformat()
        four_weeks_ago = (now - timedelta(days=28)).isoformat()

        with self.cursor() as cur:
            # Total onboarded (distinct users with 'onboarded' event or who are linked)
            cur.execute("SELECT COUNT(DISTINCT user_id) FROM events WHERE event_type = 'onboarded'")
            onboarded_events_count = cur.fetchone()[0] or 0
            cur.execute("SELECT COUNT(*) FROM users WHERE portal_type IS NOT NULL")
            currently_linked_count = cur.fetchone()[0] or 0
            total_onboarded = max(onboarded_events_count, currently_linked_count)

            # Currently active: linked + is_active=1 AND (successful poll in last 7 days OR event in last 7 days)
            cur.execute(
                """SELECT COUNT(DISTINCT u.id)
                   FROM users u
                   WHERE u.is_active = 1
                     AND u.portal_type IS NOT NULL
                     AND (
                         (u.last_poll_status = 'ok' AND u.last_poll_at >= ?)
                         OR EXISTS (SELECT 1 FROM events e WHERE e.user_id = u.id AND e.created_at >= ?)
                     )""",
                (seven_days_ago, seven_days_ago),
            )
            currently_active = cur.fetchone()[0] or 0

            # 4-week cohort retention:
            # Denominator: users whose first 'onboarded' event was >= 28 days ago (or created_at <= 28 days ago and linked)
            cur.execute(
                """SELECT DISTINCT user_id FROM events
                   WHERE event_type = 'onboarded' AND created_at <= ?
                   UNION
                   SELECT id FROM users
                   WHERE created_at <= ? AND portal_type IS NOT NULL""",
                (four_weeks_ago, four_weeks_ago),
            )
            cohort_user_ids = [r[0] for r in cur.fetchall() if r[0] is not None]
            cohort_size = len(cohort_user_ids)

            if cohort_size > 0:
                placeholders = ",".join("?" for _ in cohort_user_ids)
                params: list[Any] = [*cohort_user_ids, seven_days_ago, seven_days_ago]
                cur.execute(
                    f"""SELECT COUNT(DISTINCT u.id)
                       FROM users u
                       WHERE u.id IN ({placeholders})
                         AND u.is_active = 1
                         AND u.portal_type IS NOT NULL
                         AND (
                             (u.last_poll_status = 'ok' AND u.last_poll_at >= ?)
                             OR EXISTS (SELECT 1 FROM events e WHERE e.user_id = u.id AND e.created_at >= ?)
                         )""",
                    params,
                )
                cohort_active = cur.fetchone()[0] or 0
                retention_4w_pct = round((cohort_active / cohort_size) * 100.0, 1)
            else:
                retention_4w_pct = 100.0 if currently_active > 0 else 0.0

            # Deadlines caught
            cur.execute("SELECT COUNT(*) FROM events WHERE event_type = 'deadline_caught'")
            deadlines_caught = cur.fetchone()[0] or 0

            # Notifications sent
            cur.execute("SELECT COUNT(*) FROM events WHERE event_type = 'notification_sent'")
            notifications_sent = cur.fetchone()[0] or 0

            # Error rate over last 7 days
            cur.execute(
                "SELECT COUNT(*), SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) FROM poll_log WHERE polled_at >= ?",
                (seven_days_ago,),
            )
            row = cur.fetchone()
            total_polls_7d = row[0] or 0
            err_polls_7d = row[1] or 0
            error_rate_pct = round((err_polls_7d / total_polls_7d) * 100.0, 1) if total_polls_7d > 0 else 0.0

            # Feedback count
            cur.execute("SELECT COUNT(*) FROM feedback")
            feedback_count = cur.fetchone()[0] or 0

            return {
                "total_onboarded": total_onboarded,
                "currently_active": currently_active,
                "retention_4w_cohort_size": cohort_size,
                "retention_4w_pct": retention_4w_pct,
                "deadlines_caught": deadlines_caught,
                "notifications_sent": notifications_sent,
                "error_rate_pct": error_rate_pct,
                "feedback_count": feedback_count,
            }

    # ---- Maintenance & Backups -------------------------------------------

    def prune_old_data(self, poll_log_days: int = 30, events_days: int = 365) -> tuple[int, int]:
        """Prunes historical poll_log (> 30 days) and events (> 1 year)."""
        now = datetime.now(UTC)
        poll_cutoff = (now - timedelta(days=poll_log_days)).isoformat()
        events_cutoff = (now - timedelta(days=events_days)).isoformat()

        with self.cursor() as cur:
            cur.execute("DELETE FROM poll_log WHERE polled_at < ?", (poll_cutoff,))
            pruned_polls = cur.rowcount
            cur.execute("DELETE FROM events WHERE created_at < ?", (events_cutoff,))
            pruned_events = cur.rowcount
            return pruned_polls, pruned_events

    def backup_to(self, target_path: str) -> None:
        """Creates an atomic, consistent online backup of the SQLite database."""
        with self._lock:
            dest_conn = sqlite3.connect(target_path)
            try:
                self._conn.backup(dest_conn)
            finally:
                dest_conn.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
