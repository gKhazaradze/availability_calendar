#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────
# The deploy, as it runs ON THE BOX. The gate (my_home_page/deploy/box) has
# already fetched origin, checked this is the commit CI tested, reset the
# checkout to it and cd-ed here; this runs as root with a clean environment
# (APP=availability, DEPLOY_SHA=<commit>). CI can trigger it but not change it:
# what runs is this file, as committed on the branch (security fix plan 5.4).
# ─────────────────────────────────────────────────────────────────────────
set -euo pipefail

echo "--- Ensuring this app's edge network exists ---"
# Our compose joins `edge-availability`, shared only with the platform's Caddy
# (security fix plan 5.2). Create it if missing and make sure Caddy is
# on it, so a first deploy never depends on the platform's own order.
docker network inspect edge-availability >/dev/null 2>&1 || docker network create edge-availability
docker network connect edge-availability caddy 2>/dev/null || true

echo "--- Building & (re)starting the container ---"
cd /srv/availability
docker compose up -d --build --remove-orphans

echo "--- Pruning dangling images (keep disk in check) ---"
docker image prune -f >/dev/null 2>&1 || true

echo "--- Waiting for the API to come up (inside the container) ---"
# availability publishes no host port (Caddy owns the edge), so probe
# the app directly inside the container instead of host :80.
for i in $(seq 1 15); do
  if docker exec availability python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)" 2>/dev/null; then
    echo "API is healthy"; exit 0
  fi
  sleep 2
done
echo "API failed to come up in time"
docker compose logs --tail=40
exit 1
