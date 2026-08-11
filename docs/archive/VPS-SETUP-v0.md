# Archive — ancien runbook VPS

> **Document historique, non exécutable.** Ce runbook précède l’audit
> multi-projets du 30 juillet 2026. Il contient des hypothèses désormais
> invalidées, notamment Papers Empire décrit comme une API Quarkus, PostgreSQL
> 16, des tags `latest`, des clés SSH dupliquées entre dépôts et une compilation
> possible depuis le serveur. La cible actuelle est documentée à la racine dans
> `VPS-SETUP.md` et dans `docs/`. Ne pas appliquer les commandes ci-dessous sur
> un VPS.

# VPS Setup Runbook — Claude-managed server on Infomaniak

> **How to use this file:** Nico completes section 1 by hand. Then he SSHes into the fresh VPS (via Termius), installs Claude Code (section 1.6), starts it, and pastes this file's content (or clones the repo containing it) with: *"Execute this runbook phase by phase. Stop at every ⛔ CHECKPOINT and every 🧑 NICO step."*
>
> Rules for Claude executing this runbook:
> - Work phase by phase, in order. Verify each checkpoint before continuing.
> - 🧑 NICO = requires human action (secrets, purchases, confirmations). Stop and ask.
> - Replace placeholders everywhere: `<VPS_IP>`, `<VPS_IPV6>`, `<GH_USER>` (GitHub username).
> - Never invent values. If a command output differs from what's expected, stop and report.
> - Commit every config change to the infra repo (Phase 2) as you go.

---

## 0. Context & target architecture

**Goal:** one Infomaniak VPS hosting all of Nico's sites, managed day-to-day by Claude Code running on the server itself (levelsio-style: SSH in via Termius from laptop or phone, attach to a tmux session, prompt).

**OS: Ubuntu 24.04 LTS.** It is in Infomaniak's official VPS image list, supported until April 2029, and the most widely documented server distro — which matters when an LLM is your sysadmin. (Debian 12 is the lean alternative; Ubuntu 26.04 is too recent and not yet in Infomaniak's image list.)

### Domains & routing

| Domain | Behavior | Served from |
|---|---|---|
| `nicolaspieper.com` | Main personal site | `/srv/www/nicolaspieper.com` (static) |
| `www.nicolaspieper.com` | 301 → `nicolaspieper.com` | Caddy redirect |
| `pieper.fr`, `www.pieper.fr` | 301 → `nicolaspieper.com` — **⚠️ zone carries live email (claude1@pieper.fr), MX/SPF/DKIM must be preserved** | Caddy redirect |
| `nicolas.pieper.fr` | 301 → `nicolaspieper.com` (subdomain of pieper.fr, **not** a separate domain/transfer) | Caddy redirect |
| `papersempire.com` | Quarkus API + React SPA | API in Docker, SPA static |
| `pieperatlas.com` | Atlas index page | `/srv/www/pieperatlas.com` (static) |
| `*.pieperatlas.com` | One subdomain per experiment (wildcard DNS + wildcard cert) | `/srv/www/atlas/<name>` or Docker app |

### Architecture decisions (already made — don't relitigate)

- **Hybrid runtime:** static sites served by Caddy from disk; Quarkus/React apps in Docker Compose.
- **Caddy** as reverse proxy, in Docker, custom-built with the official `caddy-dns/infomaniak` module → automatic HTTPS for everything, wildcard `*.pieperatlas.com` cert via DNS-01.
- **App containers never publish ports.** They join a shared Docker network `web`; Caddy reaches them by container name. This closes the classic "Docker bypasses ufw" hole by construction. Only Caddy exposes 80/443.
- **Hybrid deploys:** serious sites (nicolaspieper.com, papersempire.com) deploy from private GitHub repos via Actions. Atlas experiments are coded directly on the server by Claude, pushed to GitHub when stable.
- **One shared PostgreSQL container** (per-app databases/users) to save RAM.
- **Email for pieper.fr stays at the current provider** — records copied verbatim into the Infomaniak zone.

### Directory layout

