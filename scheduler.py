"""Scheduler: drives the polling pipeline with a lightweight 1-minute tick,
respects per-user poll intervals, handles first-poll baselining (no spam),
runs scrapers via asyncio.to_thread, limits concurrency with a Semaphore,
executes deadline reminders, and performs daily data retention pruning.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot

from config import Settings
from crypto_utils import CredentialCipher
from date_utils import format_in_timezone, hours_until_due
from db import Database, User
from diff_engine import compute_diff, persist_current_state
from notifier import send_plain, send_update
from scrapers import SCRAPER_REGISTRY, PortalAuthError, PortalFetchError
from summarizer import summarize_diffs_async

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Concurrency & timeout settings
MAX_CONCURRENT_POLLS = 5
PER_USER_POLL_TIMEOUT_SECONDS = 60.0

# Timestamp of last successful scheduler tick for health checking
last_scheduler_tick: datetime | None = None


def get_last_scheduler_tick() -> datetime | None:
    return last_scheduler_tick


def _instantiate_scraper(user: User, cipher: CredentialCipher, settings: Settings) -> Any:
    scraper_cls = SCRAPER_REGISTRY.get(user.portal_type or "")
    if scraper_cls is None:
        raise ValueError(f"Unknown portal_type: {user.portal_type}")

    username = cipher.decrypt(user.portal_username_enc) if user.portal_username_enc else None
    secret = cipher.decrypt(user.portal_secret_enc) if user.portal_secret_enc else ""
    config = json.loads(user.portal_config_json) if user.portal_config_json else None

    # Handle generic HTML scraper extra parameters if applicable
    kwargs: dict[str, Any] = {
        "base_url": user.portal_base_url,
        "username": username,
        "secret": secret,
        "config": config,
        "user_agent_contact": settings.user_agent_contact,
        "allowed_domains": settings.allowed_portal_domains,
    }
    if user.portal_type == "generic_html":
        kwargs["default_tz"] = user.timezone

    return scraper_cls(**kwargs)


async def poll_one_user(
    db: Database,
    cipher: CredentialCipher,
    bot: Bot,
    user: User,
    settings: Settings,
) -> None:
    """Polls a single user's portal:
    1. Instantiates scraper and fetches portal data in a worker thread.
    2. Baselining: On first poll, stores current state without notifications.
    3. Normal cycle: Diffs against database, summarizes, sends Telegram push.
    4. Atomic persistence: Persists state only if delivery succeeded.
    5. Consecutive failure tracking & auto-pause.
    """
    try:
        scraper = _instantiate_scraper(user, cipher, settings)
    except ValueError as exc:
        logger.error("Scraper instantiation failed for user %d: %s", user.id, exc)
        db.record_poll(user.id, "error", f"instantiation: {exc}")
        return

    # Offload blocking I/O (requests + BeautifulSoup) to thread pool
    try:
        assignments, grades, announcements = await asyncio.to_thread(scraper.fetch_all)
    except PortalAuthError as exc:
        logger.warning("Auth failure for user %d: %s", user.id, exc)
        db.record_poll(user.id, "error", f"auth: {exc}")
        db.increment_consecutive_failures(user.id)
        # Notify user of auth failure and pause polling to prevent spamming
        db.set_active(user.id, False)
        await send_plain(
            bot,
            user.telegram_chat_id,
            f"⚠️ Your portal link stopped working ({exc}). I've paused polling. Run /link to reconnect.",
            db=db,
            user_id=user.id,
        )
        return
    except PortalFetchError as exc:
        logger.warning("Fetch error for user %d: %s", user.id, exc)
        db.record_poll(user.id, "error", f"fetch: {exc}")
        failures = db.increment_consecutive_failures(user.id)
        if failures >= 5:
            db.set_active(user.id, False)
            await send_plain(
                bot,
                user.telegram_chat_id,
                "⚠️ Your portal has failed 5 times in a row. I've paused polling to protect your account. Run /resume when it's back up.",
                db=db,
                user_id=user.id,
            )
        return
    except Exception as exc:
        logger.exception("Unexpected error fetching portal for user %d", user.id)
        db.record_poll(user.id, "error", f"unexpected: {exc}")
        failures = db.increment_consecutive_failures(user.id)
        if failures >= 5:
            db.set_active(user.id, False)
        return

    # Check for first-poll baselining
    is_first_poll = user.last_poll_at is None and len(db.get_seen_hashes(user.id)) == 0

    if is_first_poll:
        persist_current_state(db, user.id, assignments, grades, announcements)
        db.record_poll(user.id, "ok", detail="baseline", new_items=0, changed_items=0)
        db.reset_consecutive_failures(user.id)
        db.record_event(
            user.id,
            "onboarded",
            {"assignments": len(assignments), "grades": len(grades), "announcements": len(announcements)},
        )
        welcome_msg = (
            f"Linked ✅ Tracking {len(assignments)} assignments and {len(grades)} grades. "
            "I'll nudge you on anything new."
        )
        await send_plain(bot, user.telegram_chat_id, welcome_msg, db=db, user_id=user.id)
        logger.info("User %d successfully baselined with %d items.", user.id, len(assignments) + len(grades))
        return

    # Regular poll cycle
    diffs = compute_diff(db, user.id, assignments, grades, announcements)
    new_count = sum(1 for d in diffs if d.is_new)
    changed_count = len(diffs) - new_count

    if diffs:
        summary_lines = await summarize_diffs_async(
            diffs=diffs,
            settings=settings,
            db=db,
            user_tz=user.timezone,
        )
        send_ok = await send_update(
            bot=bot,
            chat_id=user.telegram_chat_id,
            diffs=diffs,
            summary_lines=summary_lines,
            db=db,
            user_id=user.id,
        )
        if send_ok:
            # Atomic state persistence: only update seen items after successful send
            persist_current_state(db, user.id, assignments, grades, announcements)
            db.reset_consecutive_failures(user.id)
            db.record_poll(user.id, "ok", new_items=new_count, changed_items=changed_count)
            logger.info(
                "User %d: notified %d updates (%d new, %d changed)", user.id, len(diffs), new_count, changed_count
            )
        else:
            logger.warning(
                "User %d: Telegram send failed; state not persisted so updates will retry next cycle.", user.id
            )
            db.record_poll(
                user.id, "error", detail="telegram send failed", new_items=new_count, changed_items=changed_count
            )
    else:
        # No changes: keep state fresh
        persist_current_state(db, user.id, assignments, grades, announcements)
        db.reset_consecutive_failures(user.id)
        db.record_poll(user.id, "ok", new_items=0, changed_items=0)


async def poll_tick(
    db: Database,
    cipher: CredentialCipher,
    bot: Bot,
    settings: Settings,
    semaphore: asyncio.Semaphore,
) -> None:
    """1-minute tick that polls users whose elapsed time exceeds their poll_interval_minutes."""
    global last_scheduler_tick
    last_scheduler_tick = datetime.now(UTC)

    due_users = db.get_users_due_for_poll(min_interval_minutes=settings.min_poll_interval_minutes)
    if not due_users:
        return

    logger.debug("Scheduler tick: %d user(s) due for polling", len(due_users))

    async def _guarded_poll(u: User) -> None:
        async with semaphore:
            # Add small random jitter (0.2s - 2.0s) so users don't hit portals simultaneously
            await asyncio.sleep(random.uniform(0.2, 2.0))
            try:
                await asyncio.wait_for(
                    poll_one_user(db, cipher, bot, u, settings),
                    timeout=PER_USER_POLL_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning("Poll timed out for user %d after %0.1fs", u.id, PER_USER_POLL_TIMEOUT_SECONDS)
                db.record_poll(u.id, "error", f"timeout ({PER_USER_POLL_TIMEOUT_SECONDS}s)")
            except Exception:
                logger.exception("Unhandled error polling user %d", u.id)

    tasks = [asyncio.create_task(_guarded_poll(u)) for u in due_users]
    await asyncio.gather(*tasks, return_exceptions=True)


async def check_deadline_reminders(db: Database, bot: Bot, settings: Settings) -> None:
    """Checks upcoming assignments and sends a reminder at configured windows
    (e.g. 24h and 3h before due). Tracked in deadline_reminders table so it never repeats.
    """
    users = db.list_active_linked_users()
    for user in users:
        assignments = db.get_stored_assignments(user.id)
        for a in assignments:
            due_iso = a.get("due_date")
            if not due_iso:
                continue

            # Don't remind for already submitted assignments
            if a.get("status") in ("submitted", "graded"):
                continue

            rem_hours = hours_until_due(due_iso)
            if rem_hours is None or rem_hours < 0:
                continue  # already past

            item_key = a["key"]
            for target_h in settings.reminder_hours:
                # Window: target_h - 0.25h to target_h + 0.75h (covers a 15-min check interval)
                if (target_h - 0.25) <= rem_hours <= (target_h + 0.75):
                    if not db.has_deadline_reminder(user.id, item_key, target_h):
                        due_fmt = format_in_timezone(due_iso, user.timezone)
                        reminder_text = (
                            f"⏰ <b>Upcoming Deadline Reminder</b>:\n\n"
                            f"• <b>{a['course']}</b>: {a['title']}\n"
                            f"  Due in ~{target_h}h ({due_fmt})"
                        )
                        send_ok = await send_plain(bot, user.telegram_chat_id, reminder_text, db=db, user_id=user.id)
                        if send_ok:
                            db.record_deadline_reminder(user.id, item_key, target_h)
                            db.record_event(
                                user.id,
                                "reminder_sent",
                                {
                                    "key": item_key,
                                    "course": a["course"],
                                    "title": a["title"],
                                    "hours_before": target_h,
                                },
                            )
                            logger.info("Sent %dh deadline reminder to user %d for %s", target_h, user.id, item_key)


async def daily_maintenance(db: Database) -> None:
    """Daily cleanup: prunes poll_log older than 30 days and events older than 1 year."""
    try:
        pruned_polls, pruned_events = db.prune_old_data(poll_log_days=30, events_days=365)
        logger.info("Daily maintenance completed: pruned %d poll logs, %d events.", pruned_polls, pruned_events)
    except Exception:
        logger.exception("Error during daily database maintenance.")


def build_scheduler(
    db: Database,
    cipher: CredentialCipher,
    bot: Bot,
    settings: Settings,
    anthropic_api_key: str | None = None,  # backwards compatibility
    interval_minutes: int | None = None,  # backwards compatibility
) -> AsyncIOScheduler:
    """Builds and configures the APScheduler AsyncIOScheduler instance."""
    scheduler = AsyncIOScheduler()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_POLLS)

    # 1. Main polling tick every 1 minute
    scheduler.add_job(
        poll_tick,
        "interval",
        seconds=60,
        args=[db, cipher, bot, settings, semaphore],
        id="poll_tick",
        max_instances=1,
        coalesce=True,
    )

    # 2. Deadline reminders check every 15 minutes
    scheduler.add_job(
        check_deadline_reminders,
        "interval",
        minutes=15,
        args=[db, bot, settings],
        id="deadline_reminders",
        max_instances=1,
        coalesce=True,
    )

    # 3. Daily maintenance job at 03:00 UTC
    scheduler.add_job(
        daily_maintenance,
        "cron",
        hour=3,
        minute=0,
        args=[db],
        id="daily_maintenance",
        max_instances=1,
        coalesce=True,
    )

    return scheduler
