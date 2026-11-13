"""Performs a live, consistent online backup of the SQLite database
using SQLite's online backup API, and prunes backups older than RETENTION_DAYS.
Usage:
    python scripts/backup_db.py [--source nudgebot.db] [--backup-dir ./backups] [--retention-days 7]
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from datetime import UTC, datetime

# Add parent directory to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup NudgeBot SQLite database")
    parser.add_argument("--source", default=os.environ.get("DATABASE_PATH", "nudgebot.db"), help="Source DB path")
    parser.add_argument(
        "--backup-dir", default=os.environ.get("BACKUP_DIR", "./backups"), help="Directory to store backups"
    )
    parser.add_argument("--retention-days", type=int, default=7, help="Days to keep backups")
    args = parser.parse_args()

    if not os.path.exists(args.source):
        print(f"Error: Source database file '{args.source}' does not exist.")
        sys.exit(1)

    os.makedirs(args.backup_dir, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    target_filename = f"nudgebot_backup_{timestamp}.db"
    target_path = os.path.join(args.backup_dir, target_filename)

    print(f"Starting online backup from {args.source} -> {target_path}...")
    db = Database(args.source)
    db.backup_to(target_path)
    db.close()
    print("Backup completed successfully!")

    # Prune old backups
    now_ts = datetime.now(UTC).timestamp()
    cutoff_ts = now_ts - (args.retention_days * 86400)
    backup_files = glob.glob(os.path.join(args.backup_dir, "nudgebot_backup_*.db"))

    pruned = 0
    for bf in backup_files:
        try:
            mtime = os.path.getmtime(bf)
            if mtime < cutoff_ts:
                os.remove(bf)
                pruned += 1
        except OSError:
            pass

    if pruned > 0:
        print(f"Pruned {pruned} backup(s) older than {args.retention_days} days.")


if __name__ == "__main__":
    main()