```
/srv/
  infra/                     # private git repo <GH_USER>/vps-infra — single source of truth
    proxy/                   # Caddy: Dockerfile, compose.yml, Caddyfile, .env (gitignored)
    scripts/                 # backup.sh, helpers
    CLAUDE.md                # rules of engagement for Claude on this server (Phase 9)
    runbooks/VPS-SETUP.md    # this file
  www/                       # static roots (owner: deploy)
    nicolaspieper.com/
    papersempire.com/        # React build output
    pieperatlas.com/         # atlas index
    atlas/<experiment>/      # one folder per static experiment
  apps/                      # one dir per compose stack (owner: deploy)
    shared-postgres/
    papersempire-api/
  backups/                   # local staging for DB dumps (also pushed offsite)
```

**Users:** `nico` (sudo; Claude Code runs as this user) · `deploy` (no sudo, in `docker` group; owns `/srv/www` + `/srv/apps`; used by GitHub Actions) · root SSH disabled.

---

## 1. 🧑 NICO — Human prerequisites (before Claude does anything)

1. **Buy the VPS** at infomaniak.com → *VPS Cloud* (not Lite), image **Ubuntu 24.04 LTS**. Sizing: JVM Quarkus apps cost ~300–512 MB RAM each. **4 vCPU / 8 GB / NVMe ≥ 100 GB** is comfortable for everything planned; 4 GB works if you compile Quarkus to native images. Add your SSH public key during creation (Termius: Keychain → generate ed25519 key, put the public part in the Infomaniak console). Note `<VPS_IP>` (and IPv6 if provided).
2. **Snapshot/backup option:** enable the Infomaniak VPS backup/snapshot option in the manager if offered. This is the disaster-recovery floor.
3. **Domain transfers** (nicolaspieper.com, papersempire.com, pieperatlas.com, pieper.fr):
   - Before anything: at the current DNS host, **export/screenshot the full zone of `pieper.fr`** — every MX, SPF (TXT), DKIM, DMARC record. Mail dies if these are lost. Do the same for any other zone with non-trivial records.
   - Lower TTLs to 300 on records you'll change, 24–48 h before switching.
   - Unlock each domain at the current registrar, get the auth/EPP code, start the transfer at Infomaniak. `.com` transfers take up to 5 days; `.fr` (AFNIC) is usually faster. Domains registered/transferred <60 days ago can't be transferred.
   - `nicolas.pieper.fr` is just a record inside the `pieper.fr` zone — nothing to transfer.
   - Transfers don't cause downtime by themselves; the risky moment is switching to Infomaniak's nameservers. Claude rebuilds the zones first (Phase 4), you switch NS after verification.
4. **Infomaniak API token:** manager.infomaniak.com → profile → *API tokens* → create token with **Domain** scope. Claude needs it for DNS records + wildcard certs. You'll paste it when Phase 3 asks.
5. **GitHub:** create the private repo `vps-infra`. Create a fine-grained PAT with `read:packages` (for the server to pull private images from GHCR). You'll add Actions secrets in Phase 6.
6. **Install Claude Code on the VPS** (first SSH session, as root or default user):
   ```bash
   apt update && apt install -y nodejs npm tmux git curl
   npm install -g @anthropic-ai/claude-code
   tmux new -s main
   claude   # log in, then paste this runbook
   ```
   Day-to-day (the levelsio workflow): from Termius on laptop or phone → SSH → `tmux attach -t main`. The session survives disconnects; switch devices anytime.

---

## 2. Phase 1 — Base system & hardening 🤖

```bash
# Timezone, updates
sudo timedatectl set-timezone Europe/Paris
sudo apt update && sudo apt full-upgrade -y

# Users
sudo adduser --disabled-password --gecos "" nico
sudo usermod -aG sudo nico
sudo adduser --disabled-password --gecos "" deploy
sudo mkdir -p /home/nico/.ssh /home/deploy/.ssh
sudo cp ~/.ssh/authorized_keys /home/nico/.ssh/ 2>/dev/null || true
# 🧑 NICO: confirm your Termius public key is in /home/nico/.ssh/authorized_keys
sudo chown -R nico:nico /home/nico/.ssh && sudo chmod 700 /home/nico/.ssh && sudo chmod 600 /home/nico/.ssh/authorized_keys
```

