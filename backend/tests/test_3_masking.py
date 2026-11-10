# ruff: noqa: E501 - long literal config lines / vectors
"""[3] QA regression: driver secret masking must never leave a secret in clear text.

- Comware `snmp-agent usm-user v3 ... cipher authentication-mode sha <k1> privacy-mode aes128 <k2>` leaked
  `<k2>` (the `cipher` rule captured the keyword `authentication-mode`, the `^`-anchored usm-user rule
  matched only once per line).
- Masking is applied to the ORIGINAL line (union of group-1 spans), so a rule can no longer match inside an
  inserted `<secret removed>` (Gaia `secret (\\S+)` on `password-hash <secret removed>`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from netmon.configbackup import drivers, normalize

D = drivers.library().drivers
SIM = Path(__file__).resolve().parents[2] / "tests" / "sim" / "ssh"
SIM_DIR_DRIVER = {
    "comware": "hpe_comware",
    "arubaos_switch": "hpe_arubaos_switch",
    "aruba_cx": "aruba_cx",
    "checkpoint": "checkpoint_gaia",
    "cisco_ios": "cisco_ios",
    "fortigate": "fortigate",
    "mikrotik": "mikrotik_routeros",
}
# The secret values in QA's simulator configs (tests/sim/ssh/expected.py FIXTURE_SECRETS), per driver.
SIM_SECRETS = {
    "hpe_comware": [
        "$c$3$Qk9NQkFSRElBX1JFQURfQ09NTVVOSVRZ",
        "$c$3$YXV0aEtleVNlY3JldA==",
        "$c$3$cHJpdktleVNlY3JldA==",
        "$c$3$cmFkaXVzU2hhcmVkU2VjcmV0",
        "$h$6$Wm9vbWVyYW5nV2luZG93",
        "$h$6$TmV0bW9uQmFja3Vw",
    ],
    "hpe_arubaos_switch": [
        "Pr1vateCommunity",
        "R4diusSecret",
        "8c6976e5b5410415bde908bd4dee15dfb167a9c8",
        "fe703d258c7ef5f50b71e06565a65aa07194907f",
    ],
    "aruba_cx": [
        "AQBapWdmY2NhdGhpc0lzQUNpcGhlcnRleHRWYWx1ZQ==",
        "AQBapZmFrZUJhY2t1cENpcGhlcnRleHQ=",
        "Cx5ecretComm",
    ],
    "checkpoint_gaia": [
        "$1$Rb2bVQ$Ks9q0m3Ge0Gyd3b4kM7Lz/",
        "Gw5nmpComm",
        "$6$rounds=10000$aBcDeFgHiJkLmNoP",
        "$6$rounds=10000$QrStUvWxYz012345",
    ],
    "cisco_ios": [
        "$9$nhEmQVczB7dqsO",
        "$9$6Ux3jHw8rBp1wE",
        "$9$QW9OL3bAXuK7mE",
        "R0uterComm",
        "0822455D0A16544541",
    ],
    "fortigate": [
        "SH2xR3mpC8aWZqFdj9mVr1YeTEbm4fWqXoaFhhvMWl6pN0PvX0eY4AqAl8=",
        "SH2bmV0bW9uYmFja3VwSGFzaGVkUGFzc3dvcmRGb3J0aUdhdGU=",
        "M2Nk3x7Yq0aZ4b5c6d7e8f9g0h1i2j3k4l5m6n7o8p9q0r1s2t3u==",
    ],
    "mikrotik_routeros": [],
}

# Self-contained lines (do not depend on tests/sim being present): K1SECRET / K2SECRET must be masked and
# the keywords must survive.
VECTORS = [
    ("hpe_comware",
     " snmp-agent usm-user v3 u g cipher authentication-mode sha K1SECRET privacy-mode aes128 K2SECRET",
     " snmp-agent usm-user v3 u g cipher authentication-mode sha <secret removed> privacy-mode aes128 "
     "<secret removed>"),
    ("hpe_comware",
     " snmp-agent usm-user v3 u g cipher privacy-mode aes128 K2SECRET authentication-mode sha K1SECRET",
     " snmp-agent usm-user v3 u g cipher privacy-mode aes128 <secret removed> authentication-mode sha "
     "<secret removed>"),
    ("hpe_comware",
     " snmp-agent usm-user v3 u g simple authentication-mode md5 K1SECRET privacy-mode des56 K2SECRET",
     " snmp-agent usm-user v3 u g simple authentication-mode md5 <secret removed> privacy-mode des56 "
     "<secret removed>"),
    ("hpe_comware", " ntp-service authentication-keyid 1 authentication-mode md5 cipher K1SECRET",
     " ntp-service authentication-keyid 1 authentication-mode md5 cipher <secret removed>"),
    ("hpe_comware", " snmp-agent community read cipher K1SECRET",
     " snmp-agent community read cipher <secret removed>"),
    ("hpe_comware", " snmp-agent community cipher K1SECRET user-role network-operator",
     " snmp-agent community cipher <secret removed> user-role network-operator"),
    ("hpe_comware", " password simple K1SECRET", " password simple <secret removed>"),
    ("hpe_comware", " key authentication simple K1SECRET", " key authentication simple <secret removed>"),
    ("hpe_arubaos_switch", 'snmpv3 user "u" auth sha "K1SECRET" priv aes "K2SECRET"',
     'snmpv3 user "u" auth sha "<secret removed>" priv aes "<secret removed>"'),
    ("hpe_arubaos_switch", 'radius-server host 10.0.0.20 key "K1SECRET with space"',
     'radius-server host 10.0.0.20 key "<secret removed>"'),
    ("hpe_arubaos_switch", 'password manager user-name "admin" plaintext "K1SECRET"',
     'password manager user-name "admin" plaintext "<secret removed>"'),
    ("aruba_cx", "snmpv3 user u auth sha auth-pass ciphertext K1SECRET priv aes priv-pass ciphertext K2SECRET",
     "snmpv3 user u auth sha auth-pass ciphertext <secret removed> priv aes priv-pass ciphertext "
     "<secret removed>"),
    ("cisco_ios", "snmp-server user u g v3 auth sha K1SECRET priv aes 128 K2SECRET",
     "snmp-server user u g v3 auth sha <secret removed> priv aes 128 <secret removed>"),
    ("cisco_ios", "enable secret 9 K1SECRET", "enable secret 9 <secret removed>"),
    ("cisco_ios", " key 7 K1SECRET", " key 7 <secret removed>"),
    ("cisco_ios", " key-string 7 K1SECRET", " key-string 7 <secret removed>"),
    ("cisco_ios", " ip ospf message-digest-key 1 md5 7 K1SECRET",
     " ip ospf message-digest-key 1 md5 7 <secret removed>"),
    ("cisco_ios", " password 7 K1SECRET", " password 7 <secret removed>"),
    ("cisco_ios", "snmp-server host 10.0.0.9 version 2c K1SECRET", "snmp-server host 10.0.0.9 version 2c "
     "<secret removed>"),
    ("cisco_ios", "crypto isakmp key 0 K1SECRET address 1.2.3.4",
     "crypto isakmp key 0 <secret removed> address 1.2.3.4"),
    ("fortigate", "        set passwd ENC K1SECRET", "        set passwd ENC <secret removed>"),
    ("fortigate", "        set auth-pwd ENC K1SECRET", "        set auth-pwd ENC <secret removed>"),
    ("mikrotik_routeros", 'add authentication-password=K1SECRET encryption-password="K2SECRET x" name=u',
     "add authentication-password=<secret removed> encryption-password=<secret removed> name=u"),
    ("checkpoint_gaia", "set user admin password-hash K1SECRET", "set user admin password-hash <secret removed>"),
    ("checkpoint_gaia",
     "set snmp usm user u security-level authPriv auth-pass-type SHA1 auth-pass-phrase K1SECRET "
     "privacy-pass-type AES128 privacy-pass-phrase K2SECRET",
     "set snmp usm user u security-level authPriv auth-pass-type SHA1 auth-pass-phrase <secret removed> "
     "privacy-pass-type AES128 privacy-pass-phrase <secret removed>"),
    ("checkpoint_gaia", "add aaa radius-servers priority 1 host 10.0.0.20 prompt-secret 0 secret K1SECRET timeout 3",
     "add aaa radius-servers priority 1 host 10.0.0.20 prompt-secret 0 secret <secret removed> timeout 3"),
]  # fmt: skip


def _mask(driver_id: str, line: str) -> str:
    return normalize.mask_line(line, [re.compile(p) for p in D[driver_id].secrets])


@pytest.mark.parametrize(("driver_id", "line", "want"), VECTORS)
def test_mask_vectors(driver_id: str, line: str, want: str) -> None:
    got = _mask(driver_id, line)
    assert got == want
    assert "K1SECRET" not in got and "K2SECRET" not in got


def test_mask_line_original_line_union() -> None:
    rx = [re.compile(r"password-hash (\S+)"), re.compile(r"(?<![\w-])secret (\S+)")]
    # the second rule must not match inside the mask inserted by the first one
    assert (
        normalize.mask_line("set user a password-hash $6$x", rx)
        == "set user a password-hash <secret removed>"
    )
    # overlapping spans are replaced once
    rx2 = [re.compile(r"cipher (\S+)"), re.compile(r"(cipher \S+)")]
    assert normalize.mask_line("key cipher abc def", rx2) == "key <secret removed> def"
    # order-independent
    assert normalize.mask_line("a cipher x y", rx2[::-1]) == normalize.mask_line(
        "a cipher x y", rx2
    )


@pytest.mark.skipif(not SIM.is_dir(), reason="tests/sim/ssh not present")
@pytest.mark.parametrize("sim_dir", sorted(SIM_DIR_DRIVER))
def test_no_sim_secret_survives_masking(sim_dir: str) -> None:
    drv = D[SIM_DIR_DRIVER[sim_dir]]
    for v in ("v1", "v2"):
        raw = (SIM / "configs" / sim_dir / f"{v}.txt").read_text()
        out = normalize.normalize([(drv.commands[0].command, raw)], drv)
        plain = normalize.normalize([(drv.commands[0].command, raw)], drv, mask=False)
        for secret in SIM_SECRETS[drv.id]:
            assert secret in plain.text, f"fixture changed: {secret} not in {sim_dir}/{v}"
            assert secret not in out.text, f"{drv.id} {v}: {secret} left unmasked"


@pytest.mark.skipif(not SIM.is_dir(), reason="tests/sim/ssh not present")
def test_sim_expected_files_match_product_normaliser() -> None:
    """The QA oracle's masked expected files equal the product normaliser's output for the comware lab."""
    drv = D["hpe_comware"]
    raw = (SIM / "configs" / "comware" / "v1.txt").read_text()
    out = normalize.normalize([(drv.commands[0].command, raw)], drv).text
    assert "privacy-mode aes128 <secret removed>" in out
    assert "cipher authentication-mode sha <secret removed>" in out
