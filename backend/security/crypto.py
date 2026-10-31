"""Field-level encryption at rest and blind indexes. See CONTRACTS.md → Security.

- ``encrypt(text, context)`` → ``enc1:<base64url(nonce || ciphertext || tag)>``: AES-256-GCM with
  ``subkey("enc")``, a random 96-bit nonce and the context string as AAD, so a value copied to
  another column/property (or row kind) fails to decrypt instead of being misread.
- ``decrypt(token, context)`` → the plaintext, or raises ``DecryptError`` (never garbage).
- ``blind_index(value, context)`` → ``idx1:<base64url(HMAC-SHA256(subkey("index"),
  context || 0x00 || value))[:32]>``: deterministic, so it can be looked up / made unique, but
  not reversible or guessable without the key (a plain sha256 of a short text is).

Decryption is memoized (bounded LRU keyed by the token): the hot path (``/generate``, the
firewall draft index, few-shot selection) reads the same rows over and over, and the decrypted
values live only in this process's memory — the threat model protects data at rest.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import threading
from collections import OrderedDict
from functools import lru_cache
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.security.keys import subkey

ENC_PREFIX = "enc1:"
IDX_PREFIX = "idx1:"
NONCE_BYTES = 12
IDX_CHARS = 32

_DECRYPT_CACHE_SIZE = 50_000
_dcache: OrderedDict[tuple[str, str], str] = OrderedDict()
_dlock = threading.Lock()


class DecryptError(ValueError):
    """Ciphertext is malformed, was made with another key, or belongs to another context."""


@lru_cache(maxsize=1)
def _aead() -> AESGCM:
    return AESGCM(subkey("enc"))


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def is_index(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(IDX_PREFIX)


def encrypt(plaintext: str, context: str) -> str:
    nonce = os.urandom(NONCE_BYTES)
    ct = _aead().encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
    token = ENC_PREFIX + _b64e(nonce + ct)
    _remember(token, context, plaintext)
    return token


def _remember(token: str, context: str, plaintext: str) -> None:
    with _dlock:
        _dcache[(token, context)] = plaintext
        if len(_dcache) > _DECRYPT_CACHE_SIZE:
            _dcache.popitem(last=False)


def decrypt(token: str, context: str) -> str:
    key = (token, context)
    with _dlock:
        hit = _dcache.get(key)
        if hit is not None:
            _dcache.move_to_end(key)
            return hit
    if not is_encrypted(token):
        raise DecryptError(f"not an {ENC_PREFIX} value ({context})")
    try:
        raw = _b64d(token[len(ENC_PREFIX):])
        if len(raw) < NONCE_BYTES + 16:
            raise DecryptError(f"ciphertext too short ({context})")
        plain = _aead().decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], context.encode("utf-8")).decode("utf-8")
    except (InvalidTag, binascii.Error, ValueError, UnicodeDecodeError) as e:
        if isinstance(e, DecryptError):
            raise
        raise DecryptError(f"cannot decrypt {context} (wrong key, wrong context or tampered)") from e
    _remember(token, context, plain)
    return plain


def encrypt_opt(value: str | None, context: str) -> str | None:
    return None if value is None else encrypt(value, context)


def decrypt_opt(value: str | None, context: str) -> str | None:
    return None if value is None else decrypt(value, context)


def encrypt_json(obj: Any, context: str) -> str:
    return encrypt(json.dumps(obj), context)


def decrypt_json(token: str | None, context: str) -> Any:
    if not token:
        return None
    return json.loads(decrypt(token, context))


@lru_cache(maxsize=1)
def _index_key() -> bytes:
    return subkey("index")


@lru_cache(maxsize=200_000)
def blind_index(value: str, context: str) -> str:
    mac = hmac.new(_index_key(), context.encode("utf-8") + b"\x00" + value.encode("utf-8"), hashlib.sha256)
    return IDX_PREFIX + _b64e(mac.digest())[:IDX_CHARS]


def reset_cache() -> None:
    """Forget derived keys and memoized plaintexts (tests that switch POLYTONE_DATA_KEY)."""
    from backend.security import keys

    keys.reset_cache()
    _aead.cache_clear()
    _index_key.cache_clear()
    blind_index.cache_clear()
    with _dlock:
        _dcache.clear()
