"""[3] Feature component for SSO (`sso_kerberos`, `sso_oidc`): the Kerberos keytab is
materialised while `sso_kerberos` is ON (deleted on stop); OIDC discovery is fetched lazily,
its cache is dropped when the feature turns OFF."""

from __future__ import annotations

from typing import Any

from netmon.auth.kerberos import KerberosManager
from netmon.auth.oidc import OidcClient


class SsoComponent:
    features = ("sso_kerberos", "sso_oidc")

    def __init__(self, rt: Any) -> None:
        self.rt = rt
        if getattr(rt, "kerberos", None) is None:
            rt.kerberos = KerberosManager(rt)
        if getattr(rt, "oidc", None) is None:
            rt.oidc = OidcClient()

    async def start(self, snapshot: dict[str, bool]) -> None:
        if snapshot.get("sso_kerberos"):
            await self.rt.kerberos.start()
        else:
            await self.rt.kerberos.stop()  # a stale keytab file never outlives the feature

    async def feature_changed(self, name: str, on: bool) -> None:
        if name == "sso_kerberos":
            if on:
                await self.rt.kerberos.start()
            else:
                await self.rt.kerberos.stop()
        elif name == "sso_oidc" and not on:
            self.rt.oidc.reset()

    async def stop(self) -> None:
        await self.rt.kerberos.stop()


def register_components(rt: Any) -> None:
    """Runtime.start(): SSO, report scheduler, Telegram poller, kiosk hub (§ 13.1 order)."""
    from netmon.chatack.telegram_poller import TelegramPollers
    from netmon.kiosk.hub import KioskHub
    from netmon.reports.scheduler import ReportService

    rt.sso = SsoComponent(rt)
    rt.reports = ReportService(rt)
    rt.telegram_pollers = TelegramPollers(rt)
    rt.kiosk = KioskHub(rt)
    for comp in (rt.sso, rt.reports, rt.telegram_pollers, rt.kiosk):
        rt.features.register(comp)
