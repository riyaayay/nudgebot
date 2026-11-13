"""Security utilities: SSRF protection, portal URL validation,
generic selector config validation, and sensitive-data log redacting.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

logger = logging.getLogger(__name__)

# Max permitted size for raw generic HTML JSON configs (32 KB)
MAX_CONFIG_JSON_BYTES = 32 * 1024


class SecurityError(ValueError):
    """Raised when security validation (SSRF, allowlist, schema) fails."""


def is_ip_blocked(ip_str: str) -> bool:
    """Check if an IP address belongs to private, loopback, link-local,
    multicast, or reserved ranges."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True

    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def validate_portal_url(
    url: str,
    allowed_domains: list[str] | None = None,
    allow_insecure_http: bool = False,
) -> str:
    """Validates that a URL is safe to scrape:
    1. Valid structure and scheme (must be https unless allow_insecure_http=True).
    2. Resolves domain to IP and ensures all resolved IPs are public.
    3. If allowed_domains is provided, ensures host is in the allowlist.
    """
    if not url or not isinstance(url, str):
        raise SecurityError("Portal URL cannot be empty.")

    url = url.strip()
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise SecurityError(f"Malformed URL: {exc}") from exc

    scheme = parsed.scheme.lower()
    if not allow_insecure_http and scheme != "https":
        raise SecurityError(f"Insecure scheme '{scheme}://' is blocked. Portal URLs must use 'https://'.")
    if allow_insecure_http and scheme not in ("http", "https"):
        raise SecurityError(f"Unsupported scheme '{scheme}://'. Must be http or https.")

    hostname = parsed.hostname
    if not hostname:
        raise SecurityError("Portal URL has no hostname.")

    hostname = hostname.lower()

    # Block well-known localhost aliases
    if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0") and not allow_insecure_http:
        raise SecurityError(f"Access to '{hostname}' is blocked.")

    # Check allowed domains allowlist if provided
    if allowed_domains:
        normalized_allowed = [d.strip().lower().lstrip(".") for d in allowed_domains if d.strip()]
        matched = False
        for allowed in normalized_allowed:
            if hostname == allowed or hostname.endswith(f".{allowed}"):
                matched = True
                break
        if not matched:
            raise SecurityError(f"Domain '{hostname}' is not in the ALLOWED_PORTAL_DOMAINS list: {normalized_allowed}")

    # Resolve IP and verify not private / link-local / loopback (SSRF protection)
    if not allow_insecure_http:
        try:
            port = parsed.port or (443 if scheme == "https" else 80)
            addr_info = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise SecurityError(f"Could not resolve hostname '{hostname}': {exc}") from exc

        if not addr_info:
            raise SecurityError(f"No IP address returned for hostname '{hostname}'.")

        for item in addr_info:
            sockaddr = item[4]
            ip_str = sockaddr[0]
            if is_ip_blocked(ip_str):
                raise SecurityError(f"URL resolves to disallowed private or local IP address ({ip_str}).")

    return url


class SSRFSafeRedirectSession(requests.Session):
    """Custom requests Session that checks redirect targets against SSRF rules."""

    def __init__(
        self,
        allowed_domains: list[str] | None = None,
        allow_insecure_http: bool = False,
    ):
        super().__init__()
        self.allowed_domains = allowed_domains
        self.allow_insecure_http = allow_insecure_http

    def rebuild_auth(self, prepared_request, response):  # type: ignore[no-untyped-def]
        if response.is_redirect and prepared_request.url:
            validate_portal_url(
                prepared_request.url,
                allowed_domains=self.allowed_domains,
                allow_insecure_http=self.allow_insecure_http,
            )
        super().rebuild_auth(prepared_request, response)


def create_resilient_session(
    user_agent: str,
    allowed_domains: list[str] | None = None,
    allow_insecure_http: bool = False,
    total_retries: int = 3,
    backoff_factor: float = 0.5,
) -> requests.Session:
    session = SSRFSafeRedirectSession(
        allowed_domains=allowed_domains,
        allow_insecure_http=allow_insecure_http,
    )
    session.headers["User-Agent"] = user_agent

    retry_strategy = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "HEAD"],
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def validate_generic_config(
    raw_config: str | dict[str, Any],
    allowed_domains: list[str] | None = None,
    allow_insecure_http: bool = False,
) -> dict[str, Any]:
    if isinstance(raw_config, str):
        if len(raw_config.encode("utf-8")) > MAX_CONFIG_JSON_BYTES:
            raise SecurityError(f"Config JSON exceeds size limit of {MAX_CONFIG_JSON_BYTES} bytes.")
        try:
            cfg = json.loads(raw_config)
        except Exception as exc:
            raise SecurityError(f"Invalid JSON: {exc}") from exc
    elif isinstance(raw_config, dict):
        cfg = raw_config
    else:
        raise SecurityError("Config must be a JSON string or dict.")

    if not isinstance(cfg, dict):
        raise SecurityError("Config must be a JSON object.")

    has_assignment = "assignment_row_selector" in cfg
    has_grade = "grade_row_selector" in cfg
    has_announcement = "announcement_row_selector" in cfg
    if not (has_assignment or has_grade or has_announcement):
        raise SecurityError(
            "Config must define at least one row selector: 'assignment_row_selector', "
            "'grade_row_selector', or 'announcement_row_selector'."
        )

    url_keys = ["login_url", "assignments_url", "grades_url", "announcements_url"]
    found_urls = 0
    for ukey in url_keys:
        if ukey in cfg and cfg[ukey]:
            found_urls += 1
            validate_portal_url(
                str(cfg[ukey]),
                allowed_domains=allowed_domains,
                allow_insecure_http=allow_insecure_http,
            )

    if found_urls == 0:
        raise SecurityError("Config must provide at least one valid URL ('assignments_url', 'grades_url', etc.).")

    if "login_method" in cfg:
        method = str(cfg["login_method"]).upper()
        if method not in ("GET", "POST"):
            raise SecurityError(f"login_method must be GET or POST, got '{method}'.")

    return cfg


class SecretRedactingFilter(logging.Filter):
    """Logging filter that redacts tokens, passwords, authorization headers,
    and secret parameters from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            msg = record.msg
            # Redact Telegram bot tokens (e.g. 123456789:ABCdefGHIjklMNOpqrSTUvwxYZ123456789)
            msg = re.sub(r"\b\d{9,10}:[A-Za-z0-9_-]{35}\b", "***REDACTED_BOT_TOKEN***", msg)
            # Redact Bearer tokens
            msg = re.sub(r"(Bearer\s+)[A-Za-z0-9_\-\.~]+", r"\1***REDACTED***", msg, flags=re.IGNORECASE)
            # Redact secret / password / token params
            msg = re.sub(
                r"((?:token|secret|password|api_key)\s*[:=]\s*['\"]?)[^'\"\s,;]+(['\"]?)",
                r"\1***REDACTED***\2",
                msg,
                flags=re.IGNORECASE,
            )
            record.msg = msg

        if record.args:
            if isinstance(record.args, dict):
                redacted_args = {}
                for k, v in record.args.items():
                    if any(s in k.lower() for s in ("token", "secret", "pass", "key")):
                        redacted_args[k] = "***REDACTED***"
                    else:
                        redacted_args[k] = v
                record.args = redacted_args
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    "***REDACTED***" if any(s in str(a).lower() for s in ("token=", "secret=", "password=")) else a
                    for a in record.args
                )
        return True
