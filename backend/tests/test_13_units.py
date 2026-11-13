"""[1.3] Pure rules: impact rollup, important ports, LAG degraded, template options, request
validation."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from netmon.ingest.lag_monitor import degraded, lag_states, message
from netmon.ports.importance import (
    DeviceImportance,
    default_interface_impact,
    is_trunk,
    port_reasons,
)
from netmon.schemas import ApplyTemplateRequest, BulkImpactIn, SensorCreate
from netmon.services.status import Inventory, device_status, impact_status
from netmon.templates.library import TemplateError, validate_template
from netmon.templates.selection import Selection, entry_passes


# ------------------------------------------------------------------ impact
@pytest.mark.parametrize(
    ("status", "impact", "out"),
    [
        ("down", "critical", "down"),
        ("unreachable", "critical", "unreachable"),
        ("down", "warning", "warning"),
        ("unreachable", "warning", "warning"),
        ("warning", "warning", "warning"),
        ("up", "warning", "up"),
        ("down", "none", "up"),
        ("warning", "none", "up"),
        ("unreachable", "none", "up"),
        ("paused", "none", "paused"),
        ("unknown", "none", "unknown"),
        ("unknown", "warning", "unknown"),
    ],
)
def test_impact_status(status: str, impact: str, out: str) -> None:
    assert impact_status(status, impact) == out


def test_device_status_with_impacts() -> None:
    assert device_status(True, ["up", "down"], ["critical", "critical"]) == "down"
    assert device_status(True, ["up", "down"], ["critical", "warning"]) == "warning"
    assert device_status(True, ["up", "down"], ["critical", "none"]) == "up"
    assert device_status(True, ["down"], ["none"]) == "up"
    # paused / unknown semantics do not depend on the impact
    assert device_status(True, ["paused", "paused"], ["none", "warning"]) == "paused"
    assert device_status(True, ["unknown"], ["none"]) == "unknown"
    assert device_status(True, ["up", "unknown"], ["critical", "none"]) == "unknown"
    assert device_status(False, ["down"], ["critical"]) == "paused"
    assert device_status(True, [], []) == "unknown"
    assert device_status(True, ["down"]) == "down"  # no impacts = all critical


def test_inventory_rollup_uses_impact_counts_stay_raw() -> None:
    inv = Inventory()
    inv.groups[1] = {"parent_id": None, "snmp_credentials": None}
    inv.devices[10] = {"group_id": 1, "enabled": True, "snmp_credentials": None}
    inv.devices[11] = {"group_id": 1, "enabled": True, "snmp_credentials": None}
    inv.sensors[1] = {"device_id": 10, "status": "up", "impact": "critical"}
    inv.sensors[2] = {"device_id": 10, "status": "down", "impact": "warning"}
    inv.sensors[3] = {"device_id": 11, "status": "up", "impact": "critical"}
    inv.sensors[4] = {"device_id": 11, "status": "down", "impact": "none"}
    inv.compute()
    assert inv.device_status == {10: "warning", 11: "up"}
    assert inv.group_status[1] == "warning"
    assert inv.device_counts[10]["down"] == 1 and inv.group_counts[1]["down"] == 2


# ------------------------------------------------------------------ important ports
def _row(idx: int, **kw: Any) -> dict[str, Any]:
    base = {
        "if_index": idx,
        "name": str(idx),
        "descr": str(idx),
        "kind": "physical",
        "mode": "access",
        "untagged_vlans": [1],
        "tagged_vlans": [],
        "bridge_port": idx,
        "pvid": 1,
        "mac_count": 0,
        "lag_if_index": None,
    }
    return {**base, **kw}


def test_is_trunk() -> None:
    assert is_trunk("trunk", [10, 20], 2)
    assert not is_trunk("trunk", [20], 2)  # hybrid access + voice VLAN
    assert is_trunk("trunk", [20], 1)
    assert not is_trunk("access", [10, 20, 30], 2)
    assert not is_trunk(None, None, 2)


def test_port_reasons() -> None:
    switch_nb = {"kind": "device", "device_class": "switch", "link_id": 1}
    rows = [
        _row(1, mac_count=1),  # client
        _row(2, mode="trunk", tagged_vlans=[20], mac_count=1),  # hybrid: not a trunk
        _row(3, mode="trunk", tagged_vlans=[10, 20, 30]),  # trunk
        _row(4, bridge_port=None, mode=None, untagged_vlans=[], lag_if_index=11),
        _row(5, bridge_port=None, mode=None, untagged_vlans=[], lag_if_index=11),
        _row(6, mac_count=9),  # uplink by MAC count (threshold 5)
        _row(7),  # uplink by neighbour
        _row(11, kind="lag", mode="trunk", tagged_vlans=[10, 20]),
    ]
    nbs = {7: [switch_nb], 5: [{**switch_nb, "link_id": 2}]}
    out = port_reasons(rows, lambda r: nbs.get(r["if_index"], []), 5, 2)
    assert out[1] == [] and out[2] == []
    assert out[3] == ["trunk"]
    # members: LAG + the LAG's uplink (member 5's neighbour) + the LAG's VLANs (trunk)
    assert out[4] == ["uplink", "lag", "trunk"] and out[5] == ["uplink", "lag", "trunk"]
    assert out[11] == ["uplink", "lag", "trunk"]
    assert out[6] == ["uplink"] and out[7] == ["uplink"]
    imp = DeviceImportance(out)
    assert imp.important_set() == {3, 4, 5, 6, 7, 11}
    assert 3 not in imp.important_set(include_trunks=False)
    assert default_interface_impact(imp, 3) == "critical"
    assert default_interface_impact(imp, 1) == "warning"
    assert default_interface_impact(None, 1) == "critical"
    assert default_interface_impact(imp, None) == "critical"
    # trunk_min 1: the hybrid port counts
    assert port_reasons(rows, lambda r: [], 5, 1)[2] == ["trunk"]


# ------------------------------------------------------------------ LAG degraded
def test_lag_states() -> None:
    rows = [
        _row(4, oper_status="up", lag_if_index=11),
        _row(5, oper_status="down", lag_if_index=11),
        _row(11, kind="lag", name="BAGG1", oper_status="up"),
        _row(12, kind="lag", name="BAGG2", oper_status="down"),  # no members
    ]
    assert lag_states(rows) == {11: ("BAGG1", 1, 2)}
    assert lag_states(rows, {5: True}) == {11: ("BAGG1", 2, 2)}
    assert lag_states(rows, {4: False}) == {11: ("BAGG1", 0, 2)}
    assert degraded(1, 2) and not degraded(2, 2) and not degraded(0, 2) and not degraded(0, 0)
    assert message("BAGG1", 1, 2) == "LAG BAGG1 degraded (1 of 2 members up)"


# ------------------------------------------------------------------ template options
MIN = {
    "schema": 1,
    "id": "acme_irf",
    "name": {"en": "ACME"},
    "priority": 30,
    "match": [{"requires_snmp": True}],
}


def _tpl(entry: dict[str, Any]) -> dict[str, Any]:
    return validate_template({**MIN, "entries": [entry]}, "acme_irf")


def test_expect_from_probe_and_table_when() -> None:
    sensor = {
        "kind": "sensor",
        "type": "snmp_custom",
        "name": {"en": "Members"},
        "config": {"oid": "1.3.6.1.4.1.9.1.0", "value_type": "gauge"},
        "probe": True,
        "confidence": "low",
        "expect_from_probe": {"channel": "value", "bound": "down_min"},
    }
    e = _tpl(sensor)["entries"][0]
    assert e["expect_from_probe"] == {"channel": "value", "bound": "down_min"}
    for bad, msg in (
        ({**sensor, "probe": False}, "needs probe"),
        ({**sensor, "expect_from_probe": {"channel": "nope", "bound": "down_min"}}, "channel"),
        ({**sensor, "expect_from_probe": {"channel": "value", "bound": "max"}}, "must be one"),
        ({**sensor, "expect_from_probe": {"channel": "value"}}, "missing key"),
    ):
        with pytest.raises(TemplateError) as exc:
            _tpl(bad)
        assert msg in exc.value.message
    plain = _tpl({**sensor, "expect_from_probe": None})["entries"][0]
    assert "expect_from_probe" not in plain
    table = {
        "kind": "snmp_table",
        "walk_oid": "1.3.6.1.4.1.9.2",
        "name": {"en": "Row {index}"},
        "config": {"value_type": "gauge"},
        "confidence": "low",
    }
    assert "when" not in _tpl(table)["entries"][0]
    w = _tpl({**table, "when": [{"oid": "1.3.6.1.4.1.9.1.0", "not_in": [0, 1]}]})["entries"][0]
    assert w["when"] == [{"oid": "1.3.6.1.4.1.9.1.0", "in": None, "not_in": [0, 1]}]


def test_comware_irf_entries() -> None:
    from netmon.templates.builtin import get_template

    t = get_template("hpe_comware")
    assert t is not None
    irf = [
        e
        for e in t["entries"]
        if str(e.get("walk_oid") or e.get("config", {}).get("oid") or "").startswith(
            "1.3.6.1.4.1.25506.2.91."
        )
    ]
    assert len(irf) == 4
    for e in irf:
        # [1.3.1] verified on the owner's HPE 5510-HI IRF stack (4 members, ring)
        assert e["confidence"] == "high" and "verified on HPE 5510-HI IRF" in e["notes"]
        assert e["when"] == [{"oid": "1.3.6.1.4.1.25506.2.91.1.2.0", "in": None, "not_in": [0, 1]}]
    assert irf[0]["expect_from_probe"] == {"channel": "value", "bound": "down_min"}
    # [1.3.1] role rows are labelled by hh3cStackBoardBelongtoMember (same INDEX), not by
    # entPhysicalName ("Board" on every member) -> "IRF role · Unit 1"; 1.3.0 names are renamed
    assert irf[1]["label_oid"] == "1.3.6.1.4.1.25506.2.91.3.1.2"
    assert irf[1]["label_format"] == {"en": "Unit {label}", "he": "יחידה {label}"}
    assert irf[1]["name"] == {"en": "IRF role · {label}", "he": "תפקיד IRF · {label}"}
    assert irf[1]["rename_from"] == {
        "name": {"en": "IRF role {label}", "he": "תפקיד IRF {label}"},
        "label_oid": "1.3.6.1.2.1.47.1.1.1.1.7",
    }
    assert t["entries"][-1]["kind"] == "snmp_interfaces"  # interfaces stay last


# ------------------------------------------------------------------ requests / selection
def test_requests() -> None:
    assert ApplyTemplateRequest(template_id="x").interfaces == "important"
    assert ApplyTemplateRequest(template_id="x").remove_unselected_interfaces is False
    assert ApplyTemplateRequest(template_id="x", interfaces=[3, 1, 3]).interfaces == [1, 3]
    assert ApplyTemplateRequest(template_id="x", interfaces="none").interfaces == "none"
    for bad in ("some", list(range(1, 1002)), [0]):
        with pytest.raises(ValidationError):
            ApplyTemplateRequest(template_id="x", interfaces=bad)
    assert BulkImpactIn(sensor_ids=[1], impact="none").impact == "none"
    for body in ({"sensor_ids": [], "impact": "none"}, {"sensor_ids": [1], "impact": "high"}):
        with pytest.raises(ValidationError):
            BulkImpactIn(**body)
    with pytest.raises(ValidationError):
        BulkImpactIn(sensor_ids=list(range(1001)), impact="warning")
    assert SensorCreate(device_id=1, type="ping").impact is None


def test_entry_passes_and_selection() -> None:
    entry = {
        "exclude_if_types": [24, 136],
        "include_name_regex": None,
        "exclude_name_regex": "^lo",
    }
    assert entry_passes(entry, {"type": 6, "name": "Gi1/0/1"})
    assert not entry_passes(entry, {"type": 136, "name": "Vlan-interface1"})
    assert not entry_passes(entry, {"type": 6, "name": "lo0"})
    assert not entry_passes({**entry, "include_name_regex": "^Ten"}, {"type": 6, "name": "Gi1"})
    sel = Selection("important")
    assert sel.is_important({"if_index": 9, "type": 161, "name": "Bridge-Aggregation1"}) == (
        True,
        ["lag"],
    )
    assert sel.is_important({"if_index": 1, "type": 6, "name": "Gi1/0/1"}) == (False, [])
    assert sel.legacy()  # no inventory, not a switch: 1.2 behaviour
    sel.switch_like = True
    assert not sel.legacy()
    sel.importance = DeviceImportance({1: ["trunk"]})
    assert sel.is_important({"if_index": 1, "type": 6, "name": "x"}) == (True, ["trunk"])
