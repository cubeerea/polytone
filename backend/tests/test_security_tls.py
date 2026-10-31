"""Outbound transports refuse TLS 1.2 and accept TLS 1.3 (local servers, throwaway CA)."""
from __future__ import annotations

import datetime as dt
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from backend.security import tls


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "polytone test CA")])
    ca = (
        x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                     content_commitment=False, key_encipherment=False, data_encipherment=False,
                                     key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    (d / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (d / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
    return d


MESSAGE = {
    "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
    "content": [{"type": "text", "text": "hello over tls 1.3"}], "stop_reason": "end_turn",
    "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
}


class _Handler(BaseHTTPRequestHandler):
    def _reply(self):
        n = int(self.headers.get("content-length") or 0)
        if n:
            self.rfile.read(n)
        body = json.dumps(MESSAGE).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _reply

    def log_message(self, *a):  # quiet
        pass


def _server(pki, version: ssl.TLSVersion):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ctx.maximum_version = version  # pinned to exactly this version
    ctx.load_cert_chain(pki / "cert.pem", pki / "key.pem")
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.handle_error = lambda *a: None  # handshake failures are expected
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"https://localhost:{srv.server_address[1]}"


@pytest.fixture
def servers(pki):
    s12, u12 = _server(pki, ssl.TLSVersion.TLSv1_2)
    s13, u13 = _server(pki, ssl.TLSVersion.TLSv1_3)
    yield u12, u13, str(pki / "ca.pem")
    for s in (s12, s13):
        s.shutdown()
        s.server_close()


def test_context_settings():
    ctx = tls.tls13_context()
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_httpx_refuses_tls12(servers):
    import httpx

    u12, u13, ca = servers
    with tls.httpx_client(timeout=5, cafile=ca, anthropic_defaults=False) as c:
        with pytest.raises(httpx.ConnectError):
            c.get(u12)
        assert c.get(u13).json()["id"] == "msg_test"


def test_httpx_rejects_untrusted_cert(servers):
    import httpx

    _, u13, _ = servers
    with tls.httpx_client(timeout=5, anthropic_defaults=False) as c:  # default CA store
        with pytest.raises(httpx.ConnectError):
            c.get(u13)


def test_requests_refuses_tls12(servers):
    import requests

    u12, u13, ca = servers
    s = tls.requests_session(ca)
    with pytest.raises(requests.exceptions.SSLError):
        s.get(u12, timeout=5)
    assert s.get(u13, timeout=5).json()["id"] == "msg_test"


def test_httplib2_refuses_tls12(servers):
    u12, u13, ca = servers
    h = tls.httplib2_http(timeout=5, cafile=ca)
    with pytest.raises(ssl.SSLError):
        h.request(u12)
    resp, body = tls.httplib2_http(timeout=5, cafile=ca).request(u13)
    assert resp.status == 200 and json.loads(body)["id"] == "msg_test"


def test_anthropic_client_refuses_tls12(servers, monkeypatch):
    """The real AnthropicLLM construction path (SDK + our httpx client), pointed at local servers."""
    import anthropic

    from backend.llm.client import AnthropicLLM

    u12, u13, ca = servers
    monkeypatch.setattr(tls, "default_cafile", lambda: ca)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", u12)
    llm = AnthropicLLM("test-key", effort=None)
    assert llm._client.max_retries == 0 and float(llm._client.timeout) == 6.5
    with pytest.raises(anthropic.APIConnectionError):
        llm.complete(("stable", "volatile"), "hi")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", u13)
    assert AnthropicLLM("test-key", effort=None).complete(("stable", "volatile"), "hi") == "hello over tls 1.3"


def test_gmail_transport_refuses_tls12(servers):
    pytest.importorskip("google_auth_httplib2")
    from google.oauth2.credentials import Credentials

    from backend.mcp_servers.gmail_outbound import authorized_http

    u12, u13, ca = servers
    creds = Credentials(token="test-token")
    with pytest.raises(ssl.SSLError):
        authorized_http(creds, cafile=ca).request(u12)
    resp, _ = authorized_http(creds, cafile=ca).request(u13)
    assert resp.status == 200
