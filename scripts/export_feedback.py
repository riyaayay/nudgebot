"""Exports all user feedback from SQLite to JSON or Markdown.
Usage:
    python scripts/export_feedback.py [--db /path/to/nudgebot.db] [--format json|markdown]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Add parent directory to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Export NudgeBot user feedback")
    parser.add_argument("--db", default=os.environ.get("DATABASE_PATH", "nudgebot.db"), help="Path to SQLite database")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format")
    args = parser.parse_args()

    db = Database(args.db)
    feedback_list = db.get_feedback_list(limit=500)
    db.close()

    if args.format == "json":
        print(json.dumps(feedback_list, indent=2))
        return

    print("# User Feedback Log\n")
    if not feedback_list:
        print("No feedback entries recorded yet.")
        return

    print("| Date | User | Message |")
    print("|---|---|---|")
    for item in feedback_list:
        user_str = f"@{item['username']}" if item.get("username") else f"User {item['user_id']}"
        date_str = item["created_at"].split("T")[0]
        # Clean pipes from markdown table message
        msg_str = item["message"].replace("|", "\\|").replace("\n", " ")
        print(f"| {date_str} | {user_str} | {msg_str} |")


if __name__ == "__main__":
    main()
