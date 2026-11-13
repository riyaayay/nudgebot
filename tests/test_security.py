"""Tests for security utilities: SSRF validator, URL restrictions,
config schema checks, and log secret redacting.
"""

from __future__ import annotations

import logging

import pytest

from security_utils import (
    SecretRedactingFilter,
    SecurityError,
    is_ip_blocked,
    validate_generic_config,
    validate_portal_url,
)


def test_is_ip_blocked() -> None:
    # Loopback
    assert is_ip_blocked("127.0.0.1") is True
    assert is_ip_blocked("::1") is True
    # Private subnets
    assert is_ip_blocked("10.0.1.5") is True
    assert is_ip_blocked("192.168.1.100") is True
    assert is_ip_blocked("172.16.0.1") is True
    # Link-local / Cloud metadata
    assert is_ip_blocked("169.254.169.254") is True
    # Public IPs
    assert is_ip_blocked("8.8.8.8") is False
    assert is_ip_blocked("1.1.1.1") is False


def test_validate_portal_url_blocks_http() -> None:
    with pytest.raises(SecurityError, match="must use 'https://'"):
        validate_portal_url("http://canvas.myschool.edu")


def test_validate_portal_url_blocks_localhost() -> None:
    with pytest.raises(SecurityError, match="blocked"):
        validate_portal_url("https://localhost:8443")
    with pytest.raises(SecurityError, match="blocked"):
        validate_portal_url("https://127.0.0.1")


def test_validate_portal_url_allowed_domains() -> None:
    # Matching domain
    url = validate_portal_url(
        "https://canvas.myschool.edu",
        allowed_domains=["myschool.edu"],
        allow_insecure_http=True,  # skip DNS resolution for offline test
    )
    assert url == "https://canvas.myschool.edu"

    # Non-matching domain
    with pytest.raises(SecurityError, match="not in the ALLOWED_PORTAL_DOMAINS list"):
        validate_portal_url(
            "https://canvas.evil-attacker.com",
            allowed_domains=["myschool.edu"],
            allow_insecure_http=True,
        )


def test_validate_generic_config() -> None:
    valid_cfg = {
        "assignments_url": "https://portal.myschool.edu/assignments",
        "assignment_row_selector": "table.rows tr",
        "login_method": "POST",
    }
    # Valid config passes
    res = validate_generic_config(valid_cfg, allow_insecure_http=True)
    assert res["login_method"] == "POST"

    # Missing selectors fails
    with pytest.raises(SecurityError, match="must define at least one row selector"):
        validate_generic_config(
            {"assignments_url": "https://portal.myschool.edu/assignments"}, allow_insecure_http=True
        )

    # Invalid login method fails
    with pytest.raises(SecurityError, match="login_method must be GET or POST"):
        validate_generic_config(
            {
                "assignments_url": "https://portal.myschool.edu/assignments",
                "assignment_row_selector": "tr",
                "login_method": "DELETE",
            },
            allow_insecure_http=True,
        )

    # Oversized config fails
    huge_json = "{" + '"a": "b",' * 5000 + '"assignment_row_selector": "tr"}'
    with pytest.raises(SecurityError, match="exceeds size limit"):
        validate_generic_config(huge_json, allow_insecure_http=True)


def test_secret_redacting_filter() -> None:
    redactor = SecretRedactingFilter()

    # Redacting Bearer tokens
    record = logging.LogRecord(
        "test", logging.INFO, "test.py", 10, "Calling Canvas with Bearer secret_token_abc_123", (), None
    )
    redactor.filter(record)
    assert "secret_token_abc_123" not in record.msg
    assert "REDACTED" in record.msg

    # Redacting Telegram bot token
    record2 = logging.LogRecord(
        "test", logging.INFO, "test.py", 10, "Bot token: 123456789:ABCdefGHIjklMNOpqrSTUvwxYZ123456789", (), None
    )
    redactor.filter(record2)
    assert "ABCdefGHIjklMNOpqrSTUvwxYZ" not in record2.msg
    assert "REDACTED" in record2.msg
