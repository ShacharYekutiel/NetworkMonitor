"""[2b-2] Pydantic models of the ports / search / template-library responses, exactly as
docs/api.md "Phase 2b-2 contract" (extra keys are rejected: the API cannot drift silently)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

InventoryMode = Literal["auto", "on", "off"]
DeviceClass = Literal[
    "router", "switch", "access_point", "printer", "nas", "server", "workstation", "unknown"
]
Status = Literal["up", "warning", "down", "paused", "unknown", "unreachable"]
IfStatus = Literal["up", "down", "testing", "unknown", "dormant", "notPresent", "lowerLayerDown"]
SourceState = Literal["ok", "empty", "error", "skipped", "unmapped"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortsRefreshResponse(_Out):
    device_id: int
    queued: bool


class PortPoe(_Out):
    admin_enabled: bool | None
    detection: Literal["disabled", "searching", "delivering", "fault", "test", "other_fault"] | None
    power_class: int | None
    power_w: float | None


class PortMac(_Out):
    mac: str
    vlan: int | None
    vendor: str | None
    locally_administered: bool
    ip: str | None
    ips: list[str]
    hostname: str | None
    device_id: int | None
    device_name: str | None
    first_seen: str
    last_seen: str
    via_lag: bool  # [2b-3]


class PortNeighbor(_Out):
    link_id: int
    source: Literal["lldp", "cdp", "manual"]
    kind: Literal["device", "neighbor"]
    device_id: int | None
    neighbor_id: int | None
    name: str
    port: str
    device_class: DeviceClass | None
    status: Status | None
    mgmt_address: str | None


class PortLag(_Out):
    if_index: int
    name: str
    attached: bool | None


class PortConnectedHost(_Out):  # [1.3]
    mac: str
    ip: str | None
    hostname: str | None
    vendor: str | None
    device_id: int | None
    device_name: str | None


class PortConnected(_Out):  # [1.3]
    count: int
    first: PortConnectedHost | None


class Port(_Out):
    if_index: int
    name: str
    descr: str
    alias: str
    kind: Literal["physical", "lag", "vlan", "mgmt", "other"]
    if_type: int | None
    admin_status: IfStatus
    oper_status: IfStatus
    state: Literal["up", "down", "admin_down", "absent"]
    alert: bool
    alert_reasons: list[Literal["sensor", "errors", "poe_fault"]]
    speed_bps: int | None
    duplex: Literal["full", "half", "unknown"] | None
    mac: str | None
    last_change_at: str | None
    pvid: int | None
    mode: Literal["access", "trunk"] | None
    untagged_vlans: list[int]
    tagged_vlans: list[int]
    poe: PortPoe | None
    in_bps: float | None
    out_bps: float | None
    in_errors_delta: int | None
    out_errors_delta: int | None
    is_uplink: bool
    uplink_reason: Literal["neighbor", "mac_count"] | None
    connected_kind: Literal["none", "client", "ap", "switch"]
    trunk: bool
    neighbor: PortNeighbor | None
    mac_count: int
    first_mac: PortMac | None
    macs: list[PortMac]
    sensor_id: int | None
    sensor_status: Status | None
    collected_at: str
    lag: PortLag | None  # [2b-3]
    lag_members: list[int]
    vlans_from_lag: bool
    # [1.3]
    important: bool
    important_reasons: list[Literal["uplink", "lag", "trunk"]]
    connected: PortConnected
    neighbor_name: str | None
    neighbor_mgmt_address: str | None


class PortDetail(Port):
    neighbors: list[PortNeighbor]
    macs_hidden: bool
    sensor: dict[str, Any] | None


class HealthItem(_Out):
    category: Literal["cpu", "memory", "temperature", "poe", "psu", "fan"]
    status: Status
    value: float | None
    unit: str
    text: str
    sensor_ids: list[int]
    primary_sensor_id: int


class PortSources(_Out):
    if_xtable: SourceState
    duplex: SourceState
    bridge: SourceState
    vlans: SourceState
    fdb: SourceState
    poe: SourceState
    arp: SourceState
    lag: SourceState  # [2b-3]


class PortInventoryStatus(_Out):
    last_run_at: str | None
    last_ok_at: str | None
    duration_ms: int | None
    error: str | None
    running: bool
    next_run_at: str | None
    sources: PortSources
    fdb_mib: Literal["q_bridge", "bridge"] | None
    vlan_mib: Literal["static", "current"] | None
    lag_mib: Literal["ieee8023", "if_stack", "both"] | None  # [2b-3]
    ports: int
    macs: int
    arp_entries: int
    arp_sources: int


class PortSettings(_Out):
    interval_min: int
    uplink_mac_threshold: int


class DevicePorts(_Out):
    device_id: int
    ports_effective: bool
    arp_effective: bool
    port_inventory: InventoryMode
    arp_collect: InventoryMode
    effective_class: DeviceClass
    status: PortInventoryStatus
    settings: PortSettings
    health: list[HealthItem]
    ports: list[Port]


class MacLocation(_Out):
    mac: str
    vendor: str | None
    locally_administered: bool
    ip: str | None
    ips: list[str]
    hostname: str | None
    device_id: int
    device_name: str
    device_host: str
    if_index: int
    port: str
    port_alias: str
    vlan: int | None
    is_uplink: bool
    present: bool
    first_seen: str
    last_seen: str
    best: bool
    lag_members: list[str]  # [2b-3]


class ArpHit(_Out):
    ip: str
    mac: str
    hostname: str | None
    source_device_id: int
    source_device_name: str
    present: bool
    first_seen: str
    last_seen: str


class MacQuery(_Out):
    mac: str | None
    mac_prefix: str | None
    ip: str | None


class MacSearchResult(_Out):
    query: MacQuery
    items: list[MacLocation]
    arp: list[ArpHit]


class TemplateLoadError(_Out):
    source: Literal["builtin", "user"]
    file: str
    template_id: str | None
    path: str
    message: str


class TemplateLibraryStatus(_Out):
    loaded_at: str
    builtin_count: int
    user_count: int
    overridden: list[str]
    user_dir: str
    errors: list[TemplateLoadError]
