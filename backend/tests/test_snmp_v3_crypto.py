"""SNMPv3 privacy (authPriv) requires a cipher backend.

pysnmp 7 performs AES/DES encryption through the ``cryptography`` package. When it is not
installed, pysnmp sets ``PysnmpCryptoError`` in every privacy module and raises
``EncryptionError("Ciphering services not available")`` the moment an authPriv query runs.
That shipped once in the container image (cryptography was not a declared dependency) and
broke every SNMPv3 device with encryption, so these tests fail loudly if it regresses.

They import the privacy modules in the dependency-safe order (importing a leaf priv module
first triggers a circular-import error inside pysnmp itself) and assert the backend loaded.
"""

from __future__ import annotations

import asyncio
import importlib
import os

import pytest


def test_cryptography_is_importable() -> None:
    # The exact dependency pysnmp's privacy modules import.
    crypto = importlib.import_module("cryptography")
    assert crypto.__version__
    # The specific symbols pysnmp 7's AES/DES modules pull in.
    importlib.import_module("cryptography.hazmat.decrepit.ciphers.modes")
    importlib.import_module("cryptography.hazmat.primitives.ciphers")


def test_snmp_privacy_backends_available() -> None:
    # Import the USM service first so the priv submodules initialise without a circular
    # import, mirroring how pysnmp loads them at runtime.
    importlib.import_module("pysnmp.proto.secmod.rfc3414.service")

    modules = {
        "AES128": "pysnmp.proto.secmod.rfc3826.priv.aes",
        "AES192": "pysnmp.proto.secmod.eso.priv.aes192",
        "AES256": "pysnmp.proto.secmod.eso.priv.aes256",
        "DES": "pysnmp.proto.secmod.rfc3414.priv.des",
        "3DES": "pysnmp.proto.secmod.eso.priv.des3",
    }
    broken = []
    for label, name in modules.items():
        mod = importlib.import_module(name)
        # Each privacy module sets this truthy when the cipher backend failed to import.
        if getattr(mod, "PysnmpCryptoError", False):
            broken.append(label)
    assert not broken, (
        f"SNMPv3 privacy unavailable for {broken}: the 'cryptography' package is missing. "
        "authPriv devices would fail with 'Ciphering services not available'."
    )


def test_app_builds_authpriv_user_data() -> None:
    """The app's credential builder produces a privacy-enabled USM user for authPriv."""
    from netmon.poller import snmp_client

    creds = {
        "version": "v3",
        "v3_username": "netmonv3",
        "v3_security_level": "authPriv",
        "v3_auth_protocol": "SHA",
        "v3_auth_key": "authpass123",
        "v3_priv_protocol": "AES128",
        "v3_priv_key": "privpass123",
    }
    user = snmp_client._auth_data(creds)
    # A privacy protocol is set (not the "no privacy" sentinel).
    assert user.privProtocol and tuple(user.privProtocol) != (1, 3, 6, 1, 6, 3, 10, 1, 2, 1)


# ------------------------------------------------------------------ end to end (snmpsim)
_NOBODY_PROBE: str | None = None


def _agent_cannot_read_interpreter() -> str | None:
    """As root the snmpsim agent drops to ``nobody`` and then imports pysnmp's privacy modules
    lazily (first authPriv request). In a checkout under a private directory (e.g. a worktree
    under ``/tmp/claude-0``, mode 700) ``nobody`` cannot read the interpreter / venv, so the
    agent never answers. Detect it by running the same import as ``nobody``; returns the skip
    reason, or None when the agent can work (normal checkouts, CI, non-root runs)."""
    global _NOBODY_PROBE
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        return None
    if _NOBODY_PROBE is None:
        import subprocess
        import sys

        from tests.snmpsim_agents import snmpsim_exe

        exe = snmpsim_exe() or sys.executable
        code = "import pysnmp.proto.secmod.rfc3414.service, pysnmp.proto.secmod.rfc3826.priv.aes"
        try:
            r = subprocess.run(  # noqa: S603 - fixed argv
                [sys.executable, "-c", code],
                user="nobody",
                capture_output=True,
                timeout=60,
                check=False,
            )
            ok = r.returncode == 0
            detail = (r.stderr or b"").decode(errors="replace").strip().splitlines()[-1:] or [""]
        except (OSError, subprocess.SubprocessError) as exc:
            ok, detail = False, [str(exc)]
        _NOBODY_PROBE = (
            ""
            if ok
            else (
                f"the snmpsim agent runs as 'nobody' and cannot read the interpreter "
                f"{sys.executable} / {exe} (checkout in a private directory?): {detail[0]}"
            )
        )
    return _NOBODY_PROBE or None


def _skip_if_agent_cannot_read() -> None:
    reason = _agent_cannot_read_interpreter()
    if reason:
        pytest.skip(reason)


