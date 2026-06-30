#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# Availability Calendar — Docker provisioner
#
# Run this ONCE on the server. It will:
#   - install Docker + the compose plugin (if missing)
#   - clone/sync the repo to /srv/availability
#   - create the shared `web` network and start the app NETWORK-ONLY (no host
#     port; the platform's Caddy edge serves it at availability.<domain>)
#   - generate a strong ADMIN_KEY into /srv/availability/.env on first run and
#     print it ONCE (the key never lives in the image or the repo)
#   - let the deploy user run docker/git without sudo (for GitHub Actions)
#
# Usage:
#   sudo ./setup-server.sh <github_repo_url>
# Example:
#   sudo ./setup-server.sh https://github.com/gKhazaradze/availability_calendar.git
#
# Re-running this script is safe (idempotent). It will NOT overwrite an existing
# .env / ADMIN_KEY.
# ─────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_URL="${1:-}"
INSTALL_DIR="/srv/availability"
VOLUME="availability-data"
NETWORK="web"          # shared edge network; the platform's Caddy fronts us

if [[ -z "$REPO_URL" ]]; then
    echo "Usage: sudo $0 <github_repo_url>"
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (sudo)."
    exit 1
fi

# ─── Who will run deploys (GitHub Actions SSHes in as this user) ──────────
DEPLOY_USER="${SUDO_USER:-}"
if [[ -z "$DEPLOY_USER" || "$DEPLOY_USER" == "root" ]]; then
    if id ec2-user &>/dev/null; then DEPLOY_USER="ec2-user"
    elif id ubuntu &>/dev/null; then DEPLOY_USER="ubuntu"
    else DEPLOY_USER="root"; fi
fi
echo "==> Deploy user: $DEPLOY_USER"

# ─── Install Docker (+ compose plugin) if missing ─────────────────────────
if ! command -v docker &>/dev/null; then
    echo "==> Installing Docker via get.docker.com ..."
    curl -fsSL https://get.docker.com | sh
else
    echo "==> Docker already installed: $(docker --version)"
fi
systemctl enable --now docker

if ! docker compose version &>/dev/null; then
    echo "ERROR: the Docker Compose plugin isn't available."
    echo "Install it for your distro, then re-run this script."
    exit 1
fi

# ─── Shared edge network (our compose attaches to it as external) ─────────
if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
    echo "==> Creating shared network '$NETWORK' ..."
    docker network create "$NETWORK" >/dev/null
fi

# ─── Let the deploy user drive docker/git; grant passwordless sudo for CI ──
if [[ "$DEPLOY_USER" != "root" ]]; then
    usermod -aG docker "$DEPLOY_USER"

    GIT_BIN="$(command -v git || echo /usr/bin/git)"
    DOCKER_BIN="$(command -v docker || echo /usr/bin/docker)"
    cat > /etc/sudoers.d/availability-deploy <<EOF
$DEPLOY_USER ALL=(root) NOPASSWD: $GIT_BIN, $DOCKER_BIN
EOF
    chmod 440 /etc/sudoers.d/availability-deploy
    visudo -c -f /etc/sudoers.d/availability-deploy >/dev/null
fi

# ─── Clone or sync the repo ───────────────────────────────────────────────
echo "==> Fetching repository into $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$INSTALL_DIR"

# git runs as the deploy user interactively AND as root via sudo in CI, so mark
# the dir safe for both to avoid 'dubious ownership' aborts.
sudo -u "$DEPLOY_USER" git config --global --add safe.directory "$INSTALL_DIR" || true
git config --global --add safe.directory "$INSTALL_DIR" || true

if [[ -d "$INSTALL_DIR/.git" ]]; then
    sudo -u "$DEPLOY_USER" git -C "$INSTALL_DIR" fetch --prune origin main
    sudo -u "$DEPLOY_USER" git -C "$INSTALL_DIR" reset --hard origin/main
elif [[ -z "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
    sudo -u "$DEPLOY_USER" git clone "$REPO_URL" "$INSTALL_DIR"
else
    echo "ERROR: $INSTALL_DIR already exists, isn't a git repo, and isn't empty."
    echo "Move it aside and re-run:  sudo mv $INSTALL_DIR ${INSTALL_DIR}.bak"
    exit 1
fi

# ─── Generate the ADMIN_KEY on first run (never overwrite an existing one) ──
ENV_FILE="$INSTALL_DIR/.env"
GENERATED_KEY=""
if [[ ! -f "$ENV_FILE" ]]; then
    GENERATED_KEY="$(openssl rand -base64 24 2>/dev/null || head -c 18 /dev/urandom | base64)"
    cat > "$ENV_FILE" <<EOF
ADMIN_KEY=$GENERATED_KEY
EOF
    chown "$DEPLOY_USER:$DEPLOY_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "==> Generated a new ADMIN_KEY in $ENV_FILE"
else
    echo "==> Keeping existing $ENV_FILE (ADMIN_KEY unchanged)."
fi

# ─── Launch (network-only; no host port — the platform's Caddy fronts us) ──
echo "==> Building and starting the container ..."
( cd "$INSTALL_DIR" && docker compose up -d --build --remove-orphans )

# ─── Health check (probe the app INSIDE the container; no host port) ──────
echo "==> Waiting for the site to respond ..."
OK=""
for _ in $(seq 1 15); do
    if docker exec availability python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)" 2>/dev/null; then
        OK=1; break
    fi
    sleep 2
done

if [[ "${OK:-}" != "1" ]]; then
    echo "ERROR: site did not become healthy. Logs:"
    ( cd "$INSTALL_DIR" && docker compose logs --tail=40 )
    exit 1
fi

cat <<EOF

========================================================================
 Availability Calendar is up (network-only, behind the platform's Caddy edge).

  Public URL:   https://availability.<your-domain>   (served by the platform edge)
  Reached as:   availability:8000 on the shared '$NETWORK' network
  Admin key:    stored in $ENV_FILE (X-Admin-Key)

  Manage:       cd $INSTALL_DIR && docker compose ps
  Logs:         cd $INSTALL_DIR && docker compose logs -f
  Restart:      cd $INSTALL_DIR && docker compose restart
  Database:     docker volume '$VOLUME' (mounted at /app/data)

EOF
if [[ -n "$GENERATED_KEY" ]]; then
cat <<EOF
 ┌──────────────────────────────────────────────────────────────────────┐
 │  YOUR ADMIN KEY (save it now — it won't be printed again):            │
 │                                                                        │
 │      $GENERATED_KEY
 │                                                                        │
 │  Enter it via the "Owner" button on the site to manage everything.    │
 └──────────────────────────────────────────────────────────────────────┘

EOF
fi
cat <<EOF
 Next steps:
   1. Make sure the platform edge (Caddy) is set up and add the reverse_proxy
      block + homepage card for 'availability' (see the platform repo).
   2. Configure GitHub Actions secrets for auto-deploy (see SETUP.md).
   3. From now on, 'git push' to main redeploys availability automatically.

 NOTE: '$DEPLOY_USER' was added to the 'docker' group. If you're still in
 the SSH session you ran this from, log out and back in before running
 docker commands without sudo. GitHub Actions sessions already pick it up.
========================================================================
EOF
