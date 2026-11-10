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

## Phase 2b-3 (branch `phase2b3`)
Contract: architecture.md § 12, data-model.md § Phase 2b-3 (migration `0006`), api.md § Phase 2b-3.

**Owner rule:** every new feature is behind a toggle in *Settings → Features* and is **OFF by default**
(also after an upgrade). Turning a feature on or off works without a restart; turning it off keeps its data
(retention still applies). Exception (later owner decision): the Microsoft Teams channel (10) and API tokens
(11) are not toggles — they are always available.

1. **SNMP traps** (v1/v2c/v3, v2c informs) on UDP 162: decoding with a small built-in OID-name table,
   attribution to devices by source IP / agent address, global and per-device views, live view.
   v3: no engine-ID configuration — device/group v3 credentials plus an optional list of trap users.
2. **Syslog** on UDP and TCP 514 (RFC 3164 + RFC 5424, Comware / ArubaOS-Switch conventions, octet-counting and
   newline framing), search and filters (device, severity, facility, program, text, time), live tail,
   retention 30 days (configurable). Unknown senders are listed with "Assign to device" / "Ignore".
3. **Event alerts:** match rules turn traps/syslog into the status of per-device event sensors ("SNMP traps",
   "Syslog"), so all existing alerting applies (channels, delays, escalation, acknowledge, maintenance).
   Built-in rules (offered when the feature is turned on): link down on important ports (cleared by link up),
   SNMP authentication failure, device restarted, syslog error or worse.
4. **Port flapping:** 5 link changes in 10 minutes on one port → one "flapping" warning instead of a flood;
   clears after 15 quiet minutes.
5. **SIEM forwarding:** received syslog and traps forwarded as RFC 5424 (original host and time kept) over
   UDP/TCP/TLS, never slowing the receiver.
6. **Ports view: LAG membership** (design gap found in the 2b-2 review): IEEE8023-LAG-MIB
   `dot3adAggPortAttachedAggID` (`1.2.840.10006.300.43.1.2.1.1.13`), else IF-MIB `ifStackTable`
   (`1.3.6.1.2.1.31.1.2.1.3`); member ports inherit the LAG's VLANs, MACs/`mac_count` and uplink state; the
   faceplate and the list show "member of Trk1".
7. **Login with Active Directory** (LDAPS / StartTLS, service account + user bind, nested groups for the admin
   and viewer roles, disabled/locked accounts refused, users created at first login, local accounts always work
   as break-glass), configured and tested in the UI.
8. **Audit log:** who did what (configuration changes, acknowledges, settings without secrets, logins), admin
   view with filters and CSV export, append-only.
9. **Device timeline:** one chronological view per device (status changes, alerts sent, acknowledges,
   maintenance, traps, syslog, port changes, audit entries).
10. **Microsoft Teams** notification channel via Teams Workflows webhooks (the old Office 365 connectors are
    being retired by Microsoft).
11. **API tokens** for scripts (role-limited, expiring, revocable, shown once).
12. **Deployment:** standard ports 162/514 published by Docker to unprivileged ports in the container (sender
    address preserved for external traffic; Docker Desktop does not preserve it), native installers bind the
    ports directly (Linux with `CAP_NET_BIND_SERVICE`), firewall notes, device configuration examples.

Open questions for 2b-3 (defaults in architecture § 12.16): link-down alerts for important ports only; where
2b-3 is tested (not Docker Desktop); Check Point OS syslog only; AD re-login every 12 h; retention traps 30 d /
audit 365 d; AD server and group names; SIEM target.

## Phase 3 (branch `phase3`, after 2b-3)
Contract: architecture.md § 13, data-model.md § Phase 3 (migration `0007`), api.md § Phase 3.

**Owner rule:** every feature below is behind its own toggle in *Settings → Features*, **OFF by default** (also
after an upgrade), switched at runtime without a restart; OFF keeps the data (retention still runs).

7. **Network device config backup** (Oxidized-like, toggle `config_backup`): SSH only (no Telnet), daily
   (staggered) + on demand + triggered by syslog "configuration changed" messages (built-in rules for Comware,
   ArubaOS-Switch and Cisco, disabled by default; debounced). Data-driven drivers for HPE Comware, ArubaOS-Switch,
   Aruba CX, Check Point Gaia, Cisco IOS, FortiGate, MikroTik. SSH credentials per group/device, encrypted with the
   secret key. Versions stored only on change, diff, secrets masked, 100 versions / 365 days (latest always kept),
   search across configs, download and ZIP export. Host keys pinned on first use; a changed key stops backups and
   raises an alert until accepted. "Config changed" event, optional alert. **Read-only: no restore.** Library
   asyncssh (EPL-2.0/GPL-2.0, free).
8. **NetFlow / IPFIX / sFlow** (toggle `flows`): UDP 2055 / 4739 / 6343, NetFlow v5/v9, IPFIX, sFlow v5,
   templates, sampling correction, exporters mapped to devices and interfaces (ifIndex → port names). 1-minute
   aggregates (top talkers, applications, conversations, per-interface traffic) kept 24–48 h, hourly rollups 30 days
   (TimescaleDB continuous aggregates, plain-PostgreSQL fallback). Hard budget 10 000 records/s (configurable up
   to 50 000), bounded queues, drop counters. ≈ 0.4 GB per busy exporter.
9. **New device on the network** (toggle `new_device_alerts`): a MAC never seen before on an access port (not
   uplink/LAG/trunk) → event + optional alert with vendor, IP, hostname, switch/port. 7-day learning period,
   allow-list (MACs / vendor prefixes), "acknowledge as known", scope by group, randomised MACs ignored, no flood
   when a switch is added.
10. **Single sign-on:** Kerberos/SPNEGO for domain PCs (toggle `sso_kerberos`, **recommended**; handled by the
    app, Caddy unchanged except the short-name redirect; keytab for `HTTP/netmonitor.mnidom.net` uploaded in the
    UI; roles from the 2b-3 AD groups) and "Sign in with Microsoft" (Entra ID OIDC, toggle `sso_oidc`, optional).
    The AD form login and local accounts stay.
11. **Reports** (toggle `reports`): weekly/monthly and on demand — availability (SLA %) per device/group, top
    alerts, flapping ports, port errors, config changes, new devices, top talkers. HTML email + Teams/Telegram
    summary + a printable page ("Save as PDF" in the browser; no server-side PDF engine).
12. **NOC mode** (toggle `noc`): read-only wall-screen dashboard at `/noc#<token>`, no login, limited to allowed
    IPs/CIDRs and a long random token, live updates, dark large-font theme, he/en.
13. **Acknowledge from Telegram/Teams** (toggle `chat_ack`): Telegram inline button (long polling through the
    proxy; Telegram accounts linked to users with one-time codes); Teams button opens the NetworkMonitor ack page
    (Teams Workflows cards cannot call back into an internal server — stated in the UI); optional link in email;
    signed, expiring tokens; audited; admins only.

Open questions for phase 3 (defaults in architecture § 13.13): backup accounts (ArubaOS needs a manager-level
account); masking secrets; schedule/retention; config-change alert vs event; flow exporters and sampling;
new-device learning period and scope; Kerberos vs Entra; reports recipients and timing; NOC screen; dedicated
Telegram bot; proxy details.

## Later / ideas
- Per-port PoE status table.
- Template editor in the UI.
- Remote probes for multiple sites; backup/restore of NetworkMonitor itself from the UI.
- Phase 3 follow-ups: config-backup driver editor, Telnet (only if a device needs it), server-side PDF reports,
  Kerberos SSO on native Windows (SSPI), a flow worker process, alerts driven by flow thresholds.