SSH hardening — edit `/etc/ssh/sshd_config.d/99-hardening.conf`:

```
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
X11Forwarding no
```

⛔ **CHECKPOINT:** from a **second** Termius tab, confirm `ssh nico@<VPS_IP>` works with the key **before** `sudo systemctl restart ssh`. Never lock yourself out. From here on, work as `nico`.

```bash
# Firewall — only SSH + web
sudo apt install -y ufw fail2ban unattended-upgrades etckeeper
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw allow 443/udp   # 443/udp = HTTP/3
sudo ufw enable

# fail2ban — /etc/fail2ban/jail.local :
#   [sshd]
#   enabled = true
#   backend = systemd
sudo systemctl enable --now fail2ban

# Automatic security updates, with auto-reboot at 05:00 when a kernel needs it
sudo dpkg-reconfigure -plow unattended-upgrades
# In /etc/apt/apt.conf.d/50unattended-upgrades set:
#   Unattended-Upgrade::Automatic-Reboot "true";
#   Unattended-Upgrade::Automatic-Reboot-Time "05:00";

# Swap (2G) + modest swappiness
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swap.conf && sudo sysctl --system
```

`etckeeper` (installed above) auto-commits every change under `/etc` to git — an audit trail of everything Claude touches.

⛔ **CHECKPOINT:** `sudo ufw status` shows only 22, 80, 443 · `sudo fail2ban-client status sshd` runs · `ssh root@<VPS_IP>` is refused · `swapon --show` lists the swapfile.

---

## 3. Phase 2 — Docker, layout, infra repo 🤖

```bash
# Docker (official repo)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker nico && sudo usermod -aG docker deploy
# (docker group ≈ root; acceptable on a single-admin box — see CLAUDE.md rules)

# Log rotation for containers — /etc/docker/daemon.json :
#   { "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
sudo systemctl restart docker

# Shared networks
docker network create web
docker network create db

# Layout
sudo mkdir -p /srv/{infra,www,apps,backups}
sudo mkdir -p /srv/www/{nicolaspieper.com,papersempire.com,pieperatlas.com,atlas}
sudo chown -R nico:nico /srv/infra /srv/backups
sudo chown -R deploy:deploy /srv/www /srv/apps
sudo chmod -R g+w /srv/www /srv/apps && sudo usermod -aG deploy nico

# Infra repo — single source of truth for server config
cd /srv/infra && git init -b main
mkdir -p proxy scripts runbooks
printf '*.env\n' > .gitignore    # matches .env and restic.env at any depth
# 🧑 NICO: add the GitHub remote (git remote add origin git@github.com:<GH_USER>/vps-infra.git)
#          and give Claude a deploy key or gh auth to push.
```

⛔ **CHECKPOINT:** `docker run --rm hello-world` succeeds as `nico` without sudo · `docker network ls` shows `web` and `db`.

---

## 4. Phase 3 — Caddy reverse proxy + certificates 🤖

🧑 **NICO:** paste the Infomaniak API token now. Claude stores it in `/srv/infra/proxy/.env` (mode 600, gitignored): `INFOMANIAK_API_TOKEN=xxx`

`/srv/infra/proxy/Dockerfile` — Caddy with the official Infomaniak DNS module:

```dockerfile
FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/infomaniak
FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
```

`/srv/infra/proxy/compose.yml`:

```yaml
services:
  caddy:
    build: .
    container_name: caddy
    restart: unless-stopped
    ports: ["80:80", "443:443", "443:443/udp"]
    env_file: .env
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - /srv/www:/srv/www:ro
      - caddy_data:/data
      - caddy_config:/config
    networks: [web]
networks:
  web: { external: true }
volumes:
  caddy_data:
  caddy_config:
```

`/srv/infra/proxy/Caddyfile`:

