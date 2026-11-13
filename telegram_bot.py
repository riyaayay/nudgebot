"""Telegram command handlers, rate limiting, access control, and guided /link conversation flow."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from typing import Any

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import Settings
from crypto_utils import CredentialCipher
from date_utils import format_in_timezone, hours_until_due
from db import Database
from notifier import send_plain
from scrapers import CanvasScraper, GenericHTMLScraper, PortalAuthError, PortalFetchError
from security_utils import SecurityError, validate_generic_config, validate_portal_url

logger = logging.getLogger(__name__)

# Conversation states for /link
CHOOSING_PORTAL_TYPE, CANVAS_URL, CANVAS_TOKEN, GENERIC_CONFIG, GENERIC_USER, GENERIC_PASS = range(6)
PORTAL_CHOICES = ["Canvas", "Other (generic HTML)", "Cancel"]

# Rate limiting: max commands per sliding window per chat_id
COMMAND_RATE_LIMIT = 15
COMMAND_RATE_WINDOW_SECONDS = 10.0
_user_command_times: dict[str, list[float]] = {}

# Admin alert rate limiting
_last_admin_alert_time: float = 0.0


def _is_rate_limited(chat_id: str) -> bool:
    now = time.time()
    times = _user_command_times.setdefault(chat_id, [])
    # Prune old timestamps
    _user_command_times[chat_id] = [t for t in times if now - t < COMMAND_RATE_WINDOW_SECONDS]
    if len(_user_command_times[chat_id]) >= COMMAND_RATE_LIMIT:
        return True
    _user_command_times[chat_id].append(now)
    return False


def _chat_id(update: Update) -> str:
    if update.effective_chat:
        return str(update.effective_chat.id)
    return ""


class BotContext:
    def __init__(self, db: Database, cipher: CredentialCipher, settings: Settings):
        self.db = db
        self.cipher = cipher
        self.settings = settings


def _check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Verifies that the user is permitted under ALLOWED_TELEGRAM_IDS."""
    bc: BotContext = context.bot_data["bc"]
    chat_id = _chat_id(update)
    if bc.settings.allowed_telegram_ids and chat_id not in bc.settings.allowed_telegram_ids:
        return False
    return True


async def _check_max_users(update: Update, bc: BotContext) -> bool:
    """Verifies whether new user registration would exceed MAX_USERS."""
    chat_id = _chat_id(update)
    user = bc.db.get_user_by_chat_id(chat_id)
    if not user:
        current_count = bc.db.get_user_count()
        if current_count >= bc.settings.max_users:
            if update.message:
                await update.message.reply_text(
                    "⚠️ This NudgeBot instance has reached its user capacity limit. "
                    "Please contact the bot administrator."
                )
            return False
    return True


