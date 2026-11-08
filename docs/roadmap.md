# NetworkMonitor roadmap

Decisions agreed with the owner. Each cycle ends with a stop for the owner to test on the real
office network (Windows + Docker Desktop, HPE / Aruba / Check Point equipment) before the PR.

## Done
- **Phase 1** (PR #1): MVP — ping/SNMP/HTTP/TCP/DNS sensors, alerts (email, webhook), web UI (he/en).
- **Phase 2a** (PR #2): auto-discovery, device templates, `snmp_custom` / `snmp_disk`, Telegram,
  corporate CA support, prebuilt image on ghcr.io, SNMPv3 fixes found on the office network.

## Phase 2b-1 (in progress, branch `phase2b`)
1. Device dependencies (new "unreachable because of parent" status, no alerts for children).
2. Maintenance windows (one-time and weekly, global / group / device scope).
3. Acknowledge (who / when / comment; stops escalation).
4. Escalation, two stages (persisted, survives restarts).
5. Fingerprinting of devices without SNMP (HTTP title / Server header, SSH banner).
6. HPE switch templates (ArubaOS-Switch, Comware): CPU, memory, fans, PSUs, temperature, PoE.
7. Topology: LLDP/CDP link discovery, manual links, interactive map, dependency suggestions.

## Phase 2b-2: switch ports view (first item), then the vendor template library

### Switch ports view (Portnox-style)
Owner decisions: both layouts with a toggle; details only on click, in a modal; part of 2b-2.

- On a switch's device page, **all** ports are shown, not only those with a sensor. A toggle
  switches between:
  - **Faceplate:** port squares laid out like the physical front panel (odd ports on top, even
    below, uplink/SFP ports grouped separately). Colour by state: up, no link, admin-down,
    errors. Small markers for speed (100M / 1G / 10G), PoE in use and uplink (LLDP neighbour
    is a managed device).
  - **List:** one row per port with the same state, searchable (e.g. find a MAC or an IP).
- **Clicking a port opens a modal**, closed with X or by clicking outside it. It holds all the
  details:
  - port name / alias, admin and oper state, speed, duplex, VLAN (PVID);
  - traffic and errors (chart if an interface sensor exists, otherwise "Start monitoring");
  - PoE draw;
  - what is connected:
    - the LLDP/CDP neighbour (linked to its device);
    - learned MACs with vendor (OUI), IP and hostname.
- Data behind it:
  - periodic read of the full ifTable/ifXTable;
  - VLAN from Q-BRIDGE-MIB;
  - the MAC table (BRIDGE-MIB / Q-BRIDGE-MIB dot1qTpFdbTable);
  - ARP from routers/firewalls (ipNetToMediaTable / ipNetToPhysicalTable) for MAC → IP;
  - reverse DNS for names;
  - the 2b-1 topology for neighbours.
  - Uplink ports, which learn many MACs, show a count and the neighbour, not every MAC.
- Not included: the logged-in user per port (Portnox gets it from 802.1X/RADIUS, which this
  product does not have).
- References for typical port views (no official Portnox docs found): Juniper Mist switch front
  panel, Ruckus One switch ports, Meter switch ports.

**Design reference: Juniper Mist switch dashboard (chosen by the owner).** Adopt its look, with
the owner's interaction rule (details on click in a modal, not on hover):
- **Front panel** is the default tab. Square port tiles match the physical panel, uplink/SFP
  ports sit separately, and a health strip above the ports shows CPU, memory, temperature, PoE,
  PSUs and fans. The strip reuses the sensors from the 2b-1 HPE templates (green/yellow/red;
  clicking it opens the sensor).
- **Tile legend:**

  | Tile | Meaning |
  |---|---|
  | white with gray border | empty (no link) |
  | green with an icon | connected; the icon shows what is there: client, AP, switch/uplink (LLDP neighbour), with a trunk variant |
  | orange | connected with an alert |
  | peach | empty with an alert |
  | gray, crossed-out icon | administratively disabled |

  - A port with many learned MACs and no LLDP neighbour stays plain green with a MAC count.
  - Speed, PoE and VLAN are not drawn on the tile; they are in the modal.
- **Port list** tab, one row per port: Port, Status, Connected (neighbour or MAC count), VLAN,
  Mode (access/trunk), Speed, PoE (W), Rx/Tx.
  - The Port column stays fixed while scrolling.
  - Column chooser; one filter field that searches all columns, including MAC and IP.
- **Not included:** port actions (bounce, cable test, configuration). The product stays
  read-only towards devices.
- Later: an optical (DOM) chart for fiber ports (Rx/Tx power, temperature).

### Vendor template library
- Templates move from code to **data files**, so new vendors can be added without a release
  (later: a template editor in the UI).
- Every OID carries a confidence level. A template is marked **verified** only after it was
  tested on real equipment; the others ship as **unverified**.

| Vendor | Kind | Monitored | Owner has it (can verify) |
|---|---|---|---|
| HPE ArubaOS-Switch / Comware | switch | from 2b-1 | yes |
| Aruba CX | switch | CPU, memory, hardware, PoE | yes (Aruba — product line to confirm) |
| Aruba Instant / Mobility Controller | Wi-Fi | APs up, clients per AP, AP state | yes (Aruba — product line to confirm) |
| Check Point (Gaia) | firewall | CPU, memory, connections, ClusterXL/HA state, VPN tunnels, hardware | yes |
| Fortinet FortiGate | firewall | CPU, memory, sessions, HA, VPN tunnels, disk | no |
| Cisco IOS / IOS-XE | switch / router | CPU, memory, temperature, PSUs, fans | no |
| Palo Alto | firewall | CPU, sessions, HA | no |
| MikroTik | router | CPU, memory, temperature, voltage | no |
| APC | UPS | battery %, runtime, load, on-battery | no |
| HPE iLO / Dell iDRAC | server | hardware status, disks, PSUs, temperature | no |

Open questions for 2b-2: which Aruba product lines (CX switches, Instant APs, Mobility
Controller, Central); Check Point on appliance or VM, ClusterXL or not, SNMP v3 enabled on Gaia.

## Release v1.0.0 and production (branch `release-gitops`)
- GitOps production on one Ubuntu server: `deploy/production/` on `main` is the server's desired
  state (pinned image in `release.env`, Caddy HTTPS with the internal CA's certificate, TimescaleDB,
  pre-deploy dumps, automatic container rollback, daily backups). Tag `vX.Y.Z` opens the bump PR.
  See docs/production.md.

## Phase 2b-3
- SNMP traps (v1/v2c/v3) on UDP 162 and syslog (UDP/TCP 514), standard ports. Production:
  uncomment the placeholder ports in `deploy/production/compose.yaml` (published ports keep the
  sender IP).
- Syslog search/filter, match rules that raise alerts, retention 30 days (configurable).
- Ports view: **LAG membership** (design gap found in the 2b-2 review). Today the physical members of a
  `Trk*` / `Bridge-Aggregation*` / Port-channel show no VLANs or MACs (the switch reports them on the LAG
  ifIndex, which is list-only). Read IEEE8023-LAG-MIB `dot3adAggPortAttachedAggID`
  (`1.2.840.10006.300.43.1.2.1.1.13`, index = member ifIndex → LAG ifIndex), else IF-MIB `ifStackTable`
  (`1.3.6.1.2.1.31.1.2.1.3`), so member ports inherit the LAG's VLANs, MAC list/`mac_count` and uplink state,
  and the faceplate/list show "member of Trk1".

## Later / ideas
- Per-port PoE status table.
- Template editor in the UI.
- Remote probes for multiple sites; SLA reports; backup/restore from the UI.
