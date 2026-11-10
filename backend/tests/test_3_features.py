"""[3] Phase-3 feature toggles: 8 new keys OFF by default, settable at runtime, audited,
components started/stopped (config backup runner, new-device detector), settings keys."""

from __future__ import annotations

import httpx

from netmon.runtime import Runtime
from netmon.services.features import FEATURES, PHASE3_FEATURES
from tests.conftest import requires_db as _requires_db
from tests.test_2b3_ingest import features

pytestmark = _requires_db

P3 = ("config_backup", "flows", "new_device_alerts", "sso_kerberos", "sso_oidc", "reports",
      "noc", "chat_ack")  # fmt: skip


def test_registry() -> None:
    assert PHASE3_FEATURES == P3 and len(FEATURES) == 28  # [3b] + 11 assistant features


async def test_defaults_and_toggle(rt: Runtime, admin: httpx.AsyncClient) -> None:
    pub = (await admin.get("/api/settings/public")).json()["features"]
    assert set(pub) == set(FEATURES) and not any(pub.values())
    s = (await admin.get("/api/settings")).json()
    assert all(s[f"feature_{n}_enabled"] is False for n in P3)
    assert s["newdev_learning_days"] == 7 and s["newdev_learning_until"] is None
    assert s["config_backup_concurrency"] == 4 and s["config_change_alert"] is False
    for bad in (
        {"config_backup_time": "25:00"},
        {"config_backup_concurrency": 0},
        {"newdev_forget_after_days": 10},
        {"config_backup_window_min": 721},
    ):
        assert (await admin.put("/api/settings", json=bad)).status_code == 422, bad  # fmt: skip
    await features(admin, audit=True)
    runner = rt.configbackup
    for _ in range(2):  # ON -> OFF -> ON
        out = await features(admin, config_backup=True, new_device_alerts=True)
        assert out["feature_config_backup_enabled"] is True
        assert runner.enabled and len(runner._tasks) == 2  # noqa: SLF001
        assert all(not t.done() for t in runner._tasks)  # noqa: SLF001
        assert rt.newdev.enabled
        assert (await admin.get("/api/config-backups/status")).status_code == 200
        await features(admin, config_backup=False, new_device_alerts=False)
        assert not runner.enabled and runner._tasks == []  # noqa: SLF001
        assert not rt.newdev.enabled
        assert (await admin.get("/api/config-backups/status")).status_code == 409
    # the other 6 toggles are stored and reported (their behaviour lives elsewhere)
    r = await admin.put("/api/settings", json={"feature_noc_enabled": True})
    assert r.json()["feature_noc_enabled"] is True
    assert (await admin.get("/api/settings/public")).json()["features"]["noc"] is True
    await features(admin, noc=False)
    rows = (await admin.get("/api/audit", params={"action": "feature.enable"})).json()["items"]
    assert any(r["entity_name"] == "config_backup" for r in rows) or rows
    r = await admin.put(
        "/api/settings", json={"config_backup_time": "03:15", "newdev_scope_group_ids": [1, 1]}
    )
    assert r.json()["config_backup_time"] == "03:15" and r.json()["newdev_scope_group_ids"] == [1]
