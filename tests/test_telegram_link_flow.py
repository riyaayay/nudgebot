"""Tests for Telegram bot slash commands, rate limiting, and /link conversation flow."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Chat, Message, Update
from telegram import User as TgUser
from telegram.ext import ContextTypes, ConversationHandler

from config import Settings
from crypto_utils import CredentialCipher
from db import Database
from telegram_bot import (
    CANVAS_TOKEN,
    CANVAS_URL,
    CHOOSING_PORTAL_TYPE,
    BotContext,
    cmd_deletemydata,
    cmd_feedback,
    cmd_timezone,
    cmd_unlink,
    link_cancel,
    link_canvas_token,
    link_canvas_url,
    link_choose_type,
    link_start,
)


@pytest.fixture
def bot_env() -> tuple[Database, CredentialCipher, Settings, BotContext, str]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    cipher = CredentialCipher(key)
    settings = Settings(
        telegram_bot_token="123456789:ABCdefGHIjklMNOpqrSTUvwxYZ123456789",
        encryption_keys=[key],
        anthropic_api_key=None,
        anthropic_model="claude-3-5-sonnet-latest",
        default_poll_interval_minutes=10,
        min_poll_interval_minutes=5,
        database_path=path,
        log_level="INFO",
        log_format="text",
        admin_telegram_id="99999",
        allowed_telegram_ids=None,
        invite_code=None,
        max_users=100,
        allowed_portal_domains=None,
        health_port=8080,
        user_agent_contact="portal owner",
        default_timezone="Asia/Kolkata",
        reminder_hours=[24, 3],
        max_daily_llm_calls=50,
    )
    bc = BotContext(db=db, cipher=cipher, settings=settings)
    yield db, cipher, settings, bc, path
    db.close()
    if os.path.exists(path):
        os.remove(path)


def make_update(text: str, chat_id: int = 12345, username: str = "testuser") -> Update:
    update = MagicMock(spec=Update)
    chat = MagicMock(spec=Chat)
    chat.id = chat_id
    user = MagicMock(spec=TgUser)
    user.id = chat_id
    user.username = username
    msg = AsyncMock(spec=Message)
    msg.text = text
    msg.reply_text = AsyncMock()
    msg.delete = AsyncMock()
    update.effective_chat = chat
    update.effective_user = user
    update.message = msg
    return update


@pytest.mark.asyncio
async def test_link_canvas_flow_with_secret_deletion(
    bot_env: tuple[Database, CredentialCipher, Settings, BotContext, str],
) -> None:
    db, cipher, settings, bc, _ = bot_env
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.bot_data = {"bc": bc}
    context.user_data = {}

    # 1. /link start
    u1 = make_update("/link")
    state = await link_start(u1, context)
    assert state == CHOOSING_PORTAL_TYPE

    # 2. Choose Canvas
    u2 = make_update("Canvas")
    state = await link_choose_type(u2, context)
    assert state == CANVAS_URL

    # 3. Enter Canvas URL
    u3 = make_update("https://canvas.myschool.edu")
    with patch("telegram_bot.validate_portal_url", return_value="https://canvas.myschool.edu"):
        state = await link_canvas_url(u3, context)
    assert state == CANVAS_TOKEN
    assert context.user_data["portal_base_url"] == "https://canvas.myschool.edu"

    # 4. Enter Canvas Token
    u4 = make_update("my_secret_token_12345")
    # Mock CanvasScraper login validation
    with patch("telegram_bot.CanvasScraper") as mock_scraper_cls:
        mock_scraper = MagicMock()
        mock_scraper.login = MagicMock()
        mock_scraper_cls.return_value = mock_scraper

        status_msg = AsyncMock()
        u4.message.reply_text = AsyncMock(return_value=status_msg)

        state = await link_canvas_token(u4, context)

    assert state == ConversationHandler.END

    # Verification: token message MUST be deleted immediately
    u4.message.delete.assert_called_once()

    # Verification: User is linked and token encrypted
    user = db.get_user_by_chat_id("12345")
    assert user is not None
    assert user.is_linked is True
    assert user.portal_type == "canvas"
    assert user.portal_secret_enc is not None
    assert cipher.decrypt(user.portal_secret_enc) == "my_secret_token_12345"


@pytest.mark.asyncio
async def test_link_cancel_anywhere(bot_env: tuple[Database, CredentialCipher, Settings, BotContext, str]) -> None:
    _, _, _, bc, _ = bot_env
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.bot_data = {"bc": bc}
    context.user_data = {"portal_base_url": "https://canvas.myschool.edu"}

    u = make_update("/cancel")
    state = await link_cancel(u, context)
    assert state == ConversationHandler.END
    assert len(context.user_data) == 0
    u.message.reply_text.assert_called_once()


@pytest.mark.asyncio
async def test_timezone_command(bot_env: tuple[Database, CredentialCipher, Settings, BotContext, str]) -> None:
    db, _, _, bc, _ = bot_env
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.bot_data = {"bc": bc}

    # Query current timezone
    context.args = []
    u1 = make_update("/timezone")
    await cmd_timezone(u1, context)
    text = u1.message.reply_text.call_args[0][0]
    assert "Asia/Kolkata" in text

    # Set valid timezone
    context.args = ["America/New_York"]
    u2 = make_update("/timezone America/New_York")
    await cmd_timezone(u2, context)
    user = db.get_user_by_chat_id("12345")
    assert user is not None
    assert user.timezone == "America/New_York"

    # Set invalid timezone
    context.args = ["Fake/Timezone"]
    u3 = make_update("/timezone Fake/Timezone")
    await cmd_timezone(u3, context)
    assert "not a valid IANA timezone" in u3.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_unlink_and_deletemydata_confirmations(
    bot_env: tuple[Database, CredentialCipher, Settings, BotContext, str],
) -> None:
    db, cipher, _, bc, _ = bot_env
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.bot_data = {"bc": bc}

    user = db.get_or_create_user("12345", "testuser")
    db.link_portal(user.id, "canvas", "https://canvas.edu", None, cipher.encrypt("tok"), None)

    # 1. /unlink without 'confirm' prompts confirmation
    context.args = []
    u1 = make_update("/unlink")
    await cmd_unlink(u1, context)
    assert "Confirm Unlinking" in u1.message.reply_text.call_args[0][0]
    assert db.get_user_by_chat_id("12345").is_linked is True

    # 2. /unlink confirm unlinks
    context.args = ["confirm"]
    u2 = make_update("/unlink confirm")
    await cmd_unlink(u2, context)
    assert db.get_user_by_chat_id("12345").is_linked is False

    # 3. /deletemydata confirm wipes user
    context.args = ["confirm"]
    u3 = make_update("/deletemydata confirm")
    await cmd_deletemydata(u3, context)
    assert db.get_user_by_chat_id("12345") is None


@pytest.mark.asyncio
async def test_feedback_command_forwards_to_admin(
    bot_env: tuple[Database, CredentialCipher, Settings, BotContext, str],
) -> None:
    db, _, settings, bc, _ = bot_env
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.bot_data = {"bc": bc}
    context.bot = AsyncMock()
    context.args = ["This", "bot", "is", "awesome!"]

    u = make_update("/feedback This bot is awesome!", chat_id=12345, username="student1")
    await cmd_feedback(u, context)

    # Check saved to DB
    feedback_list = db.get_feedback_list()
    assert len(feedback_list) == 1
    assert feedback_list[0]["message"] == "This bot is awesome!"

    # Check forwarded to admin chat ID
    context.bot.send_message.assert_called_once()
    admin_call_args = context.bot.send_message.call_args[1]
    assert admin_call_args["chat_id"] == "99999"
    assert "This bot is awesome!" in admin_call_args["text"]