```
{
	email claude1@pieper.fr
}

# ---- Redirects → main site ----
pieper.fr, www.pieper.fr, nicolas.pieper.fr, www.nicolaspieper.com {
	redir https://nicolaspieper.com{uri} permanent
}

# ---- Main site (static) ----
nicolaspieper.com {
	root * /srv/www/nicolaspieper.com
	encode zstd gzip
	file_server
}

# ---- Papers Empire: React SPA + Quarkus API ----
www.papersempire.com {
	redir https://papersempire.com{uri} permanent
}
papersempire.com {
	encode zstd gzip
	handle /api/* {
		reverse_proxy papersempire-api:8080
	}
	handle {
		root * /srv/www/papersempire.com
		try_files {path} /index.html
		file_server
	}
}

# ---- Atlas index ----
pieperatlas.com, www.pieperatlas.com {
	root * /srv/www/pieperatlas.com
	encode zstd gzip
	file_server
}

# ---- Atlas experiments: wildcard cert, folder-per-subdomain ----
*.pieperatlas.com {
	tls {
		dns infomaniak {env.INFOMANIAK_API_TOKEN}
	}
	encode zstd gzip
	# Dockerized experiments get explicit @host handles here, e.g.:
	# @myapp host myapp.pieperatlas.com
	# handle @myapp { reverse_proxy myapp:8080 }

	# Default: static folder named after the subdomain
	root * /srv/www/atlas/{labels.2}
	file_server
}
```

`{labels.2}` = the leftmost label of `foo.pieperatlas.com` → a **new static experiment needs zero config**: create `/srv/www/atlas/foo/index.html` and `https://foo.pieperatlas.com` is live (wildcard DNS + wildcard cert already cover it).

```bash
cd /srv/infra/proxy
docker compose build
docker compose up -d
docker exec caddy caddy validate --config /etc/caddy/Caddyfile   # must pass before any future reload
```

Certificates won't issue until DNS points here (Phase 4) — that's expected; Caddy retries automatically.

⛔ **CHECKPOINT:** `docker ps` shows caddy running · `ss -tlnp` on the host shows only 22, 80, 443 listening · commit proxy config to the infra repo.

---

## 5. Phase 4 — DNS at Infomaniak (via API) 🤖

Wait until 🧑 NICO confirms all four domains have arrived at Infomaniak.

Claude manages records through the Infomaniak API v2 (`https://api.infomaniak.com`, token scope Domain). **Before creating anything, `GET /2/zones/{zone}/records` on each zone** to see what Infomaniak imported and to mirror their exact `source` format for apex records.

```bash
# List (do this first, per zone)
curl -s -H "Authorization: Bearer $INFOMANIAK_API_TOKEN" \
  https://api.infomaniak.com/2/zones/pieperatlas.com/records | jq .

# Create (template)
curl -s -X POST https://api.infomaniak.com/2/zones/pieperatlas.com/records \
  -H "Authorization: Bearer $INFOMANIAK_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"type":"A","source":"*","target":"<VPS_IP>","ttl":3600}'
```

### Target records

| Zone | Record | Type | Target |
|---|---|---|---|
| pieper.fr | apex | A | `<VPS_IP>` |
| pieper.fr | www | CNAME | pieper.fr |
| pieper.fr | nicolas | A | `<VPS_IP>` |
| pieper.fr | **all existing MX / SPF(TXT) / DKIM / DMARC** | — | **copy verbatim from Nico's export — DO NOT modify, mail must keep flowing** |
| nicolaspieper.com | apex | A | `<VPS_IP>` |
| nicolaspieper.com | www | CNAME | nicolaspieper.com |
| papersempire.com | apex | A | `<VPS_IP>` |
| papersempire.com | www | CNAME | papersempire.com |
| pieperatlas.com | apex | A | `<VPS_IP>` |
| pieperatlas.com | www | CNAME | pieperatlas.com |
| pieperatlas.com | `*` (wildcard) | A | `<VPS_IP>` |

If the VPS has IPv6, add matching AAAA records for every A above.

Verification, then cutover:

```bash
for d in pieper.fr nicolas.pieper.fr nicolaspieper.com www.nicolaspieper.com \
         papersempire.com pieperatlas.com test.pieperatlas.com; do
  dig +short "$d" @ns1.infomaniak.com A; done
dig +short pieper.fr @ns1.infomaniak.com MX     # must match the old zone exactly
```

