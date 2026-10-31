"""AES-256-GCM field encryption + blind index (backend/security/crypto.py)."""
import base64

import pytest

from backend.security import crypto, keys


def test_roundtrip_and_format():
    tok = crypto.encrypt("hey — i don't think so", "drafts.text")
    assert tok.startswith("enc1:") and "think" not in tok
    raw = base64.urlsafe_b64decode(tok[5:] + "=" * (-len(tok[5:]) % 4))
    assert len(raw) == 12 + len("hey — i don't think so".encode()) + 16  # nonce || ct || tag
    crypto.reset_cache()  # force a real decrypt, not the memo
    assert crypto.decrypt(tok, "drafts.text") == "hey — i don't think so"


def test_random_nonce():
    assert crypto.encrypt("same", "samples.text") != crypto.encrypt("same", "samples.text")


def test_wrong_context_raises():
    tok = crypto.encrypt("secret words", "samples.text")
    crypto.reset_cache()
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(tok, "drafts.text")


def test_tampered_raises():
    tok = crypto.encrypt("secret words", "samples.text")
    crypto.reset_cache()
    body = bytearray(base64.urlsafe_b64decode(tok[5:] + "=" * (-len(tok[5:]) % 4)))
    body[-1] ^= 1
    bad = "enc1:" + base64.urlsafe_b64encode(bytes(body)).rstrip(b"=").decode()
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(bad, "samples.text")
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt("enc1:!!!", "samples.text")
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt("plain text", "samples.text")


def test_wrong_key_raises(monkeypatch):
    tok = crypto.encrypt("secret words", "samples.text")
    monkeypatch.setenv(keys.ENV_KEY, base64.b64encode(b"k" * 32).decode())
    crypto.reset_cache()
    try:
        with pytest.raises(crypto.DecryptError):
            crypto.decrypt(tok, "samples.text")
    finally:
        monkeypatch.undo()
        crypto.reset_cache()
    assert crypto.decrypt(tok, "samples.text") == "secret words"


def test_blind_index():
    a = crypto.blind_index("hello", "neo4j:Word.text")
    assert a == crypto.blind_index("hello", "neo4j:Word.text")
    assert a.startswith("idx1:") and len(a) == 5 + 32
    assert a != crypto.blind_index("hello", "content.text")  # context separates indexes
    assert a != crypto.blind_index("hellO", "neo4j:Word.text")
    # not a plain hash: changing the key changes the index
    import hashlib

    assert hashlib.sha256(b"hello").hexdigest()[:32] not in a


def test_json_helpers():
    tok = crypto.encrypt_json({"lexicon": ["deck"]}, "drafts.constraints_json")
    assert crypto.decrypt_json(tok, "drafts.constraints_json") == {"lexicon": ["deck"]}
    assert crypto.decrypt_json(None, "x") is None
