# Production (GitOps)

One Ubuntu server, Docker, no Kubernetes. The server's desired state is the folder
`deploy/production/` on `main`; the server pulls it and applies it. Operator guide (Hebrew):
[deploy/production/README.md](../deploy/production/README.md).

## Architecture

```
 GitHub (private)                                   Production server (Ubuntu)
 ───────────────                                    ──────────────────────────
 tag vX.Y.Z ─► release.yml ─► ghcr.io image :X.Y.Z   netmon-gitops.timer (every 2 min)
                    │                                    │
                    └─► PR "Deploy vX.Y.Z" ──merge──► main ◄── git fetch (/opt/netmon, read-only token)
                        (release.env only)               │
                                                         ▼ deploy/production/ changed?
                                                     sync.sh: validate ─► pg_dump ─► pull ─► up -d
                                                         ─► health (containers + https /api/health)
                                                         ─► fail: roll back containers, mark change bad
   browser ─► 443 Caddy (internal-CA cert) ─► netmon:8080 ─► TimescaleDB   (80 ─► 443 redirect)
```

- **Single repo.** The server clones this repo to `/opt/netmon` with a fine-grained token
  (Contents: read-only) and logs in to ghcr.io with a classic token (read:packages). Tokens live
  only on the server, in `/etc/netmon-gitops/` (root, 0600).
- **Only `deploy/production/` deploys.** `sync.sh` compares the git tree of `deploy/production/`
  on `origin/main` with the deployed one; code-only merges never trigger a deploy.
- **Pinned image.** `deploy/production/release.env` holds `NETMON_VERSION` and
  `NETMON_IMAGE=ghcr.io/shacharyekutiel/networkmonitor:X.Y.Z[@sha256:…]`.
  `ops/check-release.sh` (CI and server) rejects anything else. `:latest` is moved both by
  `image.yml` (every push to `main`) and `release.yml` (tags), so production never uses it.
- **Safety.** A `pg_dump` is taken before every image change (migrations are forward-only).
  A failed deploy rolls the containers back to the previous good revision and marks the change
  bad until a new change lands; the database is never restored automatically
  (`ops/restore.sh` is manual). Daily backups at 02:17 (DB + app data volume, 14 days).
- **Behind Caddy** the app runs with `NETMON_TRUST_PROXY=true` and `NETMON_COOKIE_SECURE=true`;
  `NETMON_TEST_MODE` is forced off. The app container is not published; Caddy terminates TLS with
  the certificate from the organisation's internal CA (`ops/make-csr.sh`, `ops/install-cert.sh`).

## Release procedure

1. Merge the work to `main` (CI green).
2. Tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. `release.yml` publishes `ghcr.io/shacharyekutiel/networkmonitor:X.Y.Z` (multi-arch), the GitHub
   release, and opens the PR **"Deploy vX.Y.Z to production"** (branch `release/bump-vX.Y.Z`)
   changing only `deploy/production/release.env` (version + digest).
   - Needs Settings → Actions → General → "Allow GitHub Actions to create and approve pull
     requests"; otherwise the job prints a link to open the PR by hand (the release still succeeds).
   - Set the repository variable `PRODUCTION_BUMP_PR=false` to disable the job.
   - PRs created with `GITHUB_TOKEN` do not trigger workflows: the bump job validates
     `release.env` and `compose config` itself, and `production.yml` runs again on the push to
     `main` (close/reopen the PR to run it on the PR).
4. Review and merge the PR. The server deploys within ~2 minutes; follow it with
   `journalctl -u netmon-gitops -f` or `ops/status.sh`.
5. Rollback: `git revert` the bump commit (PR + merge). If the new version already migrated the
   database, also restore the `pre-deploy-<rev>-<ts>.dump` with `ops/restore.sh`.

Changes to `compose.yaml`, `config/`, `caddy/` or `ops/` follow the same path (PR → merge →
deploy). `.github/workflows/production.yml` validates every PR touching `deploy/production/`
(shellcheck, release.env, compose config, `caddy validate`, systemd units, sync logic tests).

## Traps, syslog and Active Directory (phase 2b-3)