# ---------------------------------------------------------------------------
# Basic Commands
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = _chat_id(update)
    if _is_rate_limited(chat_id):
        await update.message.reply_text("⏳ Please slow down.")
        return

    bc: BotContext = context.bot_data["bc"]
    if not _check_access(update, context):
        await update.message.reply_text("⛔ Access to this NudgeBot instance is restricted.")
        return
    if bc.settings.invite_code:
        args = context.args or []
        user = bc.db.get_user_by_chat_id(chat_id)
        if not user:
            provided_code = args[0].strip().strip("<>").strip() if args else ""
            if not args or provided_code != bc.settings.invite_code:
                await update.message.reply_text(
                    "🔐 <b>An invite code is required to join this NudgeBot instance.</b>\n\n"
                    "Send: <code>/start invite_code</code>\n"
                    "<i>(or simply reply with your invite code)</i>",
                    parse_mode=ParseMode.HTML,
                )
                return

    if not await _check_max_users(update, bc):
        return

    username = update.effective_user.username if update.effective_user else None
    user = bc.db.get_or_create_user(
        chat_id,
        username,
        default_timezone=bc.settings.default_timezone,
        default_poll_interval=bc.settings.default_poll_interval_minutes,
    )
    bc.db.record_event(user.id, "command_used", {"command": "/start"})

    linked_msg = (
        " You're already linked to a portal — run /status for details."
        if user.is_linked
        else " Run /link to connect your campus portal and start receiving nudges."
    )

    welcome_text = (
        "👋 <b>Welcome to NudgeBot!</b>\n\n"
        "I monitor your campus portal (Canvas or custom HTML) and alert you on Telegram "
        "the second a new assignment, due date change, or grade drops.\n\n"
        "🔒 <b>Privacy &amp; Security:</b>\n"
        "Your credentials are encrypted at rest with AES/Fernet and only used on your behalf. "
        "By using NudgeBot, you confirm that you have checked your school's guidelines and "
        "consent to automated deadline checking. See /privacy for details.\n\n" + linked_msg
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = _chat_id(update)
    if _is_rate_limited(chat_id):
        return

    help_text = (
        "📖 <b>NudgeBot Commands:</b>\n\n"
        "• /link — Connect your campus portal (Canvas or generic HTML)\n"
        "• /status — Check connection status, last poll, and active settings\n"
        "• /deadlines — View upcoming assignments due in the next 7 days\n"
        "• /timezone [tz] — View or set your timezone (e.g. <code>/timezone Asia/Kolkata</code>)\n"
        "• /pause — Temporarily mute notifications without unlinking\n"
        "• /resume — Resume notifications\n"
        "• /unlink — Remove your stored credentials and cached data\n"
        "• /feedback &lt;text&gt; — Send feedback or issue report to admin\n"
        "• /privacy — View data privacy notice\n"
        "• /deletemydata — Permanently erase all your data (GDPR request)\n"
        "• /help — Show this help message"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = (
        "🛡️ <b>NudgeBot Privacy Notice:</b>\n\n"
        "<b>What is stored:</b>\n"
        "• Your Telegram chat ID and username.\n"
        "• Your portal URL and credentials (encrypted at rest with Fernet/AES128).\n"
        "• Cached titles, course names, and due dates of assignments/grades for diffing.\n\n"
        "<b>What is NEVER done:</b>\n"
        "• We never share, sell, or log your credentials in plaintext.\n"
        "• We never send credentials or tokens to LLMs (only item titles and courses).\n"
        "• We never access portals on your behalf without your explicit opt-in.\n\n"
        "<b>Your rights:</b>\n"
        "You can delete your credentials anytime with /unlink or completely wipe "
        "every trace of your data with /deletemydata."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = _chat_id(update)
    if _is_rate_limited(chat_id):
        return

    bc: BotContext = context.bot_data["bc"]
    user = bc.db.get_user_by_chat_id(chat_id)
    if not user or not user.is_linked:
        await update.message.reply_text("You are not linked to a portal yet. Run /link to get started.")
        return

    state = "Active ✅" if user.is_active else "Paused ⏸️"
    last_poll = format_in_timezone(user.last_poll_at, user.timezone) if user.last_poll_at else "Never yet"
    last_status = user.last_poll_status or "None"
    assignments = bc.db.get_stored_assignments(user.id)
    failures_note = f"\nConsecutive failures: {user.consecutive_failures}" if user.consecutive_failures > 0 else ""

    status_text = (
        f"📊 <b>Portal Status:</b>\n\n"
        f"• <b>Portal Type:</b> <code>{html.escape(user.portal_type or '')}</code>\n"
        f"• <b>State:</b> {state}\n"
        f"• <b>Timezone:</b> <code>{html.escape(user.timezone)}</code>\n"
        f"• <b>Cadence:</b> Every {user.poll_interval_minutes} minutes\n"
        f"• <b>Last Poll:</b> {last_poll} ({html.escape(last_status)})\n"
        f"• <b>Tracked Assignments:</b> {len(assignments)}"
        f"{failures_note}"
    )
    await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)


async def cmd_deadlines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = _chat_id(update)
    if _is_rate_limited(chat_id):
        return

    bc: BotContext = context.bot_data["bc"]
    user = bc.db.get_user_by_chat_id(chat_id)
    if not user or not user.is_linked:
        await update.message.reply_text("You are not linked to a portal yet. Run /link to get started.")
        return

    assignments = bc.db.get_stored_assignments(user.id)
    upcoming: list[tuple[float, dict[str, Any]]] = []

    for a in assignments:
        due = a.get("due_date")
        if not due:
            continue
        hours = hours_until_due(due)
        # Next 7 days = 0 <= hours <= 168
        if hours is not None and 0 <= hours <= 168:
            upcoming.append((hours, a))

    upcoming.sort(key=lambda x: x[0])

    if not upcoming:
        await update.message.reply_text("🎉 No assignments due in the next 7 days! Enjoy your free time.")
        return

    lines = ["📅 <b>Upcoming Deadlines (Next 7 Days):</b>\n"]
    for hours, a in upcoming:
        due_fmt = format_in_timezone(a["due_date"], user.timezone)
        h_int = int(hours)
        time_left = f"in {h_int}h" if h_int < 48 else f"in {h_int // 24}d {h_int % 24}h"
        course = html.escape(a.get("course", "Course"))
        title = html.escape(a.get("title", "Assignment"))
        points = f" ({a['points']} pts)" if a.get("points") else ""
        lines.append(f"• <b>{course}</b>: {title}{points}\n  Due: {due_fmt} (<i>{time_left}</i>)")

    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = _chat_id(update)
    if _is_rate_limited(chat_id):
        return

    bc: BotContext = context.bot_data["bc"]
    user = bc.db.get_or_create_user(chat_id, update.effective_user.username if update.effective_user else None)

    args = context.args or []
    if not args:
        await update.message.reply_text(
            f"Your current timezone is <code>{html.escape(user.timezone)}</code>.\n\n"
            "To change it, provide a valid IANA timezone name, e.g.:\n"
            "<code>/timezone America/New_York</code>\n"
            "<code>/timezone Europe/London</code>\n"
            "<code>/timezone Asia/Kolkata</code>\n"
            "<code>/timezone UTC</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    new_tz = args[0].strip()
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(new_tz)
    except (ZoneInfoNotFoundError, ValueError):
        await update.message.reply_text(
            f"❌ '{html.escape(new_tz)}' is not a valid IANA timezone.\n"
            "Please use standard names like <code>America/New_York</code>, <code>Europe/London</code>, or <code>Asia/Kolkata</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    bc.db.update_user_timezone(user.id, new_tz)
    await update.message.reply_text(
        f"✅ Timezone updated to <code>{html.escape(new_tz)}</code>. Due dates will now show in this timezone.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = _chat_id(update)
    if _is_rate_limited(chat_id):
        return

    bc: BotContext = context.bot_data["bc"]
    user = bc.db.get_or_create_user(chat_id, update.effective_user.username if update.effective_user else None)
    bc.db.set_active(user.id, False)
    await update.message.reply_text("⏸️ Notifications paused. I won't nudge you until you /resume.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = _chat_id(update)
    if _is_rate_limited(chat_id):
        return

    bc: BotContext = context.bot_data["bc"]
    user = bc.db.get_or_create_user(chat_id, update.effective_user.username if update.effective_user else None)
    bc.db.set_active(user.id, True)
    bc.db.reset_consecutive_failures(user.id)
    await update.message.reply_text("▶️ Notifications resumed! Back to monitoring your portal. 🫡")


async def cmd_unlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = _chat_id(update)
    if _is_rate_limited(chat_id):
        return

    bc: BotContext = context.bot_data["bc"]
    user = bc.db.get_user_by_chat_id(chat_id)
    if not user or not user.is_linked:
        await update.message.reply_text("You don't have a linked portal.")
        return

    args = context.args or []
    if not args or args[0].lower() != "confirm":
        await update.message.reply_text(
            "⚠️ <b>Confirm Unlinking:</b>\n"
            "This will delete your stored portal credentials and cached item history.\n"
            "To confirm, run:\n<code>/unlink confirm</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    bc.db.unlink_portal(user.id)
    bc.db.record_event(user.id, "unlinked")
    await update.message.reply_text(
        "✅ Successfully unlinked and wiped your stored credentials. Run /link whenever you want to reconnect."
    )


async def cmd_deletemydata(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = _chat_id(update)
    if _is_rate_limited(chat_id):
        return

    bc: BotContext = context.bot_data["bc"]
    user = bc.db.get_user_by_chat_id(chat_id)
    if not user:
        await update.message.reply_text("No data found for your account.")
        return

    args = context.args or []
    if not args or args[0].lower() != "confirm":
        await update.message.reply_text(
            "⚠️ <b>GDPR Permanent Data Erasure:</b>\n\n"
            "This will permanently wipe EVERYTHING about you from NudgeBot:\n"
            "• Account record\n"
            "• Encrypted credentials\n"
            "• All cached assignments, grades, and diffs\n"
            "• Poll logs and reminders\n\n"
            "To permanently delete everything, run:\n"
            "<code>/deletemydata confirm</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    bc.db.delete_user_data(user.id)
    await update.message.reply_text("👋 All your data has been permanently deleted from NudgeBot. Goodbye!")


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = _chat_id(update)
    if _is_rate_limited(chat_id):
        return

    bc: BotContext = context.bot_data["bc"]
    user = bc.db.get_or_create_user(chat_id, update.effective_user.username if update.effective_user else None)

    msg = " ".join(context.args or []).strip()
    if not msg:
        await update.message.reply_text(
            "Please include your message:\n<code>/feedback Your feedback here</code>", parse_mode=ParseMode.HTML
        )
        return

    bc.db.record_feedback(user.id, msg)
    bc.db.record_event(user.id, "feedback_received")

    # Forward to admin if configured
    if bc.settings.admin_telegram_id:
        username_str = (
            f"@{update.effective_user.username}"
            if update.effective_user and update.effective_user.username
            else f"User {chat_id}"
        )
        admin_text = (
            f"📬 <b>New User Feedback</b>\n"
            f"From: {html.escape(username_str)} (ID: <code>{chat_id}</code>)\n\n"
            f"{html.escape(msg)}"
        )
        try:
            await send_plain(context.bot, bc.settings.admin_telegram_id, admin_text)
        except Exception:
            logger.exception("Failed to forward feedback to admin.")

    await update.message.reply_text("🙏 Thank you! Your feedback has been recorded and shared with the bot maintainer.")


# ---------------------------------------------------------------------------
# Admin Commands
# ---------------------------------------------------------------------------


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    bc: BotContext = context.bot_data["bc"]
    chat_id = _chat_id(update)
    return bool(bc.settings.admin_telegram_id and chat_id == str(bc.settings.admin_telegram_id))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _is_admin(update, context):
        return

    bc: BotContext = context.bot_data["bc"]
    stats = bc.db.get_stats()
    text = (
        "📈 <b>NudgeBot Admin Statistics:</b>\n\n"
        f"• <b>Total Onboarded:</b> {stats['total_onboarded']}\n"
        f"• <b>Currently Active:</b> {stats['currently_active']}\n"
        f"• <b>4-Week Cohort Retention:</b> {stats['retention_4w_pct']}% (cohort size: {stats['retention_4w_cohort_size']})\n"
        f"• <b>Deadlines Caught:</b> {stats['deadlines_caught']}\n"
        f"• <b>Notifications Sent:</b> {stats['notifications_sent']}\n"
        f"• <b>7-Day Error Rate:</b> {stats['error_rate_pct']}%\n"
        f"• <b>Feedback Count:</b> {stats['feedback_count']}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _is_admin(update, context):
        return

    msg = " ".join(context.args or []).strip()
    if not msg:
        await update.message.reply_text("Usage: <code>/broadcast &lt;message&gt;</code>", parse_mode=ParseMode.HTML)
        return

    bc: BotContext = context.bot_data["bc"]
    users = bc.db.list_active_linked_users()
    broadcast_text = f"📢 <b>Announcement from NudgeBot:</b>\n\n{html.escape(msg)}"

    sent_count = 0
    for u in users:
        ok = await send_plain(context.bot, u.telegram_chat_id, broadcast_text)
        if ok:
            sent_count += 1
        await asyncio.sleep(0.05)  # Telegram broadcast throttle

    await update.message.reply_text(f"✅ Broadcast delivered to {sent_count}/{len(users)} active users.")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _is_admin(update, context):
        return

    bc: BotContext = context.bot_data["bc"]
    all_users = bc.db.list_all_users()
    lines = [f"👥 <b>Registered Users ({len(all_users)}):</b>\n"]
    for u in all_users[:50]:  # Cap at 50 to prevent message overflow
        username = f"@{u.telegram_username}" if u.telegram_username else f"ID:{u.telegram_chat_id}"
        portal = u.portal_type or "unlinked"
        status = "active" if u.is_active else "paused"
        lines.append(f"• {username} | {portal} | {status} | last: {u.last_poll_status or 'none'}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Guided /link Conversation Flow
# ---------------------------------------------------------------------------


async def link_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END
    chat_id = _chat_id(update)
    if _is_rate_limited(chat_id):
        return ConversationHandler.END

    if not _check_access(update, context):
        await update.message.reply_text("⛔ Access to this NudgeBot instance is restricted.")
        return
        await update.message.reply_text("⛔ Access to this instance is restricted.")
        return ConversationHandler.END

    keyboard = ReplyKeyboardMarkup([[c] for c in PORTAL_CHOICES], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Which campus portal does your institution use?\n\n"
        "• <b>Canvas</b> — Official Canvas API using a personal access token (recommended, no password required).\n"
        "• <b>Other (generic HTML)</b> — Scrapes your portal via custom CSS selector JSON.\n"
        "• <b>Cancel</b> — Exit this setup.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return CHOOSING_PORTAL_TYPE


async def link_choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return CHOOSING_PORTAL_TYPE
    choice = update.message.text.strip().lower()
    if choice.startswith("cancel"):
        await update.message.reply_text("Setup cancelled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    if choice.startswith("canvas"):
        await update.message.reply_text(
            "What is your institution's Canvas URL?\n"
            "e.g. <code>https://canvas.instructure.com</code> or <code>https://canvas.myschool.edu</code>\n\n"
            "(Type /cancel at any time to abort.)",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
        )
        return CANVAS_URL

    await update.message.reply_text(
        "Please paste your JSON selector configuration for your portal (see README for format).\n\n"
        "(Type /cancel at any time to abort.)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return GENERIC_CONFIG


async def link_canvas_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return CANVAS_URL
    bc: BotContext = context.bot_data["bc"]
    raw_url = update.message.text.strip()

    try:
        validated_url = validate_portal_url(raw_url, allowed_domains=bc.settings.allowed_portal_domains)
    except SecurityError as exc:
        await update.message.reply_text(
            f"❌ Invalid or restricted URL: {exc}\nPlease provide a valid https:// Canvas URL, or /cancel."
        )
        return CANVAS_URL

    context.user_data["portal_base_url"] = validated_url
    await update.message.reply_text(
        "Now paste your Canvas <b>personal access token</b>.\n\n"
        "To generate one:\n"
        "Canvas → Account → Settings → '+ New Access Token'.\n\n"
        "🔒 <i>Your token will be validated immediately, encrypted at rest with AES-128, "
        "and the message containing your token will be deleted immediately for security.</i>",
        parse_mode=ParseMode.HTML,
    )
    return CANVAS_TOKEN


async def link_canvas_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return CANVAS_TOKEN
    bc: BotContext = context.bot_data["bc"]
    token = update.message.text.strip()

    # Immediately delete the secret message
    try:
        await update.message.delete()
    except Exception:
        await update.message.reply_text(
            "⚠️ Could not automatically delete your token message due to Telegram permissions. "
            "Please manually delete it now for safety!"
        )

    base_url = context.user_data.get("portal_base_url")
    chat_id = _chat_id(update)
    status_msg = await update.message.reply_text("⏳ Verifying your Canvas credentials...")

    # Validate credentials before saving!
    try:
        scraper = CanvasScraper(
            base_url=base_url,
            username=None,
            secret=token,
            user_agent_contact=bc.settings.user_agent_contact,
            allowed_domains=bc.settings.allowed_portal_domains,
        )
        await asyncio.to_thread(scraper.login)
    except (PortalAuthError, SecurityError, ValueError) as exc:
        await status_msg.edit_text(f"❌ Connection failed: {exc}\nPlease check your token and URL, or /cancel.")
        return CANVAS_TOKEN
    except PortalFetchError as exc:
        await status_msg.edit_text(f"⚠️ Could not reach Canvas server: {exc}\nPlease try again in a moment, or /cancel.")
        return CANVAS_TOKEN
    except Exception as exc:
        await status_msg.edit_text(f"❌ Unexpected error validating credentials: {exc}\nPlease try again or /cancel.")
        return CANVAS_TOKEN

    # Encrypt and save
    user = bc.db.get_or_create_user(
        chat_id,
        update.effective_user.username if update.effective_user else None,
        default_timezone=bc.settings.default_timezone,
        default_poll_interval=bc.settings.default_poll_interval_minutes,
    )
    bc.db.link_portal(
        user_id=user.id,
        portal_type="canvas",
        base_url=base_url,
        username_enc=None,
        secret_enc=bc.cipher.encrypt(token),
        config_json=None,
    )
    context.user_data.clear()

    await status_msg.edit_text(
        "✅ <b>Canvas Linked &amp; Verified!</b>\n\n"
        "NudgeBot is performing an initial baseline scan. I'll track all your assignments and grades, "
        "and alert you only when something changes or a deadline approaches.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def link_generic_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return GENERIC_CONFIG
    bc: BotContext = context.bot_data["bc"]
    raw = update.message.text.strip()

    try:
        cfg = validate_generic_config(raw, allowed_domains=bc.settings.allowed_portal_domains)
    except SecurityError as exc:
        await update.message.reply_text(
            f"❌ Invalid selector config: {exc}\nPlease check the README format and try again, or /cancel."
        )
        return GENERIC_CONFIG

    context.user_data["portal_config"] = cfg
    await update.message.reply_text("Config accepted! What is your portal username?")
    return GENERIC_USER


async def link_generic_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return GENERIC_USER
    context.user_data["portal_username"] = update.message.text.strip()
    await update.message.reply_text(
        "And what is your portal password?\n\n"
        "🔒 <i>Your password message will be deleted immediately and encrypted at rest with AES-128.</i>",
        parse_mode=ParseMode.HTML,
    )
    return GENERIC_PASS


async def link_generic_pass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return GENERIC_PASS
    bc: BotContext = context.bot_data["bc"]
    password = update.message.text.strip()

    # Immediately delete secret message
    try:
        await update.message.delete()
    except Exception:
        await update.message.reply_text(
            "⚠️ Could not auto-delete message. Please delete your password message manually!"
        )

    chat_id = _chat_id(update)
    status_msg = await update.message.reply_text("⏳ Verifying your portal login credentials...")

    cfg = context.user_data["portal_config"]
    username = context.user_data["portal_username"]

    # Pre-validate login before saving!
    try:
        scraper = GenericHTMLScraper(
            base_url=None,
            username=username,
            secret=password,
            config=cfg,
            user_agent_contact=bc.settings.user_agent_contact,
            allowed_domains=bc.settings.allowed_portal_domains,
        )
        await asyncio.to_thread(scraper.login)
    except (PortalAuthError, SecurityError, ValueError) as exc:
        await status_msg.edit_text(f"❌ Login failed: {exc}\nPlease check your credentials and try again, or /cancel.")
        return GENERIC_PASS
    except PortalFetchError as exc:
        await status_msg.edit_text(f"⚠️ Could not reach portal: {exc}\nPlease try again later, or /cancel.")
        return GENERIC_PASS
    except Exception as exc:
        await status_msg.edit_text(f"❌ Error validating portal login: {exc}\nTry again or /cancel.")
        return GENERIC_PASS

    # Encrypt and save
    user = bc.db.get_or_create_user(
        chat_id,
        update.effective_user.username if update.effective_user else None,
        default_timezone=bc.settings.default_timezone,
        default_poll_interval=bc.settings.default_poll_interval_minutes,
    )
    base_url = cfg.get("assignments_url") or cfg.get("grades_url")
    bc.db.link_portal(
        user_id=user.id,
        portal_type="generic_html",
        base_url=base_url,
        username_enc=bc.cipher.encrypt(username),
        secret_enc=bc.cipher.encrypt(password),
        config_json=json.dumps(cfg),
    )
    context.user_data.clear()

    await status_msg.edit_text(
        "✅ <b>Portal Linked &amp; Verified!</b>\n\n"
        "NudgeBot will baseline your portal and nudge you whenever assignments or grades change.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def link_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("Setup cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Global Error Handler
# ---------------------------------------------------------------------------


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs unexpected errors and sends a rate-limited alert to ADMIN_TELEGRAM_ID."""
    global _last_admin_alert_time
    logger.exception("Exception while handling Telegram update: %s", context.error)

    bc: BotContext | None = context.bot_data.get("bc")
    if not bc or not bc.settings.admin_telegram_id:
        return

    now = time.time()
    # Rate limit admin error alerts to once per 60 seconds
    if now - _last_admin_alert_time > 60.0:
        _last_admin_alert_time = now
        err_msg = f"🚨 <b>NudgeBot Error Alert</b>\n\n<code>{html.escape(str(context.error)[:300])}</code>"
        try:
            await send_plain(context.bot, bc.settings.admin_telegram_id, err_msg)
        except Exception:
            pass


async def handle_fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles text messages outside active conversation handlers.
    Catches commands sent with monospace/code formatting, plain invite code replies, etc.
    """
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    chat_id = _chat_id(update)
    if _is_rate_limited(chat_id):
        return

    bc: BotContext | None = context.bot_data.get("bc")
    if not bc:
        return

    # If message starts with /start (even if formatted as code)
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        context.args = parts[1].split() if len(parts) > 1 else []
        await cmd_start(update, context)
        return

    # If user is unlinked/unregistered and sends the invite code directly
    if bc.settings.invite_code:
        clean = text.strip("<>").strip()
        if clean == bc.settings.invite_code:
            context.args = [clean]
            await cmd_start(update, context)
            return

    # Direct command text fallback (e.g. `<code>/help</code>`)
    if text.startswith("/"):
        cmd = text.split()[0].lower()
        if cmd == "/help":
            await cmd_help(update, context)
        elif cmd == "/privacy":
            await cmd_privacy(update, context)
        elif cmd == "/status":
            await cmd_status(update, context)
        elif cmd == "/deadlines":
            await cmd_deadlines(update, context)


def build_application(token: str, bc: BotContext) -> Application:
    app = Application.builder().token(token).build()
    app.bot_data["bc"] = bc

    # Slash commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("deadlines", cmd_deadlines))
    app.add_handler(CommandHandler("timezone", cmd_timezone))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("unlink", cmd_unlink))
    app.add_handler(CommandHandler("deletemydata", cmd_deletemydata))
    app.add_handler(CommandHandler("feedback", cmd_feedback))

    # Admin commands
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("users", cmd_users))

    # Guided /link conversation
    link_conv = ConversationHandler(
        entry_points=[CommandHandler("link", link_start)],
        states={
            CHOOSING_PORTAL_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, link_choose_type),
                CommandHandler("cancel", link_cancel),
            ],
            CANVAS_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, link_canvas_url),
                CommandHandler("cancel", link_cancel),
            ],
            CANVAS_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, link_canvas_token),
                CommandHandler("cancel", link_cancel),
            ],
            GENERIC_CONFIG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, link_generic_config),
                CommandHandler("cancel", link_cancel),
            ],
            GENERIC_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, link_generic_user),
                CommandHandler("cancel", link_cancel),
            ],
            GENERIC_PASS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, link_generic_pass),
                CommandHandler("cancel", link_cancel),
            ],
        },
        fallbacks=[CommandHandler("cancel", link_cancel)],
    )
    app.add_handler(link_conv)

    # General text fallback (handles code-formatted commands and plain invite codes)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_fallback_text))

    app.add_error_handler(global_error_handler)
    return app
