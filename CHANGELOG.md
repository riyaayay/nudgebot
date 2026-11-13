# Changelog

All notable changes to NudgeBot, especially those driven by user feedback from classmates, are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [1.1.0] - 2026-11-13
### Added (Changes Made After Feedback)
- **`/deadlines` view:** Added upcoming 7-day sorted deadline view in response to user feedback requesting a single command to see everything due this week.
- **Configurable reminder windows:** Added secondary 3-hour deadline reminder window in addition to the 24-hour reminder, so students get an alert right before submission deadlines.
- **Per-user timezone support:** Added `/timezone` command (defaulting to `Asia/Kolkata`) allowing classmates in different locations or daylight-saving zones to see due dates in their local time.
- **Silent first-poll baselining:** Eliminated the "first-poll spam" where linking an account sent dozens of notifications for historical assignments; first poll now silently caches items and confirms with a friendly tracking summary.
- **SSRF and credential security:** Added strict URL validation and immediate secret-message deletion in Telegram chats during `/link`.
- **GDPR `/deletemydata`:** Added complete data erasure command on user request.
- **Admin instrumentation:** Added `/stats`, `/broadcast`, and `scripts/export_metrics.py` for cohort retention and resume metrics.

## [1.0.0] - 2026-10-01
### Initial Release
- Multi-user Telegram bot with APScheduler polling.
- Canvas LMS REST API scraper and Generic HTML selector scraper.
- Content-hash diff engine for assignment and grade changes.
- Claude AI summarization with deterministic template fallback.
- Fernet encryption for credentials at rest.

