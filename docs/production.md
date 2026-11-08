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
