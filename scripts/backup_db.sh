#!/usr/bin/env bash
# Live backup of NudgeBot database using Python online backup API
set -euo pipefail

DB_PATH="${DATABASE_PATH:-data/nudgebot.db}"
BACKUP_DIR="${BACKUP_DIR:-data/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

mkdir -p "$BACKUP_DIR"
python3 scripts/backup_db.py --source "$DB_PATH" --backup-dir "$BACKUP_DIR" --retention-days "$RETENTION_DAYS"

