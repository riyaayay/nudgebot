"""Summarizes DiffResults into punchy, human Telegram messages.
Abstracts LLM providers (Gemini, OpenAI-compatible, NoLLM) behind LLMProvider interface
with per-line grounding checks, 10s timeout, max 1 retry, and deterministic template fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import requests

from date_utils import format_in_timezone, parse_to_utc
from models import DiffResult, ItemType

if TYPE_CHECKING:
    from config import Settings
    from db import Database

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You summarize campus-portal changes into short Telegram notifications for a busy student. Rules:
- One line per item, max ~20 words.
- Start with a relevant emoji (🆕 new assignment, 📊 grade posted, 📢 announcement, ♻️ due date changed).
- Include the course code, the item title, and the single most important changed detail (due date, score, or what changed).
- No fluff, no "Hey!", no explanations. Just the facts, punchy.
- Return ONLY a JSON array of strings, one per input item, same order, nothing else — no markdown fences, no preamble."""

MAX_BATCH_SIZE = 20

_daily_llm_call_count = 0
_daily_llm_date = ""

_MONTH_MAP = {
    "jan": "jan", "january": "jan",
    "feb": "feb", "february": "feb",
    "mar": "mar", "march": "mar",
    "apr": "apr", "april": "apr",
    "may": "may",
    "jun": "jun", "june": "jun",
    "jul": "jul", "july": "jul",
    "aug": "aug", "august": "aug",
    "sep": "sep", "sept": "sep", "september": "sep",
    "oct": "oct", "october": "oct",
    "nov": "nov", "november": "nov",
    "dec": "dec", "december": "dec",
}

_WEEKDAY_MAP = {
    "mon": "mon", "monday": "mon",
    "tue": "tue", "tuesday": "tue",
    "wed": "wed", "wednesday": "wed",
    "thu": "thu", "thursday": "thu",
    "fri": "fri", "friday": "fri",
    "sat": "sat", "saturday": "sat",
    "sun": "sun", "sunday": "sun",
}


def _check_and_increment_daily_quota(max_daily: int) -> bool:
    """Returns True if an LLM call is allowed within today's quota (in-memory counter)."""
    global _daily_llm_call_count, _daily_llm_date
    today_str = datetime.now(UTC).strftime("%Y-%m-%d")
    if _daily_llm_date != today_str:
        _daily_llm_date = today_str
        _daily_llm_call_count = 0

    if _daily_llm_call_count >= max_daily:
        logger.warning(
            "Daily LLM call budget reached (%d/%d). Falling back to templates.",
            _daily_llm_call_count,
            max_daily,
        )
        return False

    _daily_llm_call_count += 1
    return True


def reset_daily_quota_counter() -> None:
    """Resets the in-memory daily quota counter (useful for unit testing)."""
    global _daily_llm_call_count, _daily_llm_date
    _daily_llm_call_count = 0
    _daily_llm_date = ""


def _extract_numbers(text: str) -> set[str]:
    """Extracts and normalizes all numeric tokens from text."""
    raw = re.findall(r"\b\d+(?:\.\d+)?\b", text)
    nums: set[str] = set()
    for n in raw:
        nums.add(n)
        try:
            val = float(n)
            if val.is_integer():
                nums.add(str(int(val)))
            nums.add(f"{val:g}")
        except ValueError:
            pass
    return nums


def _extract_date_tokens(text: str) -> set[str]:
    """Extracts canonical month and weekday tokens from text."""
    words = re.findall(r"\b[a-zA-Z]+\b", text.lower())
    tokens: set[str] = set()
    for w in words:
        if w in _MONTH_MAP:
            tokens.add(_MONTH_MAP[w])
        elif w in _WEEKDAY_MAP:
            tokens.add(_WEEKDAY_MAP[w])
    return tokens


