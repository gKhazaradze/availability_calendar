"""Tests for the availability-calendar backend.

The security-critical surface is (1) tier projection — a lower tier must never
receive fields it shouldn't see, in ANY response or error body — and (2) the
seat-request state machine — no overbooking, no double-requests, no IDOR. Those
are what these lock down, alongside the two-credential auth gate and date logic.
"""

from conftest import ADMIN, friend_headers

DEST = "Gudauri"


# ─── Meta / auth gate ──────────────────────────────────────────────────────

def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_admin_verify_requires_key(client):
    assert client.post("/api/admin/verify").status_code == 401
    assert client.post("/api/admin/verify", headers={"X-Admin-Key": "nope"}).status_code == 401
    assert client.post("/api/admin/verify", headers=ADMIN).status_code == 200


def test_admin_endpoints_gated(client):
    # No key / wrong key → 401 on a representative admin route.
    assert client.get("/api/admin/trips").status_code == 401
    assert client.get("/api/admin/trips", headers={"X-Admin-Key": "x"}).status_code == 401
    assert client.get("/api/admin/trips", headers=ADMIN).status_code == 200


def test_anonymous_calendar_shows_busy_spans_without_detail(client, make_trip):
    # A richly-detailed trip...
    make_trip(destination="SecretValley", car_seats=3, notes="leaving 6am",
              start_date="2026-01-12", end_date="2026-01-14")
    # ...is visible to an anonymous viewer only as an opaque busy span.
    r = client.get("/api/calendar?from=2026-01-01&to=2026-01-31")
    assert r.status_code == 200
    trips = r.get_json()["trips"]
    assert len(trips) == 1
    t = trips[0]
    assert t["status"] == "busy"
    assert t["start_date"] == "2026-01-12" and t["end_date"] == "2026-01-14"
    for forbidden in ("destination", "location_label", "car_seats", "free_seats",
                      "participants", "notes", "category", "on_trip", "my_status",
                      "can_request"):
        assert forbidden not in t, forbidden
    # Nothing sensitive in the raw body either.
    assert b"SecretValley" not in r.data and b"leaving 6am" not in r.data


def test_anonymous_me_and_admin_still_401(client):
    # The public read is calendar-only; identity + admin stay gated so the
    # frontend still resolves an anonymous visitor as "anon".
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/admin/trips").status_code == 401


def test_me_reports_role_and_tier(client, make_friend):
    f = make_friend(tier="basic")
    assert client.get("/api/me", headers=ADMIN).get_json()["role"] == "owner"
    j = client.get("/api/me", headers=friend_headers(f["token"])).get_json()
    assert j["role"] == "friend" and j["tier"] == "basic"


def test_disabled_friend_token_401(client, make_friend):
    f = make_friend(tier="full")
    client.put(f"/api/admin/friends/{f['id']}", json={"enabled": False}, headers=ADMIN)
    assert client.get("/api/me", headers=friend_headers(f["token"])).status_code == 401


def test_rotated_token_invalidates_old(client, make_friend):
    f = make_friend(tier="full")
    old = f["token"]
    r = client.put(f"/api/admin/friends/{f['id']}", json={"rotate": True}, headers=ADMIN)
    new = r.get_json()["friend"]["token"]
    assert new != old
    assert client.get("/api/me", headers=friend_headers(old)).status_code == 401
    assert client.get("/api/me", headers=friend_headers(new)).status_code == 200


# ─── Tier projection: the leak tests ───────────────────────────────────────

def _trip_for(client, token, trip_id):
    return client.get(f"/api/trips/{trip_id}", headers=friend_headers(token)).get_json()["trip"]


def test_busy_tier_sees_only_busy_span(client, make_friend, make_trip):
    t = make_trip(destination=DEST, notes="leaving 6am")
    f = make_friend(tier="busy")
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["status"] == "busy"
    for forbidden in ("destination", "location_label", "car_seats", "free_seats",
                      "participants", "notes", "category"):
        assert forbidden not in trip, forbidden


