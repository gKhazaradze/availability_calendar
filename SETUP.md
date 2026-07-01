# Setup & deployment guide

How to stand up **availability_calendar** on the same box as the `my_home_page`
platform and `roadtrip-site`, served by the platform's Caddy edge at
`availability.<domain>`. The app runs **network-only** (no host port) and joins
the shared `web` Docker network; Caddy reaches it as `availability:8000`.

This assumes the platform edge (Caddy) is already running — see the platform
repo's `SETUP.md`. If it isn't, set that up first.

## Prerequisites

- The existing server (Amazon Linux 2023 / Ubuntu 22.04+ / Debian 12+) already
  running the platform's Caddy edge.
- A domain with wildcard DNS (`*.<domain>`) pointed at the box — so
  `availability.<domain>` resolves with no new DNS record.
- SSH access via your key pair.
- This repo pushed to GitHub.

---

## Step 1 — Provision the app on the server

SSH in and run the one-shot provisioner. It installs Docker (if needed), creates
the `web` network (if needed), clones the repo to `/srv/availability`, generates
a strong `ADMIN_KEY` into `/srv/availability/.env`, and starts the container
network-only.

```bash
ssh -i your-key.pem ec2-user@<server>     # or ubuntu@ on Ubuntu/Debian

curl -sSL https://raw.githubusercontent.com/gKhazaradze/availability_calendar/main/deploy/setup-server.sh > setup.sh
chmod +x setup.sh
sudo ./setup.sh https://github.com/gKhazaradze/availability_calendar.git
```

**Save the `ADMIN_KEY` it prints** — it's shown only once. It's the key you enter
via the **Owner** button to manage everything. (It lives in
`/srv/availability/.env`; you can also read it back there.)

The container is now up but not yet reachable from the internet — Caddy needs one
block (next step).

---

## Step 2 — Register on the platform (in the `my_home_page` repo)

Two small edits in the **platform** repo, then push:

1. **`Caddyfile`** — add a bare reverse-proxy block (the app sets its own gzip +
   security headers, so keep it bare to avoid double-compression / doubled
   headers):

   ```caddyfile
   availability.{$DOMAIN} {
       reverse_proxy availability:8000
   }
   ```

2. **`site/projects.js`** — add a homepage card:

   ```js
   { sub: "availability", title: "Availability",
     blurb: "When I'm around, when I'm away, and where there's a free seat.",
     status: "live", thumbnail: "assets/availability.png" },
   ```

   Drop a thumbnail at `site/assets/availability.png`.

Push the platform repo. Its CI syncs `/srv/platform`, `docker compose up -d`, and
**reloads Caddy**, which auto-issues the TLS cert for `availability.<domain>` on
first request (wildcard DNS already resolves the subdomain). No DNS change.

> If you edited the Caddyfile by hand on the box instead, reload the edge:
> `sudo docker exec caddy caddy reload --config /etc/caddy/Caddyfile`

---

## Step 3 — GitHub Actions auto-deploy

The workflow ([.github/workflows/main.yml](.github/workflows/main.yml)) runs the
JS/Python syntax checks + pytest + compose validation, then SSHes in and runs
`git` + `docker` via `sudo` (the provisioner installed the NOPASSWD sudoers rule
and `safe.directory` entry).

Add repo secrets (Settings → Secrets and variables → Actions):

| Name | Value |
|------|-------|
| `EC2_HOST` | server IP or hostname |
| `EC2_USER` | `ec2-user` (Amazon Linux) or `ubuntu` |
| `EC2_SSH_KEY` | contents of your deploy **private** key |
| `AVAILABILITY_URL` | `https://availability.georgelands.com` (post-deploy health check) |

Reuse the same deploy SSH key as roadtrip/platform (its public half is already in
the box's `~/.ssh/authorized_keys`). From now on, **push to `main` redeploys**.

---

## Step 4 — Verify

```bash
# Public health over HTTPS (through Caddy):
curl -sf https://availability.georgelands.com/api/health        # {"ok":true,"version":"1.0.0"}

# Anonymous calendar is public but detail-free — every trip is a bare busy span:
curl -s "https://availability.georgelands.com/api/calendar?from=2026-01-01&to=2026-01-31"
#   200 — trips carry only {start_date,end_date,status:"busy"}; no destination/seats/notes

# Identity + admin stay locked to anonymous callers:
curl -s -o /dev/null -w '%{http_code}\n' \
  "https://availability.georgelands.com/api/me"                 # 401
```

Then in a browser:

1. Open `https://availability.georgelands.com` → the **public calendar**: days
   I'm away are blocked out as *Busy* with no trip details.
2. Click **Owner**, enter the `ADMIN_KEY`.
3. Add a friend at each tier, **giving each a birthday** (that's their login).
4. Create a ski day and a multi-day stay; add a guest.
5. In a separate **incognito** window, click **Sign in**, enter that friend's
   name + birthday, and confirm:
   - `busy` sees only "unavailable" days,
   - `basic` sees destination + free seats but no names,
   - `full` sees names/notes and can **request a seat**.

   (Legacy `?u=<token>` invite links still work as a fallback — **copy link** in
   the Friends drawer — but name + birthday is the way friends sign in now.)
6. As the `full` friend, request a seat; back as owner, **approve** it in
   **Requests** — the free-seat count drops and the name appears.

---

## Troubleshooting

**`availability.georgelands.com` 502s.** The container isn't up or isn't on `web`.
`docker ps`, `docker network inspect web`, confirm `container_name: availability`
matches the Caddyfile block. Logs: `cd /srv/availability && docker compose logs -f`.

**Owner key doesn't work.** It must match `ADMIN_KEY` in `/srv/availability/.env`.
After changing `.env`, `cd /srv/availability && docker compose up -d` to recreate
the container with the new value.

**A friend can't sign in.** Sign-in is name + birthday, matched exactly (name is
case-insensitive). Check the friend has a **birthday set** in the **Friends**
drawer and that they're **active**. After 10 wrong tries from one IP, sign-in is
locked for ~15 minutes — wait it out. (Two friends sharing the same name *and*
birthday can't sign in either; give one a distinct record.)

**A friend's link stopped working.** Invite links still work as a fallback. You
rotated or disabled them, or deleted the friend. Re-enable/rotate in **Friends**
and re-share the new link — or just have them sign in with name + birthday.

**Lost the admin key.** Read it from the server: `sudo cat /srv/availability/.env`.

**Never delete the `availability-data` volume.** It holds all trips, friends, and
seat assignments. Exclude it from any `docker volume prune`.
