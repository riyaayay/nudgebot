# NudgeBot Production Readiness Audit & Remediation Log

**Author:** Senior Python Backend Engineer  
**Date:** November 2026  
**Status:** All Critical, Important, and Operational Gaps FIXED and Verified  

---

## Executive Summary

NudgeBot has been successfully taken from a "works on my machine" weekend prototype to a hardened, production-deployment-ready application. All 14 audit focus areas have been addressed with architectural integrity intact:
- The `PortalScraper` interface and `SCRAPER_REGISTRY` are preserved.
- Pure SQLite is maintained with `WAL` mode, foreign keys, `busy_timeout`, and versioned migrations.
- Blocking I/O is eliminated via `asyncio.to_thread`.
- Security vulnerabilities (SSRF, credentials lingering in chat history, unvalidated base URLs, missing key rotation) are completely remediated.
- First-poll baselining avoids message storms.
- All promised features (`/deadlines`, `/timezone`, `/feedback`, `/privacy`, `/deletemydata`, admin commands, 24h/3h deadline reminders) and metrics tracking are implemented with 100% passing tests.

---

## 1. Critical Issues (Bugs, Security Vulnerabilities & Data Loss)

| Issue ID | Description | Severity | Status | Remediation & Files |
|---|---|---|---|---|
| **CRIT-1** | Blocking I/O in Asyncio Event Loop (`requests`, `BeautifulSoup`, `Anthropic`) | Critical | **FIXED** | Scraper `fetch_all` executed in worker thread pools via `asyncio.to_thread`. LLM calls executed asynchronously with `asyncio.to_thread` fallback in `summarizer.py`. Telegram handlers remain responsive. (`scheduler.py`, `summarizer.py`) |
| **CRIT-2** | First-Poll Notification Spam on New User Link | Critical | **FIXED** | Implemented silent baselining on initial poll. First poll stores all items silently, suppresses diff notifications, emits a single friendly welcome summary (`"Linked ✅ Tracking N assignments, M grades..."`), and logs an `onboarded` event. (`scheduler.py`) |
| **CRIT-3** | SSRF Vulnerability in Portal Base URL | Critical | **FIXED** | Built `validate_portal_url()` enforcing `https://`, blocking loopback/private/link-local/cloud-metadata IP ranges via `ipaddress` inspection, blocking redirects to private subnets via `SSRFSafeRedirectSession`, and supporting `ALLOWED_PORTAL_DOMAINS`. (`security_utils.py`, `scrapers/canvas.py`, `scrapers/generic_html.py`) |
| **CRIT-4** | Credential Exposure in Telegram Chat History | Critical | **FIXED** | Telegram secret messages (tokens, passwords) are immediately deleted via `await update.message.delete()`. If deletion fails, user receives a prominent warning to delete or revoke it immediately. (`telegram_bot.py`) |
| **CRIT-5** | Blind Credential Storage Without Verification | Critical | **FIXED** | `/link` now performs an immediate live test authentication (`scraper.login()`) in a worker thread before writing credentials to the database. Clear error messages are provided on failure. (`telegram_bot.py`) |
| **CRIT-6** | Non-Atomic State Persistence and Lost Notifications | Critical | **FIXED** | `send_update()` returns boolean delivery status. `persist_current_state()` is called **only if** delivery succeeded (or if there were zero diffs). On delivery failure, items remain un-persisted and will be retried on the next poll cycle. (`scheduler.py`, `notifier.py`) |
| **CRIT-7** | Missing Key Rotation and Single Key Dependency | Critical | **FIXED** | Upgraded `CredentialCipher` to support `MultiFernet` with comma-separated `ENCRYPTION_KEYS` (newest first). New encryptions use the primary key; decryptions fall back across all configured keys. Backward compatibility with `ENCRYPTION_KEY` maintained. (`crypto_utils.py`) |

---

## 2. Important Issues (Reliability, Architecture & Gaps)

