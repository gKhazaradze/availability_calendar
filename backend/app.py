"""
Availability Calendar — backend.

George shares a calendar of his availability with friends: which days he's free,
which he's away on a trip (a single ski day or a multi-day stay), how many car
seats are free, and who's coming along. Different friends see different amounts
of detail, controlled by a per-friend visibility *tier*.

Two credential types (the one real departure from the roadtrip template):

  - Owner (George): the `X-Admin-Key` header, constant-time compared to the
    ADMIN_KEY env var. Full CRUD on trips/friends/participants; sees everything.
  - Friend: a per-friend secret token in the `X-User-Token` header. The token is
    only an identifier — the friend's tier and enabled-state are re-read from the
    DB on every request, so a downgrade/disable takes effect immediately. Tiers:
      busy  → only that a day is unavailable (no destination/seats/people)
      basic → + destination, dates, free-seat count
      full  → + participant names, notes, and the ability to request a seat
  - Anonymous: no credential → 401 on the API; the frontend shows a locked page.

Two cross-cutting invariants (see the project plan / design review):

  1. gunicorn runs multiple *sync* workers (separate processes, no shared
     memory), so every integrity rule is enforced inside a SQLite transaction
     (`BEGIN IMMEDIATE` + a conditional UPDATE / a partial unique index), never
     by "read in Python, then write".
  2. No data leaks through error or response shape: error bodies are opaque
     codes (never a destination/notes/name), and every trip in a response goes
     through one central project_trip() — never a raw row.

This same Flask app also serves the static frontend, so a single container
(see ../Dockerfile and ../docker-compose.yml) runs the whole site.
"""

import os
import re
import hmac
import secrets
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

from flask import (
    Flask, request, jsonify, g, abort, make_response, send_from_directory,
)

# ─── Configuration ───────────────────────────────────────────────────────

# The owner key that unlocks all editing. Set it via the ADMIN_KEY env var
# (docker-compose.yml); the fallback is only for local dev.
ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "avail.db"
FRONTEND_DIR = BASE_DIR.parent / "frontend"

# DB location is env-configurable so Docker can point it at a mounted volume.
DATABASE_PATH = os.environ.get("AVAILABILITY_DB", str(DEFAULT_DB))
SCHEMA_PATH = BASE_DIR / "schema.sql"

MAX_CONTENT_LENGTH = 64 * 1024
MAX_NAME = 80
MAX_DEST = 120
MAX_LABEL = 120
MAX_CATEGORY = 40
MAX_NOTES = 2000
MAX_SEATS = 50
MAX_RANGE_DAYS = 366          # cap on a calendar query window

TIERS = ("busy", "basic", "full")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

# Gzip responses (Flask is the edge server inside the container and also serves
# the static frontend). Optional — the app still runs without flask_compress.
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    app.logger.warning("flask_compress not installed; responses won't be gzipped")


# ─── Database ─────────────────────────────────────────────────────────────

