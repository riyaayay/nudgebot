"""Formats summarized diffs and alerts into Telegram messages and sends them robustly.
Handles HTML escaping, message splitting under 4,096 characters, error classification
(RetryAfter, Forbidden, NetworkError, BadRequest), and metrics logging.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import TYPE_CHECKING

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError

from models import DiffResult, ItemType

if TYPE_CHECKING:
    from db import Database

logger = logging.getLogger(__name__)

# Telegram has a hard limit of 4096 chars. We keep chunks comfortably below.
MAX_MESSAGE_CHARS = 3800
MAX_ITEMS_PER_MESSAGE = 20


def escape_html(text: str | None) -> str:
    """Escapes user and portal-generated text for safe HTML parse mode."""
    if not text:
        return ""
    return html.escape(str(text), quote=False)


def build_message_chunks(summary_lines: list[str]) -> list[str]:
    """Builds HTML formatted message chunks, splitting them if they exceed 3800 characters."""
    if not summary_lines:
        return []

    header = "🔔 <b>NudgeBot found something new:</b>\n\n"
    chunks: list[str] = []
    current_chunk = header
    items_in_chunk = 0

    for line in summary_lines:
        # Each line has been escaped or formatted safely
        safe_line = f"• {escape_html(line)}\n"
        if len(current_chunk) + len(safe_line) > MAX_MESSAGE_CHARS or items_in_chunk >= MAX_ITEMS_PER_MESSAGE:
            chunks.append(current_chunk.strip())
            current_chunk = "🔔 <b>More updates:</b>\n\n" + safe_line
            items_in_chunk = 1
        else:
            current_chunk += safe_line
            items_in_chunk += 1

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks


async def send_plain(
    bot: Bot,
    chat_id: str,
    text: str,
    db: Database | None = None,
    user_id: int | None = None,
) -> bool:
    """Sends a plain text message, handling RetryAfter and Forbidden."""
    for attempt in range(3):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return True
        except RetryAfter as exc:
            wait_time = float(exc.retry_after)
            logger.warning("Telegram rate limit hit. Waiting %0.1fs for chat_id=%s", wait_time, chat_id)
            await asyncio.sleep(wait_time)
        except Forbidden:
            logger.info("Bot was blocked by user chat_id=%s; deactivating user.", chat_id)
            if db and user_id:
                db.set_active(user_id, False)
            return False
        except (NetworkError, TelegramError) as exc:
            logger.warning("Telegram send_plain failed (attempt %d/3) for chat_id=%s: %s", attempt + 1, chat_id, exc)
            if attempt < 2:
                await asyncio.sleep(1.0)
            else:
                return False
    return False


async def send_update(
    bot: Bot,
    chat_id: str,
    diffs: list[DiffResult],
    summary_lines: list[str],
    db: Database | None = None,
    user_id: int | None = None,
) -> bool:
    """Sends summarized diff notifications via Telegram.
    Returns True if delivery succeeded, False if it failed.
    State persistence should only occur if this returns True.
    """
    if not diffs:
        return True

    chunks = build_message_chunks(summary_lines)
    all_chunks_sent = True

    for chunk in chunks:
        chunk_sent = False
        for attempt in range(3):
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                )
                chunk_sent = True
                break
            except RetryAfter as exc:
                wait_time = float(exc.retry_after)
                logger.warning("Telegram RetryAfter: sleeping %0.1fs for chat_id=%s", wait_time, chat_id)
                await asyncio.sleep(wait_time)
            except Forbidden:
                logger.warning("User chat_id=%s blocked NudgeBot. Deactivating user.", chat_id)
                if db and user_id:
                    db.set_active(user_id, False)
                return False
            except BadRequest as exc:
                logger.warning(
                    "Telegram BadRequest with HTML formatting for chat_id=%s: %s. Retrying in plain text.", chat_id, exc
                )
                # Fallback to plain text in case of malformed markup
                try:
                    # Strip basic HTML tags
                    plain = chunk.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
                    await bot.send_message(chat_id=chat_id, text=plain)
                    chunk_sent = True
                    break
                except Exception:
                    logger.exception("Plain text fallback also failed for chat_id=%s", chat_id)
                    all_chunks_sent = False
                    break
            except (NetworkError, TelegramError) as exc:
                logger.warning("Telegram network error (attempt %d/3) for chat_id=%s: %s", attempt + 1, chat_id, exc)
                if attempt < 2:
                    await asyncio.sleep(1.0)
                else:
                    all_chunks_sent = False
                    break

        if not chunk_sent:
            all_chunks_sent = False
            break

    if all_chunks_sent and db and user_id:
        db.update_last_notified(user_id)
        # Record notification_sent event
        db.record_event(user_id, "notification_sent", {"item_count": len(diffs)})

        # Record deadline_caught event for new assignments or changed due dates
        for diff in diffs:
            if diff.item.item_type == ItemType.ASSIGNMENT:
                is_due_change = (
                    not diff.is_new
                    and diff.previous_fields
                    and diff.previous_fields.get("due_date") != diff.item.mutable_fields.get("due_date")
                )
                if diff.is_new or is_due_change:
                    db.record_event(
                        user_id,
                        "deadline_caught",
                        {
                            "key": diff.item.key,
                            "course": diff.item.course,
                            "title": diff.item.title,
                            "is_new": diff.is_new,
                            "is_due_change": is_due_change,
                        },
                    )

    return all_chunks_sent