def _start_v3_agent(  # type: ignore[no-untyped-def]
    ip: str,
    port: int,
    auth_proto: str,
    priv_proto: str,
    auth_key: str = "NetmonAuth2026",
    priv_key: str = "NetmonPriv2026",
):
    """snmpsim agent with one authPriv USM user. snmpsim picks the data file by SNMPv3
    context name; with the empty context real managers send, the "self" file is used."""
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    from tests.snmpsim_agents import snmpsim_exe, system, write_snmprec

    exe = snmpsim_exe()
    if exe is None:
        return None, None
    data = Path(tempfile.mkdtemp(prefix="netmon-v3sim-"))
    data.chmod(0o755)
    write_snmprec(
        data / "self.snmprec", system("HPE v3 test", "1.3.6.1.4.1.11.2.3.7.11.160", f"sw-{ip}")
    )
    cache = data / "cache"
    cache.mkdir()
    args = [
        exe, f"--data-dir={data}", f"--cache-dir={cache}", f"--agent-udpv4-endpoint={ip}:{port}",
        "--v3-user=netmon", f"--v3-auth-proto={auth_proto}", f"--v3-auth-key={auth_key}",
        f"--v3-priv-proto={priv_proto}", f"--v3-priv-key={priv_key}",
    ]  # fmt: skip
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        shutil.chown(cache, "nobody")
        args += ["--process-user=nobody", "--process-group=nogroup"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, data


@pytest.mark.parametrize(
    ("auth", "priv", "app_auth", "app_priv"),
    [
        ("SHA", "AES", "SHA", "AES128"),
        ("MD5", "DES", "MD5", "DES"),
        ("SHA256", "AES256", "SHA256", "AES256"),
    ],
)
async def test_v3_authpriv_get_against_agent(
    auth: str, priv: str, app_auth: str, app_priv: str
) -> None:
    """A real encrypted SNMPv3 GET through the app's client (the path that failed with
    'Ciphering services not available' in the container image)."""
    import shutil

    from netmon.poller.snmp_client import SnmpError, snmp_get

    ip, port = "127.0.4.10", 1161
    _skip_if_agent_cannot_read()
    proc, data = _start_v3_agent(ip, port, auth, priv)
    if proc is None:
        pytest.skip("snmpsim not installed")
    creds = {
        "version": "v3", "port": port, "timeout_s": 2, "retries": 1,
        "v3_username": "netmon", "v3_security_level": "authPriv",
        "v3_auth_protocol": app_auth, "v3_auth_key": "NetmonAuth2026",
        "v3_priv_protocol": app_priv, "v3_priv_key": "NetmonPriv2026",
    }  # fmt: skip
    try:
        result = None
        for _ in range(40):  # agent start-up
            try:
                result = await snmp_get(ip, creds, ["1.3.6.1.2.1.1.5.0"])
                break
            except SnmpError:
                if proc.poll() is not None:
                    pytest.fail("snmpsim agent exited")
                await asyncio.sleep(0.5)
        assert result is not None, "no SNMPv3 response"
        assert result["1.3.6.1.2.1.1.5.0"] in (f"sw-{ip}", f"sw-{ip}".encode())

        wrong = {**creds, "v3_auth_key": "WrongAuthKey99"}
        with pytest.raises(SnmpError):
            await snmp_get(ip, wrong, ["1.3.6.1.2.1.1.5.0"])
    finally:
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(data, ignore_errors=True)


async def _wait_up(ip: str, creds: dict, proc) -> None:  # type: ignore[no-untyped-def]
    from netmon.poller.snmp_client import SnmpError, snmp_get

    for _ in range(40):
        try:
            await snmp_get(ip, creds, ["1.3.6.1.2.1.1.5.0"])
            return
        except SnmpError:
            if proc.poll() is not None:
                pytest.fail("snmpsim agent exited")
            await asyncio.sleep(0.5)
    pytest.fail(f"no SNMPv3 response from {ip}")


async def test_v3_same_username_different_keys_do_not_clash() -> None:
    """Regression: pysnmp keys SNMPv3 users by name inside an engine. With one shared
    engine, a device with its own "netmon" credentials and devices inheriting a group's
    "netmon" credentials (other keys) overwrote each other, so concurrent polls failed
    with "Wrong SNMP PDU digest" or timeouts. Each credential set now has its own engine."""
    import shutil

    from netmon.poller.snmp_client import snmp_get

    def v3(auth_key: str, priv_key: str) -> dict:
        return {
            "version": "v3", "port": 1161, "timeout_s": 2, "retries": 1,
            "v3_username": "netmon", "v3_security_level": "authPriv",
            "v3_auth_protocol": "SHA", "v3_auth_key": auth_key,
            "v3_priv_protocol": "AES128", "v3_priv_key": priv_key,
        }  # fmt: skip

    _skip_if_agent_cannot_read()
    a_ip, b_ip = "127.0.4.21", "127.0.4.22"
    a_creds, b_creds = v3("GroupAuth2026", "GroupPriv2026"), v3("DeviceAuth2026", "DevicePriv2026")
    a_proc, a_data = _start_v3_agent(a_ip, 1161, "SHA", "AES", "GroupAuth2026", "GroupPriv2026")
    if a_proc is None:
        pytest.skip("snmpsim not installed")
    b_proc, b_data = _start_v3_agent(b_ip, 1161, "SHA", "AES", "DeviceAuth2026", "DevicePriv2026")
    try:
        await _wait_up(a_ip, a_creds, a_proc)
        await _wait_up(b_ip, b_creds, b_proc)
        for _ in range(3):  # several polling cycles, both devices at once
            ra, rb = await asyncio.gather(
                snmp_get(a_ip, dict(a_creds), ["1.3.6.1.2.1.1.5.0"]),
                snmp_get(b_ip, dict(b_creds), ["1.3.6.1.2.1.1.5.0"]),
            )
            assert ra["1.3.6.1.2.1.1.5.0"] in (f"sw-{a_ip}", f"sw-{a_ip}".encode())
            assert rb["1.3.6.1.2.1.1.5.0"] in (f"sw-{b_ip}", f"sw-{b_ip}".encode())
    finally:
        for proc, data in ((a_proc, a_data), (b_proc, b_data)):
            proc.terminate()
            proc.wait(10)
            shutil.rmtree(data, ignore_errors=True)
