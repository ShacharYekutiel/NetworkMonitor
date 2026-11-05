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

## Phase 2b-2: vendor template library
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

## Phase 2b-3
- SNMP traps (v1/v2c/v3) on UDP 162 and syslog (UDP/TCP 514), standard ports.
- Syslog search/filter, match rules that raise alerts, retention 30 days (configurable).

## Later / ideas
- Per-port PoE status table.
- Template editor in the UI.
- Remote probes for multiple sites; SLA reports; backup/restore from the UI.