def _collect_source_fields(diff: DiffResult, user_tz: str) -> tuple[set[str], set[str]]:
    """Gathers normalized numbers and date tokens from all source fields."""
    item = diff.item
    text_parts: list[str] = [item.course or "", item.title or "", item.key or ""]

    for _k, v in item.mutable_fields.items():
        if v is not None:
            text_parts.append(str(v))

    if diff.previous_fields:
        for _k, v in diff.previous_fields.items():
            if v is not None:
                text_parts.append(str(v))

    # Also parse due dates to extract localized date components
    for d_val in (item.mutable_fields.get("due_date"), (diff.previous_fields or {}).get("due_date")):
        if d_val and isinstance(d_val, str):
            text_parts.append(d_val)
            formatted = format_in_timezone(d_val, user_tz)
            text_parts.append(formatted)
            dt = parse_to_utc(d_val, user_tz)
            if dt is not None:
                text_parts.append(str(dt.year))
                text_parts.append(str(dt.month))
                text_parts.append(f"{dt.month:02d}")
                text_parts.append(str(dt.day))
                text_parts.append(f"{dt.day:02d}")

    combined = " ".join(text_parts)
    return _extract_numbers(combined), _extract_date_tokens(combined)


def is_line_grounded(line: str, diff: DiffResult, user_tz: str = "Asia/Kolkata") -> bool:
    """Verifies that every number, score, and date token in the LLM line
    is grounded in the source item's fields. Returns False if any hallucinated token is found.
    """
    source_nums, source_dates = _collect_source_fields(diff, user_tz)
    line_nums = _extract_numbers(line)
    line_dates = _extract_date_tokens(line)

    for num in line_nums:
        if num not in source_nums:
            return False

    for date_tok in line_dates:
        if date_tok not in source_dates:
            return False

    return True


def _fallback_line(diff: DiffResult, user_tz: str = "Asia/Kolkata") -> str:
    """Deterministic, clean template line for an item when LLM is disabled or fails."""
    item = diff.item
    if item.item_type == ItemType.GRADE:
        score = item.mutable_fields.get("score")
        max_score = item.mutable_fields.get("max_score")
        letter = item.mutable_fields.get("letter")
        score_str = f"{score}/{max_score}" if max_score and score else str(score or letter or "Graded")
        return f"📊 Grade posted — {item.course}: {item.title} → {score_str}"

    if item.item_type == ItemType.ASSIGNMENT:
        due = item.mutable_fields.get("due_date")
        due_str = f" (due {format_in_timezone(due, user_tz)})" if due else ""
        if diff.is_new:
            prefix = "🆕 New assignment"
        elif diff.previous_fields and diff.previous_fields.get("due_date") != due:
            prefix = "♻️ Due date changed"
        else:
            prefix = "🔔 Assignment updated"
        return f"{prefix} — {item.course}: {item.title}{due_str}"

    if item.item_type == ItemType.ANNOUNCEMENT:
        return f"📢 Announcement — {item.course}: {item.title}"

    return f"🔔 Update — {item.course}: {item.title}"


def _diff_to_payload(diff: DiffResult, user_tz: str = "Asia/Kolkata") -> dict[str, Any]:
    """Serializes DiffResult to payload. Never includes sensitive credentials, tokens, or cookies."""
    item = diff.item
    safe_fields: dict[str, Any] = {}
    for k in ("due_date", "score", "max_score", "letter", "points", "status", "submission_type"):
        if k in item.mutable_fields and item.mutable_fields[k] is not None:
            safe_fields[k] = item.mutable_fields[k]

    prev_safe: dict[str, Any] = {}
    if diff.previous_fields:
        for k in ("due_date", "score", "max_score", "letter", "points", "status", "submission_type"):
            if k in diff.previous_fields and diff.previous_fields[k] is not None:
                prev_safe[k] = diff.previous_fields[k]

    if safe_fields.get("due_date"):
        safe_fields["due_date_formatted"] = format_in_timezone(safe_fields["due_date"], user_tz)
    if prev_safe.get("due_date"):
        prev_safe["due_date_formatted"] = format_in_timezone(prev_safe["due_date"], user_tz)

    return {
        "type": item.item_type.value,
        "is_new": diff.is_new,
        "course": item.course,
        "title": item.title,
        "fields": safe_fields,
        "previous_fields": prev_safe if prev_safe else None,
    }