def test_basic_tier_sees_seats_not_people(client, make_friend, make_trip):
    t = make_trip(destination=DEST, car_seats=3, notes="leaving 6am")
    f = make_friend(tier="basic")
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["destination"] == DEST
    assert trip["free_seats"] == 3
    assert "participants" not in trip
    assert "notes" not in trip
    assert trip["can_request"] is False        # only full may request


def test_full_tier_sees_everything_visible(client, make_friend, make_trip):
    t = make_trip(destination=DEST, car_seats=3, notes="leaving 6am")
    f = make_friend(tier="full")
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["destination"] == DEST
    assert trip["notes"] == "leaving 6am"
    assert trip["participants"] == []
    assert trip["my_status"] is None
    assert trip["can_request"] is True


def test_full_tier_sees_confirmed_names_not_status(client, make_friend, make_trip):
    t = make_trip(car_seats=3)
    client.post(f"/api/admin/trips/{t['id']}/participants", json={"name": "Alex"}, headers=ADMIN)
    f = make_friend(tier="full")
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["participants"] == [{"name": "Alex"}]
    # No status/source leak to a friend.
    assert all(set(p.keys()) == {"name"} for p in trip["participants"])


def test_busy_only_privacy_hides_detail_from_full_friend(client, make_friend, make_trip):
    t = make_trip(destination=DEST, privacy="busy_only", car_seats=4)
    f = make_friend(tier="full")
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["status"] == "busy"
    assert "destination" not in trip
    assert "free_seats" not in trip
    # Owner still sees it fully.
    owner_trip = client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]
    assert owner_trip["destination"] == DEST


def test_pending_requests_not_surfaced_to_other_full_friends(client, make_friend, make_trip):
    t = make_trip(car_seats=3)
    asker = make_friend(name="Asker", tier="full")
    other = make_friend(name="Other", tier="full")
    client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(asker["token"]))
    trip = _trip_for(client, other["token"], t["id"])
    assert "pending_count" not in trip
    assert trip["participants"] == []          # pending asker not shown
    assert "1" not in str(trip.get("participants"))


def test_owner_sees_pending_with_status(client, make_friend, make_trip):
    t = make_trip(car_seats=3)
    asker = make_friend(name="Asker", tier="full")
    client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(asker["token"]))
    trip = client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]
    assert trip["pending_count"] == 1
    assert trip["participants"][0]["status"] == "pending"
    assert trip["privacy"] == "normal"


def test_name_match_marks_trip_as_mine(client, make_friend, make_trip):
    t = make_trip(car_seats=3)
    # Owner adds a GUEST named "Alex" (no friend_id link).
    client.post(f"/api/admin/trips/{t['id']}/participants", json={"name": "Alex"}, headers=ADMIN)
    # Case-insensitive name match: friend "alex" is flagged on_trip even at busy tier.
    alex = make_friend(name="alex", tier="busy")
    sam = make_friend(name="Sam", tier="busy")
    a = _trip_for(client, alex["token"], t["id"])
    s = _trip_for(client, sam["token"], t["id"])
    assert a["on_trip"] is True
    assert s["on_trip"] is False
    # Still no detail/name leak at busy tier — only the boolean.
    assert "participants" not in a and "destination" not in a


def test_busy_only_still_marks_own_trip_without_detail(client, make_friend, make_trip):
    t = make_trip(privacy="busy_only", car_seats=3)
    client.post(f"/api/admin/trips/{t['id']}/participants", json={"name": "Alex"}, headers=ADMIN)
    alex = make_friend(name="Alex", tier="full")
    a = _trip_for(client, alex["token"], t["id"])
    assert a["on_trip"] is True            # knows it's their own day
    assert "destination" not in a          # private trip still hides everything else


def test_confirmed_by_request_sets_on_trip_and_blocks_request(client, make_friend, make_trip):
    t = make_trip(car_seats=2)
    f = make_friend(tier="full")
    client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(f["token"]))
    rid = client.get("/api/admin/requests", headers=ADMIN).get_json()["requests"][0]["id"]
    client.post(f"/api/admin/requests/{rid}/approve", headers=ADMIN)
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["on_trip"] is True
    assert trip["can_request"] is False


