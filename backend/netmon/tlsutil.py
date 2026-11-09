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


# ---------------------------------------------------------------- [2b-3] PEM trust bundle
_PEM_RE_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_RE_END = "-----END CERTIFICATE-----"


def _pem_blocks(data: bytes) -> list[str]:
    """PEM certificate blocks in ``data``; a DER file becomes one PEM block."""
    if _PEM_RE_BEGIN.encode() in data:
        text = data.decode("ascii", "ignore")
        out = []
        pos = 0
        while True:
            start = text.find(_PEM_RE_BEGIN, pos)
            if start < 0:
                break
            end = text.find(_PEM_RE_END, start)
            if end < 0:
                break
            end += len(_PEM_RE_END)
            out.append(text[start:end])
            pos = end
        return out
    if data[:1] == b"\x30":  # DER SEQUENCE
        try:
            return [ssl.DER_cert_to_PEM_cert(data).strip()]
        except (ValueError, TypeError):
            return []
    return []


def _read_file_blocks(path: str) -> list[str]:
    try:
        with open(path, "rb") as fh:
            return _pem_blocks(fh.read())
    except OSError:
        return []


def _os_store_blocks() -> list[str]:
    blocks: list[str] = []
    enum = getattr(ssl, "enum_certificates", None)  # Windows only
    if enum is not None:
        for store in ("ROOT", "CA"):
            try:
                for cert, encoding, _trust in enum(store):
                    if encoding == "x509_asn":
                        blocks.append(ssl.DER_cert_to_PEM_cert(cert).strip())
            except (OSError, ssl.SSLError, ValueError):  # pragma: no cover - Windows
                continue
        return blocks
    paths = ssl.get_default_verify_paths()
    for cafile in (os.environ.get(paths.openssl_cafile_env), paths.cafile):
        if cafile and os.path.isfile(cafile):
            blocks.extend(_read_file_blocks(cafile))
            break
    capath = os.environ.get(paths.openssl_capath_env) or paths.capath
    if capath and os.path.isdir(capath) and not blocks:
        try:
            names = sorted(os.listdir(capath))
        except OSError:
            names = []
        for name in names:
            full = os.path.join(capath, name)
            if os.path.isfile(full):
                blocks.extend(_read_file_blocks(full))
    return blocks


def _certifi_blocks() -> list[str]:
    try:
        import certifi
    except ImportError:  # pragma: no cover
        return []
    return _read_file_blocks(certifi.where())


def _extra_blocks() -> list[str]:
    blocks: list[str] = []
    for path in extra_ca_files():
        got = _read_file_blocks(path)
        if not got:
            log.warning("Ignoring %s=%s: no certificate found", EXTRA_CA_ENV, path)
        blocks.extend(got)
    ca_dir = extra_ca_dir()
    if ca_dir:
        try:
            names = sorted(os.listdir(ca_dir))
        except OSError:
            names = []
        for name in names:
            if name.lower().endswith(CA_SUFFIXES):
                blocks.extend(_read_file_blocks(os.path.join(ca_dir, name)))
    return blocks


@functools.cache
def trust_bundle_pem() -> str:
    """One PEM bundle with the same trust set as :func:`client_ssl_context`: the OS store
    (Windows ROOT/CA stores, else the default verify file/dir), certifi and the extra CAs
    (``NETMON_EXTRA_CA_FILE`` / ``NETMON_EXTRA_CA_DIR``). Libraries that take CA data and
    *replace* the default store (ldap3 ``Tls(ca_certs_data=…)``) use this. Duplicates are
    removed. Call ``trust_bundle_pem.cache_clear()`` after changing the environment."""
    seen: set[str] = set()
    out: list[str] = []
    for block in (*_os_store_blocks(), *_certifi_blocks(), *_extra_blocks()):
        key = "".join(block.split())
        if key in seen:
            continue
        seen.add(key)
        out.append(block)
    return "\n".join(out) + ("\n" if out else "")