def _parse_llm_json(raw_text: str) -> list[str] | None:
    """Strips markdown fences and parses JSON array of strings."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
    except Exception:
        return None

    if isinstance(data, list):
        if all(isinstance(x, str) for x in data):
            return data
    elif isinstance(data, dict):
        for key in ("summaries", "items", "results", "notifications"):
            val = data.get(key)
            if isinstance(val, list) and all(isinstance(x, str) for x in val):
                return val
    return None


def _safe_post_with_retry(
    url: str,
    headers: dict[str, str],
    json_data: dict[str, Any],
    provider_name: str,
    timeout: float = 10.0,
) -> tuple[int | None, str | None, float, str | None]:
    """Posts JSON to URL with hard timeout (10s) and max 1 retry on 5xx or network errors.
    Returns: (status_code, response_text, latency_ms, fallback_reason)
    Never logs prompt or response content.
    """
    start = time.perf_counter()
    try:
        resp = requests.post(url, headers=headers, json=json_data, timeout=timeout)
        latency_ms = (time.perf_counter() - start) * 1000

        if resp.status_code == 200:
            return resp.status_code, resp.text, latency_ms, None

        if resp.status_code == 429 or "RESOURCE_EXHAUSTED" in resp.text:
            return resp.status_code, resp.text, latency_ms, "rate_limited_429"

        if resp.status_code >= 500:
            time.sleep(1.0)
            resp2 = requests.post(url, headers=headers, json=json_data, timeout=timeout)
            latency_ms = (time.perf_counter() - start) * 1000
            if resp2.status_code == 200:
                return resp2.status_code, resp2.text, latency_ms, None
            if resp2.status_code == 429 or "RESOURCE_EXHAUSTED" in resp2.text:
                return resp2.status_code, resp2.text, latency_ms, "rate_limited_429"
            return resp2.status_code, None, latency_ms, f"http_{resp2.status_code}"

        return resp.status_code, None, latency_ms, f"http_{resp.status_code}"
    except requests.Timeout:
        latency_ms = (time.perf_counter() - start) * 1000
        return None, None, latency_ms, "timeout"
    except requests.RequestException:
        try:
            time.sleep(1.0)
            resp2 = requests.post(url, headers=headers, json=json_data, timeout=timeout)
            latency_ms = (time.perf_counter() - start) * 1000
            if resp2.status_code == 200:
                return resp2.status_code, resp2.text, latency_ms, None
            return resp2.status_code, None, latency_ms, f"http_{resp2.status_code}"
        except requests.Timeout:
            latency_ms = (time.perf_counter() - start) * 1000
            return None, None, latency_ms, "timeout"
        except Exception as retry_exc:
            latency_ms = (time.perf_counter() - start) * 1000
            return None, None, latency_ms, f"network_error_{type(retry_exc).__name__}"
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        return None, None, latency_ms, f"error_{type(exc).__name__}"


# ---------------------------------------------------------------------------
# Provider Interface & Implementations
# ---------------------------------------------------------------------------


class LLMProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the provider for logging and metrics."""
        ...

    @abstractmethod
    def summarize(self, payload: list[dict[str, Any]]) -> list[str] | None:
        """Summarizes a batch payload.
        Returns a list of summary strings (same length as payload), or None on failure."""
        ...


class NoLLMProvider(LLMProvider):
    """Default provider returning None, resulting in deterministic template summaries."""

    @property
    def provider_name(self) -> str:
        return "none"

    def summarize(self, payload: list[dict[str, Any]]) -> list[str] | None:
        return None


class GeminiProvider(LLMProvider):
    """Google Gemini provider using plain REST via requests."""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash-lite") -> None:
        self.api_key = api_key
        self.model = model

    @property
    def provider_name(self) -> str:
        return "gemini"

    def summarize(self, payload: list[dict[str, Any]]) -> list[str] | None:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        headers = {"Content-Type": "application/json"}
        req_body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": json.dumps(payload)}],
                }
            ],
            "systemInstruction": {
                "parts": [{"text": _SYSTEM_PROMPT}],
            },
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
            },
        }

        _status, text, latency_ms, reason = _safe_post_with_retry(
            url=url,
            headers=headers,
            json_data=req_body,
            provider_name=self.provider_name,
            timeout=10.0,
        )

        if reason or not text:
            logger.warning(
                "LLM summarization fallback [provider=%s, latency=%.1fms, reason=%s]",
                self.provider_name,
                latency_ms,
                reason or "empty_response",
            )
            return None

        try:
            data = json.loads(text)
            candidates = data.get("candidates") or []
            if not candidates:
                logger.warning(
                    "LLM summarization fallback [provider=%s, latency=%.1fms, reason=no_candidates]",
                    self.provider_name,
                    latency_ms,
                )
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                return None
            content_text = parts[0].get("text", "")
            parsed = _parse_llm_json(content_text)
            if parsed is None or len(parsed) != len(payload):
                logger.warning(
                    "LLM summarization fallback [provider=%s, latency=%.1fms, reason=invalid_json_or_length]",
                    self.provider_name,
                    latency_ms,
                )
                return None
            logger.info(
                "LLM summarization completed [provider=%s, latency=%.1fms, items=%d]",
                self.provider_name,
                latency_ms,
                len(payload),
            )
            return parsed
        except Exception as exc:
            logger.warning(
                "LLM summarization fallback [provider=%s, latency=%.1fms, reason=parse_error_%s]",
                self.provider_name,
                latency_ms,
                type(exc).__name__,
            )
            return None


