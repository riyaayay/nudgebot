"""Tests for Telegram message formatting, HTML escaping, chunking, and error handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest, Forbidden

from models import Assignment, DiffResult
from notifier import build_message_chunks, escape_html, send_update


def test_html_escaping() -> None:
    assert escape_html("CS <101> & 'Math'") == "CS &lt;101&gt; &amp; 'Math'"
    assert escape_html(None) == ""


def test_message_splitting_large_payload() -> None:
    # 50 summary lines
    lines = [f"🆕 New assignment — CS {i}: Problem Set {i} (due Oct {i})" for i in range(1, 51)]
    chunks = build_message_chunks(lines)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 3800
        assert "🔔 <b>" in chunk


@pytest.mark.asyncio
async def test_send_update_forbidden_deactivates_user() -> None:
    mock_bot = AsyncMock()
    mock_bot.send_message.side_effect = Forbidden("Bot was blocked by the user")

    mock_db = MagicMock()
    mock_db.set_active = MagicMock()

    diff = DiffResult(item=Assignment(course="CS 101", title="HW1", key="c1:a1"), is_new=True)

    success = await send_update(
        bot=mock_bot,
        chat_id="12345",
        diffs=[diff],
        summary_lines=["New HW1"],
        db=mock_db,
        user_id=42,
    )

    assert success is False
    mock_db.set_active.assert_called_once_with(42, False)


@pytest.mark.asyncio
async def test_send_update_bad_request_retries_plain_text() -> None:
    mock_bot = AsyncMock()
    mock_bot.send_message.side_effect = [BadRequest("Can't parse entities"), AsyncMock()]

    diff = DiffResult(item=Assignment(course="CS 101", title="HW1", key="c1:a1"), is_new=True)

    with patch("notifier.logger.warning"):
        success = await send_update(
            bot=mock_bot,
            chat_id="12345",
            diffs=[diff],
            summary_lines=["New HW1"],
        )

    assert success is True
    assert mock_bot.send_message.call_count == 2
