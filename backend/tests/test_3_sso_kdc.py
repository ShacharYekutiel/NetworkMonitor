"""[3] Kerberos against a real MIT KDC (tests/sim/kdc/setup_kdc.sh; skipped when krb5kdc or
python-gssapi are missing): the keytab parser on a real ktadd keytab (+ the rc4-only one),
`token_mech` on real SPNEGO / raw krb5 tokens, and the real acceptor `gss_accept` (principal,
mutual-auth token, replay refused, wrong keytab refused). No database needed."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from netmon.auth import kerberos_pure as kp

REPO = Path(__file__).resolve().parents[2]
KDC_SH = REPO / "tests" / "sim" / "kdc" / "setup_kdc.sh"
CLIENT = REPO / "tests" / "sim" / "kdc" / "client_token.py"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or importlib.util.find_spec("gssapi") is None or not KDC_SH.is_file(),
    reason="needs Linux, python-gssapi and tests/sim/kdc",
)


@pytest.fixture(scope="module")
def kdc(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    d = tmp_path_factory.mktemp("kdc")
    r = subprocess.run(["bash", str(KDC_SH), "start", str(d)], capture_output=True, text=True)
    if r.returncode == 3:
        pytest.skip("krb5kdc / kdb5_util not installed")
    assert r.returncode == 0, r.stderr
    yield d
    subprocess.run(["bash", str(KDC_SH), "stop", str(d)], capture_output=True)


def client_token(kdc: Path, user: str, raw: bool = False) -> bytes:
    import base64

    args = [sys.executable, str(CLIENT), "spnego", "--user", user, "--kdc-dir", str(kdc)]
    if raw:
        args.append("--raw-krb5")
    env = dict(os.environ, KRB5_CONFIG=str(kdc / "krb5.conf"))
    r = subprocess.run(args, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return base64.b64decode(r.stdout.strip().split()[-1])


def test_real_keytab_parses(kdc: Path) -> None:
    entries = kp.parse_keytab((kdc / "http.keytab").read_bytes())
    principals = {e.principal for e in entries}
    assert "HTTP/localhost@MNIDOM.TEST" in principals
    assert {18, 17} <= {e.enctype for e in entries}
    assert kp.keytab_warnings(entries) == []
    rc4 = kdc / "rc4.keytab"
    if rc4.is_file():
        assert "rc4_only" in kp.keytab_warnings(kp.parse_keytab(rc4.read_bytes()))


def test_real_acceptor(kdc: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from netmon.auth.kerberos import KerberosFailure, gss_accept

    monkeypatch.setenv("KRB5_CONFIG", str(kdc / "krb5.conf"))
    monkeypatch.setenv("KRB5RCACHEDIR", str(tmp_path))
    tok = client_token(kdc, "jdoe")
    assert kp.token_mech(tok) == "kerberos"
    principal, out = gss_accept(str(kdc / "http.keytab"), tok)
    assert principal == "jdoe@MNIDOM.TEST"
    assert kp.split_principal(principal) == ("jdoe", "MNIDOM.TEST")
    assert out  # mutual authentication token
    with pytest.raises(KerberosFailure):  # replay cache
        gss_accept(str(kdc / "http.keytab"), tok)
    raw = client_token(kdc, "asmith", raw=True)
    assert kp.token_mech(raw) == "kerberos"
    assert gss_accept(str(kdc / "http.keytab"), raw)[0] == "asmith@MNIDOM.TEST"
    rc4 = kdc / "rc4.keytab"
    if rc4.is_file():
        with pytest.raises(KerberosFailure):  # keys of the other keytab do not match
            gss_accept(str(rc4), client_token(kdc, "nogroup"))
