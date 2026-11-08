"""[3] Kerberos / SPNEGO acceptor (architecture § 13.5.1).

`KerberosManager` (rt.kerberos) materialises the uploaded keytab while `sso_kerberos` is ON
(`${NETMON_DATA_DIR}/run/http.keytab`, 0600, deleted on stop), writes the generated krb5.conf,
points `KRB5_CONFIG` / `KRB5RCACHEDIR` at the run directory **before** `gssapi` is imported, and
accepts SPNEGO tokens with python-gssapi in worker threads (semaphore 8).

`gssapi` is imported lazily: the app runs without the GSSAPI system library; the feature then
reports state `unavailable`. Native Windows: always `unavailable` (an SSPI acceptor is "Later").
Tests replace `acceptor` with a fake (sync callable `(keytab_path, token) -> (principal, out)`).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from netmon.auth import kerberos_pure
from netmon.services import crypto

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.auth.kerberos")

AAD_KEYTAB = b"kerberos.keytab"
MAX_PARALLEL = 8
KEYTAB_NAME = "http.keytab"


class KerberosFailure(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


@dataclass
class AcceptResult:
    principal: str
    out_token: bytes | None


Acceptor = Callable[[str, bytes], tuple[str, bytes | None]]


def gssapi_available() -> tuple[bool, str | None]:
    if sys.platform == "win32":
        return False, "Kerberos SSO needs the Linux/Docker installation"
    try:
        import importlib.util

        if importlib.util.find_spec("gssapi") is None:
            return False, "python-gssapi is not installed (pip install 'netmon[kerberos]')"
    except (ImportError, ValueError):
        return False, "python-gssapi is not installed"
    return True, None


def gss_accept(keytab_path: str, token: bytes) -> tuple[str, bytes | None]:
    """The real acceptor (blocking; runs in a thread). No KDC traffic: the ticket is
    decrypted with the keytab key; the replay cache lives in KRB5RCACHEDIR."""
    import gssapi  # lazy: needs libgssapi_krb5

    try:
        creds = gssapi.Credentials(usage="accept", store={"keytab": keytab_path})
        ctx = gssapi.SecurityContext(creds=creds, usage="accept")
        out = ctx.step(token)
        # SPNEGO keeps a failure as the context's last error (raised on the next access)
        complete = ctx.complete
        principal = str(ctx.initiator_name) if complete else ""
    except Exception as exc:  # noqa: BLE001 - GSSError (bad/expired/replayed ticket, skew, …)
        raise KerberosFailure("sso_failed", str(exc)[:300]) from None
    if not complete or not principal:
        raise KerberosFailure("sso_failed", "the security context needs another leg")
    return principal, (bytes(out) if out else None)


def decrypt_keytab(stored: dict[str, Any], secret_key: str) -> bytes | None:
    enc = stored.get("keytab_enc")
    if not enc:
        return None
    try:
        return base64.b64decode(crypto.decrypt(secret_key, enc, AAD_KEYTAB))
    except (crypto.DecryptError, ValueError):
        return None


def encrypt_keytab(data: bytes, secret_key: str) -> str:
    return crypto.encrypt(secret_key, base64.b64encode(data).decode("ascii"), AAD_KEYTAB)


class KerberosManager:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.state = "disabled"  # ready | unavailable | disabled | error
        self.error: str | None = None
        self.acceptor: Acceptor = gss_accept
        self._sem = asyncio.Semaphore(MAX_PARALLEL)
        self.realms: list[str] = []
        self.ntlm_refused = 0

    @property
    def run_dir(self) -> Path:
        return Path(self.rt.settings.data_dir) / "run"

    @property
    def keytab_path(self) -> Path:
        return self.run_dir / KEYTAB_NAME

    def platform_supported(self) -> bool:
        if self.acceptor is not gss_accept:  # tests: a fake acceptor
            return True
        return gssapi_available()[0]

    async def start(self) -> None:
        """Feature ON (startup or toggle): materialise keytab + krb5.conf."""
        from netmon.services import settings_p3c

        async with self.rt.sessionmaker() as session:
            stored = await settings_p3c.get(session, "kerberos")
            from netmon.services import settings as settings_svc

            ldap = await settings_svc.get(session, "ldap")
        self.realms = [str(r).upper() for r in stored.get("realms") or []] or (
            kerberos_pure.default_realms(ldap.get("domain"))
        )
        if self.acceptor is gss_accept:
            ok, why = gssapi_available()
            if not ok:
                self.state, self.error = "unavailable", why
                return
        data = decrypt_keytab(stored, self.rt.secret_key)
        if data is None:
            self.state = "error"
            self.error = (
                "No keytab uploaded"
                if not stored.get("keytab_enc")
                else "The stored keytab cannot be decrypted (secret.key changed?); upload it again"
            )
            return
        try:
            await asyncio.to_thread(self._write_files, data, self.realms[0] if self.realms else "")
        except OSError as exc:
            self.state, self.error = "error", f"Cannot write the keytab: {exc}"[:300]
            return
        self.state, self.error = "ready", None

    def _write_files(self, keytab: bytes, realm: str) -> None:
        run = self.run_dir
        run.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(run, 0o700)
        conf = run / "krb5.conf"
        conf.write_text(kerberos_pure.krb5_conf(realm), encoding="utf-8")
        path = self.keytab_path
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, keytab)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        # must be set before gssapi (MIT krb5) reads its configuration
        os.environ["KRB5_CONFIG"] = str(conf)
        os.environ["KRB5RCACHEDIR"] = str(run)

    async def stop(self) -> None:
        with contextlib.suppress(OSError):
            self.keytab_path.unlink()
        self.state, self.error = "disabled", None

    async def reload(self) -> None:
        """Settings/keytab changed while ON."""
        from netmon.api.deps import feature_on

        if feature_on(self.rt, "sso_kerberos"):
            await self.start()
        else:
            await self.stop()

    def health_state(self) -> str:
        from netmon.api.deps import feature_on

        if not feature_on(self.rt, "sso_kerberos"):
            return "disabled"
        return self.state

    async def accept(self, token: bytes) -> AcceptResult:
        """Raises KerberosFailure(reason) with reason `sso_ntlm` | `sso_failed`."""
        mech = kerberos_pure.token_mech(token)
        if mech == "ntlm":
            self.ntlm_refused += 1
            raise KerberosFailure("sso_ntlm")
        if mech != "kerberos":
            raise KerberosFailure("sso_failed", "not a Kerberos token")
        if self.state != "ready":
            raise KerberosFailure("sso_failed", f"acceptor {self.state}: {self.error or ''}")
        path = str(self.keytab_path)
        async with self._sem:
            try:
                principal, out = await asyncio.to_thread(self.acceptor, path, token)
            except KerberosFailure:
                raise
            except Exception as exc:  # noqa: BLE001 - any GSS problem is a failed sign-in
                raise KerberosFailure("sso_failed", str(exc)[:300]) from None
        return AcceptResult(principal, out)
