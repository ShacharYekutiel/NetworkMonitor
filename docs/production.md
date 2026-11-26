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
- **Safety.** A `pg_dump` is taken before every image change (migrations are forward-only; from 1.4.1 on, an
  image started on a database a newer release migrated starts without migrating, so the container rollback works).
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
   database, also restore the `pre-deploy-<rev>-<ts>.dump` with `ops/restore.sh`. A change that moves the db image
   (TimescaleDB) has its own steps: see "[1.5.1] Upgrade and rollback".

Changes to `compose.yaml`, `config/`, `caddy/` or `ops/` follow the same path (PR → merge →
deploy). `.github/workflows/production.yml` validates every PR touching `deploy/production/`
(shellcheck, release.env, compose config, `caddy validate`, systemd units, sync logic tests).

## Health endpoint

`GET https://<fqdn>/api/health` is public and answers **only** `{"status": "ok"}` (200) or
`{"status": "fail"}` (503, database unreachable). `sync.sh` (`wait_healthy`: 2xx **and** `"status": "ok"`), the
`netmon` container healthcheck (`urlopen`, any non-2xx fails) and external uptime monitors only need that.
Version, uptime, poller, receiver (`ingest`), Kerberos and assistant states and `test_mode` are at
`GET /api/health/details`, which needs a signed-in user or an API token (any role).

- **`ops/status.sh`** prints the public answer and, when the root-only file `/etc/netmon-gitops/status-token`
  (0600) exists, the details too (and the *Assistant* state taken from them). Put a **viewer** API token in it
  (UI: *API tokens*, role *Viewer*, choose an expiry): `sudo install -m 0600 /dev/stdin /etc/netmon-gitops/status-token`
  and paste the token, then Ctrl-D. The token goes to curl on stdin, never on a command line. Without the file the
  script says the details are not shown.
- **External monitoring** that parsed the old body (version, `database`, `ingest`, …) must either check only
  the HTTP code / `status`, or call `/api/health/details` with `Authorization: Bearer <viewer token>`.
  A monitor that matched `"degraded"` must now treat HTTP 503 / `"fail"` as down.

## Traps, syslog and Active Directory (phase 2b-3)

