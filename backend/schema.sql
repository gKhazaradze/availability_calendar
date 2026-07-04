-- Schema for the availability-calendar backend.
-- Run automatically on first start if the DB doesn't exist (see init_db()).
--
-- Tables:
--   friends       — one per person George shares with; each has a secret token
--                   and a visibility tier (busy < basic < full).
--   trips         — a day or multi-day span George is away; carries how many
--                   passenger seats he's offering, an optional privacy flag,
--                   and an optional map location (a point, or a circle when
--                   area_radius_m is set).
--   participants  — unified roster + seat-request table. An owner-added guest is
--                   confirmed/owner; a friend's seat request is pending/request;
--                   approve -> confirmed, decline -> declined.
--   activities    — per-trip bullet list of what's on offer ("skiing", "sauna").
--   pins          — per-trip labeled map markers, optionally linked to one of
--                   the trip's activities.

PRAGMA foreign_keys = ON;

-- ─── Friends (per-person accounts) ─────────────────────────────────────────
-- Login is name + birthday (a low-entropy shared secret — see /api/login).
-- The `token` is no longer shared as a link; it's the internal bearer credential
-- a successful login hands back, kept in the browser and sent as X-User-Token.
CREATE TABLE IF NOT EXISTS friends (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT    NOT NULL,
  token      TEXT    NOT NULL UNIQUE,             -- secrets.token_urlsafe(24); session credential
  birthday   TEXT,                                -- 'YYYY-MM-DD'; name+birthday is the login secret (NULL = can't log in)
  tier       TEXT    NOT NULL CHECK (tier IN ('busy','basic','full')),
  enabled    INTEGER NOT NULL DEFAULT 1,          -- 0 = revoked, token + login 401
  created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ─── Trips (single-day or inclusive multi-day spans) ──────────────────────
CREATE TABLE IF NOT EXISTS trips (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  destination    TEXT    NOT NULL,
  location_label TEXT    NOT NULL DEFAULT '',     -- "where I am" marker for a stay
  start_date     TEXT    NOT NULL,                -- 'YYYY-MM-DD'
  end_date       TEXT    NOT NULL,                -- 'YYYY-MM-DD', inclusive
  car_seats      INTEGER NOT NULL DEFAULT 0,      -- passenger seats offered (driver excluded)
  notes          TEXT    NOT NULL DEFAULT '',
  category       TEXT    NOT NULL DEFAULT 'other',-- 'ski' | 'summer' | 'other' | ...
  privacy        TEXT    NOT NULL DEFAULT 'normal'
                         CHECK (privacy IN ('normal','busy_only')),
  -- kind = 'trip' is the normal roadtrip entry; 'busy' is a bare
  -- unavailable-day marker (no destination/seats/people) with an optional
  -- `context` reason shown only to full-tier friends. A busy event reads as
  -- "unavailable" to everyone regardless of tier.
  kind           TEXT    NOT NULL DEFAULT 'trip'
                         CHECK (kind IN ('trip','busy')),
  context        TEXT    NOT NULL DEFAULT '',
  -- Optional trip location: a dropped pin (lat/lng), widened to a circle when
  -- area_radius_m is set. All three NULL = no location. (Existing DBs get these
  -- via ALTERs in init_db(), so keep them nullable with no non-NULL default.)
  area_lat       REAL,
  area_lng       REAL,
  area_radius_m  REAL,
  created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
  CHECK (end_date >= start_date),
  CHECK (car_seats >= 0)
);

-- Speeds up the calendar overlap query (start <= :to AND end >= :from).
CREATE INDEX IF NOT EXISTS idx_trips_dates ON trips (start_date, end_date);

-- ─── Participants (roster + seat requests, one unified table) ──────────────
CREATE TABLE IF NOT EXISTS participants (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  trip_id      INTEGER NOT NULL REFERENCES trips(id)   ON DELETE CASCADE,
  friend_id    INTEGER          REFERENCES friends(id) ON DELETE SET NULL,
  display_name TEXT    NOT NULL,                 -- snapshot name (guests may have no friend_id)
  status       TEXT    NOT NULL CHECK (status IN ('confirmed','pending','declined')),
  source       TEXT    NOT NULL CHECK (source IN ('owner','request')),
  created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
  decided_at   TEXT
);

-- At most ONE active (pending or confirmed) row per (trip, friend). A friend
-- can't double-request or request a trip they're already confirmed on. Declined
-- rows are excluded, so re-requesting after a decline still works. NULL
-- friend_id (owner-added guests) is naturally exempt — SQL NULLs aren't equal.
CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_active
  ON participants (trip_id, friend_id)
  WHERE status IN ('pending','confirmed') AND friend_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_participants_trip ON participants (trip_id);

-- ─── Activities (a trip's bullet list of things on offer) ──────────────────
-- Owned entirely by the trip form: every save replaces the whole list, so rows
-- carry only their text and position. Deleting a trip cascades.
CREATE TABLE IF NOT EXISTS activities (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  trip_id  INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  name     TEXT    NOT NULL,
  position INTEGER NOT NULL DEFAULT 0             -- order within the trip's list
);
CREATE INDEX IF NOT EXISTS idx_activities_trip ON activities (trip_id, position);

-- ─── Pins (labeled map markers on a trip) ──────────────────────────────────
-- A pin may point at one of the trip's activities (activity_id) — "this is
-- where the skiing is". Replaced wholesale on trip save, like activities;
-- ON DELETE SET NULL only matters mid-transaction while a save swaps the
-- activity rows out from under still-referencing pins.
CREATE TABLE IF NOT EXISTS pins (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  trip_id     INTEGER NOT NULL REFERENCES trips(id)      ON DELETE CASCADE,
  activity_id INTEGER          REFERENCES activities(id) ON DELETE SET NULL,
  lat         REAL    NOT NULL,
  lng         REAL    NOT NULL,
  label       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pins_trip ON pins (trip_id);

-- ─── Login attempts (rate-limit fuel for /api/login) ───────────────────────
-- Name+birthday is a guessable credential, so the login endpoint is throttled
-- per client IP. Attempts live in the DB (not process memory) because gunicorn
-- runs multiple worker processes that share nothing else — an in-memory counter
-- would only throttle one worker. Old rows are pruned opportunistically on write.
CREATE TABLE IF NOT EXISTS login_attempts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ip         TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts (ip, created_at);
