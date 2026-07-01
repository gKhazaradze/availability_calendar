# Availability Calendar

A private calendar that shows friends **when I'm around, when I'm away, and where
there's a free seat** — without showing everyone the same level of detail.

- A **month calendar** marks free days vs. days I'm on a trip (a single ski day
  or a multi-day stay), with a location label and how many car seats are free.
- **Per-friend access**: each friend gets their own link and a *visibility tier*.
- **Seat requests**: full-tier friends can ask for a seat; I approve or decline.

Built as a sibling to `roadtrip-site` and plugged into the `my_home_page`
platform: one Flask container (gunicorn + SQLite + a no-build vanilla-JS
frontend) running network-only behind the platform's Caddy edge at
`availability.<domain>`.

## Visibility tiers

Every friend record has one of three tiers. **Enforcement is entirely
server-side** — a lower tier is never sent fields it shouldn't see.

| Tier | Sees |
|------|------|
| `busy`  | only that a day is **unavailable** (no destination, seats, or people) |
| `basic` | + destination, dates, and **free-seat count** |
| `full`  | + participant names, notes, and can **request a seat** |

A trip can also be marked **private** (`busy_only`), which collapses it to
"unavailable" for *everyone* but me — even full-tier friends.

Anyone who opens the bare link with **no personal invite** still gets a public
view of the same calendar, but every trip collapses to an opaque **busy** span —
the days are blocked out with no destination, seats, or people. Seeing any detail
requires a per-friend link.

## Two credentials

| Who | Credential | How |
|-----|-----------|-----|
| Owner (me) | `ADMIN_KEY` | entered via the **Owner** button, sent as `X-Admin-Key` |
| Friend | per-friend token | arrives once in the invite link `?u=<token>`, then `X-User-Token` |

The owner key unlocks all editing. Friend tokens are passwordless: I create a
friend (name + tier), the server mints a secret token, and I share the link. The
token's tier/enabled-state is re-read from the DB on every request, so changing a
friend's tier — or disabling them — takes effect immediately.

## Structure

```
availability_calendar/
├── frontend/                  Static site (HTML/CSS/JS). No build step.
│   ├── index.html
│   ├── styles.css
│   ├── api.js                 Backend client (admin key + friend token)
│   └── app.js                 Calendar grid, detail modal, owner admin, requests
├── backend/                   Flask API + serves the static frontend
│   ├── app.py                 reads ADMIN_KEY + AVAILABILITY_DB from the environment
│   ├── schema.sql             friends / trips / participants (+ partial unique index)
│   ├── requirements.txt
│   ├── gunicorn.conf.py
│   ├── conftest.py            pytest harness (two credentials)
│   └── test_app.py            tier-leak, overbooking, idempotency, dates, transitions
├── Dockerfile                 One image: gunicorn + Flask serving everything
├── docker-compose.yml         Production (network-only, behind Caddy)
├── docker-compose.dev.yml     Dev overlay (publishes localhost:8000, --reload)
├── deploy/setup-server.sh     One-shot Docker provisioner
└── .github/workflows/main.yml CI: push to main → tests → rebuild + restart
```

## Running locally

A single container runs gunicorn, which serves both the API and the static site.
Use the dev overlay — it republishes `localhost:8000` and enables auto-reload:

```bash
docker network create web   # once — the base compose joins this shared network
ADMIN_KEY=dev-key docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
# open http://localhost:8000  → click "Owner", enter dev-key
```

The SQLite database lives in the named `availability-data` volume so it survives
restarts and rebuilds.

### Without Docker

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
ADMIN_KEY=dev-key python app.py        # serves API + frontend on http://localhost:8000
pytest -q                              # run the tests
```

## How it works (owner)

1. Click **Owner**, enter the admin key.
2. Click any day to **add a trip** (or **＋ Trip** in the header). Set the
   destination, date range, car seats offered, category, privacy, and notes.
3. Open **Friends** to add friends, set each one's tier, copy their invite link,
   rotate a leaked link, or disable/delete someone.
4. **Requests** shows the seat-request queue — approve or decline. Approvals are
   guarded against overbooking, so two people can't both claim the last seat.

## API surface

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET`  | `/api/health` | none | Health check |
| `POST` | `/api/admin/verify` | admin | Validate the admin key |
| `GET`  | `/api/me` | friend/owner | Who am I + my tier |
| `GET`  | `/api/calendar?from=&to=` | public | Trips in a range, projected to the caller (anon → busy spans only) |
| `GET`  | `/api/trips/:id` | friend/owner | Tier-projected single trip |
| `POST` | `/api/trips/:id/request-seat` | friend (full) | Request a seat |
| `DELETE` | `/api/requests/:id` | friend | Cancel own pending request |
| `GET/POST` | `/api/admin/trips` | admin | List / create trips |
| `PUT/DELETE` | `/api/admin/trips/:id` | admin | Edit / delete a trip |
| `GET/POST` | `/api/admin/friends` | admin | List / create friends |
| `PUT/DELETE` | `/api/admin/friends/:id` | admin | Edit / rotate-token / delete |
| `POST` | `/api/admin/trips/:id/participants` | admin | Add a guest |
| `DELETE` | `/api/admin/participants/:id` | admin | Remove a participant |
| `GET`  | `/api/admin/requests` | admin | Pending seat requests |
| `POST` | `/api/admin/requests/:id/approve` | admin | Approve (overbooking-safe) |
| `POST` | `/api/admin/requests/:id/decline` | admin | Decline |

## Deploying

Deploys as a **network-only** container behind the platform's Caddy edge; the
GitHub Actions workflow rebuilds and restarts it on every push to `main`. A
one-shot provisioner installs Docker, creates the shared `web` network, generates
an `ADMIN_KEY`, and starts the container:

```bash
sudo ./deploy/setup-server.sh https://github.com/<you>/availability_calendar.git
```

Full walkthrough (platform edge, the Caddy block + homepage card, CI secrets) is
in [SETUP.md](SETUP.md) and the platform repo's `SETUP.md`.