| Issue ID | Description | Severity | Status | Remediation & Files |
|---|---|---|---|---|
| **IMP-1** | Per-User Polling Interval Ignored | Important | **FIXED** | Replaced global interval job with a 1-minute tick evaluating `(now - last_poll_at) >= poll_interval_minutes`. Enforced `MIN_POLL_INTERVAL_MINUTES` minimum of 5 minutes. (`scheduler.py`, `db.py`) |
| **IMP-2** | Unbounded Sequential Polling & Lack of Concurrency Limits | Important | **FIXED** | Implemented bounded concurrency with `asyncio.Semaphore(5)`, per-user timeout of 60 seconds (`asyncio.wait_for`), and random jitter (0.2s–2.0s) to prevent thundering herd requests. (`scheduler.py`) |
| **IMP-3** | SQLite Concurrency, Missing WAL Mode, and Lack of Schema Migrations | Important | **FIXED** | Enabled `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, and `busy_timeout=5000`. Built an ordered migration engine with `schema_version` table (5 migrations: baseline, user metadata, events, reminders, feedback) with automatic legacy v1 migration. (`db.py`) |
| **IMP-4** | Telegram Message Formatting and Error Fragility | Important | **FIXED** | Replaced legacy markdown with HTML formatting via `html.escape()`. Implemented automatic chunking under 3,800 chars. Handled `RetryAfter`, `Forbidden` (auto-deactivates user), and `BadRequest` (falls back to plain text). (`notifier.py`) |
| **IMP-5** | Canvas Pagination Missing | Important | **FIXED** | Added RFC 5988 `Link` header traversal (`rel="next"`) in `CanvasScraper._get_paginated` up to a safety ceiling of 10 pages, ensuring users with many active courses get all items. (`scrapers/canvas.py`) |
| **IMP-6** | Summarizer Hardening, Model Alias, Batching and Cost Controls | Important | **FIXED** | Configurable `ANTHROPIC_MODEL` via env (default `claude-3-5-sonnet-latest`). Added diff chunking into groups of up to 20 items, API timeouts (20s), max retries (2), daily quota enforcement (`MAX_DAILY_LLM_CALLS`), and verified zero credentials are sent to LLM. (`summarizer.py`) |
| **IMP-7** | Scraper Error Classification and Auto-Backoff | Important | **FIXED** | Differentiated `PortalAuthError` (401/403) from `PortalFetchError` (5xx/timeout). Auto-pause after 5 consecutive failures with a single notification to protect user accounts. (`scheduler.py`, `scrapers/base.py`) |
| **IMP-8** | Lack of Access Control and User Limits | Important | **FIXED** | Implemented `ALLOWED_TELEGRAM_IDS` allowlist, `INVITE_CODE` registration mode (`/start <code>`), command rate limiting (15 cmds/10s), and `MAX_USERS` cap. (`telegram_bot.py`) |
| **IMP-9** | Missing Feature Commands and Reminders | Important | **FIXED** | Implemented `/deadlines` (upcoming 7-day sorted view), `/timezone` (default `Asia/Kolkata`), 24h and 3h deadline reminders, `/feedback`, `/stats`, `/broadcast`, `/users`, `/deletemydata`, and `/privacy`. (`telegram_bot.py`, `scheduler.py`) |
| **IMP-10** | Missing Metrics and Cohort Retention Instrumentation | Important | **FIXED** | Added `events` table tracking `onboarded`, `notification_sent`, `deadline_caught`, `reminder_sent`, `feedback_received`, `command_used`, `unlinked`. Built cohort 4-week retention calculation in `db.get_stats()` and `scripts/export_metrics.py`. (`db.py`, `scripts/export_metrics.py`) |

---

## 3. Nice-to-Have / Operational Improvements

| Issue ID | Description | Severity | Status | Remediation & Files |
|---|---|---|---|---|
| **NICE-1** | Healthcheck and Docker Production Hardening | Nice-to-have | **FIXED** | Multi-stage slim Dockerfile running as non-root `appuser:appgroup` (UID 10001). Zero-dependency HTTP `/healthz` server on `HEALTH_PORT=8080` failing with 503 if scheduler stalls > 180s. Docker `HEALTHCHECK` configured. (`health.py`, `Dockerfile`, `docker-compose.yml`) |
| **NICE-2** | Logging Security and JSON Logs | Nice-to-have | **FIXED** | Added `SecretRedactingFilter` scrubbing tokens, passwords, authorization headers, and keys from all logs. Added `LOG_FORMAT=json` support. Redacted config summary printed at startup. (`config.py`, `security_utils.py`) |
| **NICE-3** | Automated Database Backups and Retention | Nice-to-have | **FIXED** | Added `scripts/backup_db.py` and `scripts/backup_db.sh` using SQLite online backup API (`conn.backup()`) with retention pruning. Added optional backup sidecar in `docker-compose.yml`. (`scripts/backup_db.py`, `scripts/backup_db.sh`) |
| **NICE-4** | Test Coverage, Type Hints, and CI | Nice-to-have | **FIXED** | 44 tests passing covering all modules. Configured `ruff`, `mypy`, and `pytest` in `pyproject.toml`. Added GitHub Actions workflow `.github/workflows/ci.yml`. (`tests/`, `pyproject.toml`, `.github/workflows/ci.yml`) |
| **NICE-5** | Documentation & Runbook | Nice-to-have | **FIXED** | Created `DEPLOY.md`, `RUNBOOK.md`, `PRIVACY.md`, `CHANGELOG.md`, `Makefile`, and updated `README.md`. |