def test_error_bodies_never_leak_destination(client, make_friend, make_trip):
    t = make_trip(destination="SecretValley", car_seats=1, notes="secret note")
    basic = make_friend(tier="basic")
    # 403 when a basic friend tries to request — must not echo the destination.
    r = client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(basic["token"]))
    assert r.status_code == 403
    assert b"SecretValley" not in r.data and b"secret note" not in r.data
    # 404 for an unknown trip — generic body.
    r = client.get("/api/trips/99999", headers=friend_headers(basic["token"]))
    assert r.status_code == 404
    assert r.get_json() == {"error": "not_found"}


# ─── Seat requests: state machine ──────────────────────────────────────────

def test_request_then_owner_approve_confirms_and_drops_seat(client, make_friend, make_trip):
    t = make_trip(car_seats=2)
    f = make_friend(tier="full")
    r = client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(f["token"]))
    assert r.status_code == 201
    echo = r.get_json()["trip"]
    assert echo["my_status"] == "pending" and echo["can_request"] is False
    # Owner approves.
    req = client.get("/api/admin/requests", headers=ADMIN).get_json()["requests"]
    assert len(req) == 1
    rid = req[0]["id"]
    assert client.post(f"/api/admin/requests/{rid}/approve", headers=ADMIN).status_code == 200
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["my_status"] == "confirmed"
    assert trip["free_seats"] == 1
    assert trip["participants"] == [{"name": "Friend"}]


def test_my_status_is_per_viewer_and_no_id_leaks(client, make_friend, make_trip):
    t = make_trip(car_seats=3)
    asker = make_friend(name="Asker", tier="full")
    other = make_friend(name="Other", tier="full")
    client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(asker["token"]))
    mine = _trip_for(client, asker["token"], t["id"])
    assert mine["my_status"] == "pending"
    # No participant id is exposed to friends at all (self-removal is by friend_id).
    assert "my_request_id" not in mine and "my_participant_id" not in mine
    theirs = _trip_for(client, other["token"], t["id"])
    assert theirs["my_status"] is None


def test_overbooking_blocked_on_approve(client, make_friend, make_trip):
    t = make_trip(car_seats=1)
    a = make_friend(name="A", tier="full")
    b = make_friend(name="B", tier="full")
    client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(a["token"]))
    client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(b["token"]))
    reqs = client.get("/api/admin/requests", headers=ADMIN).get_json()["requests"]
    assert len(reqs) == 2
    r1 = client.post(f"/api/admin/requests/{reqs[0]['id']}/approve", headers=ADMIN)
    r2 = client.post(f"/api/admin/requests/{reqs[1]['id']}/approve", headers=ADMIN)
    assert r1.status_code == 200
    assert r2.status_code == 409 and r2.get_json()["error"] == "seat_unavailable"
    owner_trip = client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]
    assert sum(1 for p in owner_trip["participants"] if p["status"] == "confirmed") == 1


def test_double_request_blocked(client, make_friend, make_trip):
    t = make_trip(car_seats=3)
    f = make_friend(tier="full")
    assert client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(f["token"])).status_code == 201
    r = client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(f["token"]))
    assert r.status_code == 409 and r.get_json()["error"] == "already_requested"


def test_decline_then_rerequest_allowed(client, make_friend, make_trip):
    t = make_trip(car_seats=3)
    f = make_friend(tier="full")
    client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(f["token"]))
    rid = client.get("/api/admin/requests", headers=ADMIN).get_json()["requests"][0]["id"]
    assert client.post(f"/api/admin/requests/{rid}/decline", headers=ADMIN).status_code == 200
    # Re-request after a decline works (declined rows are excluded from the index).
    assert client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(f["token"])).status_code == 201


def test_busy_and_basic_cannot_request(client, make_friend, make_trip):
    t = make_trip(car_seats=3)
    for tier in ("busy", "basic"):
        f = make_friend(name=tier, tier=tier)
        r = client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(f["token"]))
        assert r.status_code == 403, tier