All 2b-3 features are **off** after the upgrade and are turned on in the UI (*Settings → Features*); none of
them needs a server change or a deploy. Operator details (Hebrew), device examples and commands:
[deploy/production/README.md](../deploy/production/README.md#snmp-traps-ו-syslog-שלב-2b-3).

- **Ports.** `compose.yaml` publishes `162:1162/udp` (traps), `514:1514/udp` and `514:1514/tcp` (syslog) on the
  `netmon` service and sets `NETMON_TRAP_PORT=1162`, `NETMON_SYSLOG_PORT=1514`, `NETMON_SYSLOG_TCP_PORT=1514`
  (the image defaults are the same; the app stays non-root, uid 10001). The receivers open their sockets only
  while the feature is ON (`/api/health` → `ingest`, *Settings → Features* status line).
- **Sender IP.** Published ports use iptables DNAT, which keeps the source address of external senders, so
  messages are attributed by IP. Traffic from the Docker host itself goes through `docker-proxy` and arrives from
  the bridge gateway (`172.x.0.1`): test from another machine. Docker Desktop (Windows/macOS) never keeps the
  sender IP (the UI then shows a NAT warning); it is not used in production.
- **UDP conntrack.** A sender that kept sending while the container was replaced can keep a stale conntrack
  entry. `ops/sync.sh` runs `conntrack -D -p udp --dport 162` / `514` (best effort, when the `conntrack` package
  is installed — `bootstrap.sh` installs it) after a deploy or drift repair that recreated the `netmon` container.
- **Firewall.** ufw does **not** filter Docker-published ports (Docker's `FORWARD`/`DOCKER` chains run first).
  Restrict senders in the `DOCKER-USER` chain on the *original* port, e.g.
  `iptables -I DOCKER-USER -p udp -m conntrack --ctstate DNAT --ctdir ORIGINAL --ctorigdstport 514 ! -s 10.0.0.0/8 -j DROP`.
  `ops/ingest-firewall.sh` (optional, not applied automatically) maintains a `NETMON-INGEST` chain from
  `/etc/netmon-gitops/ingest-allow.conf`; `netmon-ingest-firewall.service` (installed, not enabled) re-applies it
  at boot and Docker restarts. `--ctstate DNAT` keeps the app's outgoing SIEM forwarding to remote port 514
  unaffected. The app's ignore list is a second layer.
- **Conflicts.** `ss -lunpt '( sport = :162 or sport = :514 )'` must show only `docker-proxy` (no `snmptrapd`,
  no rsyslog `imudp`/`imtcp` on 514). `ops/status.sh` shows the listeners and the sender filter.
- **Disk.** Syslog at `warning` from ~30 switches ≈ 1 msg/s ≈ 75 MB/day (≈ 2.2 GB for the default 30 days);
  `informational` everywhere is ~10× that.
- **Active Directory (LDAPS).** The DC certificate is verified against the same trust set as HTTPS: the image's
  OS store, certifi and `/etc/netmon-gitops/ca/` (mounted as `/etc/netmon/certs`, the default
  `NETMON_EXTRA_CA_DIR`). The corporate root CA installed by `bootstrap.sh` is enough; DC names in the settings
  must match their certificates. `ldap3` is a regular dependency of the image.
- **pg_trgm** (log text search) ships with the TimescaleDB image; the app creates the extension at startup
  (`/api/health` → `"trgm": true`).

## Phase 3: flows, config backup, SSO, reports, chat acknowledge

Everything new is **off** after the upgrade and is turned on in *Settings → Features*. The only server-side items are
the flow ports (already in `compose.yaml`), the Caddy short-name redirect (in `caddy/Caddyfile`), the outbound proxy
for the app (`/etc/netmon-gitops/app.env`, server only) and NTP. Operator guide (Hebrew):
[deploy/production/README.md](../deploy/production/README.md#שלב-3-flows-גיבוי-קונפיגורציה-sso).

### Flow ports
- `compose.yaml` publishes `2055:2055/udp` (NetFlow v5/v9 + IPFIX), `4739:4739/udp` (IPFIX) and `6343:6343/udp`
  (sFlow v5) and sets `NETMON_NETFLOW_PORT` / `NETMON_IPFIX_PORT` / `NETMON_SFLOW_PORT` to the same values (the image
  defaults). All are > 1024, so there is no remapping and the non-root app binds them directly. Nothing listens until
  *Flows* is ON (`/api/health` → `ingest.netflow|ipfix|sflow`).
- Source-IP preservation, the Docker-host test caveat (`172.x.0.1`) and the Docker Desktop caveat of the traps/syslog
  section apply unchanged. NetFlow/IPFIX exporters are identified by their UDP source address, sFlow by the agent
  address in the datagram; unknown exporters are listed in the UI and dropped until accepted.
- **Conntrack.** Exporters send every few seconds from one fixed source port, so a stale entry after a container
  replacement would never expire. `ops/sync.sh` now flushes UDP 162, 514, 2055, 4739 and 6343 after a deploy or drift
  repair that recreated `netmon`.
- **Firewall.** `ops/ingest-firewall.sh` (optional) covers the three flow ports with the same allow list
  (`/etc/netmon-gitops/ingest-allow.conf` = device networks). `ops/status.sh` shows the flow listeners, the sender
  filter, the database size with the flow tables (≈ 0.4 GB per busy exporter at the default retention) and the
  NTP state.
- **Outbound only:** config backup connects to the devices on TCP 22; no inbound port.

### Caddy: one canonical name
`https://netmonitor` (short name) now **redirects** to `https://<NETMON_FQDN>` instead of serving the app: the Kerberos
SPN exists for the FQDN only, and SSO, the session cookie and the Origin check must use one host. The certificate
keeps both SANs (`make-csr.sh` is unchanged), so the redirect shows no warning. No SSO directive is needed: Caddy's
`reverse_proxy` passes `Authorization: Negotiate` and `WWW-Authenticate` through; the OIDC callback is under `/api`;
`/noc` and `/ack` are SPA routes (their tokens are in URL fragments, and Caddy has no access log).

### Kerberos SSO (AD administrator, once)
1. DNS: `netmonitor.mnidom.net` must be an **A record** (with a CNAME, Chrome/Edge request a ticket for the target).
2. Service account, e.g. `svc-netmon-http`: password never expires, *This account supports Kerberos AES 256 bit
   encryption* checked, no other rights.
3. On a domain controller (elevated prompt):
   ```
   setspn -S HTTP/netmonitor.mnidom.net MNIDOM\svc-netmon-http
   ktpass /princ HTTP/netmonitor.mnidom.net@MNIDOM.NET /mapuser MNIDOM\svc-netmon-http /crypto AES256-SHA1 /ptype KRB5_NT_PRINCIPAL /pass <new password> /out netmon-http.keytab
   ```
   `ktpass` sets that password on the account and maps the principal, so the AES salt in the keytab matches AD.
   Check with `setspn -L MNIDOM\svc-netmon-http` (exactly one `HTTP/netmonitor.mnidom.net`; `setspn -X` finds
   duplicates). Only the FQDN gets an SPN.
4. Upload `netmon-http.keytab` in *Settings → Single sign-on → Kerberos*; the principal must be listed with etype 18
   (aes256-cts). Then **delete the file** (it is equivalent to the account's password).
5. Browsers (GPO): `https://netmonitor.mnidom.net` in the **Local intranet** zone (*Site to Zone Assignment List*,
   value `1`; used by Edge and Chrome on Windows), or the Chrome/Edge policy `AuthServerAllowlist =
   netmonitor.mnidom.net`. Firefox: `network.negotiate-auth.trusted-uris`.
6. Server time: NTP to the domain controllers (skew > 5 min breaks Kerberos):
   ```bash
   sudo mkdir -p /etc/systemd/timesyncd.conf.d
   printf '[Time]\nNTP=dc1.mnidom.net dc2.mnidom.net\n' | sudo tee /etc/systemd/timesyncd.conf.d/netmon.conf
   sudo systemctl restart systemd-timesyncd && timedatectl timesync-status
   ```
7. *Active Directory* (`ldap`, 2b-3) must be ON and configured: SSO users get their role from the same AD groups.

**Keytab handling.** Nothing is stored on the host: the keytab is uploaded through the UI (≤ 64 KiB, admin only),
stored AES-GCM-encrypted in the database (covered by the daily DB backup) and written to
`/data/run/http.keytab` (0600, inside the `netmon-data` volume) only while *Kerberos SSO* is ON; `/data/run/`
(keytab, generated `krb5.conf`, replay cache) is recreated at start and is not needed in a restore. Rotation: run
`ktpass` again with a new password (the kvno increases) and upload the new keytab; the UI warns when only RC4 keys
(etype 23) are present. The image ships MIT krb5 (`libgssapi-krb5-2`) and python-gssapi; no `/etc/krb5.conf` is used.

### Entra ID (only if *Sign in with Microsoft* is wanted)
App registration (single tenant), platform **Web**, redirect URI
`https://netmonitor.mnidom.net/api/auth/oidc/callback`; a client secret (enter its expiry date in the UI, which warns
30 days before); app roles `NetMon.Admin` / `NetMon.Viewer` assigned to users/groups in *Enterprise applications*;
token configuration: optional claims `email`, `preferred_username`. The proxy must allow
`login.microsoftonline.com`.

### Outbound proxy for the app
httpx (Telegram, Teams, OIDC, webhooks, reports) honours `HTTPS_PROXY` / `NO_PROXY` from
`/etc/netmon-gitops/app.env` (host file, never in git); SSH, SNMP, flows and LDAP never use a proxy. `bootstrap.sh`
creates `app.env` only once (a new server also gets the internal domain, e.g. `.mnidom.net`, in `NO_PROXY`). On the
existing server edit it by hand:
```
HTTPS_PROXY=http://<proxy>:<port>
HTTP_PROXY=http://<proxy>:<port>
NO_PROXY=localhost,127.0.0.1,::1,db,netmon,proxy,.mnidom.net,<internal webhook targets>
```
then `cd /opt/netmon/deploy/production && sudo docker compose --env-file release.env up -d netmon` (recreates the
container with the new environment). The proxy must allow **long requests (≥ 70 s)** to `api.telegram.org`
(Telegram long polling for *Chat acknowledge*); if it cuts them, lower *Telegram poll timeout* (10–50 s, default 50)
in the settings.

### Database growth and backups
Config versions ≈ 30 MB for 30 devices × 100 versions; flows ≈ 0.4 GB per busy exporter (raw 24 h, minutes 48 h,
hourly 30 d); reports ≈ 200 KB per run; `known_macs` ≈ 200 B per MAC. The daily backup (DB dump + data volume)
covers config versions, flows, reports and the encrypted keytab; nothing new has to be added to it.
