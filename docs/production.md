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