def test_self_leave_affects_only_caller(client, make_friend, make_trip):
    t = make_trip(car_seats=3)
    a = make_friend(name="A", tier="full")
    b = make_friend(name="B", tier="full")
    client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(a["token"]))
    client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(b["token"]))
    # The endpoint resolves by friend_id — no id to guess, so A only ever removes A.
    assert client.delete(f"/api/trips/{t['id']}/me", headers=friend_headers(a["token"])).status_code == 200
    names = [r["display_name"] for r in client.get("/api/admin/requests", headers=ADMIN).get_json()["requests"]]
    assert names == ["B"]                       # B's request untouched


def test_friend_with_no_row_cannot_leave(client, make_friend, make_trip):
    t = make_trip(car_seats=3)
    f = make_friend(tier="full")
    assert client.delete(f"/api/trips/{t['id']}/me", headers=friend_headers(f["token"])).status_code == 404


def test_friend_can_leave_confirmed_and_frees_seat(client, make_friend, make_trip):
    t = make_trip(car_seats=2)
    f = make_friend(tier="full")
    client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(f["token"]))
    rid = client.get("/api/admin/requests", headers=ADMIN).get_json()["requests"][0]["id"]
    client.post(f"/api/admin/requests/{rid}/approve", headers=ADMIN)
    assert _trip_for(client, f["token"], t["id"])["free_seats"] == 1
    # Now confirmed — the friend can leave, which frees the seat.
    assert client.delete(f"/api/trips/{t['id']}/me", headers=friend_headers(f["token"])).status_code == 200
    after = _trip_for(client, f["token"], t["id"])
    assert after["on_trip"] is False
    assert after["my_status"] is None
    assert after["free_seats"] == 2


def test_owner_add_respects_capacity(client, make_trip):
    t = make_trip(car_seats=1)
    assert client.post(f"/api/admin/trips/{t['id']}/participants", json={"name": "Alex"}, headers=ADMIN).status_code == 201
    r = client.post(f"/api/admin/trips/{t['id']}/participants", json={"name": "Sam"}, headers=ADMIN)
    assert r.status_code == 409 and r.get_json()["error"] == "seat_unavailable"


# ─── Edit transitions ──────────────────────────────────────────────────────

def test_reduce_seats_below_confirmed_rejected(client, make_trip):
    t = make_trip(car_seats=2)
    client.post(f"/api/admin/trips/{t['id']}/participants", json={"name": "Alex"}, headers=ADMIN)
    client.post(f"/api/admin/trips/{t['id']}/participants", json={"name": "Sam"}, headers=ADMIN)
    r = client.put(f"/api/admin/trips/{t['id']}", json={"car_seats": 1}, headers=ADMIN)
    assert r.status_code == 409 and r.get_json()["error"] == "seats_below_confirmed"


def test_tier_downgrade_declines_pending(client, make_friend, make_trip):
    t = make_trip(car_seats=3)
    f = make_friend(tier="full")
    client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(f["token"]))
    assert len(client.get("/api/admin/requests", headers=ADMIN).get_json()["requests"]) == 1
    # Downgrade full -> basic: their pending request must be declined.
    client.put(f"/api/admin/friends/{f['id']}", json={"tier": "basic"}, headers=ADMIN)
    assert client.get("/api/admin/requests", headers=ADMIN).get_json()["requests"] == []


def test_delete_trip_cascades_participants(client, make_trip):
    t = make_trip(car_seats=2)
    client.post(f"/api/admin/trips/{t['id']}/participants", json={"name": "Alex"}, headers=ADMIN)
    # Delete succeeds despite a child row -> ON DELETE CASCADE (FK enforcement on).
    assert client.delete(f"/api/admin/trips/{t['id']}", headers=ADMIN).status_code == 200
    assert client.get(f"/api/trips/{t['id']}", headers=ADMIN).status_code == 404


# ─── Dates / calendar overlap ──────────────────────────────────────────────