class OpenAICompatibleProvider(LLMProvider):
    """OpenAI-compatible REST provider (works with Groq, OpenRouter, Ollama, OpenAI)."""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "llama-3.1-8b-instant",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    @property
    def provider_name(self) -> str:
        return "openai_compat"

    def summarize(self, payload: list[dict[str, Any]]) -> list[str] | None:
        url = self.base_url if self.base_url.endswith("/chat/completions") else f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req_body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            "temperature": 0.0,
        }

        _status, text, latency_ms, reason = _safe_post_with_retry(
            url=url,
            headers=headers,
            json_data=req_body,
            provider_name=self.provider_name,
            timeout=10.0,
        )

        if reason or not text:
            logger.warning(
                "LLM summarization fallback [provider=%s, latency=%.1fms, reason=%s]",
                self.provider_name,
                latency_ms,
                reason or "empty_response",
            )
            return None

        try:
            data = json.loads(text)
            choices = data.get("choices") or []
            if not choices:
                logger.warning(
                    "LLM summarization fallback [provider=%s, latency=%.1fms, reason=no_choices]",
                    self.provider_name,
                    latency_ms,
                )
                return None
            content_text = choices[0].get("message", {}).get("content", "")
            parsed = _parse_llm_json(content_text)
            if parsed is None or len(parsed) != len(payload):
                logger.warning(
                    "LLM summarization fallback [provider=%s, latency=%.1fms, reason=invalid_json_or_length]",
                    self.provider_name,
                    latency_ms,
                )
                return None
            logger.info(
                "LLM summarization completed [provider=%s, latency=%.1fms, items=%d]",
                self.provider_name,
                latency_ms,
                len(payload),
            )
            return parsed
        except Exception as exc:
            logger.warning(
                "LLM summarization fallback [provider=%s, latency=%.1fms, reason=parse_error_%s]",
                self.provider_name,
                latency_ms,
                type(exc).__name__,
            )
            return None


class AnthropicProvider(LLMProvider):
    """Optional Anthropic Claude provider maintained for backward compatibility."""

    def __init__(self, api_key: str, model: str = "claude-3-5-sonnet-latest") -> None:
        self.api_key = api_key
        self.model = model

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def summarize(self, payload: list[dict[str, Any]]) -> list[str] | None:
        try:
            import anthropic  # type: ignore[import-untyped]

            client = anthropic.Anthropic(api_key=self.api_key, timeout=10.0, max_retries=1)
            response = client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(payload)}],
            )
            text = "".join(block.text for block in response.content if block.type == "text").strip()
            return _parse_llm_json(text)
        except Exception as exc:
            logger.warning("Anthropic summarizer failed (%s).", exc)
            return None


def get_llm_provider(settings: Settings) -> LLMProvider:
    """Factory selecting the appropriate LLMProvider based on Settings."""
    provider_name = (settings.llm_provider or "none").lower()
    if provider_name == "gemini":
        if not settings.llm_api_key:
            logger.warning("LLM_PROVIDER is 'gemini' but LLM_API_KEY is not set. Falling back to NoLLMProvider.")
            return NoLLMProvider()
        return GeminiProvider(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )
    if provider_name in ("openai_compat", "openai"):
        return OpenAICompatibleProvider(
            api_key=settings.llm_api_key or "",
            base_url=settings.llm_base_url or "https://api.openai.com/v1",
            model=settings.llm_model,
        )
    if provider_name == "anthropic":
        if not settings.llm_api_key:
            return NoLLMProvider()
        return AnthropicProvider(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )
    return NoLLMProvider()


