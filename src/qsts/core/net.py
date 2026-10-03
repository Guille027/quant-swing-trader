"""Outbound HTTPS helpers.

Certificate verification is never disabled. On Windows/macOS we verify against the OPERATING SYSTEM
trust store (via `truststore`), so corporate proxies or antivirus HTTPS inspection whose root
certificate is installed in Windows are accepted exactly as the browser accepts them.
"""
from __future__ import annotations

import ssl
import urllib.request

try:
    import truststore
except ImportError:  # pragma: no cover
    truststore = None


def ssl_context() -> ssl.SSLContext:
    if truststore is not None:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ssl.create_default_context()


def urlopen(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())
