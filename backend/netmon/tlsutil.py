"""Shared TLS client context for every outbound verified TLS connection.

Corporate networks often run a TLS-inspecting firewall that re-signs HTTPS with a
private root CA. That CA is trusted by the operating system (deployed by the IT
department), but not by certifi's Mozilla bundle, which is what httpx uses by default.
So every outbound client (Telegram, webhooks, HTTPS sensors, SMTP STARTTLS/TLS) uses
:func:`client_ssl_context`, which trusts:

1. The operating system trust store, through ``truststore``: the Windows certificate
   store (including CAs deployed by group policy) on Windows, the Keychain on macOS,
   and the system bundle on Linux (``/etc/ssl/certs``; in the Docker image this
   includes every CA dropped into ``deploy/certs`` at build time).
2. certifi's Mozilla bundle, as before (so a host with an empty system store keeps
   working exactly as it did).
3. Optionally the PEM file(s) named by ``NETMON_EXTRA_CA_FILE`` (``os.pathsep``-separated).
4. Every ``*.crt`` / ``*.pem`` / ``*.cer`` (PEM or DER) in the directory named by
   ``NETMON_EXTRA_CA_DIR`` (default ``/etc/netmon/certs`` when it exists). The compose
   file mounts ``deploy/certs`` there, so a prebuilt image (ghcr.io) trusts the corporate
   CAs without a rebuild.

If ``truststore`` is not installed, a stdlib context with the system default paths
(which honour ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``) plus 2. and 3. is used instead.

The context is built once per process and shared (SSLContext is safe to share between
connections). Call ``client_ssl_context.cache_clear()`` after changing the environment.
"""

from __future__ import annotations

import functools
import logging
import os
import ssl

log = logging.getLogger("netmon.tlsutil")

EXTRA_CA_ENV = "NETMON_EXTRA_CA_FILE"
EXTRA_CA_DIR_ENV = "NETMON_EXTRA_CA_DIR"
DEFAULT_EXTRA_CA_DIR = "/etc/netmon/certs"
CA_SUFFIXES = (".crt", ".pem", ".cer")


def _base_context() -> ssl.SSLContext:
    try:
        import truststore
    except ImportError:  # pragma: no cover - truststore is a hard dependency
        return ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _load_certifi(ctx: ssl.SSLContext) -> None:
    try:
        import certifi
    except ImportError:  # pragma: no cover - certifi comes with httpx
        return
    try:
        ctx.load_verify_locations(cafile=certifi.where())
    except (OSError, ssl.SSLError) as exc:  # pragma: no cover - broken install
        log.warning("Could not load the certifi CA bundle: %s", exc)


def extra_ca_files() -> list[str]:
    raw = os.environ.get(EXTRA_CA_ENV, "")
    return [p.strip() for p in raw.split(os.pathsep) if p.strip()]


def extra_ca_dir() -> str | None:
    path = os.environ.get(EXTRA_CA_DIR_ENV, "").strip() or DEFAULT_EXTRA_CA_DIR
    return path if os.path.isdir(path) else None


def _load_ca_dir(ctx: ssl.SSLContext, directory: str) -> None:
    """Trust every certificate file (PEM, possibly a chain, or DER) in ``directory``."""
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        log.warning("Cannot read %s=%s: %s", EXTRA_CA_DIR_ENV, directory, exc)
        return
    for name in names:
        if not name.lower().endswith(CA_SUFFIXES):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            if b"-----BEGIN CERTIFICATE-----" in data:
                ctx.load_verify_locations(cadata=data.decode("ascii", "ignore"))
            else:
                ctx.load_verify_locations(cadata=data)  # DER
        except (OSError, ssl.SSLError, ValueError) as exc:
            log.warning("Ignoring CA file %s: %s", path, exc)
        else:
            log.info("Trusting extra CA certificates from %s", path)


@functools.cache
def client_ssl_context() -> ssl.SSLContext:
    """Verifying TLS client context (hostname check + chain validation)."""
    ctx = _base_context()
    _load_certifi(ctx)
    for path in extra_ca_files():
        try:
            ctx.load_verify_locations(cafile=path)
        except (OSError, ssl.SSLError) as exc:
            # A broken extra file must not take the whole alerting/polling down.
            log.warning("Ignoring %s=%s: %s", EXTRA_CA_ENV, path, exc)
        else:
            log.info("Trusting extra CA certificates from %s", path)
    ca_dir = extra_ca_dir()
    if ca_dir:
        _load_ca_dir(ctx, ca_dir)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def httpx_verify(verify_tls: bool = True) -> ssl.SSLContext | bool:
    """Value for httpx's ``verify=``: the shared context, or False when disabled."""
    return client_ssl_context() if verify_tls else False
