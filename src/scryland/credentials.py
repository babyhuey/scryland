"""Encrypted credential storage for auto-login.

Credentials are encrypted at rest using Fernet symmetric encryption.
The encryption key is derived from a user-provided passphrase using PBKDF2.

Usage:
    scryland credentials set    — prompts for username, password, and passphrase
    scryland credentials clear  — deletes stored credentials

The encrypted credentials are stored in .scryland_credentials (gitignored).
The passphrase is never stored — you enter it once per session if auto-login is needed.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger("scryland")

_CREDENTIALS_FILE = ".scryland_credentials"
_SALT_FILE = ".scryland_salt"


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a Fernet key from a passphrase and salt using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def save_credentials(username: str, password: str, passphrase: str) -> None:
    """Encrypt and save credentials to disk."""
    import os

    salt = os.urandom(16)
    key = _derive_key(passphrase, salt)
    fernet = Fernet(key)

    payload = json.dumps({"username": username, "password": password}).encode()
    encrypted = fernet.encrypt(payload)

    # Save salt
    salt_path = Path(_SALT_FILE)
    salt_path.write_bytes(salt)
    salt_path.chmod(0o600)

    # Save encrypted credentials
    cred_path = Path(_CREDENTIALS_FILE)
    cred_path.write_bytes(encrypted)
    cred_path.chmod(0o600)

    logger.info("Credentials saved (encrypted)")


def load_credentials(passphrase: str) -> tuple[str, str] | None:
    """Load and decrypt credentials from disk.

    Returns (username, password) or None if credentials don't exist or
    passphrase is wrong.
    """
    cred_path = Path(_CREDENTIALS_FILE)
    salt_path = Path(_SALT_FILE)

    if not cred_path.exists() or not salt_path.exists():
        return None

    salt = salt_path.read_bytes()
    key = _derive_key(passphrase, salt)
    fernet = Fernet(key)

    try:
        decrypted = fernet.decrypt(cred_path.read_bytes())
        data = json.loads(decrypted)
        return data["username"], data["password"]
    except InvalidToken:
        logger.warning("Wrong passphrase — could not decrypt credentials")
        return None
    except (json.JSONDecodeError, KeyError):
        logger.warning("Corrupted credentials file")
        return None


def credentials_exist() -> bool:
    """Check if encrypted credentials are stored."""
    return Path(_CREDENTIALS_FILE).exists() and Path(_SALT_FILE).exists()


def clear_credentials() -> None:
    """Delete stored credentials."""
    for path in [Path(_CREDENTIALS_FILE), Path(_SALT_FILE)]:
        if path.exists():
            path.unlink()
    logger.info("Credentials cleared")
