"""[2b-3] Built-in OID name table (pure, longest-prefix match; data-model "OID name table").

`name(oid)` returns the name of the longest known prefix with the remainder appended
(`1.3.6.1.2.1.2.2.1.1.5` -> `ifIndex.5`), or None when no prefix is known."""

from __future__ import annotations

NAMES: dict[str, str] = {
    # SNMPv2-MIB notifications
    "1.3.6.1.6.3.1.1.5.1": "coldStart",
    "1.3.6.1.6.3.1.1.5.2": "warmStart",
    "1.3.6.1.6.3.1.1.5.3": "linkDown",
    "1.3.6.1.6.3.1.1.5.4": "linkUp",
    "1.3.6.1.6.3.1.1.5.5": "authenticationFailure",
    "1.3.6.1.6.3.1.1.5.6": "egpNeighborLoss",
    "1.3.6.1.2.1.1.3.0": "sysUpTime.0",
    "1.3.6.1.6.3.1.1.4.1.0": "snmpTrapOID.0",
    "1.3.6.1.6.3.1.1.4.3.0": "snmpTrapEnterprise.0",
    "1.3.6.1.6.3.18.1.3.0": "snmpTrapAddress.0",
    "1.3.6.1.6.3.18.1.4.0": "snmpTrapCommunity.0",
    "1.3.6.1.2.1.1.1.0": "sysDescr.0",
    "1.3.6.1.2.1.1.5.0": "sysName.0",
    # IF-MIB
    "1.3.6.1.2.1.2.2.1.1": "ifIndex",
    "1.3.6.1.2.1.2.2.1.2": "ifDescr",
    "1.3.6.1.2.1.2.2.1.3": "ifType",
    "1.3.6.1.2.1.2.2.1.7": "ifAdminStatus",
    "1.3.6.1.2.1.2.2.1.8": "ifOperStatus",
    "1.3.6.1.2.1.31.1.1.1.1": "ifName",
    "1.3.6.1.2.1.31.1.1.1.18": "ifAlias",
    # BRIDGE-MIB
    "1.3.6.1.2.1.17.0.1": "newRoot",
    "1.3.6.1.2.1.17.0.2": "topologyChange",
    # ENTITY-MIB
    "1.3.6.1.2.1.47.2.0.1": "entConfigChange",
    # LLDP-MIB
    "1.0.8802.1.1.2.0.0.1": "lldpRemTablesChange",
    # POWER-ETHERNET-MIB
    "1.3.6.1.2.1.105.0.1": "pethPsePortOnOffNotification",
    "1.3.6.1.2.1.105.0.2": "pethMainPowerUsageOnNotification",
    "1.3.6.1.2.1.105.0.3": "pethMainPowerUsageOffNotification",
    # NET-SNMP notifications
    "1.3.6.1.4.1.8072.4.0.1": "nsNotifyStart",
    "1.3.6.1.4.1.8072.4.0.2": "nsNotifyShutdown",
    "1.3.6.1.4.1.8072.4.0.3": "nsNotifyRestart",
    # vendor enterprise roots
    "1.3.6.1.4.1.11": "hp",
    "1.3.6.1.4.1.25506": "h3c",
    "1.3.6.1.4.1.14823": "aruba",
    "1.3.6.1.4.1.47196": "arubaCx",
    "1.3.6.1.4.1.2620": "checkpoint",
    "1.3.6.1.4.1.9": "cisco",
    "1.3.6.1.4.1.8072": "netSnmp",
}

VENDOR_ROOTS = frozenset(("hp", "h3c", "aruba", "arubaCx", "checkpoint", "cisco", "netSnmp"))

# IF-MIB status columns rendered with their enum name (`down(2)`)
IF_STATUS_ENUM = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}
IF_STATUS_COLUMNS = ("1.3.6.1.2.1.2.2.1.7", "1.3.6.1.2.1.2.2.1.8")
IF_INDEX_COLUMN = "1.3.6.1.2.1.2.2.1.1"
SYS_UPTIME_0 = "1.3.6.1.2.1.1.3.0"
SNMP_TRAP_OID_0 = "1.3.6.1.6.3.1.1.4.1.0"
SNMP_TRAP_ENTERPRISE_0 = "1.3.6.1.6.3.1.1.4.3.0"
SNMP_TRAP_ADDRESS_0 = "1.3.6.1.6.3.18.1.3.0"
LINK_DOWN = "1.3.6.1.6.3.1.1.5.3"
LINK_UP = "1.3.6.1.6.3.1.1.5.4"


def name(oid: str) -> str | None:
    """Longest known prefix (whole arcs) + the remainder."""
    oid = oid.strip().lstrip(".")
    if oid in NAMES:
        return NAMES[oid]
    arcs = oid.split(".")
    for n in range(len(arcs) - 1, 0, -1):
        prefix = ".".join(arcs[:n])
        hit = NAMES.get(prefix)
        if hit is not None:
            return hit + "." + ".".join(arcs[n:])
    return None


def under(oid: str, column: str) -> bool:
    """True when `oid` equals `column` or is an instance of it (`column.x`)."""
    return oid == column or oid.startswith(column + ".")


def enum_display(oid: str, value: str) -> str:
    """`display` of a varbind: the enum name for the IF-MIB status columns."""
    for col in IF_STATUS_COLUMNS:
        if under(oid, col):
            try:
                n = int(value)
            except ValueError:
                return value
            label = IF_STATUS_ENUM.get(n)
            return f"{label}({n})" if label else value
    return value
