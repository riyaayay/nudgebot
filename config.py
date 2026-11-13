"""Centralized config loading and validation.
Everything else imports from here instead of touching os.environ directly,
so there is one place to see what NudgeBot needs and ensure all secrets
are validated, sanitized, and redacted.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from security_utils import SecretRedactingFilter

load_dotenv()


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


def _parse_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    items = [x.strip() for x in raw.replace(",", " ").split() if x.strip()]
    return items if items else None


def _parse_int_list(raw: str | None, default: list[int]) -> list[int]:
    if not raw:
        return default
    items: list[int] = []
    for x in raw.replace(",", " ").split():
        x = x.strip()
        if x:
            try:
                items.append(int(x))
            except ValueError:
                pass
    return sorted(list(set(items)), reverse=True) if items else default


@dataclass(frozen=True, init=False)
class Settings:
    telegram_bot_token: str
    encryption_keys: list[str]
    llm_provider: str  # none | gemini | openai_compat
    llm_api_key: str | None
    llm_base_url: str | None
    llm_model: str
    llm_max_calls_per_day: int
    default_poll_interval_minutes: int
    min_poll_interval_minutes: int
    database_path: str
    log_level: str
    log_format: str
    admin_telegram_id: str | None
    allowed_telegram_ids: list[str] | None
    invite_code: str | None
    max_users: int
    allowed_portal_domains: list[str] | None
    health_port: int
    user_agent_contact: str
    default_timezone: str
    reminder_hours: list[int]

    def __init__(
        self,
        telegram_bot_token: str,
        encryption_keys: list[str],
        default_poll_interval_minutes: int = 10,
        min_poll_interval_minutes: int = 5,
        database_path: str = "data/nudgebot.db",
        log_level: str = "INFO",
        log_format: str = "text",
        admin_telegram_id: str | None = None,
        allowed_telegram_ids: list[str] | None = None,
        invite_code: str | None = None,
        max_users: int = 100,
        allowed_portal_domains: list[str] | None = None,
        health_port: int = 8080,
        user_agent_contact: str = "portal owner",
        default_timezone: str = "Asia/Kolkata",
        reminder_hours: list[int] | None = None,
        llm_provider: str = "none",
        llm_api_key: str | None = None,
        llm_base_url: str | None = None,
        llm_model: str = "gemini-2.5-flash-lite",
        llm_max_calls_per_day: int = 200,
        anthropic_api_key: str | None = None,
        anthropic_model: str | None = None,
        max_daily_llm_calls: int | None = None,
    ) -> None:
        object.__setattr__(self, "telegram_bot_token", telegram_bot_token)
        object.__setattr__(self, "encryption_keys", encryption_keys)

        final_key = llm_api_key or anthropic_api_key
        final_model = llm_model if llm_model != "gemini-2.5-flash-lite" else (anthropic_model or llm_model)
        final_max = max_daily_llm_calls if max_daily_llm_calls is not None else llm_max_calls_per_day

        object.__setattr__(self, "llm_provider", llm_provider)
        object.__setattr__(self, "llm_api_key", final_key)
        object.__setattr__(self, "llm_base_url", llm_base_url)
        object.__setattr__(self, "llm_model", final_model)
        object.__setattr__(self, "llm_max_calls_per_day", final_max)

        object.__setattr__(self, "default_poll_interval_minutes", default_poll_interval_minutes)
        object.__setattr__(self, "min_poll_interval_minutes", min_poll_interval_minutes)
        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "log_level", log_level)
        object.__setattr__(self, "log_format", log_format)
        object.__setattr__(self, "admin_telegram_id", admin_telegram_id)
        object.__setattr__(self, "allowed_telegram_ids", allowed_telegram_ids)
        object.__setattr__(self, "invite_code", invite_code)
        object.__setattr__(self, "max_users", max_users)
        object.__setattr__(self, "allowed_portal_domains", allowed_portal_domains)
        object.__setattr__(self, "health_port", health_port)
        object.__setattr__(self, "user_agent_contact", user_agent_contact)
        object.__setattr__(self, "default_timezone", default_timezone)
        object.__setattr__(self, "reminder_hours", reminder_hours if reminder_hours is not None else [24, 3])

    @property
    def encryption_key(self) -> str:
        """Backward-compatible property returning the primary encryption key."""
        return self.encryption_keys[0]

    @property
    def anthropic_api_key(self) -> str | None:
        """Backward-compatible property."""
        return self.llm_api_key

    @property
    def anthropic_model(self) -> str:
        """Backward-compatible property."""
        return self.llm_model

    @property
    def max_daily_llm_calls(self) -> int:
        """Backward-compatible property."""
        return self.llm_max_calls_per_day

    def print_redacted_summary(self) -> None:
        """Prints safe startup configuration summary with all secrets redacted."""
        logger = logging.getLogger("nudgebot.config")
        token_prefix = self.telegram_bot_token.split(":")[0] if ":" in self.telegram_bot_token else "..."
        domains = ", ".join(self.allowed_portal_domains) if self.allowed_portal_domains else "any (https required)"
        allowed_users = f"{len(self.allowed_telegram_ids)} configured" if self.allowed_telegram_ids else "open (public)"
        llm_status = f"{self.llm_provider} (model: {self.llm_model})" if self.llm_provider != "none" else "disabled (template summaries)"
        logger.info("=== NudgeBot Configuration ===")
        logger.info("Telegram Bot Token: %s:***", token_prefix)
        logger.info("Active Encryption Keys: %d loaded", len(self.encryption_keys))
        logger.info("LLM Summaries: %s", llm_status)
        logger.info("Polling Cadence: default=%dm, minimum=%dm", self.default_poll_interval_minutes, self.min_poll_interval_minutes)
        logger.info("Database Path: %s", self.database_path)
        logger.info("Default Timezone: %s", self.default_timezone)
        logger.info("Reminder Windows: %s hours", self.reminder_hours)
        logger.info("Max Users: %d | Access Allowlist: %s | Invite Code: %s", self.max_users, allowed_users, "Yes" if self.invite_code else "No")
        logger.info("Allowed Domains: %s", domains)
        logger.info("Health Check Port: %d", self.health_port)
        logger.info("==============================")


def load_settings() -> Settings:
    # Telegram Bot Token
    token = _require("TELEGRAM_BOT_TOKEN")
    if not (":" in token and len(token) > 20):
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN appears invalid (expected format like '123456789:ABCdef...'). "
            "Get one from @BotFather on Telegram."
        )

    # Encryption key(s)
    raw_keys = os.environ.get("ENCRYPTION_KEYS", "").strip() or os.environ.get("ENCRYPTION_KEY", "").strip()
    if not raw_keys:
        raise ConfigError(
            "Missing ENCRYPTION_KEY (or ENCRYPTION_KEYS). Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    encryption_keys = [k.strip() for k in raw_keys.replace(",", " ").split() if k.strip()]

    # LLM configuration
    llm_provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if not llm_provider:
        if os.environ.get("GEMINI_API_KEY"):
            llm_provider = "gemini"
        elif os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_BASE_URL"):
            llm_provider = "openai_compat"
        else:
            llm_provider = "none"

    if llm_provider not in ("none", "gemini", "openai_compat", "anthropic"):
        raise ConfigError(
            f"Invalid LLM_PROVIDER '{llm_provider}'. Must be 'none', 'gemini', or 'openai_compat'."
        )

    llm_api_key = (
        os.environ.get("LLM_API_KEY", "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or None
    )

    llm_base_url = os.environ.get("LLM_BASE_URL", "").strip() or None

    default_model = "gemini-2.5-flash-lite" if llm_provider == "gemini" else "llama-3.1-8b-instant"
    llm_model = os.environ.get("LLM_MODEL", "").strip() or os.environ.get("ANTHROPIC_MODEL", "").strip() or default_model

    try:
        llm_max_calls_per_day = int(
            os.environ.get("LLM_MAX_CALLS_PER_DAY", "").strip()
            or os.environ.get("MAX_DAILY_LLM_CALLS", "200")
        )
    except ValueError as exc:
        raise ConfigError("LLM_MAX_CALLS_PER_DAY must be an integer.") from exc

    # Timezone validation
    default_tz = os.environ.get("DEFAULT_TIMEZONE", "Asia/Kolkata").strip()
    try:
        ZoneInfo(default_tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(
            f"Invalid DEFAULT_TIMEZONE '{default_tz}'. Use a valid IANA timezone (e.g. 'Asia/Kolkata', 'America/New_York')."
        ) from exc

    # Poll intervals
    try:
        default_interval = int(os.environ.get("DEFAULT_POLL_INTERVAL_MINUTES", "10"))
        min_interval = int(os.environ.get("MIN_POLL_INTERVAL_MINUTES", "5"))
    except ValueError as exc:
        raise ConfigError("Poll intervals must be integers.") from exc

    if default_interval < 1:
        raise ConfigError("DEFAULT_POLL_INTERVAL_MINUTES must be >= 1.")
    if min_interval < 1:
        raise ConfigError("MIN_POLL_INTERVAL_MINUTES must be >= 1.")
    if default_interval < min_interval:
        default_interval = min_interval

    # Health port
    try:
        health_port = int(os.environ.get("HEALTH_PORT", "8080"))
    except ValueError as exc:
        raise ConfigError("HEALTH_PORT must be an integer (e.g. 8080).") from exc

    # Max users
    try:
        max_users = int(os.environ.get("MAX_USERS", "100"))
    except ValueError as exc:
        raise ConfigError("MAX_USERS must be an integer (e.g. 100).") from exc

    # Admin telegram ID
    admin_id = os.environ.get("ADMIN_TELEGRAM_ID", "").strip() or None

    # Allowed Telegram IDs
    allowed_ids = _parse_list(os.environ.get("ALLOWED_TELEGRAM_IDS"))

    # Invite code
    invite_code = os.environ.get("INVITE_CODE", "").strip() or None

    # Allowed portal domains
    allowed_domains = _parse_list(os.environ.get("ALLOWED_PORTAL_DOMAINS"))

    # Reminder hours (e.g. "24, 3")
    reminder_hours = _parse_int_list(os.environ.get("REMINDER_HOURS"), [24, 3])

    return Settings(
        telegram_bot_token=token,
        encryption_keys=encryption_keys,
        llm_provider=llm_provider,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_max_calls_per_day=llm_max_calls_per_day,
        default_poll_interval_minutes=default_interval,
        min_poll_interval_minutes=min_interval,
        database_path=os.environ.get("DATABASE_PATH", "nudgebot.db").strip(),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        log_format=os.environ.get("LOG_FORMAT", "text").lower(),
        admin_telegram_id=admin_id,
        allowed_telegram_ids=allowed_ids,
        invite_code=invite_code,
        max_users=max_users,
        allowed_portal_domains=allowed_domains,
        health_port=health_port,
        user_agent_contact=os.environ.get("USER_AGENT_CONTACT", "portal owner").strip(),
        default_timezone=default_tz,
        reminder_hours=reminder_hours,
    )


class JSONFormatter(logging.Formatter):
    """Formats log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


def configure_logging(level: str, log_format: str = "text") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(SecretRedactingFilter())

    if log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
        )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.handlers = [handler]

    # Silence verbose dependencies
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