def get_db():
    """Request-scoped SQLite connection in autocommit mode.

    isolation_level=None hands transaction control to us so we can issue an
    explicit `BEGIN IMMEDIATE` (acquire the writer lock up front) for the
    integrity-critical writes. busy_timeout makes a second worker WAIT for the
    lock instead of erroring with 'database is locked'.
    """
    if "db" not in g:
        conn = sqlite3.connect(DATABASE_PATH, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def writing():
    """Run a write inside an explicit immediate transaction.

    Commits on success; rolls back and re-raises on ANY exception (including the
    HTTPException raised by abort()/fail(), so a mid-transaction reject undoes
    its own partial work).
    """
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def init_db():
    """Create tables if the database file doesn't exist yet."""
    first_run = not Path(DATABASE_PATH).exists()
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        conn.commit()
        if first_run:
            app.logger.info(f"Initialized new database at {DATABASE_PATH}")
    finally:
        conn.close()


# ─── Failures (opaque codes only — never interpolate data) ────────────────

def fail(status, code):
    """Abort with a controlled JSON body `{"error": code}` and no data in it."""
    resp = jsonify(error=code)
    resp.status_code = status
    abort(resp)


@app.errorhandler(400)
def _bad_request(e):
    return jsonify(error="bad_request"), 400

@app.errorhandler(401)
def _unauthorized(e):
    return jsonify(error="unauthorized"), 401

@app.errorhandler(403)
def _forbidden(e):
    return jsonify(error="forbidden"), 403

@app.errorhandler(404)
def _not_found(e):
    return jsonify(error="not_found"), 404

@app.errorhandler(409)
def _conflict(e):
    return jsonify(error="conflict"), 409

@app.errorhandler(413)
def _too_large(e):
    return jsonify(error="payload_too_large"), 413

@app.errorhandler(500)
def _server_error(e):
    return jsonify(error="server_error"), 500


# ─── Security headers ──────────────────────────────────────────────────────

@app.after_request
def set_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    # Load-bearing: stops the `?u=<token>` invite link leaking via Referer.
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.path.startswith("/api/"):
        # Tier-projected data + tokens must never be cached by intermediaries.
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ─── Auth / viewer resolution ──────────────────────────────────────────────

def is_owner_request():
    provided = request.headers.get("X-Admin-Key", "").strip()
    return bool(ADMIN_KEY) and hmac.compare_digest(provided, ADMIN_KEY)


def require_admin():
    if not is_owner_request():
        abort(401)


def get_viewer():
    """Resolve the caller into a viewer dict.

    role is 'owner' | 'friend' | 'anon'. The friend's tier + enabled-state are
    read fresh from the DB every call (the token is only an identifier).
    """
    if is_owner_request():
        return {"role": "owner", "tier": "owner", "friend_id": None, "name": None}

    token = request.headers.get("X-User-Token", "").strip()
    if token:
        friend = get_db().execute(
            "SELECT id, name, tier FROM friends WHERE token = ? AND enabled = 1",
            (token,),
        ).fetchone()
        if friend:
            return {
                "role": "friend",
                "tier": friend["tier"],
                "friend_id": friend["id"],
                "name": friend["name"],
            }
    return {"role": "anon", "tier": None, "friend_id": None, "name": None}


def require_viewer():
    viewer = get_viewer()
    if viewer["role"] == "anon":
        abort(401)
    return viewer


# ─── Validation helpers ────────────────────────────────────────────────────

def valid_date(s):
    if not isinstance(s, str) or not DATE_RE.match(s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def clean_str(value, max_len, *, allow_empty=False, default=""):
    """Coerce to a trimmed string within a length bound, or fail(400)."""
    if value is None:
        if allow_empty:
            return default
        fail(400, "missing_field")
    if not isinstance(value, str):
        fail(400, "bad_field_type")
    value = value.strip()
    if not value and not allow_empty:
        fail(400, "empty_field")
    if len(value) > max_len:
        fail(400, "field_too_long")
    return value


def parse_trip_payload(body, existing=None):
    """Validate + normalize a trip create/update body.

    `existing` is the current row for an update (PUT): omitted fields keep their
    current value. Returns a dict of the final column values. Calls fail() on any
    validation error (opaque codes only).
    """
    def cur(field, fallback):
        return existing[field] if existing is not None else fallback

    has = (lambda k: k in body) if isinstance(body, dict) else (lambda k: False)
    if not isinstance(body, dict):
        fail(400, "bad_body")

    destination = (clean_str(body.get("destination"), MAX_DEST)
                   if existing is None or has("destination")
                   else cur("destination", ""))

    location_label = (clean_str(body.get("location_label"), MAX_LABEL, allow_empty=True)
                      if has("location_label") or existing is None
                      else cur("location_label", ""))

    notes = (clean_str(body.get("notes"), MAX_NOTES, allow_empty=True)
             if has("notes") or existing is None
             else cur("notes", ""))

    category = (clean_str(body.get("category"), MAX_CATEGORY, allow_empty=True, default="other")
                if has("category") or existing is None
                else cur("category", "other")) or "other"

    start_date = body.get("start_date") if (has("start_date") or existing is None) else cur("start_date", None)
    end_date = body.get("end_date") if (has("end_date") or existing is None) else cur("end_date", None)
    if not valid_date(start_date) or not valid_date(end_date):
        fail(400, "bad_date")
    if end_date < start_date:
        fail(400, "end_before_start")

    if has("car_seats") or existing is None:
        car_seats = body.get("car_seats", 0)
    else:
        car_seats = cur("car_seats", 0)
    if not isinstance(car_seats, int) or isinstance(car_seats, bool):
        fail(400, "bad_seats")
    if car_seats < 0 or car_seats > MAX_SEATS:
        fail(400, "bad_seats")

    privacy = (body.get("privacy") if (has("privacy") or existing is None) else cur("privacy", "normal")) or "normal"
    if privacy not in ("normal", "busy_only"):
        fail(400, "bad_privacy")

    return {
        "destination": destination,
        "location_label": location_label,
        "start_date": start_date,
        "end_date": end_date,
        "car_seats": car_seats,
        "notes": notes,
        "category": category,
        "privacy": privacy,
    }


# ─── Tier projection (the heart of it) ─────────────────────────────────────

def confirmed_rows(db, trip_id):
    return db.execute(
        "SELECT id, friend_id, display_name FROM participants "
        "WHERE trip_id = ? AND status = 'confirmed' ORDER BY id",
        (trip_id,),
    ).fetchall()


def project_trip(trip, viewer, db):
    """Return ONLY the fields `viewer` is allowed to see for `trip`.

    This is the single chokepoint for tier enforcement. Every endpoint that
    returns trip data returns this — never a raw sqlite3.Row.
    """
    is_owner = viewer["role"] == "owner"
    tier = viewer["tier"]

    # The always-visible minimum: a busy span. Anyone authenticated learns that
    # a day is unavailable (that's the busy tier's entire purpose) — but nothing
    # about what's happening.
    out = {
        "id": trip["id"],
        "start_date": trip["start_date"],
        "end_date": trip["end_date"],
        "status": "busy",
    }

    show_detail = is_owner or (trip["privacy"] == "normal" and tier in ("basic", "full"))
    if not show_detail:
        return out

    confirmed = confirmed_rows(db, trip["id"])
    confirmed_count = len(confirmed)

    out.update({
        "destination": trip["destination"],
        "location_label": trip["location_label"],
        "category": trip["category"],
        "car_seats": trip["car_seats"],
        "free_seats": max(0, trip["car_seats"] - confirmed_count),
        "confirmed_count": confirmed_count,
    })

    show_names = is_owner or (trip["privacy"] == "normal" and tier == "full")
    if show_names and not is_owner:
        # Full-tier friend: confirmed NAMES only — never status/source (so a
        # decline of someone else is never exposed).
        out["participants"] = [{"name": p["display_name"]} for p in confirmed]
        out["notes"] = trip["notes"]

    if viewer["role"] == "friend":
        # The viewer's OWN latest request row (and nobody else's). The id is the
        # viewer's own data, so it's safe to return — it lets them cancel a
        # pending request without exposing anyone else's row.
        own = db.execute(
            "SELECT id, status FROM participants "
            "WHERE trip_id = ? AND friend_id = ? ORDER BY id DESC LIMIT 1",
            (trip["id"], viewer["friend_id"]),
        ).fetchone()
        out["my_status"] = own["status"] if own else None
        out["my_request_id"] = own["id"] if own and own["status"] == "pending" else None
        out["can_request"] = (tier == "full" and out["my_status"] not in ("pending", "confirmed"))

    if is_owner:
        out["privacy"] = trip["privacy"]
        out["notes"] = trip["notes"]
        out["updated_at"] = trip["updated_at"]
        # Owner sees the whole roster: pending first, then confirmed, then declined.
        roster = db.execute(
            "SELECT id, friend_id, display_name, status, source, created_at, decided_at "
            "FROM participants WHERE trip_id = ? "
            "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END, id",
            (trip["id"],),
        ).fetchall()
        out["participants"] = [dict(r) for r in roster]
        out["pending_count"] = sum(1 for r in roster if r["status"] == "pending")

    return out


def friend_public(row, base_url):
    """Owner-facing friend record incl. token + ready-to-share invite link."""
    return {
        "id": row["id"],
        "name": row["name"],
        "tier": row["tier"],
        "enabled": bool(row["enabled"]),
        "token": row["token"],
        "invite_link": f"{base_url}/?u={row['token']}",
        "created_at": row["created_at"],
    }


# ─── API: meta ─────────────────────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(ok=True, version="1.0.0")


@app.route("/api/admin/verify", methods=["POST"])
def admin_verify():
    require_admin()
    return jsonify(ok=True, role="owner")


@app.route("/api/me", methods=["GET"])
def me():
    viewer = require_viewer()
    if viewer["role"] == "owner":
        return jsonify(role="owner")
    return jsonify(role="friend", name=viewer["name"], tier=viewer["tier"])


# ─── API: calendar (tier-projected reads) ──────────────────────────────────

@app.route("/api/calendar", methods=["GET"])
def calendar():
    """Trips overlapping [from, to], projected to the caller's tier.

    Uses an OVERLAP test (start <= :to AND end >= :from), not containment, so a
    multi-day trip that began before the window but is still ongoing inside it
    is included.
    """
    viewer = require_viewer()
    frm = request.args.get("from", "")
    to = request.args.get("to", "")
    if not valid_date(frm) or not valid_date(to):
        fail(400, "bad_date")
    if to < frm:
        fail(400, "bad_range")
    if (datetime.strptime(to, "%Y-%m-%d") - datetime.strptime(frm, "%Y-%m-%d")).days > MAX_RANGE_DAYS:
        fail(400, "range_too_large")

    db = get_db()
    rows = db.execute(
        "SELECT * FROM trips WHERE start_date <= ? AND end_date >= ? "
        "ORDER BY start_date, id",
        (to, frm),
    ).fetchall()
    return jsonify(
        from_=frm, to=to,
        trips=[project_trip(t, viewer, db) for t in rows],
    )


@app.route("/api/trips/<int:trip_id>", methods=["GET"])
def get_trip(trip_id):
    viewer = require_viewer()
    db = get_db()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if trip is None:
        abort(404)
    return jsonify(trip=project_trip(trip, viewer, db))


# ─── API: seat requests (friend, full tier) ────────────────────────────────

@app.route("/api/trips/<int:trip_id>/request-seat", methods=["POST"])
def request_seat(trip_id):
    viewer = get_viewer()
    if viewer["role"] != "friend":
        abort(401)
    db = get_db()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if trip is None:
        abort(404)
    # Only a full-tier friend on a normal-privacy trip may request. Every friend
    # tier already sees the trip exists (busy span) via the calendar, so a 403
    # here leaks nothing — it's purely "you can't request this".
    if not (trip["privacy"] == "normal" and viewer["tier"] == "full"):
        abort(403)

    try:
        with writing() as db:
            # The partial unique index blocks a second active (pending/confirmed)
            # row for this (trip, friend) — so double-taps and request-while-
            # confirmed both fail here. A previously declined row doesn't block.
            db.execute(
                "INSERT INTO participants "
                "(trip_id, friend_id, display_name, status, source, created_at) "
                "VALUES (?, ?, ?, 'pending', 'request', datetime('now'))",
                (trip_id, viewer["friend_id"], viewer["name"]),
            )
    except sqlite3.IntegrityError:
        fail(409, "already_requested")

    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    return jsonify(trip=project_trip(trip, get_viewer(), get_db())), 201


@app.route("/api/requests/<int:req_id>", methods=["DELETE"])
def cancel_request(req_id):
    """A friend cancels their OWN pending request (IDOR-guarded)."""
    viewer = get_viewer()
    if viewer["role"] != "friend":
        abort(401)
    with writing() as db:
        cur = db.execute(
            "DELETE FROM participants "
            "WHERE id = ? AND friend_id = ? AND status = 'pending'",
            (req_id, viewer["friend_id"]),
        )
        if cur.rowcount != 1:
            fail(404, "not_found")
    return jsonify(ok=True)


# ─── API: admin — trips ────────────────────────────────────────────────────

@app.route("/api/admin/trips", methods=["GET"])
def admin_list_trips():
    require_admin()
    viewer = get_viewer()
    db = get_db()
    rows = db.execute("SELECT * FROM trips ORDER BY start_date, id").fetchall()
    return jsonify(trips=[project_trip(t, viewer, db) for t in rows])


@app.route("/api/admin/trips", methods=["POST"])
def admin_create_trip():
    require_admin()
    body = request.get_json(silent=True)
    data = parse_trip_payload(body)
    with writing() as db:
        cur = db.execute(
            "INSERT INTO trips "
            "(destination, location_label, start_date, end_date, car_seats, notes, category, privacy, created_at, updated_at) "
            "VALUES (:destination, :location_label, :start_date, :end_date, :car_seats, :notes, :category, :privacy, datetime('now'), datetime('now'))",
            data,
        )
        trip_id = cur.lastrowid
    db = get_db()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    return jsonify(trip=project_trip(trip, get_viewer(), db)), 201


@app.route("/api/admin/trips/<int:trip_id>", methods=["PUT"])
def admin_update_trip(trip_id):
    require_admin()
    body = request.get_json(silent=True)
    with writing() as db:
        existing = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if existing is None:
            fail(404, "not_found")
        data = parse_trip_payload(body, existing=existing)
        # Don't allow shrinking capacity below the people already confirmed.
        confirmed = db.execute(
            "SELECT COUNT(*) AS n FROM participants WHERE trip_id = ? AND status = 'confirmed'",
            (trip_id,),
        ).fetchone()["n"]
        if data["car_seats"] < confirmed:
            fail(409, "seats_below_confirmed")
        data["id"] = trip_id
        db.execute(
            "UPDATE trips SET destination=:destination, location_label=:location_label, "
            "start_date=:start_date, end_date=:end_date, car_seats=:car_seats, notes=:notes, "
            "category=:category, privacy=:privacy, updated_at=datetime('now') WHERE id=:id",
            data,
        )
    db = get_db()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    return jsonify(trip=project_trip(trip, get_viewer(), db))


@app.route("/api/admin/trips/<int:trip_id>", methods=["DELETE"])
def admin_delete_trip(trip_id):
    require_admin()
    with writing() as db:
        cur = db.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
        if cur.rowcount != 1:
            fail(404, "not_found")
    return jsonify(ok=True)


# ─── API: admin — participants & requests ──────────────────────────────────

@app.route("/api/admin/trips/<int:trip_id>/participants", methods=["POST"])
def admin_add_participant(trip_id):
    """Owner adds a confirmed participant — a named guest or an existing friend.

    Capacity is enforced atomically so confirmed never exceeds car_seats.
    """
    require_admin()
    body = request.get_json(silent=True) or {}
    friend_id = body.get("friend_id")
    name = body.get("name")

    with writing() as db:
        trip = db.execute("SELECT car_seats FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if trip is None:
            fail(404, "not_found")

        if friend_id is not None:
            if not isinstance(friend_id, int) or isinstance(friend_id, bool):
                fail(400, "bad_friend_id")
            friend = db.execute("SELECT name FROM friends WHERE id = ?", (friend_id,)).fetchone()
            if friend is None:
                fail(404, "not_found")
            display_name = friend["name"]
        else:
            display_name = clean_str(name, MAX_NAME)

        confirmed = db.execute(
            "SELECT COUNT(*) AS n FROM participants WHERE trip_id = ? AND status = 'confirmed'",
            (trip_id,),
        ).fetchone()["n"]
        if confirmed >= trip["car_seats"]:
            fail(409, "seat_unavailable")

        try:
            db.execute(
                "INSERT INTO participants "
                "(trip_id, friend_id, display_name, status, source, created_at, decided_at) "
                "VALUES (?, ?, ?, 'confirmed', 'owner', datetime('now'), datetime('now'))",
                (trip_id, friend_id, display_name),
            )
        except sqlite3.IntegrityError:
            # That friend already has an active (pending/confirmed) row — approve
            # their request instead of double-adding.
            fail(409, "already_active")

    db = get_db()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    return jsonify(trip=project_trip(trip, get_viewer(), db)), 201


@app.route("/api/admin/participants/<int:pid>", methods=["DELETE"])
def admin_remove_participant(pid):
    """Remove any participant (confirmed/pending/declined) — frees a seat."""
    require_admin()
    with writing() as db:
        cur = db.execute("DELETE FROM participants WHERE id = ?", (pid,))
        if cur.rowcount != 1:
            fail(404, "not_found")
    return jsonify(ok=True)


@app.route("/api/admin/requests", methods=["GET"])
def admin_list_requests():
    """Pending seat requests across all trips — the owner's approval queue."""
    require_admin()
    db = get_db()
    rows = db.execute(
        "SELECT p.id, p.trip_id, p.display_name, p.created_at, "
        "       t.destination, t.start_date, t.end_date, t.car_seats, "
        "       (SELECT COUNT(*) FROM participants c "
        "          WHERE c.trip_id = t.id AND c.status = 'confirmed') AS confirmed_count "
        "FROM participants p JOIN trips t ON t.id = p.trip_id "
        "WHERE p.status = 'pending' ORDER BY p.created_at, p.id"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["free_seats"] = max(0, r["car_seats"] - r["confirmed_count"])
        out.append(d)
    return jsonify(requests=out)


@app.route("/api/admin/requests/<int:req_id>/approve", methods=["POST"])
def admin_approve_request(req_id):
    """pending → confirmed, atomically guarded against overbooking.

    The conditional UPDATE confirms ONLY if the trip still has a free seat at
    commit time, so two near-simultaneous approvals on a 1-seat trip can't both
    win (separate gunicorn workers; the count and the update are one statement
    under the writer lock).
    """
    require_admin()
    with writing() as db:
        cur = db.execute(
            "UPDATE participants SET status = 'confirmed', decided_at = datetime('now') "
            "WHERE id = ? AND status = 'pending' "
            "  AND (SELECT COUNT(*) FROM participants c "
            "         WHERE c.trip_id = participants.trip_id AND c.status = 'confirmed') "
            "    < (SELECT car_seats FROM trips WHERE id = participants.trip_id)",
            (req_id,),
        )
        if cur.rowcount != 1:
            row = db.execute("SELECT status FROM participants WHERE id = ?", (req_id,)).fetchone()
            if row is None:
                fail(404, "not_found")
            if row["status"] != "pending":
                fail(409, "already_decided")
            fail(409, "seat_unavailable")
        trip_id = db.execute(
            "SELECT trip_id FROM participants WHERE id = ?", (req_id,)
        ).fetchone()["trip_id"]
    db = get_db()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    return jsonify(trip=project_trip(trip, get_viewer(), db))


@app.route("/api/admin/requests/<int:req_id>/decline", methods=["POST"])
def admin_decline_request(req_id):
    """pending → declined. (Removing a CONFIRMED passenger is a separate
    action: DELETE /api/admin/participants/:id, which recounts seats.)"""
    require_admin()
    with writing() as db:
        cur = db.execute(
            "UPDATE participants SET status = 'declined', decided_at = datetime('now') "
            "WHERE id = ? AND status = 'pending'",
            (req_id,),
        )
        if cur.rowcount != 1:
            row = db.execute("SELECT id FROM participants WHERE id = ?", (req_id,)).fetchone()
            fail(404 if row is None else 409, "not_found" if row is None else "already_decided")
        trip_id = db.execute(
            "SELECT trip_id FROM participants WHERE id = ?", (req_id,)
        ).fetchone()["trip_id"]
    db = get_db()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    return jsonify(trip=project_trip(trip, get_viewer(), db))


# ─── API: admin — friends ──────────────────────────────────────────────────

@app.route("/api/admin/friends", methods=["GET"])
def admin_list_friends():
    require_admin()
    db = get_db()
    rows = db.execute("SELECT * FROM friends ORDER BY name COLLATE NOCASE, id").fetchall()
    base = request.host_url.rstrip("/")
    return jsonify(friends=[friend_public(r, base) for r in rows])


@app.route("/api/admin/friends", methods=["POST"])
def admin_create_friend():
    require_admin()
    body = request.get_json(silent=True) or {}
    name = clean_str(body.get("name"), MAX_NAME)
    tier = body.get("tier")
    if tier not in TIERS:
        fail(400, "bad_tier")
    token = secrets.token_urlsafe(24)
    with writing() as db:
        cur = db.execute(
            "INSERT INTO friends (name, token, tier, enabled, created_at) "
            "VALUES (?, ?, ?, 1, datetime('now'))",
            (name, token, tier),
        )
        fid = cur.lastrowid
    db = get_db()
    row = db.execute("SELECT * FROM friends WHERE id = ?", (fid,)).fetchone()
    return jsonify(friend=friend_public(row, request.host_url.rstrip("/"))), 201


@app.route("/api/admin/friends/<int:fid>", methods=["PUT"])
def admin_update_friend(fid):
    """Edit name/tier/enabled and/or rotate the token.

    If the friend ends up below 'full' tier or disabled, their PENDING requests
    are declined (only a full, enabled friend may hold a live request) so the
    seat math stays correct. Confirmed seats are left in place.
    """
    require_admin()
    body = request.get_json(silent=True) or {}
    with writing() as db:
        friend = db.execute("SELECT * FROM friends WHERE id = ?", (fid,)).fetchone()
        if friend is None:
            fail(404, "not_found")

        name = clean_str(body.get("name"), MAX_NAME) if "name" in body else friend["name"]
        tier = body.get("tier") if "tier" in body else friend["tier"]
        if tier not in TIERS:
            fail(400, "bad_tier")
        enabled = friend["enabled"]
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                fail(400, "bad_enabled")
            enabled = 1 if body["enabled"] else 0
        token = secrets.token_urlsafe(24) if body.get("rotate") else friend["token"]

        db.execute(
            "UPDATE friends SET name = ?, tier = ?, enabled = ?, token = ? WHERE id = ?",
            (name, tier, enabled, token, fid),
        )
        if tier != "full" or enabled == 0:
            db.execute(
                "UPDATE participants SET status = 'declined', decided_at = datetime('now') "
                "WHERE friend_id = ? AND status = 'pending'",
                (fid,),
            )
    db = get_db()
    row = db.execute("SELECT * FROM friends WHERE id = ?", (fid,)).fetchone()
    return jsonify(friend=friend_public(row, request.host_url.rstrip("/")))


@app.route("/api/admin/friends/<int:fid>", methods=["DELETE"])
def admin_delete_friend(fid):
    """Delete a friend. Their pending/declined rows are removed; confirmed seats
    are kept as named guests (friend_id → NULL via ON DELETE SET NULL)."""
    require_admin()
    with writing() as db:
        friend = db.execute("SELECT id FROM friends WHERE id = ?", (fid,)).fetchone()
        if friend is None:
            fail(404, "not_found")
        db.execute(
            "DELETE FROM participants WHERE friend_id = ? AND status != 'confirmed'",
            (fid,),
        )
        db.execute("DELETE FROM friends WHERE id = ?", (fid,))
    return jsonify(ok=True)


# ─── Static frontend ───────────────────────────────────────────────────────
# Served from the same origin so the API and frontend share a host. The API
# routes above are more specific and always match first.

@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(FRONTEND_DIR, path)


# ─── Boot ──────────────────────────────────────────────────────────────────

init_db()

if __name__ == "__main__":
    # Dev-only server. Production (Docker) uses gunicorn.
    app.run(host="0.0.0.0", port=8000, debug=True)