def test_create_rejects_bad_dates(client):
    bad = {"destination": "X", "start_date": "2026-1-5", "end_date": "2026-01-06"}
    assert client.post("/api/admin/trips", json=bad, headers=ADMIN).status_code == 400
    inverted = {"destination": "X", "start_date": "2026-02-10", "end_date": "2026-02-01"}
    r = client.post("/api/admin/trips", json=inverted, headers=ADMIN)
    assert r.status_code == 400 and r.get_json()["error"] == "end_before_start"


def test_calendar_inclusive_and_overlap(client, make_friend, make_trip):
    # Multi-day stay that starts BEFORE the query window but is ongoing inside it.
    t = make_trip(destination="Bansko", start_date="2026-03-10", end_date="2026-03-20", car_seats=2)
    f = make_friend(tier="basic")
    h = friend_headers(f["token"])
    # Window fully inside the trip -> overlap query must still return it.
    j = client.get("/api/calendar?from=2026-03-14&to=2026-03-16", headers=h).get_json()
    assert [x["id"] for x in j["trips"]] == [t["id"]]
    # Inclusive end: a window touching only the last day still returns it.
    j = client.get("/api/calendar?from=2026-03-20&to=2026-03-25", headers=h).get_json()
    assert [x["id"] for x in j["trips"]] == [t["id"]]
    # A window entirely after the trip returns nothing.
    j = client.get("/api/calendar?from=2026-03-21&to=2026-03-25", headers=h).get_json()
    assert j["trips"] == []


def test_calendar_rejects_oversized_and_bad_range(client, make_friend):
    f = make_friend(tier="busy")
    h = friend_headers(f["token"])
    assert client.get("/api/calendar?from=2026-01-01&to=2030-01-01", headers=h).status_code == 400
    assert client.get("/api/calendar?from=2026-05-01&to=2026-04-01", headers=h).status_code == 400
    assert client.get("/api/calendar?from=bad&to=2026-04-01", headers=h).status_code == 400


# ─── Friend login (name + birthday) ────────────────────────────────────────
# Login exchanges a name+birthday for the friend's existing bearer token; the
# token still drives every later request, so these tests cover only the new
# credential exchange and its throttle. Distinct X-Forwarded-For values isolate
# the per-IP rate-limit tests from each other.

def _login(client, name, birthday, ip="10.0.0.1"):
    return client.post(
        "/api/login",
        json={"name": name, "birthday": birthday},
        headers={"X-Forwarded-For": ip},
    )


def test_login_success_returns_working_token(client, make_friend):
    make_friend(name="Nino", tier="full", birthday="1995-04-10")
    r = _login(client, "Nino", "1995-04-10")
    assert r.status_code == 200
    body = r.get_json()
    assert body["name"] == "Nino" and body["tier"] == "full"
    me = client.get("/api/me", headers=friend_headers(body["token"]))
    assert me.status_code == 200 and me.get_json()["tier"] == "full"


def test_login_is_case_insensitive_and_trims_name(client, make_friend):
    make_friend(name="Nino", tier="basic", birthday="1995-04-10")
    assert _login(client, "  nino ", "1995-04-10").status_code == 200


def test_login_wrong_birthday_is_opaque_401(client, make_friend):
    make_friend(name="Nino", tier="full", birthday="1995-04-10")
    r = _login(client, "Nino", "1995-04-11")
    assert r.status_code == 401 and r.get_json()["error"] == "bad_login"


def test_login_unknown_name_401(client, make_friend):
    make_friend(name="Nino", tier="full", birthday="1995-04-10")
    r = _login(client, "Ghost", "1995-04-10")
    assert r.status_code == 401 and r.get_json()["error"] == "bad_login"


def test_login_disabled_friend_denied(client, make_friend):
    f = make_friend(name="Nino", tier="full", birthday="1995-04-10")
    client.put(f"/api/admin/friends/{f['id']}", json={"enabled": False}, headers=ADMIN)
    assert _login(client, "Nino", "1995-04-10").status_code == 401


