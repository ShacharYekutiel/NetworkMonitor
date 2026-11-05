"""Phase 2b-2: switch ports view (port inventory, MAC table, ARP, reverse DNS cache, inventory
status), device inventory flags, sensor type snmp_walk_count.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

OLD_SENSOR_TYPES = (
    "'ping','snmp_system','snmp_cpu','snmp_memory','snmp_interface','http','tcp_port','dns',"
    "'snmp_custom','snmp_disk'"
)
NEW_SENSOR_TYPES = OLD_SENSOR_TYPES + ",'snmp_walk_count'"
DEVICE_CLASSES = "'router','switch','access_point','printer','nas','server','workstation','unknown'"


def _type_check(types: str) -> list[str]:
    return [
        "ALTER TABLE sensors DROP CONSTRAINT IF EXISTS sensors_type_check",
        f"ALTER TABLE sensors ADD CONSTRAINT sensors_type_check CHECK (type IN ({types}))",
    ]


UPGRADE = [
    # 1. sensor type
    *_type_check(NEW_SENSOR_TYPES),
    # 2. device inventory flags
    "ALTER TABLE devices ADD COLUMN port_inventory text NOT NULL DEFAULT 'auto' "
    "CONSTRAINT devices_port_inventory_check CHECK (port_inventory IN ('auto','on','off'))",
    "ALTER TABLE devices ADD COLUMN arp_collect text NOT NULL DEFAULT 'auto' "
    "CONSTRAINT devices_arp_collect_check CHECK (arp_collect IN ('auto','on','off'))",
    # 3. switch ports (latest snapshot)
    """
    CREATE TABLE switch_ports (
        device_id integer NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        if_index integer NOT NULL,
        name varchar(255) NOT NULL DEFAULT '',
        descr varchar(255) NOT NULL DEFAULT '',
        alias varchar(255) NOT NULL DEFAULT '',
        if_type integer NULL,
        kind text NOT NULL CONSTRAINT switch_ports_kind_check
            CHECK (kind IN ('physical','lag','vlan','mgmt','other')),
        admin_status text NOT NULL,
        oper_status text NOT NULL,
        speed_bps bigint NULL,
        duplex text NULL CONSTRAINT switch_ports_duplex_check
            CHECK (duplex IN ('full','half','unknown')),
        mac varchar(17) NULL,
        last_change_at timestamptz NULL,
        bridge_port integer NULL,
        pvid integer NULL,
        mode text NULL CONSTRAINT switch_ports_mode_check CHECK (mode IN ('access','trunk')),
        untagged_vlans integer[] NOT NULL DEFAULT '{}',
        tagged_vlans integer[] NOT NULL DEFAULT '{}',
        poe_admin_enabled boolean NULL,
        poe_detection text NULL CONSTRAINT switch_ports_poe_detection_check
            CHECK (poe_detection IN ('disabled','searching','delivering','fault','test',
                                     'other_fault')),
        poe_class smallint NULL CONSTRAINT switch_ports_poe_class_check
            CHECK (poe_class BETWEEN 0 AND 4),
        poe_power_w double precision NULL,
        in_octets numeric(20,0) NULL,
        out_octets numeric(20,0) NULL,
        counter_bits smallint NULL CONSTRAINT switch_ports_counter_bits_check
            CHECK (counter_bits IN (32,64)),
        in_errors bigint NULL,
        out_errors bigint NULL,
        in_bps double precision NULL,
        out_bps double precision NULL,
        in_errors_delta bigint NULL,
        out_errors_delta bigint NULL,
        mac_count integer NOT NULL DEFAULT 0,
        collected_at timestamptz NOT NULL,
        PRIMARY KEY (device_id, if_index)
    )""",
    # 4. MAC table
    """
    CREATE TABLE port_macs (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        device_id integer NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        if_index integer NOT NULL,
        mac varchar(17) NOT NULL,
        vlan integer NULL,
        present boolean NOT NULL DEFAULT true,
        first_seen timestamptz NOT NULL,
        last_seen timestamptz NOT NULL
    )""",
    "CREATE UNIQUE INDEX ux_port_macs_key ON port_macs (device_id, mac, (COALESCE(vlan, -1)))",
    "CREATE INDEX ix_port_macs_mac ON port_macs (mac)",
    "CREATE INDEX ix_port_macs_device_if ON port_macs (device_id, if_index) WHERE present",
    # 5. ARP
    """
    CREATE TABLE arp_entries (
        source_device_id integer NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        ip varchar(45) NOT NULL,
        mac varchar(17) NOT NULL,
        if_index integer NULL,
        present boolean NOT NULL DEFAULT true,
        first_seen timestamptz NOT NULL,
        last_seen timestamptz NOT NULL,
        PRIMARY KEY (source_device_id, ip)
    )""",
    "CREATE INDEX ix_arp_entries_mac ON arp_entries (mac)",
    "CREATE INDEX ix_arp_entries_ip ON arp_entries (ip)",
    # 6. reverse DNS cache
    """
    CREATE TABLE dns_cache (
        ip varchar(45) PRIMARY KEY,
        name varchar(255) NULL,
        resolved_at timestamptz NOT NULL
    )""",
    # 7. inventory status
    f"""
    CREATE TABLE port_inventory_status (
        device_id integer PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
        last_run_at timestamptz NULL,
        last_ok_at timestamptz NULL,
        duration_ms integer NULL,
        error text NULL,
        ports integer NOT NULL DEFAULT 0,
        macs integer NOT NULL DEFAULT 0,
        arp_entries integer NOT NULL DEFAULT 0,
        sources jsonb NOT NULL DEFAULT '{{}}',
        detected_class text NULL CONSTRAINT port_inventory_status_detected_class_check
            CHECK (detected_class IN ({DEVICE_CLASSES})),
        detected_at timestamptz NULL
    )""",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


DOWNGRADE = [
    "DROP TABLE IF EXISTS port_inventory_status",
    "DROP TABLE IF EXISTS dns_cache",
    "DROP TABLE IF EXISTS arp_entries",
    "DROP TABLE IF EXISTS port_macs",
    "DROP TABLE IF EXISTS switch_ports",
    "ALTER TABLE devices DROP COLUMN IF EXISTS arp_collect, DROP COLUMN IF EXISTS port_inventory",
    # channels cascade with the sensor; measurements stay (no FK), as in 0003's downgrade
    "DELETE FROM sensors WHERE type = 'snmp_walk_count'",
    *_type_check(OLD_SENSOR_TYPES),
]


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