⛔ **CHECKPOINT:** all records resolve correctly against Infomaniak's nameservers, **including pieper.fr MX/TXT**. Only then 🧑 NICO switches each domain to Infomaniak DNS in the manager (if not already done by the transfer). After propagation: send + receive a test email on claude1@pieper.fr, and `curl -I https://nicolaspieper.com` returns 200 with a valid certificate; `curl -I https://pieper.fr` returns 301 → nicolaspieper.com.

---

## 6. Phase 5 — Shared PostgreSQL 🤖

`/srv/apps/shared-postgres/compose.yml`:

```yaml
services:
  postgres:
    image: postgres:16
    container_name: shared-postgres
    restart: unless-stopped
    env_file: .env            # POSTGRES_PASSWORD=<generate: openssl rand -base64 24>
    volumes:
      - pgdata:/var/lib/postgresql/data
    networks: [db]
    # no ports: reachable only from containers on the db network
networks:
  db: { external: true }
volumes:
  pgdata:
```

Per app: `docker exec -it shared-postgres psql -U postgres` → `CREATE USER papersempire WITH PASSWORD '...'; CREATE DATABASE papersempire OWNER papersempire;` — one user+database per app, credentials only in that app's `.env`. Quarkus JDBC URL: `jdbc:postgresql://shared-postgres:5432/papersempire`.

⛔ **CHECKPOINT:** `docker exec shared-postgres pg_isready` → accepting connections.

---

## 7. Phase 6 — GitHub deploys 🤖

### Server side

```bash
# Deploy key for GitHub Actions
sudo chown -R deploy:deploy /home/deploy/.ssh && sudo chmod 700 /home/deploy/.ssh
sudo -u deploy ssh-keygen -t ed25519 -N "" -f /home/deploy/.ssh/id_ed25519
sudo cat /home/deploy/.ssh/id_ed25519          # → Actions secret DEPLOY_SSH_KEY (copy, then clear terminal)
sudo -u deploy sh -c 'cat /home/deploy/.ssh/id_ed25519.pub >> /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys'
# GHCR pull access for private images (🧑 NICO provides the read:packages PAT)
sudo -u deploy docker login ghcr.io -u <GH_USER>
```

🧑 **NICO — per site repo, add Actions secrets:** `DEPLOY_SSH_KEY` (private key above), `VPS_HOST` = `<VPS_IP>`.

### Workflow A — static site (nicolaspieper.com, React SPA builds, atlas experiments once stabilized)

`.github/workflows/deploy.yml` in the site repo:

```yaml
name: Deploy
on: { push: { branches: [main] } }
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # For React: setup-node + npm ci + npm run build, then rsync ./dist/
      - name: Deploy over SSH
        env:
          KEY: ${{ secrets.DEPLOY_SSH_KEY }}
          HOST: ${{ secrets.VPS_HOST }}
        run: |
          mkdir -p ~/.ssh && echo "$KEY" > ~/.ssh/id && chmod 600 ~/.ssh/id
          ssh-keyscan -H "$HOST" >> ~/.ssh/known_hosts
          # source dir: ./public/ for plain HTML repos, ./dist/ for React builds
          rsync -az --delete -e "ssh -i ~/.ssh/id" ./public/ deploy@"$HOST":/srv/www/nicolaspieper.com/
```

### Workflow B — Quarkus app (papersempire-api)

Repo contains a `Dockerfile` (standard Quarkus JVM image; consider GraalVM native to cut RAM ~5×). Actions builds → pushes `ghcr.io/<GH_USER>/papersempire-api:latest` → SSH restart:

```yaml
name: Deploy API
on: { push: { branches: [main] } }
jobs:
  build-deploy:
    runs-on: ubuntu-latest
    permissions: { contents: read, packages: write }
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with: { registry: ghcr.io, username: "${{ github.actor }}", password: "${{ secrets.GITHUB_TOKEN }}" }
      - uses: docker/build-push-action@v6
        with: { context: ., push: true, tags: "ghcr.io/<GH_USER>/papersempire-api:latest" }
      - name: Restart on VPS
        env:
          KEY: ${{ secrets.DEPLOY_SSH_KEY }}
          HOST: ${{ secrets.VPS_HOST }}
        run: |
          mkdir -p ~/.ssh && echo "$KEY" > ~/.ssh/id && chmod 600 ~/.ssh/id
          ssh-keyscan -H "$HOST" >> ~/.ssh/known_hosts
          ssh -i ~/.ssh/id deploy@"$HOST" \
            "cd /srv/apps/papersempire-api && docker compose pull && docker compose up -d && docker image prune -f"
```