def test_login_friend_without_birthday_cannot_login(client, make_friend):
    make_friend(name="Nino", tier="full")            # NULL birthday
    assert _login(client, "Nino", "1995-04-10").status_code == 401


def test_login_requires_name_and_valid_birthday(client, make_friend):
    make_friend(name="Nino", tier="full", birthday="1995-04-10")
    assert client.post("/api/login", json={"name": "Nino"}).status_code == 400            # no birthday
    assert client.post("/api/login", json={"name": "Nino", "birthday": "nope"}).status_code == 400
    assert client.post("/api/login", json={"birthday": "1995-04-10"}).status_code == 400  # no name


def test_login_ambiguous_name_and_birthday_denied(client, make_friend):
    # Two enabled friends sharing BOTH name and birthday: login can't pick one,
    # so it denies rather than guess — and leaks nothing about the collision.
    make_friend(name="Sam", tier="full", birthday="2000-01-01")
    make_friend(name="Sam", tier="busy", birthday="2000-01-01")
    r = _login(client, "Sam", "2000-01-01")
    assert r.status_code == 401 and r.get_json()["error"] == "bad_login"


def test_login_rate_limited_per_ip(client, make_friend):
    make_friend(name="Nino", tier="full", birthday="1995-04-10")
    for _ in range(10):                               # 10 misses are allowed
        assert _login(client, "Nino", "1900-01-01", ip="9.9.9.9").status_code == 401
    assert _login(client, "Nino", "1900-01-01", ip="9.9.9.9").status_code == 429
    # Lockout blocks even the CORRECT credential during the window...
    assert _login(client, "Nino", "1995-04-10", ip="9.9.9.9").status_code == 429
    # ...but a different IP is unaffected.
    assert _login(client, "Nino", "1995-04-10", ip="8.8.8.8").status_code == 200


def test_login_success_clears_failure_streak(client, make_friend):
    make_friend(name="Nino", tier="full", birthday="1995-04-10")
    for _ in range(9):
        assert _login(client, "Nino", "1900-01-01", ip="7.7.7.7").status_code == 401
    assert _login(client, "Nino", "1995-04-10", ip="7.7.7.7").status_code == 200   # resets streak
    for _ in range(9):                                 # 9 fresh misses don't trip the limit
        assert _login(client, "Nino", "1900-01-01", ip="7.7.7.7").status_code == 401


def test_admin_create_rejects_bad_birthday(client):
    r = client.post("/api/admin/friends",
                    json={"name": "Nino", "tier": "full", "birthday": "not-a-date"}, headers=ADMIN)
    assert r.status_code == 400


def test_admin_can_set_and_clear_birthday(client, make_friend):
    f = make_friend(name="Nino", tier="full")          # created with no birthday
    assert f["birthday"] is None
    client.put(f"/api/admin/friends/{f['id']}", json={"birthday": "1995-04-10"}, headers=ADMIN)
    assert _login(client, "Nino", "1995-04-10").status_code == 200
    client.put(f"/api/admin/friends/{f['id']}", json={"birthday": ""}, headers=ADMIN)   # clear
    assert _login(client, "Nino", "1995-04-10").status_code == 401


def test_login_throttle_window_expires(client, make_friend):
    # Failures older than LOGIN_WINDOW_SEC don't count: 10 stale misses leave the
    # gate open (a wrong guess still 401s, not 429). Seed the rows directly with
    # an old timestamp rather than waiting out the real 15-minute window.
    import os
    import sqlite3
    make_friend(name="Nino", tier="full", birthday="1995-04-10")
    con = sqlite3.connect(os.environ["AVAILABILITY_DB"])
    con.executemany(
        "INSERT INTO login_attempts (ip, created_at) VALUES (?, datetime('now', '-2 hours'))",
        [("5.5.5.5",)] * 10,
    )
    con.commit()
    con.close()
    # Not locked out — the stale rows are outside the window.
    assert _login(client, "Nino", "1900-01-01", ip="5.5.5.5").status_code == 401
    assert _login(client, "Nino", "1995-04-10", ip="5.5.5.5").status_code == 200
