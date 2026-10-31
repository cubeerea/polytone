"""TLS 1.3-only transports for everything that leaves the machine. See CONTRACTS.md → Network.

- ``tls13_context()`` — ``ssl.SSLContext`` with ``minimum_version = TLSv1_3``, certificate and
  hostname verification on, trusting the certifi CA bundle (what httpx/requests use by default).
- ``httpx_client()`` — for the Anthropic SDK (``anthropic.DefaultHttpxClient`` keeps the SDK's
  connection limits; the SDK's own timeout / retry settings still apply).
- ``requests_session()`` — for google-auth token refresh and the OAuth flow (an ``HTTPAdapter``
  that hands the context to urllib3).
- ``httplib2_http()`` — for googleapiclient (``httplib2.Http(tls_minimum_version=TLSv1_3)``).

``cafile`` exists for tests (a local server with a throwaway CA); production passes nothing.
"""
from __future__ import annotations

import ssl
from typing import Any

MIN_VERSION = ssl.TLSVersion.TLSv1_3


def default_cafile() -> str | None:
    try:
        import certifi

        return certifi.where()
    except ImportError:  # pragma: no cover - certifi ships with httpx/requests
        return None


def tls13_context(cafile: str | None = None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=cafile or default_cafile())
    ctx.minimum_version = MIN_VERSION
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def httpx_client(
    *, timeout: Any = None, cafile: str | None = None, anthropic_defaults: bool = True,
    keepalive_expiry: float | None = None,
) -> Any:
    """``keepalive_expiry``: seconds an idle pooled connection is kept (httpx default: 5)."""
    ctx = tls13_context(cafile)
    kwargs: dict[str, Any] = {"verify": ctx}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if keepalive_expiry is not None:
        kwargs["limits"] = _limits(keepalive_expiry)
    if anthropic_defaults:
        try:
            import anthropic

            return anthropic.DefaultHttpxClient(**kwargs)
        except (ImportError, AttributeError):  # pragma: no cover
            pass
    import httpx

    return httpx.Client(**kwargs)


def _limits(keepalive_expiry: float) -> Any:
    """The Anthropic SDK's default pool limits with a longer idle keep-alive."""
    try:
        from anthropic._constants import DEFAULT_CONNECTION_LIMITS as d
    except ImportError:  # pragma: no cover
        d = None
    try:
        import httpx2 as hx
    except ImportError:  # pragma: no cover - older SDKs use httpx
        import httpx as hx
    if d is None:
        return hx.Limits(max_connections=1000, max_keepalive_connections=100, keepalive_expiry=keepalive_expiry)
    return hx.Limits(max_connections=d.max_connections, max_keepalive_connections=d.max_keepalive_connections,
                     keepalive_expiry=keepalive_expiry)


def _tls_adapter_cls() -> Any:
    from requests.adapters import HTTPAdapter

    class TLS13Adapter(HTTPAdapter):
        def __init__(self, ctx: ssl.SSLContext, **kw: Any) -> None:
            self._ctx = ctx
            super().__init__(**kw)

        def init_poolmanager(self, *args: Any, **kwargs: Any) -> Any:
            kwargs["ssl_context"] = self._ctx
            return super().init_poolmanager(*args, **kwargs)

        def proxy_manager_for(self, *args: Any, **kwargs: Any) -> Any:
            kwargs["ssl_context"] = self._ctx
            return super().proxy_manager_for(*args, **kwargs)

        def build_connection_pool_key_attributes(self, request: Any, verify: Any, cert: Any = None) -> Any:
            # some requests versions put their own default context in the pool key; always use ours
            host, pool = super().build_connection_pool_key_attributes(request, verify, cert)
            pool["ssl_context"] = self._ctx
            pool["cert_reqs"] = "CERT_REQUIRED"
            return host, pool

    return TLS13Adapter


def mount_tls13(session: Any, cafile: str | None = None) -> Any:
    """Route every https:// request of a ``requests.Session`` through a TLS 1.3-only context."""
    session.mount("https://", _tls_adapter_cls()(tls13_context(cafile)))
    return session


def requests_session(cafile: str | None = None) -> Any:
    import requests

    return mount_tls13(requests.Session(), cafile)


def httplib2_http(*, timeout: float | None = 30, cafile: str | None = None) -> Any:
    import httplib2

    return httplib2.Http(timeout=timeout, ca_certs=cafile or default_cafile(), tls_minimum_version=MIN_VERSION)