# ---------------------------------------------------------------------------
# Public Summarization Functions
# ---------------------------------------------------------------------------


async def summarize_diffs_async(
    diffs: list[DiffResult],
    settings: Settings | None = None,
    db: Database | None = None,
    user_tz: str = "Asia/Kolkata",
    provider: LLMProvider | None = None,
    anthropic_api_key: str | None = None,
    anthropic_model: str = "claude-3-5-sonnet-latest",
    max_daily_calls: int | None = None,
) -> list[str]:
    """Summarizes diffs in chunks of MAX_BATCH_SIZE (20) asynchronously.
    Enforces daily call cap via DB (or in-memory counter), 10s timeout,
    per-line grounding check, and falls back to deterministic templates on any failure.
    """
    if not diffs:
        return []

    # Resolve provider
    if provider is None:
        if settings is not None:
            provider = get_llm_provider(settings)
        elif anthropic_api_key:
            provider = AnthropicProvider(api_key=anthropic_api_key, model=anthropic_model)
        else:
            provider = NoLLMProvider()

    # If provider is NoLLMProvider, fast path to templates
    if provider.provider_name == "none":
        return [_fallback_line(d, user_tz) for d in diffs]

    max_calls = (
        settings.llm_max_calls_per_day
        if settings is not None
        else (max_daily_calls if max_daily_calls is not None else 200)
    )

    results: list[str] = []
    for i in range(0, len(diffs), MAX_BATCH_SIZE):
        batch = diffs[i : i + MAX_BATCH_SIZE]

        # Check daily quota
        quota_ok = False
        if db is not None:
            current_count = db.get_daily_llm_call_count()
            if current_count >= max_calls:
                logger.warning(
                    "Daily LLM call budget reached (%d/%d in DB). Falling back to templates.",
                    current_count,
                    max_calls,
                )
                quota_ok = False
            else:
                quota_ok = True
        else:
            quota_ok = _check_and_increment_daily_quota(max_calls)

        if not quota_ok:
            results.extend([_fallback_line(d, user_tz) for d in batch])
            continue

        # Prepare sanitized payload
        payload = [_diff_to_payload(d, user_tz) for d in batch]

        # Record LLM call in DB if DB is active
        if db is not None:
            db.record_llm_call(provider.provider_name)

        # Execute blocking LLM HTTP call in a thread to keep async loop responsive
        lines = await asyncio.to_thread(provider.summarize, payload)

        if lines is not None and len(lines) == len(batch):
            # Precision guardrail: verify each line's grounding
            batch_lines: list[str] = []
            for item_idx, (line, diff_item) in enumerate(zip(lines, batch)):
                if is_line_grounded(line, diff_item, user_tz):
                    batch_lines.append(line)
                else:
                    logger.info(
                        "Per-line grounding check failed for item %d (%s); reverting to template",
                        item_idx,
                        diff_item.item.course,
                    )
                    batch_lines.append(_fallback_line(diff_item, user_tz))
            results.extend(batch_lines)
        else:
            # Output must be a JSON array of strings, same length and order as the input.
            # Otherwise use the template for the whole batch.
            logger.warning(
                "LLM provider '%s' failed or returned mismatched batch length (%s vs %s). Reverting batch to templates.",
                provider.provider_name,
                len(lines) if lines is not None else None,
                len(batch),
            )
            results.extend([_fallback_line(d, user_tz) for d in batch])

    return results


def summarize_diffs(
    diffs: list[DiffResult],
    settings: Settings | None = None,
    db: Database | None = None,
    user_tz: str = "Asia/Kolkata",
    provider: LLMProvider | None = None,
    anthropic_api_key: str | None = None,
    anthropic_model: str = "claude-3-5-sonnet-latest",
    max_daily_calls: int | None = None,
) -> list[str]:
    """Synchronous entry point maintained for backwards compatibility.
    Runs summarize_diffs_async via asyncio.run if not in an active loop."""
    if not diffs:
        return []

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        return [_fallback_line(d, user_tz) for d in diffs]

    return asyncio.run(
        summarize_diffs_async(
            diffs=diffs,
            settings=settings,
            db=db,
            user_tz=user_tz,
            provider=provider,
            anthropic_api_key=anthropic_api_key,
            anthropic_model=anthropic_model,
            max_daily_calls=max_daily_calls,
        )
    )
