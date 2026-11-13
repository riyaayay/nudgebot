"""Tests for metrics calculations, cohort retention math, export_metrics script, and health server."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from db import Database
from health import HealthServer


def test_metrics_cohort_retention_calculation() -> None:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path)
        now = datetime.now(UTC)

        # Cohort 1: 10 users onboarded 35 days ago (>= 28 days)
        # 8 remain active today, 2 went inactive
        for i in range(1, 11):
            u = db.get_or_create_user(f"cohort_user_{i}", f"u{i}")
            db.link_portal(u.id, "canvas", "https://canvas.edu", None, "enc", None)
            created_str = (now - timedelta(days=35)).isoformat()
            db.record_event(u.id, "onboarded")
            with db.cursor() as cur:
                cur.execute("UPDATE users SET created_at = ? WHERE id = ?", (created_str, u.id))
                cur.execute(
                    "UPDATE events SET created_at = ? WHERE user_id = ? AND event_type='onboarded'", (created_str, u.id)
                )

            if i <= 8:
                # Active in last 7 days
                poll_time = (now - timedelta(days=2)).isoformat()
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET last_poll_at = ?, last_poll_status = 'ok' WHERE id = ?", (poll_time, u.id)
                    )
            else:
                # Inactive (last poll 20 days ago)
                poll_time = (now - timedelta(days=20)).isoformat()
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET last_poll_at = ?, last_poll_status = 'ok' WHERE id = ?", (poll_time, u.id)
                    )

        stats = db.get_stats()
        assert stats["total_onboarded"] == 10
        assert stats["currently_active"] == 8
        assert stats["retention_4w_cohort_size"] == 10
        assert stats["retention_4w_pct"] == 80.0  # 8 / 10 = 80.0%

        db.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_export_metrics_script_runs_cleanly() -> None:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        cmd = [sys.executable, "scripts/export_metrics.py", "--db", path, "--seed-demo"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        stdout = result.stdout

        assert "=== NUDGEBOT METRICS JSON ===" in stdout
        assert "=== READY-TO-PASTE LATEX LINE ===" in stdout
        assert "classmates onboarded;" in stdout
        assert "still active after 4 weeks;" in stdout
        assert "deadlines caught;" in stdout
    finally:
        if os.path.exists(path):
            os.remove(path)


@pytest.mark.asyncio
async def test_health_server_endpoint() -> None:
    server = HealthServer(port=8999, scheduler_interval_seconds=60)
    await server.start()

    def _fetch() -> tuple[int, str]:
        url = "http://127.0.0.1:8999/healthz"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.getcode(), resp.read().decode("utf-8")

    try:
        code, data = await asyncio.to_thread(_fetch)
        assert code == 200
        assert "healthy" in data
    finally:
        await server.stop()