Server-side stack `/srv/apps/papersempire-api/compose.yml`:

```yaml
services:
  app:
    image: ghcr.io/<GH_USER>/papersempire-api:latest
    container_name: papersempire-api
    restart: unless-stopped
    env_file: .env             # DB credentials, secrets — never in git
    networks: [web, db]
    # no ports: Caddy reaches it as papersempire-api:8080 on the web network
networks:
  web: { external: true }
  db: { external: true }
```

⛔ **CHECKPOINT:** push a commit to each repo → site updates · `https://papersempire.com/api/...` answers through Caddy · no new ports in `ss -tlnp`.

---

## 8. Phase 7 — Backups (non-negotiable) 🤖

Three layers. An AI-managed production server without tested backups is how horror stories start — the community has real incidents of agents wiping data.

1. **Infomaniak VPS snapshots** — 🧑 NICO enables in the manager (done in §1.2). Disaster floor.
2. **Everything is in git** — site repos on GitHub, `/etc` via etckeeper, server config in `vps-infra`.
3. **Offsite data backups with restic** — 🧑 NICO subscribes to Infomaniak Swiss Backup (cheap, S3-compatible) and provides the S3 credentials + endpoint, or any S3 bucket.

`/srv/infra/scripts/backup.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
# 1. Dump all databases
docker exec shared-postgres pg_dumpall -U postgres | gzip > /srv/backups/pg_dumpall_$(date +%F).sql.gz
find /srv/backups -name 'pg_dumpall_*' -mtime +7 -delete
# 2. Push to offsite repo (credentials in /srv/infra/scripts/restic.env, mode 600, gitignored)
source /srv/infra/scripts/restic.env   # RESTIC_REPOSITORY, RESTIC_PASSWORD, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
restic backup /srv/www /srv/apps /srv/backups /srv/infra /home/nico --exclude '**/node_modules'
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
```

Install restic (`sudo apt install -y restic`), `restic init` the repo once, run nightly at 04:00 via cron or systemd timer, log to `/var/log/backup.log`.

⛔ **CHECKPOINT — test the restore, not just the backup:** `restic snapshots` lists runs · restore one file to `/tmp` and diff it · restore a `pg_dumpall` into a scratch container and check tables. Repeat this restore test monthly.

---

## 9. Phase 8 — Monitoring & upkeep 🤖

- **Uptime Kuma** (Docker, on the `web` network, no ports) at `status.pieperatlas.com` — add an explicit `@status` handle in the wildcard Caddyfile block, protect with `basic_auth`. Monitor every site + the API.
- **External check:** 🧑 NICO adds nicolaspieper.com + papersempire.com to a free external monitor (e.g. UptimeRobot) — a self-hosted monitor dies with the server.
- **Weekly cron:** `docker system prune -af --filter "until=168h"` (keeps disk clean) — never prune volumes.
- **Monthly routine for Claude:** `apt update && apt full-upgrade`, rebuild Caddy image (picks up new Caddy release), `docker compose pull` all stacks, check `df -h`, `free -h`, `fail2ban-client status sshd`, restore test.

---

## 10. Phase 9 — Claude Code as resident admin 🤖

```bash
sudo npm install -g @anthropic-ai/claude-code   # keep updated: claude update
```

Write `/srv/infra/CLAUDE.md` (and symlink to `/home/nico/CLAUDE.md`) with exactly this content, then commit:

