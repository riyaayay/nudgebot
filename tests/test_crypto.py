"""Tests for Fernet and MultiFernet encryption key management and rotation."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from crypto_utils import CredentialCipher


def test_single_key_roundtrip() -> None:
    key = Fernet.generate_key().decode()
    cipher = CredentialCipher(key)
    assert cipher.key_count == 1

    plaintext = "super_secret_canvas_token_12345"
    encrypted = cipher.encrypt(plaintext)
    assert encrypted != plaintext
    decrypted = cipher.decrypt(encrypted)
    assert decrypted == plaintext


def test_multifernet_key_rotation() -> None:
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    # Old cipher with old key only
    old_cipher = CredentialCipher(old_key)
    ciphertext_old = old_cipher.encrypt("my_old_password")

    # New cipher configured with newest key first, then old key fallback
    rotating_cipher = CredentialCipher([new_key, old_key])
    assert rotating_cipher.key_count == 2

    # Should successfully decrypt ciphertext created with old key
    decrypted = rotating_cipher.decrypt(ciphertext_old)
    assert decrypted == "my_old_password"

    # New encryptions should use the new key
    ciphertext_new = rotating_cipher.encrypt("my_new_password")
    # Old cipher cannot decrypt new ciphertext
    with pytest.raises(ValueError, match="Could not decrypt stored credential"):
        old_cipher.decrypt(ciphertext_new)

    # Rotating cipher can decrypt both
    assert rotating_cipher.decrypt(ciphertext_new) == "my_new_password"


def test_invalid_key_raises_helpful_error() -> None:
    with pytest.raises(ValueError, match="is not a valid Fernet key"):
        CredentialCipher("invalid-key-not-base64")

    with pytest.raises(ValueError, match="No encryption key provided"):
        CredentialCipher("")


def test_empty_string_handling() -> None:
    key = Fernet.generate_key().decode()
    cipher = CredentialCipher(key)
    assert cipher.encrypt("") == ""
    assert cipher.decrypt("") == ""
