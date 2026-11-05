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

import importlib


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