```markdown
# This server — rules of engagement

You are managing a production VPS hosting nicolaspieper.com, papersempire.com,
pieperatlas.com (+wildcard experiments), and redirects from pieper.fr.
Layout, architecture and recipes: /srv/infra/runbooks/VPS-SETUP.md. Read it first.

## Hard rules
1. NEVER run rm -rf outside a path you just created. No recursive deletes in /srv without listing contents first and asking.
2. NEVER touch /srv/backups, restic config, or snapshot settings except to ADD backups.
3. NEVER modify MX/SPF/DKIM/DMARC records on pieper.fr. Email is live there.
4. NEVER disable ufw, fail2ban, or SSH hardening. Never publish container ports; apps join the `web` network instead.
5. Database changes: pg_dump the target DB to /srv/backups BEFORE any migration/DDL.
6. Caddyfile changes: `docker exec caddy caddy validate --config /etc/caddy/Caddyfile` before reload; reload with `docker exec caddy caddy reload --config /etc/caddy/Caddyfile`.
7. Commit every config change in /srv/infra and /etc (etckeeper) with a clear message. The infra repo is the audit trail.
8. Secrets live only in gitignored .env files (mode 600). Never commit, print, or paste them.
9. Serious sites (nicolaspieper.com, papersempire.com) change via GitHub → Actions, not by editing /srv/www directly. Atlas experiments MAY be edited live — that's what they're for.
10. Unsure or destructive? Stop and ask Nico.
```

**Permissions:** run `claude` normally (with permission prompts) for anything touching prod. `--dangerously-skip-permissions` is levelsio's raw-dog mode — if used at all, only inside `/srv/www/atlas/` experiments, never for system administration.

---

## 11. Final verification checklist 🤖

```bash
ss -tlnp                                   # only 22, 80, 443
sudo ufw status verbose                    # deny incoming default
sudo fail2ban-client status sshd
curl -sI https://pieper.fr | head -3               # 301 → nicolaspieper.com
curl -sI https://nicolas.pieper.fr | head -3       # 301 → nicolaspieper.com
curl -sI https://www.nicolaspieper.com | head -3   # 301 → apex
curl -sI https://nicolaspieper.com | head -3       # 200
curl -sI https://papersempire.com | head -3        # 200
curl -sI https://anything.pieperatlas.com | head -3   # valid wildcard cert; 404 is fine until the folder exists
dig +short pieper.fr MX                    # unchanged from old zone
restic snapshots | tail -3
docker ps                                  # all Up / healthy
```

Then: SSL Labs test on nicolaspieper.com (expect A), send/receive test mail on claude1@pieper.fr, reboot the VPS once and confirm everything comes back by itself (`restart: unless-stopped` + fstab swap).

---

## Appendix A — Recipe: new atlas experiment

**Static (30 seconds):** `mkdir /srv/www/atlas/<name>` → write `index.html` → live at `https://<name>.pieperatlas.com`. No DNS, no cert, no reload. Git-init it; push to GitHub when it graduates.

**Quarkus/React or any Docker app:** create `/srv/apps/<name>/compose.yml` (copy papersempire-api pattern: `container_name: <name>`, networks `[web]` (+`db`), **no ports**) → `docker compose up -d` → add to the wildcard block in the Caddyfile **above** the static fallback:

```
	@<name> host <name>.pieperatlas.com
	handle @<name> { reverse_proxy <name>:8080 }
```

→ validate + reload Caddy (CLAUDE.md rule 6).

## Appendix B — Recipe: new standalone domain

Buy/transfer at Infomaniak → A/CNAME records via API (Phase 4 pattern) → new site block in Caddyfile → validate + reload. Cert issues automatically via HTTP-01 (no token needed for non-wildcard).

## Appendix C — Gotchas

- **Docker bypasses ufw** when a container publishes ports (`-p`). That's why nothing but Caddy ever publishes. If you ever see `0.0.0.0:xxxx` in `docker ps`, fix it.
- **Wildcard certs cover one level only:** `a.pieperatlas.com` ✅, `a.b.pieperatlas.com` ❌.
- **Infomaniak API `source` field:** apex may be `""` or the zone name — mirror what `GET /records` returns for existing entries.
- **JVM memory:** cap Quarkus containers (`mem_limit: 512m`) or use native images; watch `free -h` before adding apps.
- **Caddy is the only cert owner.** Never install certbot alongside; two ACME clients fight over port 80.
- **tmux sessions die on reboot** — after a reboot, `tmux new -s main` and restart `claude`.
