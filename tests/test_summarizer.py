"""Tests for pluggable LLM summarizer: Gemini REST, OpenAI-compatible REST,
NoLLM fallback, grounding checks, malformed JSON, wrong length, 429 backoff,
timeout, and daily quota limits.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from db import Database
from models import Assignment, DiffResult, Grade
from summarizer import (
    GeminiProvider,
    NoLLMProvider,
    OpenAICompatibleProvider,
    _diff_to_payload,
    is_line_grounded,
    reset_daily_quota_counter,
    summarize_diffs,
    summarize_diffs_async,
)


def test_template_fallback_without_key() -> None:
    diffs = [
        DiffResult(
            item=Assignment(course="CS 101", title="HW1", key="c1:a1", due_date="2026-10-05T23:59:00Z"), is_new=True
        ),
        DiffResult(
            item=Grade(course="ECON 201", title="Quiz 2", key="e1:q2", score="95", max_score="100"), is_new=True
        ),
    ]
    lines = summarize_diffs(diffs, anthropic_api_key=None, user_tz="UTC")

    assert len(lines) == 2
    assert lines[0].startswith("🆕 New assignment — CS 101: HW1")
    assert "Oct 05" in lines[0]
    assert lines[1] == "📊 Grade posted — ECON 201: Quiz 2 → 95/100"


def test_no_llm_provider_explicit() -> None:
    diffs = [DiffResult(item=Assignment(course="CS 101", title="HW1", key="c1:a1"), is_new=True)]
    provider = NoLLMProvider()
    assert provider.provider_name == "none"
    assert provider.summarize([_diff_to_payload(diffs[0])]) is None

    lines = summarize_diffs(diffs, provider=provider)
    assert len(lines) == 1
    assert "🆕 New assignment — CS 101: HW1" in lines[0]


def test_payload_contains_no_secrets() -> None:
    diff = DiffResult(item=Assignment(course="CS 101", title="HW1", key="c1:a1"), is_new=True)
    payload = _diff_to_payload(diff)

    assert "password" not in payload
    assert "token" not in payload
    assert "secret" not in payload
    assert "cookie" not in payload
    assert payload["course"] == "CS 101"
    assert payload["title"] == "HW1"


def test_gemini_provider_success() -> None:
    provider = GeminiProvider(api_key="mock_key", model="gemini-2.5-flash-lite")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '{"candidates": [{"content": {"parts": [{"text": "[\\"🆕 CS 101: HW1 (due Oct 05)\\"]"}]}}]}'

    with patch("requests.post", return_value=mock_resp) as mock_post:
        res = provider.summarize([{"dummy": "data"}])
        assert res == ["🆕 CS 101: HW1 (due Oct 05)"]
        assert mock_post.called
        call_url = mock_post.call_args[0][0]
        assert "generativelanguage.googleapis.com" in call_url
        assert "key=mock_key" in call_url


def test_openai_compatible_provider_success() -> None:
    provider = OpenAICompatibleProvider(
        api_key="mock_key",
        base_url="https://api.groq.com/openai/v1",
        model="llama-3.1-8b-instant",
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '{"choices": [{"message": {"content": "[\\"🆕 CS 101: HW1 (due Oct 05)\\"]"}}]}'

    with patch("requests.post", return_value=mock_resp) as mock_post:
        res = provider.summarize([{"dummy": "data"}])
        assert res == ["🆕 CS 101: HW1 (due Oct 05)"]
        assert mock_post.called
        call_url = mock_post.call_args[0][0]
        assert call_url == "https://api.groq.com/openai/v1/chat/completions"


@pytest.mark.asyncio
async def test_malformed_llm_output_falls_back_to_templates() -> None:
    diffs = [DiffResult(item=Assignment(course="CS 101", title="HW1", key="c1:a1"), is_new=True)]
    mock_provider = MagicMock()
    mock_provider.provider_name = "mock"
    mock_provider.summarize.return_value = None

    lines = await summarize_diffs_async(diffs, provider=mock_provider)
    assert len(lines) == 1
    assert "🆕 New assignment — CS 101: HW1" in lines[0]


@pytest.mark.asyncio
async def test_wrong_array_length_falls_back_to_templates() -> None:
    diffs = [
        DiffResult(item=Assignment(course="CS 101", title="HW1", key="c1:a1"), is_new=True),
        DiffResult(item=Assignment(course="MATH 200", title="HW2", key="m1:a2"), is_new=True),
    ]
    mock_provider = MagicMock()
    mock_provider.provider_name = "mock"
    # Returns array of length 1 instead of 2
    mock_provider.summarize.return_value = ["🆕 Only one line returned"]

    lines = await summarize_diffs_async(diffs, provider=mock_provider)
    assert len(lines) == 2
    assert "🆕 New assignment — CS 101: HW1" in lines[0]
    assert "🆕 New assignment — MATH 200: HW2" in lines[1]


@pytest.mark.asyncio
async def test_per_line_grounding_check_hallucinated_number() -> None:
    diffs = [
        DiffResult(
            item=Assignment(course="CS 101", title="HW1", key="c1:a1", due_date="2026-10-05T23:59:00Z"),
            is_new=True,
        ),
        DiffResult(
            item=Grade(course="ECON 201", title="Quiz 2", key="e1:q2", score="95", max_score="100"),
            is_new=True,
        ),
    ]

    # Line 0 is fully grounded (CS 101, HW1, Oct 05).
    # Line 1 hallucinates score 80 (actual score is 95/100).
    mock_provider = MagicMock()
    mock_provider.provider_name = "mock"
    mock_provider.summarize.return_value = [
        "🆕 CS 101: HW1 (due Oct 05)",
        "📊 ECON 201: Quiz 2 → 80/100",
    ]

    lines = await summarize_diffs_async(diffs, provider=mock_provider, user_tz="UTC")
    assert len(lines) == 2
    # Grounded line preserved
    assert lines[0] == "🆕 CS 101: HW1 (due Oct 05)"
    # Hallucinated line reverted to template
    assert lines[1] == "📊 Grade posted — ECON 201: Quiz 2 → 95/100"


def test_is_line_grounded_date_check() -> None:
    diff = DiffResult(
        item=Assignment(course="CS 101", title="HW1", key="c1:a1", due_date="2026-10-05T23:59:00Z"),
        is_new=True,
    )
    # Correct date
    assert is_line_grounded("🆕 CS 101: HW1 due Oct 05", diff, user_tz="UTC") is True
    # Hallucinated month (Nov instead of Oct)
    assert is_line_grounded("🆕 CS 101: HW1 due Nov 05", diff, user_tz="UTC") is False
    # Hallucinated day (15 instead of 05)
    assert is_line_grounded("🆕 CS 101: HW1 due Oct 15", diff, user_tz="UTC") is False


@pytest.mark.asyncio
async def test_rate_limit_429_backoff_and_fallback() -> None:
    provider = GeminiProvider(api_key="mock_key")
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.text = '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}'

    with patch("requests.post", return_value=mock_resp) as mock_post:
        res = provider.summarize([{"dummy": "data"}])
        assert res is None
        # On 429, must NOT retry in a loop
        assert mock_post.call_count == 1

    diffs = [DiffResult(item=Assignment(course="CS 101", title="HW1", key="c1:a1"), is_new=True)]
    lines = await summarize_diffs_async(diffs, provider=provider)
    assert len(lines) == 1
    assert "🆕 New assignment" in lines[0]


@pytest.mark.asyncio
async def test_timeout_fallback() -> None:
    provider = OpenAICompatibleProvider(api_key="mock_key")

    with patch("requests.post", side_effect=requests.exceptions.Timeout("Request timed out")):
        res = provider.summarize([{"dummy": "data"}])
        assert res is None

    diffs = [DiffResult(item=Assignment(course="CS 101", title="HW1", key="c1:a1"), is_new=True)]
    lines = await summarize_diffs_async(diffs, provider=provider)
    assert len(lines) == 1
    assert "🆕 New assignment" in lines[0]


@pytest.mark.asyncio
async def test_daily_call_limit_enforcement_in_memory() -> None:
    diffs = [DiffResult(item=Assignment(course="CS 101", title="HW1", key="c1:a1"), is_new=True)]
    reset_daily_quota_counter()

    mock_provider = MagicMock()
    mock_provider.provider_name = "mock"
    mock_provider.summarize.return_value = ["🆕 Summary"]

    # With quota=0, immediately falls back to template without calling provider
    lines = await summarize_diffs_async(
        diffs,
        provider=mock_provider,
        max_daily_calls=0,
    )
    mock_provider.summarize.assert_not_called()
    assert len(lines) == 1
    assert "🆕 New assignment" in lines[0]


@pytest.mark.asyncio
async def test_daily_call_limit_enforcement_db(tmp_path: pytest.TempPathFactory) -> None:
    db_file = str(tmp_path / "test_llm_quota.db")
    db = Database(db_file)

    diffs = [DiffResult(item=Assignment(course="CS 101", title="HW1", key="c1:a1"), is_new=True)]
    mock_provider = MagicMock()
    mock_provider.provider_name = "mock"
    mock_provider.summarize.return_value = ["🆕 CS 101: HW1"]

    # Record 2 calls in DB
    db.record_llm_call("mock")
    db.record_llm_call("mock")
    assert db.get_daily_llm_call_count() == 2

    # If max_calls is 2, quota is reached, should not call provider
    lines = await summarize_diffs_async(
        diffs,
        db=db,
        provider=mock_provider,
        max_daily_calls=2,
    )
    mock_provider.summarize.assert_not_called()
    assert "🆕 New assignment" in lines[0]

    # If max_calls is 5, quota is not reached, should call provider
    lines = await summarize_diffs_async(
        diffs,
        db=db,
        provider=mock_provider,
        max_daily_calls=5,
    )
    mock_provider.summarize.assert_called_once()
    assert lines[0] == "🆕 CS 101: HW1"
    assert db.get_daily_llm_call_count() == 3
