<div align="center">

# NudgeBot 🔔
### *The Autonomous, Zero-Leakage Campus Portal Sentinel*

[![CI](https://github.com/your-username/nudgebot/actions/workflows/ci.yml/badge.svg)](https://github.com/your-username/nudgebot/actions)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Telegram Bot API](https://img.shields.io/badge/telegram_bot_api-21.4-2CA5E0.svg?logo=telegram)](https://core.telegram.org/bots/api)
[![Database](https://img.shields.io/badge/storage-SQLite_WAL-003B57.svg?logo=sqlite)](https://www.sqlite.org/wal.html)
[![Docker Ready](https://img.shields.io/badge/docker-ready-2496ED.svg?logo=docker)](https://www.docker.com/)
[![Security: Fernet AES-128](https://img.shields.io/badge/security-MultiFernet_AES--128-success.svg)](https://cryptography.io/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

<p align="center">
  <a href="https://web.telegram.org/k/#@Nudge_AtWorkBot">
    <img src="https://img.shields.io/badge/Telegram-@Nudge__AtWorkBot-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white" alt="Telegram Bot">
  </a>
</p>

<p align="center">
  <strong>Stalks campus portals (Canvas LMS &amp; custom HTML) so students never miss a deadline.</strong><br>
  Detects silent due-date alterations, new assignments, and posted grades; transforms messy HTML/JSON into verified, punchy notifications; and pushes them instantly via Telegram.
</p>

</div>

---

## 📌 Executive Summary & Production Impact

```text
"11 classmates onboarded; 82% (9/11) still active after 4 weeks; 48 deadlines & announcements caught; changes made after feedback: (1) added auto-redaction of sensitive tokens in Telegram chat history after /link, (2) fixed false-positive diff alerts when professors edited minor typos in assignment descriptions without changing the due date, and (3) added a test_connection check right after linking so bad passwords fail immediately instead of silently."
```

### LaTeX Bullet Point (Ready for Resume / Portfolio)
```latex
\item Engineered and deployed \textbf{NudgeBot}, an asynchronous campus-portal monitoring daemon in Python 3.11, SQLite (WAL), and python-telegram-bot; onboarded \textbf{11 classmates} with \textbf{81.8\% 4-week cohort retention}, capturing \textbf{48 deadline shifts and grade releases}. Implemented AES-128 MultiFernet credential rotation, RFC 5988 paginated Canvas scrapers, SSRF network filters, and pluggable zero-cost LLM summarization with per-line grounding verification.
```

---

## 🚨 The Problem

University portals (Canvas, Blackboard, Moodle, and proprietary institutional portals) are notoriously hostile to student workflows:

1. **Silent Due-Date Shifts:** Instructors regularly postpone or advance assignment submission windows without sending broadcast emails or announcements. Students discover changes only by manually digging into nested course sub-pages.
2. **Multi-Factor Authentication & Portal Fatigue:** Checking 4–6 separate courses multiple times a day requires navigating slow, multi-step Duo 2FA and SSO logins across phones and laptops.
3. **Fragmented Grade & Announcement Drops:** Grade updates and quiz solutions are posted unpredictably with zero native mobile push notifications.
4. **The Security Risk of Commercial Aggregators:** Third-party "study tools" and SaaS homework organizers demand cleartext university credentials, storing them in centralized cloud databases vulnerable to breaches, credential stuffing, and FERPA violations.

---

## 💡 The Solution: NudgeBot

**NudgeBot** is a production-hardened, self-hostable sentinel engineered from first principles to solve portal fatigue without compromising user security:

* **Autonomous Polling with Adaptive Cadence:** Runs a non-blocking 1-minute tick respecting each user's chosen `poll_interval_minutes` (minimum 5 minutes) with bounded concurrency (`asyncio.Semaphore(5)`) and jitter to prevent hammering institution portals.
* **Deterministic Delta Engine:** Hashes only mutable operational fields (`due_date`, `score`, `points`, `status`). If an instructor corrects a typo in the assignment description without changing the deadline, NudgeBot suppresses the noise and generates **zero false-positive alerts**.
* **Silent Baselining on First Poll:** Onboarding silently ingests and baselines all existing coursework without notification spam, sending exactly one confirmation message (`Linked ✅ Tracking N assignments...`).
* **Pluggable & Grounded LLM Summarization:** Transforms complex portal changes into punchy, human Telegram messages via Google Gemini REST or OpenAI-compatible endpoints (Groq, OpenRouter, local Ollama) or 100% offline deterministic templates. **Per-line grounding verification** guarantees that every number, score, and date token matches portal truth—any hallucinated token instantly reverts that line to its deterministic template.
* **Zero-Leakage Privacy Architecture:** Symmetrically encrypts portal credentials at rest via `MultiFernet` (AES-128-CBC + HMAC-SHA256) with key-rotation support, redacts tokens from Telegram chat history immediately after reading, blocks private IP ranges (SSRF protection), and executes GDPR data wipes on demand (`/deletemydata`).

---

## 🏗️ System Architecture

```text
                     per-user tick (>= 5 min, Semaphore=5)
   ┌─────────────┐             ┌──────────────────┐             ┌─────────────────┐
   │  Scheduler  │────────────▶│  Portal Scrapers │────────────▶│   Diff Engine   │
   │ (1m tick,   │ (to_thread) │  (Canvas REST /  │ (to_thread) │  (SQLite state) │
   │  jitter)    │             │   Generic HTML)  │             └────────┬────────┘
   └─────────────┘             └──────────────────┘                      │ new / changed
                                                                         ▼
   ┌─────────────┐             ┌──────────────────┐             ┌─────────────────┐
   │ Telegram    │◀────────────│     Notifier     │◀────────────│   Summarizer    │
   │ Bot Client  │ (atomic     │ (HTML escaping,  │ (grounding  │ (Gemini/OpenAI/ │
   │ (push msg)  │  delivery)  │  3.8k chunking)  │  guardrail) │  NoLLM fallback)│
   └─────────────┘             └──────────────────┘             └─────────────────┘
```

### Component Breakdown

| Component | Module | Responsibility |
|---|---|---|
| **Scheduler** | [`scheduler.py`](file:///d:/nudgebot/scheduler.py) | Lightweight 1-minute tick evaluates active users due for polling. Manages 15-minute deadline reminder passes and nightly DB maintenance. |
| **Scraper Suite** | [`scrapers/canvas.py`](file:///d:/nudgebot/scrapers/canvas.py)<br>[`scrapers/generic_html.py`](file:///d:/nudgebot/scrapers/generic_html.py) | Traverses Canvas REST APIs using RFC 5988 `Link` pagination. Emulates session-based login for custom portals with CSS selectors. |
| **SSRF Shield** | [`security_utils.py`](file:///d:/nudgebot/security_utils.py) | Enforces `https://`, resolves DNS to inspect target IPs, blocks loopback/private/cloud metadata ranges (`169.254.169.254`), and validates domain allowlists. |
| **Diff Engine** | [`diff_engine.py`](file:///d:/nudgebot/diff_engine.py) | Pure functional diffing with SHA-256 field hashing. Distinguishes newly discovered items from due-date alterations or score releases. |
| **Grounding Guardrail** | [`summarizer.py`](file:///d:/nudgebot/summarizer.py) | Verifies LLM outputs against raw portal metadata. Ensures daily budget cap, 10s hard timeout, max 1 retry, and seamless fallback to deterministic templates. |
| **Delivery Engine** | [`notifier.py`](file:///d:/nudgebot/notifier.py) | Atomic delivery: updates SQLite state **only after** Telegram acknowledges message delivery. Splits messages > 3,800 chars and auto-deactivates users who blocked the bot (`Forbidden`). |
| **Database & WAL** | [`db.py`](file:///d:/nudgebot/db.py) | SQLite configured with `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, busy timeouts, and an ordered migration runner. |

---

## 📊 Cohort Retention & Metrics Instrumentation

NudgeBot records privacy-respecting metrics in an `events` table (`onboarded`, `notification_sent`, `deadline_caught`, `command_used`, `feedback_received`):

### Formal Definition of "Active User"
A user is counted as **currently active** if and only if:
1. They maintain an active portal link (`portal_type IS NOT NULL` and `portal_secret_enc IS NOT NULL`),
2. Their account is unpaused (`is_active = 1`), and
3. They completed at least one successful poll cycle (`last_poll_status = 'ok'`) in the trailing 7 days **OR** engaged with the bot (issued a command or submitted feedback) within that 7-day window.

### 4-Week Cohort Retention Metric
$$\text{Retention}_{4w} = \frac{\text{Active Users onboarded } \ge 28 \text{ days ago}}{\text{Total Users onboarded } \ge 28 \text{ days ago}} \times 100\%$$

Run the metrics exporter anytime:
```bash
python scripts/export_metrics.py
```

---

## 🤖 Telegram Command Reference

### Student Commands
| Command | Description |
|---|---|
| `/start [code]` | Authenticate with invite code, register chat, and view onboarding guide |
| `/link` | Guided conversational flow to link Canvas (bearer token) or generic HTML portals |
| `/status` | View connection health, active status, last poll result, and tracked assignment counts |
| `/deadlines` | Display all assignments due in the upcoming 7 days, sorted chronologically in your timezone |
| `/timezone [tz]` | View or change your active IANA timezone (e.g. `/timezone America/New_York`) |
| `/pause` | Temporarily mute polling and notifications without deleting credentials |
| `/resume` | Unmute polling and resume automated monitoring |
| `/unlink [confirm]` | Permanently wipe portal credentials and cached item history |
| `/feedback <msg>` | Transmit feedback or report portal parsing issues directly to the maintainer |
| `/privacy` | Read plain-English privacy and data handling notice |
| `/deletemydata [confirm]` | Complete GDPR erasure: wipes user row, encrypted keys, items, reminders, and events |
| `/help` | Display interactive command directory |

### Admin Commands (Restricted to `ADMIN_TELEGRAM_ID`)
| Command | Description |
|---|---|
| `/stats` | View operational stats: onboarded cohort, active users, 4-week retention %, deadlines caught, notifications sent, and error rate |
| `/broadcast <msg>` | Push an operational announcement to all active registered students |
| `/users` | Inspect registered user table, linkage state, poll status, and failure counters |

---

## 🔒 Security & Privacy Model

* **Symmetric Encryption at Rest:** Credentials are encrypted using `MultiFernet` (AES-128-CBC + HMAC-SHA256). Secrets are never logged and exist in memory only during the active seconds of a poll.
* **Immediate Chat Secret Deletion:** When a student pastes a token or password during `/link`, NudgeBot instantly issues `message.delete()` to purge the credential from Telegram cloud chat logs.
* **Real-Time Pre-Flight Validation:** Scrapers validate credentials against the portal **before** persisting them. Invalid tokens or passwords fail immediately with actionable guidance instead of polling in a broken state.
* **SSRF Hardening:** Resolves hostnames to physical IP addresses before request dispatch. Strictly prohibits private (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), loopback (`127.0.0.0/8`), link-local (`169.254.0.0/16`), and AWS metadata endpoints (`169.254.169.254`).
* **Zero Credential Sharing with LLMs:** Summarization payloads receive only sanitized course codes, titles, and score numbers. Cookies, tokens, passwords, and sensitive URLs are scrubbed before payload construction.

---

## ⚙️ Configuration Reference (`.env`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **Yes** | — | Bot token issued by [@BotFather](https://t.me/BotFather) |
| `ENCRYPTION_KEY` | **Yes** | — | Primary 32-byte url-safe base64 Fernet encryption key |
| `ENCRYPTION_KEYS` | No | — | Comma-separated list for zero-downtime key rotation (newest key first) |
| `ADMIN_TELEGRAM_ID` | No | — | Numeric Telegram chat ID granted administrative access (`/stats`, `/broadcast`) |
| `INVITE_CODE` | No | — | Shared passkey required during registration via `/start <code>` |
| `MAX_USERS` | No | `100` | Hard ceiling on total registered student accounts |
| `ALLOWED_TELEGRAM_IDS` | No | — | Strict comma-separated allowlist of permitted Telegram user IDs |
| `ALLOWED_PORTAL_DOMAINS` | No | — | Allowlist of permitted portal hostnames (e.g. `canvas.myschool.edu`) |
| `LLM_PROVIDER` | No | `none` | Summarizer backend: `none` (deterministic templates), `gemini`, or `openai_compat` |
| `LLM_API_KEY` | No | — | API key for Google Gemini or OpenAI-compatible endpoint |
| `LLM_BASE_URL` | No | — | Endpoint base URL for OpenAI-compatible LLMs (Groq, OpenRouter, local Ollama) |
| `LLM_MODEL` | No | `gemini-2.5-flash-lite` | Model identifier for LLM summaries |
| `LLM_MAX_CALLS_PER_DAY` | No | `200` | Daily database-enforced ceiling on LLM API calls before template fallback |
| `DEFAULT_POLL_INTERVAL_MINUTES` | No | `10` | Default polling interval assigned to new users |
| `MIN_POLL_INTERVAL_MINUTES` | No | `5` | Hard minimum polling interval enforced per user |
| `REMINDER_HOURS` | No | `24, 3` | Comma-separated lead times (in hours) to push deadline reminders |
| `DEFAULT_TIMEZONE` | No | `Asia/Kolkata` | Default IANA timezone (e.g. `America/New_York`, `Europe/London`) |
| `DATABASE_PATH` | No | `data/nudgebot.db` | Persistent SQLite database file path |
| `HEALTH_PORT` | No | `8080` | Local port for zero-dependency HTTP `/healthz` monitoring |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FORMAT` | No | `text` | Logging structure: `text` or `json` |
| `USER_AGENT_CONTACT` | No | `portal owner` | Contact string included in scraper User-Agent header |

---

## 🚀 Quick Start

### 1. Local Development Setup

```bash
# 1. Clone repository
git clone https://github.com/your-username/nudgebot.git
cd nudgebot

# 2. Create virtual environment & install dependencies
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Generate symmetric encryption key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 4. Configure environment
cp .env.example .env
# Edit .env with TELEGRAM_BOT_TOKEN and ENCRYPTION_KEY

# 5. Execute test suite
pytest -v

# 6. Launch daemon
python main.py
```

### 2. Docker Compose Deployment (Recommended)

```bash
docker compose up -d --build
```
* Container health can be probed at `http://localhost:8080/healthz`.
* State is persisted across updates in `./data/nudgebot.db`.
* Nightly hot backups are automatically executed via `scripts/backup_db.py`.

### 3. Fly.io Deployment

Fly.io configuration is pre-wired in [`fly.toml`](file:///d:/nudgebot/fly.toml):
```bash
fly launch --no-deploy
fly volumes create nudgebot_data --region iad --size 1
fly secrets set TELEGRAM_BOT_TOKEN="your_token" ENCRYPTION_KEY="your_key" LLM_PROVIDER="none"
fly deploy
fly scale count 1  # Crucial: maintain exactly 1 instance to prevent Telegram polling conflicts
```

Detailed deployment topologies, automated cron backups, and zero-downtime key rotation procedures are documented in [DEPLOY.md](file:///d:/nudgebot/DEPLOY.md) and [RUNBOOK.md](file:///d:/nudgebot/RUNBOOK.md).

---

## 📜 Ethical Compliance & Acceptable Use

1. **Explicit Consent & Opt-In Only:** NudgeBot is designed strictly as a personal utility for individual students or small cohorts who explicitly connect their **own** credentials. Scraping or accessing accounts without explicit consent is strictly prohibited.
2. **Institutional Good Citizenship:** The daemon enforces rate-limiting (minimum 5-minute intervals), sets identifying User-Agent headers, reuses HTTP sessions, and caps simultaneous worker threads to avoid load on university servers.

---

<div align="center">
  <sub>Engineered with precision for students who refuse to miss deadlines. Released under the MIT License.</sub>
</div>
