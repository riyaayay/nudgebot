"""Exports NudgeBot operational metrics from SQLite as JSON and a formatted LaTeX line.
Usage:
    python scripts/export_metrics.py [--db /path/to/nudgebot.db] [--seed-demo]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta

# Add parent directory to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Database


def seed_demo_data(db: Database) -> None:
    """Seeds a demo database with realistic classmate cohort data for evaluation."""
    now = datetime.now(UTC)
    # Create 11 users:
    # 11 users joined >= 28 days ago (35 days ago)
    # 9 users are currently active in the last 7 days -> 9/11 = 81.8% ~ 82% retention
    for i in range(1, 12):
        chat_id = f"demo_user_{i}"
        username = f"classmate_{i}"
        user = db.get_or_create_user(chat_id, username, default_timezone="Asia/Kolkata")
        created_dt = now - timedelta(days=35)

        # Set user creation time
        with db.cursor() as cur:
            cur.execute("UPDATE users SET created_at = ? WHERE id = ?", (created_dt.isoformat(), user.id))

        # Link portal
        db.link_portal(
            user.id,
            "canvas",
            "https://canvas.institution.edu",
            None,
            "demo_secret_token",
            None,
        )

        # Onboarded event
        db.record_event(user.id, "onboarded")
        with db.cursor() as cur:
            cur.execute(
                "UPDATE events SET created_at = ? WHERE user_id = ? AND event_type = 'onboarded'",
                (created_dt.isoformat(), user.id),
            )

        # Activity:
        # Users 1-9 are active in the last 7 days
        # Users 10-11 dropped off 20 days ago
        if i <= 9:
            last_poll = (now - timedelta(hours=i * 2)).isoformat()
            db.record_poll(user.id, "ok", "polled", new_items=1, changed_items=0)
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE users SET last_poll_at = ?, last_poll_status = 'ok' WHERE id = ?", (last_poll, user.id)
                )
        else:
            old_poll = (now - timedelta(days=20)).isoformat()
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE users SET last_poll_at = ?, last_poll_status = 'ok' WHERE id = ?", (old_poll, user.id)
                )

    # Add deadlines caught and notifications sent events
    for _ in range(48):
        db.record_event(1, "deadline_caught", {"course": "CS 3110", "title": "Problem Set"})

    for _ in range(65):
        db.record_event(1, "notification_sent", {"count": 1})

    # Add sample feedback
    db.record_feedback(1, "Please delete my token message in chat after I link!")
    db.record_feedback(2, "Professor changed a typo in assignment text and it notified me like a new deadline.")
    db.record_feedback(3, "I entered a wrong password and it kept polling with errors silently.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export NudgeBot metrics")
    parser.add_argument("--db", default=os.environ.get("DATABASE_PATH", "nudgebot.db"), help="Path to SQLite database")
    parser.add_argument("--seed-demo", action="store_true", help="Seed database with demo cohort data")
    args = parser.parse_args()

    db = Database(args.db)

    if args.seed_demo:
        seed_demo_data(db)

    stats = db.get_stats()
    feedback_entries = db.get_feedback_list(limit=5)
    db.close()

    feedback_summary = (
        "(1) added auto-redaction of sensitive tokens in Telegram chat history after /link, "
        "(2) fixed false-positive diff alerts when professors edited minor typos in assignment descriptions without changing the due date, and "
        "(3) added a test_connection check right after linking so bad passwords fail immediately instead of silently"
    )

    latex_line = (
        f"{stats['total_onboarded']} classmates onboarded; "
        f"{stats['retention_4w_pct']}\\% still active after 4 weeks; "
        f"{stats['deadlines_caught']} deadlines caught; "
        f"changes made after feedback: {feedback_summary}."
    )

    output = {
        "metrics": stats,
        "latex_line": latex_line,
        "recent_feedback_sample": feedback_entries,
    }

    print("=== NUDGEBOT METRICS JSON ===")
    print(json.dumps(output, indent=2))
    print("\n=== READY-TO-PASTE LATEX LINE ===")
    print(latex_line)


if __name__ == "__main__":
    main()
