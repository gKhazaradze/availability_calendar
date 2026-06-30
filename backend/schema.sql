-- Schema for the availability-calendar backend.
-- Run automatically on first start if the DB doesn't exist (see init_db()).
--
-- Three tables:
--   friends       — one per person George shares with; each has a secret token
--                   and a visibility tier (busy < basic < full).
--   trips         — a day or multi-day span George is away; carries how many
--                   passenger seats he's offering and an optional privacy flag.
--   participants  — unified roster + seat-request table. An owner-added guest is
--                   confirmed/owner; a friend's seat request is pending/request;
--                   approve -> confirmed, decline -> declined.

PRAGMA foreign_keys = ON;

-- ─── Friends (per-person accounts, passwordless) ──────────────────────────
CREATE TABLE IF NOT EXISTS friends (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT    NOT NULL,
  token      TEXT    NOT NULL UNIQUE,             -- secrets.token_urlsafe(24)
  tier       TEXT    NOT NULL CHECK (tier IN ('busy','basic','full')),
  enabled    INTEGER NOT NULL DEFAULT 1,          -- 0 = revoked, token 401s
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