All 2b-3 features are **off** after the upgrade and are turned on in the UI (*Settings → Features*); none of
them needs a server change or a deploy. Operator details (Hebrew), device examples and commands:
[deploy/production/README.md](../deploy/production/README.md#snmp-traps-ו-syslog-שלב-2b-3).

- **Ports.** `compose.yaml` publishes `162:1162/udp` (traps), `514:1514/udp` and `514:1514/tcp` (syslog) on the
  `netmon` service and sets `NETMON_TRAP_PORT=1162`, `NETMON_SYSLOG_PORT=1514`, `NETMON_SYSLOG_TCP_PORT=1514`
  (the image defaults are the same; the app stays non-root, uid 10001). The receivers open their sockets only
  while the feature is ON (`/api/health/details` → `ingest`, *Settings → Features* status line).
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
  (`/api/health/details` → `"trgm": true`).

### Syslog flood protection — behaviour change on upgrade (migration `0018`)

Two protections are **ON by default, also after an upgrade** (*Settings → Traps & syslog → Syslog flood
protection*, each with its current value and an explanation; turning one off restores the old behaviour):

- **Deduplication** (`syslog_dedup_enabled`, window `syslog_dedup_window_s` = 60 s): identical messages (same
  source, program, MSGID, severity and text) within 60 s of the first one are stored as **one row** with
  `repeat_count` / `last_seen`; the Logs page shows `×37`. Histograms and totals count messages. Anything that
  reads `syslog_messages` directly must take `repeat_count` into account.
- **Per-source rate limit** (`syslog_rate_limit_enabled`, `syslog_rate_limit_per_min` = 3000): above 3000 new rows
  per minute from one source the excess is not stored; it is counted (`dropped.source_rate_limit`, per source in
  *Logs → Flood protection*) and a system event says so at most once an hour per source. A healthy device never
  comes close (the noisiest device in the owner's network sends ~59 rows/min); it is a storm guard.

**Filter rules** (drop, or keep only the newest N per source) are empty after the upgrade and change nothing until
an operator adds one (*Logs → Flood protection*, with the top talkers of the last 24 h and a one-click "Create
rule"). Cleaning already-stored rows is only ever offered, with the exact count, and runs in batches. Event rules
and SIEM forwarding still see every message. Migration `0018` adds three columns with constant defaults (no table
rewrite) and one empty partial index; a rollback to the previous image keeps working on the migrated database
(its INSERT leaves the new columns at their defaults); `netmon migrate --to 0016` removes them.

## Phase 3: flows, config backup, SSO, reports, chat acknowledge

Everything new is **off** after the upgrade and is turned on in *Settings → Features*. The only server-side items are
the flow ports (already in `compose.yaml`), the Caddy short-name redirect (in `caddy/Caddyfile`), the outbound proxy
for the app (`/etc/netmon-gitops/app.env`, server only) and NTP. Operator guide (Hebrew):
[deploy/production/README.md](../deploy/production/README.md#שלב-3-flows-גיבוי-קונפיגורציה-sso).

### Flow ports
- `compose.yaml` publishes `2055:2055/udp` (NetFlow v5/v9 + IPFIX), `4739:4739/udp` (IPFIX) and `6343:6343/udp`
  (sFlow v5) and sets `NETMON_NETFLOW_PORT` / `NETMON_IPFIX_PORT` / `NETMON_SFLOW_PORT` to the same values (the image
  defaults). All are > 1024, so there is no remapping and the non-root app binds them directly. Nothing listens until
  *Flows* is ON (`/api/health/details` → `ingest.netflow|ipfix|sflow`).
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
1. DNS: `netmonitor.example.net` must be an **A record** (with a CNAME, Chrome/Edge request a ticket for the target).
2. Service account, e.g. `svc-netmon-http`: password never expires, *This account supports Kerberos AES 256 bit
   encryption* checked, no other rights.
3. On a domain controller (elevated prompt):
   ```
   setspn -S HTTP/netmonitor.example.net EXAMPLE\svc-netmon-http
   ktpass /princ HTTP/netmonitor.example.net@EXAMPLE.NET /mapuser EXAMPLE\svc-netmon-http /crypto AES256-SHA1 /ptype KRB5_NT_PRINCIPAL /pass <new password> /out netmon-http.keytab
   ```
   `ktpass` sets that password on the account and maps the principal, so the AES salt in the keytab matches AD.
   Check with `setspn -L EXAMPLE\svc-netmon-http` (exactly one `HTTP/netmonitor.example.net`; `setspn -X` finds
   duplicates). Only the FQDN gets an SPN.
4. Upload `netmon-http.keytab` in *Settings → Single sign-on → Kerberos*; the principal must be listed with etype 18
   (aes256-cts). Then **delete the file** (it is equivalent to the account's password).
5. Browsers (GPO): `https://netmonitor.example.net` in the **Local intranet** zone (*Site to Zone Assignment List*,
   value `1`; used by Edge and Chrome on Windows), or the Chrome/Edge policy `AuthServerAllowlist =
   netmonitor.example.net`. Firefox: `network.negotiate-auth.trusted-uris`.
6. Server time: NTP to the domain controllers (skew > 5 min breaks Kerberos):
   ```bash
   sudo mkdir -p /etc/systemd/timesyncd.conf.d
   printf '[Time]\nNTP=dc1.example.net dc2.example.net\n' | sudo tee /etc/systemd/timesyncd.conf.d/netmon.conf
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
`https://netmonitor.example.net/api/auth/oidc/callback`; a client secret (enter its expiry date in the UI, which warns
30 days before); app roles `NetMon.Admin` / `NetMon.Viewer` assigned to users/groups in *Enterprise applications*;
token configuration: optional claims `email`, `preferred_username`. The proxy must allow
`login.microsoftonline.com`.

### Outbound proxy for the app
httpx (Telegram, Teams, OIDC, webhooks, reports) honours `HTTPS_PROXY` / `NO_PROXY` from
`/etc/netmon-gitops/app.env` (host file, never in git); SSH, SNMP, flows and LDAP never use a proxy. `bootstrap.sh`
creates `app.env` only once (a new server also gets the internal domain, e.g. `.example.net`, in `NO_PROXY`). On the
existing server edit it by hand:
```
HTTPS_PROXY=http://<proxy>:<port>
HTTP_PROXY=http://<proxy>:<port>
NO_PROXY=localhost,127.0.0.1,::1,db,netmon,proxy,.example.net,<internal webhook targets>
```
then `cd /opt/netmon/deploy/production && sudo docker compose --env-file release.env up -d netmon` (recreates the
container with the new environment). The proxy must allow **long requests (≥ 70 s)** to `api.telegram.org`
(Telegram long polling for *Chat acknowledge*); if it cuts them, lower *Telegram poll timeout* (10–50 s, default 50)
in the settings.

### Database growth and backups
Config versions ≈ 30 MB for 30 devices × 100 versions; flows ≈ 0.4 GB per busy exporter (raw 24 h, minutes 48 h,
hourly 30 d); reports ≈ 200 KB per run; `known_macs` ≈ 200 B per MAC. The daily backup (DB dump + data volume)
covers config versions, flows, reports and the encrypted keytab; nothing new has to be added to it.

## Phase 3b: "Ask the network" assistant (Amazon Bedrock)

Contract: [architecture.md § 14.9](architecture.md#149-deployment-3b) (and § 14.2, § 14.6). Hebrew step-by-step
guide for the owner: [deploy/production/README.md](../deploy/production/README.md) ("עוזר AI").

**Everything is OFF after the upgrade.** Nothing changes on the server unless the owner configures it: no new
container, image package, inbound port or volume. What the server side adds:

| File | Purpose |
|---|---|
| `ops/assistant-net.sh` | `check`: DNS, address class (private/public), direct-vs-proxy decision (same NO_PROXY rules as the app) and reachability for the Bedrock / Roles Anywhere / KB / control-plane hosts and any VPC endpoint name. `no-proxy [--restart] ENTRY…`: adds entries to `NO_PROXY` in `app.env` once. |
| `ops/status.sh` | New section *Assistant*: state from `/api/health/details` (needs the optional `status-token`, see *Health endpoint*), proxy and whether `*.vpce.amazonaws.com` bypasses it, optional certificate host files (expiry, key mode). The *Time* section now also mentions AWS signing (5 min skew). |
| `aws/*.json` | IAM templates with placeholders: `bedrock-invoke-policy.json`, `bedrock-knowledge-base-policy.json` (only with a KB), `bedrock-list-models-policy.json` (optional *List models* button), `rolesanywhere-trust-policy.json`, `vpc-endpoint-policy.json`. |
| `aws/render-policies.py` | Fills the templates (stdlib only, runs on a Windows admin PC too): `python3 render-policies.py --account 111122223333 --region us-east-1 --profile us.anthropic.<model> [--light-profile …] [--trust-anchor-id …] [--kb-id …]` → `./out/*.json`. |
| `compose.yaml` | Read-only mount `/etc/netmon-gitops/aws` → `/etc/netmon/aws` (only used when the certificate is kept as host files). |
| `bootstrap.sh` | A new server's `app.env` gets `.vpce.amazonaws.com` in `NO_PROXY`; creates `/etc/netmon-gitops/aws`. |

Native installers: `deploy/linux/install.sh --https-proxy …` and `deploy/windows/install.ps1 -HttpsProxy …` now
always add `.vpce.amazonaws.com` to the service's `NO_PROXY`; add private regional names with `--no-proxy` /
`-NoProxy`.

Order of work: (1) region and inference profile → (2) network → (3) IAM Roles Anywhere (or (4) an access key) →
(5) optional Knowledge Base → *Settings → Assistant* → **Test connection** → bottom switch ON → (6) caps.

### 1. Region and inference profile

In il-central-1, Claude models are offered only through the **global** cross-region inference profile. Two
supported setups:

| | **A. us-east-1 + `us.` profile (recommended)** | **B. il-central-1 + `global.` profile** |
|---|---|---|
| Where prompts are processed | Only in the US regions listed in the profile | Any commercial AWS region worldwide |
| App settings | Region `us-east-1`, model `us.anthropic.<model>` | Region `il-central-1`, model `global.anthropic.<model>` |
| Network from the office | Needs a path to us-east-1 (Direct Connect gateway, TGW peering, or the public endpoint via the proxy; § 2) | VPC endpoint in the existing il-central-1 VPC that Direct Connect already reaches (simplest) |
| Latency | +~150 ms per round trip Israel ↔ us-east-1 — negligible next to model time (seconds) | Lowest to the endpoint; the model may still run far away |
| Policy | Clear data geography (US) for the security team | Requires accepting "anywhere"; region-restricting SCPs must allow global inference |
| Price | AWS pricing page for the US profile | AWS pricing page for the global profile (AWS has priced global profiles differently from geographic ones — compare) |

Either way: Bedrock does not keep prompts by default (§ 6), the AWS bill and CloudWatch metrics are in the source
region, and the setting can be changed later (new Test required).

**Enable model access** (once per AWS account, in the chosen source region): Bedrock console → switch to the region
→ *Model catalog* → *Anthropic* → choose a current Claude model → if asked, submit the Anthropic **use-case form**
and/or *Request model access* (the console flow has changed over time: in newer consoles serverless models are
enabled on first use, but Anthropic models still need the one-time use-case form). An organisation SCP or an IAM
deny on `aws-marketplace:*` subscription actions can block the first use — the AWS admin does this step with
an admin role.

**Find the inference profile ID:** Bedrock console → *Infer* → *Cross-region inference* (in some console versions:
*Inference profiles*) → the *System-defined* tab → the row for the chosen Claude model with the `us.` (A) or
`global.` (B) prefix → copy **Inference profile ID** (e.g. `us.anthropic.claude-<model>-v1:0`) and keep the
**ARN** for the policy. CLI alternative:

```bash
aws bedrock list-inference-profiles --region us-east-1 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId,'anthropic')].[inferenceProfileId,status]" --output table
```

Optional *light model* (`model_id_light`): a cheaper Claude model's profile from the same list, used for background
features (alert notes, correlation, digest, report summaries). Calling the bare model ID instead of the profile ID
fails with "on-demand throughput isn't supported" (shown as `bedrock_model_invalid`).

**SCPs:** cross-region inference is evaluated in the destination regions too. If the organisation restricts regions
with `aws:RequestedRegion`, the AWS admin must allow the regions of the `us.` profile (A) or follow the AWS
documentation for global inference profiles (B) — check *Bedrock → Cross-region inference → prerequisites* in the
current AWS docs.

### 2. Network

| Host (per chosen region `<r>`) | Needed for |
|---|---|
| `bedrock-runtime.<r>.amazonaws.com` (or its VPC endpoint name) | always (Converse / ConverseStream) |
| `rolesanywhere.<r>.amazonaws.com` (or VPC endpoint) | IAM Roles Anywhere (`CreateSession`) |
| `bedrock-agent-runtime.<r>.amazonaws.com` | only with the Knowledge Base |
| `bedrock.<r>.amazonaws.com` | only for the optional *List models* button |

All HTTPS/443 outbound from the server; nothing inbound. **Clock:** SigV4 rejects requests with > 5 min skew — NTP
to the DCs (already needed by Kerberos; `status.sh` → *Time*).

**VPC interface endpoints (recommended).** In the region of the endpoint you call (A: us-east-1, B: il-central-1),
VPC console → *Endpoints* → *Create endpoint* → *AWS services* → `com.amazonaws.<r>.bedrock-runtime` (plus
`com.amazonaws.<r>.rolesanywhere` when offered in that region and Roles Anywhere should stay private, plus
`com.amazonaws.<r>.bedrock-agent-runtime` for a KB) → subnets in two AZs → **security group: inbound TCP 443 from
the office server's address** (`/32`; add office ranges only if other hosts need it) → policy: *Custom*, paste
`aws/vpc-endpoint-policy.json` (filled; limits the endpoint to the NetMonitor role/user). Private DNS: see below.

**Reaching us-east-1 from the office (setup A).** Direct Connect lands in the owner's il-central-1 network today.
Options, in order of preference:
1. **Direct Connect gateway (global) → us-east-1.** A DX gateway is not tied to a region: associate the existing DX
   gateway (if Direct Connect uses a transit VIF: with a Transit Gateway in us-east-1; if it uses a private VIF:
   with the virtual private gateway of a us-east-1 VPC) and add the us-east-1 VPC CIDR to the association's
   *allowed prefixes*. The office router learns the prefix over BGP; traffic goes DX location → AWS backbone →
   us-east-1. A DX gateway does not route between the VPCs/TGWs associated with it — that is not needed here (only
   office ↔ us-east-1). CIDRs must not overlap.
2. **Transit Gateway inter-region peering.** Peer the il-central-1 TGW (already reached over DX) with a us-east-1
   TGW; add static routes on both TGW route tables (peering attachments have no route propagation) for the us-east-1
   VPC CIDR and the office ranges, and advertise the us-east-1 CIDR to the office over DX. Inter-region data
   transfer is charged per GB (small for text).
3. **Public endpoint through the corporate proxy** (no AWS network work): leave *Endpoint URL* empty, *Proxy: from
   environment* (or *custom*), and allow `bedrock-runtime.us-east-1.amazonaws.com` and
   `rolesanywhere.<r>.amazonaws.com` on the proxy. Traffic is TLS end to end and SigV4-signed; the proxy must not
   buffer streamed responses (Test warns `stream_buffered`). A Direct Connect *public VIF*, if one exists, also
   reaches the public endpoints.

VPC peering is **not** an option for office traffic (not transitive).

**DNS.** Prefer the **endpoint-specific name** from the endpoint's *Details* tab
(`vpce-0abc…-xyz.bedrock-runtime.<r>.vpce.amazonaws.com`) as the app's *Endpoint URL* (`https://…`, no path): it
resolves in public DNS to the endpoint's private IPs, so no DNS change is needed. Only if the regional name
(`bedrock-runtime.<r>.amazonaws.com`) must resolve privately: enable *private DNS* on the endpoint, create a Route 53
Resolver **inbound endpoint** in that VPC and an AD DNS **conditional forwarder** for that exact name to it (a
forwarder for all of `amazonaws.com` would break other AWS access). Same for `rolesanywhere`.

**Proxy / NO_PROXY.** With a proxy in `app.env`, a VPC endpoint name must bypass it (otherwise the request goes to
the internet, or fails because the proxy cannot resolve it). Either choose *Proxy: direct* in *Settings → Assistant*
(affects only the assistant's AWS calls) or keep *from environment* and:

```bash
sudo /opt/netmon/deploy/production/ops/assistant-net.sh no-proxy --restart .vpce.amazonaws.com
# regional names that resolve privately (private DNS), e.g.:
sudo /opt/netmon/deploy/production/ops/assistant-net.sh no-proxy --restart bedrock-runtime.il-central-1.amazonaws.com rolesanywhere.il-central-1.amazonaws.com
# verify routing and reachability (read-only):
sudo /opt/netmon/deploy/production/ops/assistant-net.sh check --region us-east-1 --host vpce-0abc…-xyz.bedrock-runtime.us-east-1.vpce.amazonaws.com
```

New servers get `.vpce.amazonaws.com` in `NO_PROXY` from `bootstrap.sh`. The app's *Test connection* shows the host,
`via: direct|proxy …`, the address class (`private` expected over DX) and warns `vpce_via_proxy`.

### 3. IAM Roles Anywhere (recommended)

The server gets **temporary** AWS credentials (default 1 h) with an X.509 certificate from the internal PKI; no
long-lived secret exists. Create everything in one region where Roles Anywhere is available (normally the Bedrock
region of § 1; check that IAM Roles Anywhere is offered there) — that is the app's *Roles Anywhere region*.

1. **Trust anchor.** PKI admin exports the **issuing CA** certificate *Example-PKI-SERV-CA* as Base-64 (PEM) (CA
   console → CA properties → *View certificate* → *Copy to file* → Base-64, or `certutil -ca.cert serv-ca.cer` and
   `certutil -encode`). AWS admin: IAM Roles Anywhere → *Create a trust anchor* → name `example-pki-serv-ca` →
   *External certificate bundle* → paste the PEM. (If PKI-ROOT-CA is used as the anchor instead, the issuing CA must
   be uploaded as the chain in the app.) Before the CA certificate itself is renewed, add the new CA certificate to
   the bundle so both are trusted during the change-over.
2. **Revocation (CRL).** Roles Anywhere does **not** download CRLs from the CDP. Import the CA's CRL and refresh it
   on the CA's publication schedule (e.g. a scheduled task on the CA or an admin host):
   ```bash
   aws rolesanywhere import-crl --region <ra-region> --name example-pki-serv-ca --enabled \
     --crl-data fileb://Example-PKI-SERV-CA.crl --trust-anchor-arn <trust-anchor-arn>
   aws rolesanywhere update-crl --region <ra-region> --crl-id <crl-id> --crl-data fileb://Example-PKI-SERV-CA.crl
   ```
   (`certutil -CRL` publishes; the `.crl` is DER.) Without CRL import, rely on short certificate lifetimes; the
   emergency stop is *Disable* on the profile or trust anchor.
3. **Role** `netmon-bedrock`: IAM → Roles → *Create role* → *Custom trust policy* → paste the filled
   `aws/rolesanywhere-trust-policy.json`. It trusts only `rolesanywhere.amazonaws.com`, only through this trust
   anchor (`aws:SourceArn`) and only for a certificate with subject `CN=netmonitor.example.net` issued by
   `CN=Example-PKI-SERV-CA` (`aws:PrincipalTag/x509Subject/CN`, `…/x509Issuer/CN`; confirm both values on the issued
certificate with `openssl x509 -in netmonitor.cer -noout -subject -issuer` and adjust the template if they differ). Permissions: add an inline
   policy from the filled `aws/bedrock-invoke-policy.json`; add `aws/bedrock-knowledge-base-policy.json` **only** if
   a Knowledge Base is used and `aws/bedrock-list-models-policy.json` only for the optional *List models* button.
   - Converse and ConverseStream are authorised by `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`
     (there is no separate `bedrock:Converse` IAM action); nothing else is granted (no `bedrock:*`, no write, no
     `iam:*`).
   - The first statement allows the chosen inference profile(s); the second allows the underlying foundation model
     in **any** region (a cross-region profile routes to several regions; `*` also matches the region-less ARN used
     by `global.` profiles) **only** when the call comes through those profiles (`bedrock:InferenceProfileArn`).
   - Role *Maximum session duration* ≥ the app's session duration (default 3600 s).
   - **[1.4.1] The CN in the trust policy must equal the subject CN of the issued certificate, character for
     character.** The role is assumed only when `aws:PrincipalTag/x509Subject/CN` matches; any difference (short
     name instead of the FQDN, another host name, a CN the CA rewrote from the template) fails with
     *AccessDenied* on `CreateSession` although the certificate itself is valid. Read the CN from the **issued**
     certificate, not from the request:
     ```
     openssl x509 -in netmonitor.cer -noout -subject -issuer     # Linux; add -inform der for a DER file
     certutil -dump netmonitor.cer                                # Windows: the "Subject:" and "Issuer:" lines
     ```
     and render the policy with exactly that value: `render-policies.py … --cert-cn <subject CN>
     --trust-anchor-id <id or ARN>`. The script accepts the trust anchor as the bare id **or** as the full ARN from
     the console (it checks the ARN's region and account against `--ra-region` / `--account` and stops on a
     mismatch), rejects a value that is not a plain CN (`CN=…`, several RDNs, wildcards) and prints the CN and the
     trust anchor ARN the rendered policy expects.
4. **Profile:** IAM Roles Anywhere → *Create a profile* → name `netmonitor` → role `netmon-bedrock` → session
   duration 1 h (must be ≥ the app's setting) → no session policy needed. Note the three ARNs (trust anchor,
   profile, role).
5. **Certificate (CSR generated in the app).** *Settings → Assistant* → auth mode *IAM Roles Anywhere* →
   **Create CSR**: the app generates the private key (it never leaves the server; stored encrypted in the DB) and
   offers `netmonitor.csr` with subject `CN=netmonitor.example.net`. The PKI admin issues it from a template with:
   key usage *Digital Signature*; EKU **Client Authentication**; subject *supplied in the request*; RSA ≥ 2048 or ECDSA
   P-256/P-384; SHA-256; validity 1 year (≤ 2). E.g. duplicate the *Computer* template as `NetMonBedrockClient`
   (Compatibility ≥ 2008, *Extensions → Application Policies* = Client Authentication only, *Subject Name* = Supply
   in the request), publish it, then:
   ```
   certreq -submit -config "<ca-host>\Example-PKI-SERV-CA" -attrib "CertificateTemplate:NetMonBedrockClient" netmonitor.csr netmonitor.cer
   ```
   [1.4.1] `CertificateTemplate:` takes the **template name** (the *Template name* field on the template's
   *General* tab, no spaces, e.g. `NetMonBedrockClient`), **not** the *Template display name* (e.g. "NetMon Bedrock
   Client"); with the display name `certreq` answers *The requested certificate template is not supported by this
   CA*. `certutil -CATemplates -config "<ca-host>\Example-PKI-SERV-CA"` lists the names the CA offers.

   The certificate card in *Settings → Assistant* has **two modes**:
   - **Generate a CSR** (this step, recommended): the private key is created on the server and stays there. After
     the CA has signed the request, upload **only the issued certificate** (`netmonitor.cer`, plus the chain when
     the anchor is the root). There is no private key to upload — the key field is not shown in this mode, and a
     certificate that does not belong to the pending request's key is refused.
   - **Upload certificate and key**: for a key pair created elsewhere (`.pfx`, or PEM certificate + private key).
     Use it only when the CSR flow is not possible.
   Upload `netmonitor.cer` (PEM or DER; plus the issuing CA as chain only when the anchor is the root) in the same
   form → the app checks that it matches the key and shows subject, issuer, expiry. Alternatives: upload a `.pfx`
   exported from AD CS, or host files (`NETMON_AWS_RA_CERT_FILE` / `_KEY_FILE` / `_CHAIN_FILE` in `app.env` pointing
   to `/etc/netmon/aws/…`; host folder `/etc/netmon-gitops/aws/`, key `0600` owned by uid `10001`).
6. **In the app:** region, Roles Anywhere region, trust anchor / profile / role ARNs, model ID (§ 1), Endpoint URL
   and Roles Anywhere endpoint (§ 2, empty = public), proxy mode → **Test connection** → the bottom switch.
7. **Rotation.** The app warns 30 days before expiry (UI, a daily `system` event, `status.sh` for host files):
   *Create CSR* again (new key) → issue → upload → Test. Nothing changes in AWS while the CN and the issuing CA
   stay the same. Revoke the old certificate on the CA after the switch (and refresh the CRL in AWS).

### 4. Alternative: static access key

Only if Roles Anywhere is not possible. IAM → Users → `netmon-bedrock-key`, **no console access**, inline policy =
the same filled `aws/bedrock-invoke-policy.json` (+ KB policy if used). *Security credentials* → *Create access key*
→ *Application running outside AWS*. In the app: auth mode *Access key*, the key ID, the secret (write-only,
encrypted) and the creation date (reminder at 90 days). Rotate every 90 days: create the second key → enter it →
Test → *Deactivate* the old key → delete it a week later. Optional extra guard: an IAM condition on
`aws:SourceVpce` (VPC endpoint) or `aws:SourceIp` (public endpoint via the proxy's egress address). Add the user's
ARN to the VPC endpoint policy if one is used.

### 5. Knowledge Base (optional, feature `ai_knowledge`)

1. S3 bucket in the **same region** as Bedrock (block public access, SSE-S3 or SSE-KMS) with the runbooks /
   procedures (PDF, DOCX, HTML, MD, TXT). **No secrets in the documents** — passages are shown to admins and sent
   to the model.
2. Bedrock → *Knowledge Bases* → *Create* → S3 data source → a **multilingual** embeddings model (Hebrew + English)
   → vector store (compare the options' fixed monthly minimum before choosing) → *Sync*; schedule re-syncs or sync
   after changing documents.
3. Copy the **Knowledge Base ID**; render the policies with `--kb-id` and attach
   `bedrock-knowledge-base-policy.json` to the role/user (`bedrock:Retrieve` on that KB only).
4. Network: a `com.amazonaws.<r>.bedrock-agent-runtime` VPC endpoint (or allow
   `bedrock-agent-runtime.<r>.amazonaws.com` on the proxy).
5. *Settings → Assistant → Knowledge Base*: KB ID, endpoint → **Test** → bottom switch.

### 6. Cost, the monthly cap, data handling

- Cost = input tokens × input price + output tokens × output price (+ cache read/write). A typical question uses
  ≈ 6 000–20 000 input tokens over 2–4 model calls and 300–900 output tokens; with *illustrative* prices of 3 / 15 USD
  per million input / output tokens that is ≈ 0.03 USD per question, ≈ 1 USD per day for 30 questions.
- In the app (*Settings → Assistant → Usage and caps*): enter the **prices** of the chosen model and profile from
  the AWS Bedrock pricing page (USD per million tokens; the app ships no prices), then set the **monthly cost cap in
  USD** (e.g. 50). The token cap (default 5 M/month), per-admin daily questions (50) and the background stop
  (80 %: background AI features stop first, interactive use stops at 100 %) apply as well. Without prices only the
  token cap works.
- Second guard in AWS: *AWS Budgets* → cost budget filtered on the Amazon Bedrock service with an e-mail alert at
  80 % / 100 %. Compare the app's usage page with Cost Explorer after a few days.
- **Data handling — for the organisation's security team to verify** against the current AWS service terms, the AWS
  DPA and the account's settings (the app makes no compliance statement): according to AWS documentation at the
  time of writing, Amazon Bedrock does not store prompts and completions by default, does not use them to train
  models and does not share them with the model provider; inference runs in the source region or in the regions of
  the chosen cross-region profile (`us.`: US regions; `global.`: any commercial region); **model invocation
  logging** is an account-level opt-in (*Bedrock → Settings*) that, if enabled, writes prompts and answers to the
  account's CloudWatch Logs / S3. What NetMonitor sends and redacts: architecture.md § 14.6.1.

### Troubleshooting (assistant)

| Test step / code | Check |
|---|---|
| `endpoint` fails, `aws_network` | `assistant-net.sh check …`; VPC endpoint SG allows 443 from the server; route over DX; `NO_PROXY` / *Proxy: direct*; corporate CA in `/etc/netmon-gitops/ca` if the proxy inspects TLS. |
| `vpce_via_proxy` warning | `assistant-net.sh no-proxy --restart .vpce.amazonaws.com` or *Proxy: direct*. |
| `rolesanywhere_failed` "Untrusted certificate" | Trust anchor holds the issuing CA? Chain uploaded when the anchor is the root? CRL marks it revoked? |
| `rolesanywhere_failed` "Unable to assume role" | Trust policy CN / issuer CN / trust-anchor ARN; profile lists the role; session duration ≤ profile/role maximum. |
| `aws_clock_skew` | NTP (`status.sh` → Time). |
| `bedrock_model_access` | Model access / Anthropic use-case form in that region (§ 1). |
| `bedrock_model_invalid` | Use the inference **profile** ID (`us.…` / `global.…`), not the bare model ID; profile exists in that region. |
| `bedrock_access_denied` | Policy ARNs (region, account, profile ID, foundation-model ID); VPC endpoint policy; SCPs for cross-region inference. |
| `stream_buffered` warning | The proxy buffers responses — use a VPC endpoint with *Proxy: direct*, or a proxy exception. |

## Alert severity (feature `alert_severity`)

**Off** after the upgrade; turned on in *Settings → Features* ("Alert severity"). No server change, no port, no
deploy step; migration `0015` only adds nullable columns. While off nothing changes (messages, rule matching, API
fields). When on, every active problem and every new event gets one of five severities — critical > major >
minor > warning > info:

- **Default classification** (*Settings → General → Alert severity*, every row editable, "Reset to defaults"): down + impact
  critical → critical, down + impact warning → major, down + impact none → minor, warning → warning, recoveries and
  other informational changes → info.
- **Per sensor**: an optional severity for the down state and one for the warning state (sensor page or the
  multi-select bar, like *impact*). **Trap / syslog rules**: an optional severity per rule.
- **Notification rules** can be limited to some severities (empty = all, the behaviour before). Email, Telegram and
  Teams messages show a *Severity* row; webhook payloads get a `severity` key (existing keys unchanged).
- A severity is stored when the problem starts; editing the classification later re-classifies **active** problems
  only (audit log `sensor.severity`), history keeps its values. Problems that started before the feature was turned
  on are classified when it is turned on; their history stays without a severity.

Contract: [api.md § Alert severity](api.md#severity-alert-severity).

## [1.5.1] Upgrade and rollback (read before deploying)

The TimescaleDB image upgrade is **not** part of 1.5.1. 1.5.1 installs the tools that make it safe; the image moves
afterwards in a separate one-line PR ("The database image change after 1.5.1" below).

**What changes in 1.5.1.**
- **App image** on Python 3.14 (`python:3.14-slim`; the SPA is built with Node 26). No setting changes, **no** schema
  migrations.
- **Caddy** 2.10.2 → 2.11.9 with `max_header_size 64KiB` in `caddy/Caddyfile` (2.11.6 lowered the default to 16 KiB;
  Kerberos `Authorization: Negotiate` headers of users in many AD groups need more). busybox (init) 1.37 → 1.38.
- **Database image unchanged**: `timescale/timescaledb:2.31.0-pg16` (`deploy/production/compose.yaml`).
- **The app updates the TimescaleDB extension by itself** when the db image provides a newer version: at startup,
  before its migrations, it runs `ALTER EXTENSION timescaledb UPDATE` and logs
  `TimescaleDB extension updated from X to Y` (`backend/netmon/db/timescale.py`, `update_extension`, called from
  `Runtime.start`). With the unchanged image there is nothing to update.
- **Fixed ops scripts**, installed on the server by this deploy (`install_ops` in `ops/sync.sh` runs after the deploy
  is healthy; the timer runs the installed copies):
  - `ops/sync.sh` refuses a `deploy/production` change whose db image cannot load the extension version in the
    database, before anything is touched (marked bad, notification "…refused…cannot load TimescaleDB…"); an automatic
    rollback or drift repair to such a revision keeps `db` on the image it runs (notification "…The db container
    stays on …"); a db image change takes the pre-deploy `pg_dump` like an app image change; and after a db image
    change with the same app image it recreates the `netmon` container once `db` is healthy, so the app starts again
    and updates the extension (Compose itself recreates only `db`).
  - `ops/restore.sh` restores a dump at the extension version that wrote it, then updates (see rollback (b) below).
  - `ops/backup.sh`, `ops/lib.sh`: the shared dump / version helpers.

**Deploy.** Tag `v1.5.1` and merge the PR "Deploy v1.5.1 to production". No backup by hand: the app image changes, so
the `sync.sh` installed today takes `/var/backups/netmon/pre-deploy-<rev>-<ts>.dump` first. The database and its
extension are not touched, so the background jobs keep running as before.

**Verify** (`cd /opt/netmon/deploy/production` on the server): `sudo ./ops/status.sh` (1.5.1, healthy), and that the
new scripts are installed: `journalctl -u netmon-gitops | grep 'installed /usr/local/lib/netmon-gitops'` (or compare
`ops/sync.sh` with the installed copy: `sudo cmp ops/sync.sh /usr/local/lib/netmon-gitops/sync.sh`).

**Rollback: fully reversible.** Revert **only** `release.env` (the app goes back to 1.5.0; 1.5.1 has no migrations and
did not change the extension):
```bash
git switch -c rollback-1.5.0 origin/main
git revert -m 1 <merge commit of "Deploy v1.5.1 to production">   # touches release.env only
git push -u origin rollback-1.5.0                                # PR, merge
```
The server redeploys 1.5.0 within ~2 minutes. Do not revert the whole release merge (that also brings back the old
`ops/` scripts, which the database image change below relies on).

**Native installs (Linux, Windows).** 1.5.1 does not change TimescaleDB there (Linux: the OS package; Windows:
`install.ps1` installs TimescaleDB (`-TimescaleVersion`) only when it is missing). The automatic update needs the extension owner;
native installs create the extension as `postgres`, so after an OS package update of TimescaleDB the app logs a
WARNING with the command instead. Then:
```bash
sudo runuser -u postgres -- pg_dump -Fc netmon > netmon-before-update.dump      # first: the only way back
sudo systemctl stop netmon
sudo runuser -u postgres -- psql -X -d netmon -c 'ALTER EXTENSION timescaledb UPDATE'
sudo runuser -u postgres -- psql -X -d netmon -c "SELECT default_version, installed_version FROM pg_available_extensions WHERE name = 'timescaledb'"
sudo systemctl start netmon
```
(Windows: `Stop-Service NetworkMonitor`, the same SQL with `& 'C:\Program Files\PostgreSQL\16\bin\psql.exe' -X -h
127.0.0.1 -U postgres -d netmon -c ...`, `Start-Service NetworkMonitor`.) The job checks below work the same with
`psql -X -d netmon` as `postgres`. Restoring a dump: the version-aware steps in
[deploy/README.md](../deploy/README.md#backup-and-restore) ("Restore = same TimescaleDB version first, then update");
the Debian/RPM packages keep earlier versions, the Windows zip holds only one (copy the older DLLs as described there).

### The database image change after 1.5.1 (TimescaleDB 2.31.0 → 2.32.1)

A separate PR changes one line of `deploy/production/compose.yaml`: `timescale/timescaledb:2.31.0-pg16` →
`2.32.1-pg16` (PostgreSQL 16.15 → 16.16 inside the image; same data directory, no PostgreSQL major upgrade). The same
PR moves the defaults of `deploy/docker-compose.yml`, `deploy/docker-compose.dev.yml`, `deploy/.env.example` and
`install.ps1 -TimescaleVersion`. Merge it **only after 1.5.1 runs healthy** (so the fixed scripts are installed).

**What is irreversible: the extension update.** Facts (checked against the published images and Timescale's docs):
- The 2.32.1-pg16 image ships the libraries and install scripts of the earlier releases (it is built `FROM` the
  previous image), including `timescaledb-2.31.0.so`, `timescaledb--2.31.0.sql` and `timescaledb--2.31.0--2.32.1.sql`.
  It opens a database still on 2.31.0 and can `CREATE EXTENSION timescaledb VERSION '2.31.0'`.
- The 2.31.0-pg16 image has nothing newer than 2.31.0. Once the database is on 2.32.1 it **cannot open it**
  (`could not access file "$libdir/timescaledb-2.32.1"`; Timescale troubleshooting). Timescale supports a downgrade
  script for one step only (2.32.1 → 2.32.0), not back to 2.31.0: do not use it.
- Under PostgreSQL 16.16 the 2.31.0 library fails every background job (retention, compression, continuous-aggregate
  refresh; TimescaleDB 2.31.1/2.32.1 release notes, fix #11015). So the database must not stay on 2.31.0 under the
  new image: the extension has to be updated right after `db` moves.

**What happens when the PR is merged** (the `sync.sh` installed by 1.5.1):
1. The db image changes, so it takes `/var/backups/netmon/pre-deploy-<rev>-<ts>.dump` (extension 2.31.0) first.
2. `docker compose up -d --remove-orphans` recreates `db` on 2.32.1 (extension still 2.31.0). It does **not** restart
   `netmon`: the app image is unchanged and its `depends_on: db` has no `restart: true`.
3. `sync.sh` sees the db image changed while the `netmon` container was not recreated, waits for `db` to be healthy and
   runs `docker compose up -d --no-deps --force-recreate netmon`. The app (1.5.1) starts, `Runtime.start` calls
   `update_extension`, which runs `ALTER EXTENSION timescaledb UPDATE` (2.31.0 → 2.32.1) before anything else.
   Nothing else updates the extension: the running app only reconnects after the db restart, it does not re-run its
   startup.
4. Health check, as for every deploy. If it fails, the automatic rollback to the 1.5.1 revision keeps `db` on 2.32.1
   when the extension was already updated (notification "…The db container stays on
   timescale/timescaledb:2.32.1-pg16…"), and the change is marked bad.

For the plain Docker install (`deploy/docker-compose.yml`) the `netmon` service has `depends_on: db: restart: true`,
so `docker compose up -d` restarts the app after recreating `db` and the app updates the extension the same way.

**Verify after the image change** (`cd /opt/netmon/deploy/production` on the server):
```bash
P='sudo docker compose --env-file release.env exec -T db psql -X -U netmon -d netmon'
# installed vs available extension version: both 2.32.1
$P -c "SELECT default_version AS available, installed_version FROM pg_available_extensions WHERE name = 'timescaledb'"
sudo docker compose --env-file release.env logs netmon | grep 'TimescaleDB extension updated'
journalctl -u netmon-gitops | grep 'recreating it so the app updates the TimescaleDB extension'
# background jobs: last_run_status Success, total_failures not growing, next_start in the future
$P -c "SELECT j.job_id, j.proc_name, j.hypertable_name, s.last_run_status, s.last_successful_finish,
             s.total_runs, s.total_failures, s.next_start
      FROM timescaledb_information.jobs j LEFT JOIN timescaledb_information.job_stats s USING (job_id)
      WHERE j.job_id >= 1000 ORDER BY 1"
$P -c "SELECT job_id, proc_name, start_time, sqlerrcode, err_message FROM timescaledb_information.job_errors
      WHERE start_time > now() - interval '1 day' ORDER BY start_time DESC LIMIT 20"
# do not wait for the schedule: run one job now (job_id from the list; an error is printed if it fails)
$P -c "CALL run_job(1000)"
sudo ./ops/status.sh
```
The jobs are the retention policies (`measurements`, `traps`, `syslog_messages`, flow tables), the compression
policy of `measurements` and, with the Flows feature, the continuous-aggregate refresh policies of the hourly flow
views. Several run only every 12–24 h, so right after the change `last_run_status` can still be empty: use
`CALL run_job(<id>)`. If the installed version is still 2.31.0 (the app could not update it: a WARNING with the
command is in its log), restart the app: `sudo docker compose --env-file release.env restart netmon`.

**Rollback after the image change.** Reverting the db image line is **refused** by `sync.sh` once the extension is on
2.32.1 (marked bad, nothing touched). The ways back:

**(a) App back to 1.5.0, keep the 2.32.1 database image.** Supported: 1.5.1 has no migrations, 1.5.0 never touches
the extension version, and TimescaleDB 2.32.0/2.32.1 list no backward-incompatible change or removed function among
those the app uses (`create_hypertable`, retention/compression policies, `drop_chunks`, `timescaledb_information`).
CI job `app-on-new-db` (`.github/workflows/production.yml`) runs the app image pinned in `release.env` on the updated
extension: healthy, TimescaleDB on, every policy job runs. Revert only `release.env` as in the 1.5.1 rollback above.

**(b) The pre-change data from the pre-deploy dump.** Everything written since the dump is lost. `ops/restore.sh`
(installed by 1.5.1) reads the dump's extension version, recreates the database from `template0`, creates the
extension at **that** version, restores (`timescaledb_pre_restore` / `post_restore`), then runs
`ALTER EXTENSION timescaledb UPDATE` to the image's version. If the db image does not ship the dump's version it stops
**before** touching anything and names the tag to use (`timescale/timescaledb:<version>-pg16`).
- **(b1) keep the 2.32.1 image** (simplest):
  ```bash
  cd /opt/netmon/deploy/production
  ls -lt /var/backups/netmon/pre-deploy-*.dump | head        # the one taken before the db image change
  sudo ./ops/restore.sh --dump /var/backups/netmon/pre-deploy-<rev>-<ts>.dump
  # shows "DB: timescale/timescaledb:2.32.1-pg16 (TimescaleDB 2.32.1); dump on 2.31.0 -> create 2.31.0, restore,
  # update to 2.32.1"; type RESTORE. The current database is dumped first to /var/backups/netmon/pre-restore-<ts>.dump.
  ```
- **(b2) back to the 2.31.0 image as well.** A PR that reverts the db image PR (`compose.yaml` back to
  `timescale/timescaledb:2.31.0-pg16`). `sync.sh` refuses it while the database is on 2.32.1 (marked bad), so apply
  it by hand together with the restore:
  ```bash
  sudo systemctl stop netmon-gitops.timer
  cd /opt/netmon && sudo git fetch origin && sudo git checkout --detach origin/main
  cd deploy/production
  sudo ./ops/restore.sh --dump /var/backups/netmon/pre-deploy-<rev>-<ts>.dump   # dump on 2.31.0 -> create 2.31.0, no update
  sudo ./ops/sync.sh --force          # clears the bad mark, records the revision (healthy)
  sudo systemctl start netmon-gitops.timer
  ```
  A dump written **after** the change (a daily `netmon-db-<ts>.dump` on 2.32.1) cannot be restored under 2.31.0:
  `restore.sh` stops with "…written by TimescaleDB 2.32.1, but timescale/timescaledb:2.31.0-pg16 (compose.yaml)
  cannot create that version…". Use (b1) for those.

## [1.5.0] Upgrade and rollback (read before deploying)

**What changes on upgrade.** Nothing to do before deploying. At the first start 1.5.0 runs migrations 0013–0016 and
0018 (there is no 0017). The gitops deploy takes its `pg_dump` first, as for every image change.

**What operators notice afterwards.**
- **New look.** The web UI is redesigned (navigation, device tree, tables, right-click menus). No setting is lost.
- **`GET /api/health` is minimal** (see "Health endpoint"): the public body is only `{"status": "ok"}`, or
  `{"status": "fail"}` with HTTP 503 when the database is unreachable (it used to be 200 "degraded"). External
  monitoring that read the old body must use the HTTP status, or call `/api/health/details` with a viewer token.
  `ops/status.sh` prints the details only when `/etc/netmon-gitops/status-token` exists.
- **Syslog flood protection is active at once** (see "[syslog-protection]" above): identical messages within 60 s
  become one row with a repeat count, and one source is capped at 3000 stored rows per minute. Both have an off
  switch in *Settings → Traps & syslog*. Filter rules start empty.
- **Trap authentication warning.** *Settings → Traps & syslog* shows a warning while the mode is "Accept any
  community". The mode itself is not changed by the upgrade.
- **Notifications.** When no enabled rule sends to an enabled channel, the Notifications page says so and offers a
  default rule. Existing rules are untouched.
- **Alert severity** stays a feature that is off until an admin turns it on (see "Alert severity").
- **Moving devices and groups** (drag and drop, multi-select, "Move to…") is available to admins. A move that makes
  devices lose inherited SNMP settings is shown as a warning before it is confirmed.

**The plaintext secret copies are still kept.** 1.5.0 does not remove them (the "[1.4.2] Upgrade and rollback"
section below still applies); `netmon secrets remove-plaintext` remains the operator's choice.

**Rollback to 1.4.2.** The container rollback works on the migrated database (an older image starts without
migrating). Rows written by 1.5.0 features (severity values, syslog repeat counts, filter rules) are ignored by
1.4.2. To go back to the pre-upgrade data, restore the `pre-deploy-<rev>-<ts>.dump` with `ops/restore.sh`.

## [1.4.2] Upgrade and rollback (read before deploying)

**What changes on upgrade.** Nothing to do. At the first start 1.4.2 runs migration 0012: every stored SNMP
community / v3 key, discovery credential, trap community / v3 key, the SMTP password, the Telegram bot token and the
Teams / webhook URLs and headers get an **encrypted copy** next to the value. The UI, the API and polling do not
change. The log shows `Migration 0012: encrypted copies of stored secrets added {...}`. Audit rows that held
discovery extra communities or webhook URLs in clear are masked (one way). Every browser keeps its session.

**The plaintext stays for one release, on purpose.** If the deploy fails its health check, `ops/sync.sh` starts the
previous image (1.4.1) on the database 1.4.2 has already migrated, and 1.4.1 only reads the plaintext. Keeping it in
sync is what makes that rollback — and a later manual `git revert` to 1.4.1 — work with every sensor, mail, Telegram
and Teams message intact. So, until one of these happens, the database and its dumps **still contain the
plaintext**:
- the next release (1.4.3) removes it in its migration, once 1.4.2 has run well; or
- you remove it now, when you no longer need a rollback to 1.4.1:

  ```bash
  cd /opt/netmon/deploy/production
  sudo docker compose --env-file release.env exec netmon netmon secrets remove-plaintext
  sudo docker compose --env-file release.env restart netmon
  sudo docker compose --env-file release.env exec netmon netmon secrets status
  ```

  Undo (needed **before** any rollback to 1.4.1 after a removal): `netmon secrets restore-plaintext`, then restart.

**What the admin sees on a rollback to 1.4.1.**
- Automatic (failed deploy): the notification "Deploy … FAILED … Rolled back to …: healthy. The database was NOT
  restored." 1.4.1 logs one WARNING "The database is at migration 0012, which this version does not know …
  Starting without migrating" and runs normally on the plaintext. The encrypted copies are ignored by 1.4.1. Fix
  the cause and deploy 1.4.2 again; at its next start it re-synchronises every copy (also values changed under 1.4.1).
- Manual `git revert` after you ran `remove-plaintext`: run `netmon secrets restore-plaintext` with the 1.4.2 image
  **first** (otherwise 1.4.1 would find no community, no SMTP password, no bot token). Alternatively, with the server
  stopped, `netmon migrate --to 0011` returns the database to the exact 1.4.1 form.
- **Rolling back to 1.4.0 or older** (no rollback guard: it fails with "Can't locate revision"): restore the
  `pre-deploy-<rev>-<ts>.dump` with `ops/restore.sh` (deploy/production/README.md, "שחזור").

## [1.4.2] `secret.key`

`/data/secret.key` (data volume) signs sessions and is the key of every encrypted value. It is in the daily
`netmon-data-<ts>.tar.gz` backup, **not** in the DB dump — keep both.
- **Lost / replaced:** while the plaintext copies exist (1.4.2 default) nothing stops working except the secrets
  that were only ever stored encrypted (LDAP bind password, Kerberos keytab, OIDC client secret, SSH credentials,
  assistant AWS keys) — enter those again. After `remove-plaintext`, SNMP sensors of the affected devices fail and
  the device page reports unreadable credentials until they are entered again. Better: restore `secret.key` from
  the data backup.
- **Rotate** (e.g. after a suspected leak), server stopped:

  ```bash
  cd /opt/netmon/deploy/production
  sudo docker compose --env-file release.env stop netmon
  sudo docker compose --env-file release.env run --rm --no-deps netmon netmon secrets rekey
  sudo docker compose --env-file release.env up -d netmon
  ```

  Every encrypted value is re-encrypted in one transaction (nothing changes if one cannot be decrypted), then the
  key file is replaced (`secret.key.old` is kept — delete it once the server runs). Everyone signs in again;
  chat-acknowledge links in messages already sent stop working. If the key file was replaced but the command was
  interrupted: `netmon secrets rekey --old-key-file /data/secret.key.old`.

## [1.4.2] Container hardening

All services drop every Linux capability and add back only what they need, cannot gain privileges, run on a
read-only root filesystem and have resource limits (table: architecture.md § 16.2). What it means for the server:
- The database runs as `postgres` (uid 70) instead of starting as root; the existing volume is already owned by it.
- Caddy's admin API is off: a certificate change restarts the proxy (`ops/install-cert.sh` does it).
- Limits: app 4 GB / 2048 processes, Caddy 512 MB, init 64 MB; the database has no memory limit (its
  `shared_buffers` were sized to the host's RAM when it was created). A large installation that needs more changes
  `compose.yaml` by PR.
- HSTS `includeSubDomains` (only when **every** host under the domain serves HTTPS): `NETMON_HSTS_INCLUDE_SUBDOMAINS=true`
  in `/etc/netmon-gitops/app.env` and `NETMON_HSTS=max-age=31536000; includeSubDomains` in `site.env`.
- Left out on purpose: a non-root Caddy (the host TLS key is root-only `0600`, and the deploy that would change its
  ownership runs the previously installed `sync.sh`); user namespaces / seccomp profiles beyond Docker's default.

## [1.4.2] Release pin and branch protection

- `ops/check-release.sh` now requires `NETMON_IMAGE=…:<version>@sha256:<digest>` (the bump PR from `release.yml`
  always writes it). Emergency hand-made release with a tag only: run the check / sync with
  `NETMON_ALLOW_UNPINNED_IMAGE=1` (systemd drop-in `Environment=` for `netmon-gitops.service`), then pin it.
- `production.yml` fails if anything but the placeholders is committed under `deploy/certs/`.
- `.github/CODEOWNERS` names the owner for `deploy/production/**` (including `ops/`), `.github/workflows/**`,
  `deploy/certs/**` and the `Dockerfile`. It takes effect only with branch protection. Settings the owner should
  enable on `main` (*Settings → Rules → Rulesets*, or *Branches → Branch protection rules*):
  - Require a pull request before merging, at least 1 approval, **Require review from Code Owners**, dismiss stale
    approvals on new commits.
  - Require status checks to pass: `validate` (Production config), the CI jobs (`backend`, `frontend`, `docker`,
    `scripts`, E2E) and **CodeQL** (`Analyze`); require branches to be up to date.
  - Block force pushes and deletions; include administrators (no bypass) or keep the bypass list empty.
  - Optional: require signed commits / signed tags (`v*` tag protection), restrict who may push `v*` tags (a tag
    publishes an image and opens the deploy PR).
