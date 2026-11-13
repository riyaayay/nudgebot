"""Symmetric encryption for anything sensitive we persist to SQLite
(portal passwords, API tokens). Supports key rotation via MultiFernet.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = logging.getLogger(__name__)


class CredentialCipher:
    """Manages encryption and decryption of sensitive portal credentials.

    Accepts either:
    - A single Fernet key string
    - A comma- or whitespace-separated string of keys (newest first)
    - A list/sequence of key strings (newest first)

    When multiple keys are provided:
    - encrypt() uses the FIRST key (newest primary key).
    - decrypt() attempts decryption with each key in order (supporting old keys).
    """

    def __init__(self, keys: str | list[str]):
        if isinstance(keys, str):
            # Split comma or whitespace separated keys
            raw_keys = [k.strip() for k in keys.replace(",", " ").split() if k.strip()]
        else:
            raw_keys = [k.strip() for k in keys if k and k.strip()]

        if not raw_keys:
            raise ValueError(
                "No encryption key provided. Set ENCRYPTION_KEY or ENCRYPTION_KEYS.\n"
                "Generate a key with:\n"
                '  python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )

        fernet_instances: list[Fernet] = []
        for i, k in enumerate(raw_keys):
            try:
                fernet_instances.append(Fernet(k.encode("utf-8")))
            except Exception as exc:
                raise ValueError(
                    f"Encryption key at index {i} is not a valid Fernet key. "
                    "Generate a valid key using:\n"
                    '  python -c "from cryptography.fernet import Fernet; '
                    'print(Fernet.generate_key().decode())"'
                ) from exc

        self._multi_fernet = MultiFernet(fernet_instances)
        self._key_count = len(fernet_instances)
        logger.debug("CredentialCipher initialized with %d active key(s)", self._key_count)

    @property
    def key_count(self) -> int:
        return self._key_count

    def encrypt(self, plaintext: str) -> str:
        """Encrypt plaintext using the newest primary key."""
        if not plaintext:
            return ""
        return self._multi_fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt ciphertext trying all configured keys in order."""
        if not ciphertext:
            return ""
        try:
            return self._multi_fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "Could not decrypt stored credential. The ciphertext is invalid or "
                "ENCRYPTION_KEY / ENCRYPTION_KEYS does not contain the key used to encrypt it."
            ) from exc

    def rotate(self, ciphertext: str) -> str:
        """Re-encrypt ciphertext with the newest primary key."""
        if not ciphertext:
            return ""
        try:
            return self._multi_fernet.rotate(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Could not rotate stored credential; decryption failed.") from exc
