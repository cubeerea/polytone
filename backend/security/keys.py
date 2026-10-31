"""Master key and per-purpose subkeys. See CONTRACTS.md → Security.

The 256-bit master key lives in the macOS Keychain (service ``com.polytone.app``, account
``data-key-v1``) and is created on first use. ``POLYTONE_DATA_KEY`` (base64 of 32 bytes) overrides
it: tests and CI set it so they never touch the real Keychain.

Subkeys are HKDF-SHA256(master, info=b"polytone/<purpose>/v1"). Purposes in use:
``enc`` (AES-256-GCM content key), ``index`` (HMAC blind index for lookups), ``audit`` (audit chain).
"""
import base64
import binascii
import hashlib
import hmac
import os
import secrets
import threading
from functools import lru_cache

KEYCHAIN_SERVICE = "com.polytone.app"
KEYCHAIN_ACCOUNT = "data-key-v1"
ENV_KEY = "POLYTONE_DATA_KEY"
KEY_BYTES = 32

_lock = threading.Lock()


class KeyUnavailable(RuntimeError):
    """The master key can't be read or created (no Keychain and no POLYTONE_DATA_KEY)."""


def _decode(value: str) -> bytes:
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as e:
        raise KeyUnavailable(f"{ENV_KEY} is not valid base64") from e
    if len(raw) != KEY_BYTES:
        raise KeyUnavailable(f"data key must be {KEY_BYTES} bytes, got {len(raw)}")
    return raw


def _keychain_get_or_create() -> bytes:
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError as e:  # pragma: no cover - keyring is a hard dependency
        raise KeyUnavailable("keyring is not installed") from e
    try:
        stored = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        if stored:
            return _decode(stored)
        fresh = base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode()
        keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, fresh)
        # Read back: a Keychain that silently drops writes would otherwise lose every record.
        if keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT) != fresh:
            raise KeyUnavailable("Keychain did not keep the new data key")
        return _decode(fresh)
    except KeyringError as e:
        raise KeyUnavailable(f"Keychain unavailable: {e}") from e


@lru_cache(maxsize=1)
def master_key() -> bytes:
    with _lock:
        env = os.environ.get(ENV_KEY)
        return _decode(env) if env else _keychain_get_or_create()


def _hkdf(key: bytes, info: bytes, length: int = KEY_BYTES) -> bytes:
    # RFC 5869 with an all-zero salt; one block suffices for 32 bytes.
    prk = hmac.new(b"\x00" * hashlib.sha256().digest_size, key, hashlib.sha256).digest()
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


@lru_cache(maxsize=None)
def subkey(purpose: str) -> bytes:
    return _hkdf(master_key(), f"polytone/{purpose}/v1".encode())


def reset_cache() -> None:
    """Forget cached keys (tests that switch POLYTONE_DATA_KEY)."""
    master_key.cache_clear()
    subkey.cache_clear()
