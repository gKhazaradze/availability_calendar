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
                      "can_request", "activities", "area", "pins"):
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
                      "participants", "notes", "category", "activities", "area", "pins"):
        assert forbidden not in trip, forbidden


def test_basic_tier_sees_destination_not_seats_or_people(client, make_friend, make_trip):
    t = make_trip(destination=DEST, car_seats=3, notes="leaving 6am")
    f = make_friend(tier="basic")
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["destination"] == DEST
    # Seat counts are full-tier only now — hidden from basic, and not derivable
    # (car_seats/confirmed_count are withheld too, so free_seats can't be recomputed).
    for hidden in ("free_seats", "car_seats", "confirmed_count", "participants", "notes"):
        assert hidden not in trip, hidden
    assert trip["can_request"] is False        # only full may request


def test_full_tier_sees_everything_visible(client, make_friend, make_trip):
    t = make_trip(destination=DEST, car_seats=3, notes="leaving 6am")
    f = make_friend(tier="full")
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["destination"] == DEST
    assert trip["free_seats"] == 3             # seat counts are full-tier detail
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


def test_linked_member_sees_full_detail_regardless_of_tier(client, make_friend, make_trip):
    # The owner adds a low-tier friend's ACCOUNT to a trip: being on the trip
    # unlocks full detail (seats, activities, pins, names, notes) for that one
    # trip, no matter the friend's tier.
    for tier in ("busy", "basic"):
        t = make_trip(**EXTRAS, destination=DEST, car_seats=3, notes="leaving 6am")
        f = make_friend(name=f"Member-{tier}", tier=tier)
        r = client.post(f"/api/admin/trips/{t['id']}/participants",
                        json={"friend_id": f["id"]}, headers=ADMIN)
        assert r.status_code == 201, r.get_json()
        trip = _trip_for(client, f["token"], t["id"])
        assert trip["on_trip"] is True
        assert trip["destination"] == DEST
        assert trip["free_seats"] == 2                       # 3 seats − 1 (them)
        assert trip["activities"] == EXTRAS["activities"]
        assert trip["notes"] == "leaving 6am"
        assert trip["pins"][0]["label"] == "Lift base"
        assert {"name": f"Member-{tier}"} in trip["participants"]
        assert trip["can_request"] is False                  # already on it


def test_name_match_alone_does_not_unlock_detail(client, make_friend, make_trip):
    # A guest the owner typed by NAME (no account link) lights up a same-named
    # friend's calendar (on_trip) but must NOT expose the trip's full detail — a
    # name collision isn't proof of identity.
    t = make_trip(**EXTRAS, destination=DEST, car_seats=3, notes="leaving 6am")
    client.post(f"/api/admin/trips/{t['id']}/participants", json={"name": "Casey"}, headers=ADMIN)
    for tier in ("busy", "basic"):
        f = make_friend(name="Casey", tier=tier)
        trip = _trip_for(client, f["token"], t["id"])
        assert trip["on_trip"] is True
        for hidden in ("free_seats", "activities", "pins", "participants", "notes"):
            assert hidden not in trip, (tier, hidden)


def test_linked_member_on_busy_only_trip_still_hidden(client, make_friend, make_trip):
    # busy_only trumps even a linked membership: a private trip stays a bare busy
    # span for its participants — only the owner sees a private trip's detail.
    t = make_trip(privacy="busy_only", destination=DEST, car_seats=3)
    f = make_friend(tier="full")
    client.post(f"/api/admin/trips/{t['id']}/participants",
                json={"friend_id": f["id"]}, headers=ADMIN)
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["on_trip"] is True
    assert trip["status"] == "busy"
    for hidden in ("destination", "free_seats", "activities", "participants"):
        assert hidden not in trip, hidden


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


# ─── Activities, map area & pins (trip extras) ─────────────────────────────
# area travels with the basic detail block; the activity list + exact labeled
# pins are full/owner only. Everything replaces wholesale on save; pins link to
# activities by index into the submitted (or stored) list.

EXTRAS = {
    "activities": ["Skiing", "Sauna", "Hot wine"],
    "area": {"lat": 42.48, "lng": 44.48, "radius_m": 8000},
    "pins": [
        {"lat": 42.505, "lng": 44.478, "label": "Lift base", "activity": 0,
         "maps_url": "https://maps.app.goo.gl/liftbase"},
        {"lat": 42.47, "lng": 44.5, "label": "Chalet", "activity": None},
    ],
}


def test_extras_roundtrip_for_owner(client, make_trip):
    t = make_trip(**EXTRAS)
    got = client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]
    assert got["activities"] == ["Skiing", "Sauna", "Hot wine"]
    assert got["area"] == {"lat": 42.48, "lng": 44.48, "radius_m": 8000.0}
    assert got["pins"] == [
        {"lat": 42.505, "lng": 44.478, "label": "Lift base",
         "maps_url": "https://maps.app.goo.gl/liftbase", "activity": 0},
        {"lat": 42.47, "lng": 44.5, "label": "Chalet", "maps_url": "", "activity": None},
    ]


def test_area_without_radius_is_a_plain_pin(client, make_trip):
    t = make_trip(area={"lat": 41.7, "lng": 44.8})
    got = client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]
    assert got["area"] == {"lat": 41.7, "lng": 44.8, "radius_m": None}
    # radius_m: 0 canonicalizes to the same plain pin
    client.put(f"/api/admin/trips/{t['id']}",
               json={"area": {"lat": 41.7, "lng": 44.8, "radius_m": 0}}, headers=ADMIN)
    got = client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]
    assert got["area"]["radius_m"] is None


def test_basic_sees_area_but_not_activities_or_pins(client, make_friend, make_trip):
    t = make_trip(**EXTRAS)
    f = make_friend(tier="basic")
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["area"]["lat"] == 42.48        # rough map area stays with basic
    assert "activities" not in trip            # activity list is full-tier only
    assert "pins" not in trip


def test_full_sees_pins_linked_to_activities(client, make_friend, make_trip):
    t = make_trip(**EXTRAS)
    f = make_friend(tier="full")
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["pins"][0]["label"] == "Lift base"
    assert trip["activities"][trip["pins"][0]["activity"]] == "Skiing"
    assert trip["pins"][1]["activity"] is None


def test_busy_and_anon_never_see_extras(client, make_friend, make_trip):
    make_trip(**EXTRAS, destination="SecretValley",
              start_date="2026-01-12", end_date="2026-01-14")
    f = make_friend(tier="busy")
    for headers in (friend_headers(f["token"]), None):
        r = client.get("/api/calendar?from=2026-01-01&to=2026-01-31",
                       headers=headers or {})
        t = r.get_json()["trips"][0]
        for forbidden in ("activities", "area", "pins"):
            assert forbidden not in t, forbidden
        # Raw-body sweep: no activity text, pin label, coordinate, or maps link leaks.
        for secret in (b"Skiing", b"Lift base", b"Chalet", b"42.48", b"maps.app.goo.gl"):
            assert secret not in r.data, secret


def test_busy_only_privacy_hides_extras_from_full(client, make_friend, make_trip):
    t = make_trip(**EXTRAS, privacy="busy_only")
    f = make_friend(tier="full")
    trip = _trip_for(client, f["token"], t["id"])
    for forbidden in ("activities", "area", "pins"):
        assert forbidden not in trip, forbidden


def test_extras_kept_on_partial_update_replaced_when_sent(client, make_trip):
    t = make_trip(**EXTRAS)
    # PUT without the extras keys → everything kept.
    client.put(f"/api/admin/trips/{t['id']}", json={"notes": "new plan"}, headers=ADMIN)
    got = client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]
    assert got["activities"] == EXTRAS["activities"]
    assert len(got["pins"]) == 2 and got["area"]["radius_m"] == 8000.0
    # Sending them replaces wholesale; pin indexes resolve against the NEW list.
    r = client.put(f"/api/admin/trips/{t['id']}", json={
        "activities": ["Rafting"],
        "pins": [{"lat": 1, "lng": 2, "label": "Put-in", "activity": 0}],
    }, headers=ADMIN)
    got = r.get_json()["trip"]
    assert got["activities"] == ["Rafting"]
    assert got["pins"] == [{"lat": 1.0, "lng": 2.0, "label": "Put-in", "maps_url": "", "activity": 0}]
    # Empty lists and a null area clear everything.
    r = client.put(f"/api/admin/trips/{t['id']}",
                   json={"activities": [], "pins": [], "area": None}, headers=ADMIN)
    got = r.get_json()["trip"]
    assert got["activities"] == [] and got["pins"] == []
    assert "area" not in got


def test_pins_alone_link_against_stored_activities(client, make_trip):
    t = make_trip(activities=["Skiing", "Sledding"])
    r = client.put(f"/api/admin/trips/{t['id']}",
                   json={"pins": [{"lat": 5, "lng": 6, "activity": 1}]}, headers=ADMIN)
    assert r.status_code == 200
    assert r.get_json()["trip"]["pins"][0]["activity"] == 1
    # Index past the stored list → rejected.
    r = client.put(f"/api/admin/trips/{t['id']}",
                   json={"pins": [{"lat": 5, "lng": 6, "activity": 2}]}, headers=ADMIN)
    assert r.status_code == 400 and r.get_json()["error"] == "bad_pin_activity"


def test_pin_maps_url_roundtrips_falls_back_and_rejects_bad_schemes(client, make_trip):
    # A custom http(s) link round-trips verbatim.
    t = make_trip(activities=["Skiing"], pins=[
        {"lat": 1, "lng": 2, "label": "Base", "activity": 0,
         "maps_url": "https://maps.app.goo.gl/abc123"},
    ])
    got = client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]
    assert got["pins"][0]["maps_url"] == "https://maps.app.goo.gl/abc123"
    # Omitting it (or sending blank) stores "" — the client renders a coordinate
    # link in that case, so nothing is lost.
    r = client.put(f"/api/admin/trips/{t['id']}",
                   json={"pins": [{"lat": 1, "lng": 2, "activity": 0}]}, headers=ADMIN)
    assert r.get_json()["trip"]["pins"][0]["maps_url"] == ""
    # Non-http(s) values are rejected — they'd otherwise become an unsafe href.
    for bad in ("javascript:alert(1)", "data:text/html,x", "ftp://host/x", "notaurl"):
        r = client.put(f"/api/admin/trips/{t['id']}",
                       json={"pins": [{"lat": 1, "lng": 2, "activity": 0, "maps_url": bad}]},
                       headers=ADMIN)
        assert r.status_code == 400 and r.get_json()["error"] == "bad_pin_url", bad


def test_extras_validation_rejects_garbage(client, make_trip):
    t = make_trip()
    bad_bodies = [
        {"area": {"lat": 91, "lng": 0}},
        {"area": {"lat": 0, "lng": 181}},
        {"area": {"lat": 0, "lng": 0, "radius_m": 500_001}},
        {"area": {"lat": "x", "lng": 0}},
        {"area": {"lat": True, "lng": 0}},
        {"area": []},
        {"activities": "not-a-list"},
        {"activities": [""]},
        {"activities": ["x" * 121]},
        {"activities": ["a"] * 51},
        {"pins": "nope"},
        {"pins": ["zap"]},
        {"pins": [{"lat": 0}]},                                  # missing lng
        {"pins": [{"lat": 0, "lng": 0, "activity": 0}]},         # trip has no activities
        {"pins": [{"lat": 0, "lng": 0, "activity": True}]},
        {"pins": [{"lat": 0, "lng": 0, "activity": -1}]},
        {"pins": [{"lat": 0, "lng": 0, "label": "y" * 121}]},
        {"pins": [{"lat": 0, "lng": 0}] * 101},
        {"pins": [{"lat": 0, "lng": 0, "maps_url": "javascript:alert(1)"}]},
        {"pins": [{"lat": 0, "lng": 0, "maps_url": "/relative"}]},
        {"pins": [{"lat": 0, "lng": 0, "maps_url": "h" * 501}]},
    ]
    for body in bad_bodies:
        r = client.put(f"/api/admin/trips/{t['id']}", json=body, headers=ADMIN)
        assert r.status_code == 400, body
    # Same guard on create: a pin can't link past the submitted activities list.
    r = client.post("/api/admin/trips", json={
        "destination": "X", "start_date": "2026-02-01", "end_date": "2026-02-01",
        "pins": [{"lat": 0, "lng": 0, "activity": 0}],
    }, headers=ADMIN)
    assert r.status_code == 400 and r.get_json()["error"] == "bad_pin_activity"


def test_delete_trip_cascades_extras(client, make_trip):
    t = make_trip(**EXTRAS)
    # Delete succeeds despite activity/pin child rows -> ON DELETE CASCADE.
    assert client.delete(f"/api/admin/trips/{t['id']}", headers=ADMIN).status_code == 200
    assert client.get(f"/api/trips/{t['id']}", headers=ADMIN).status_code == 404


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


def test_friend_rename_propagates_to_participants(client, make_friend, make_trip):
    # A friend's name is snapshotted onto each trip they're on; renaming the
    # friend must update those snapshots everywhere — no remove-and-re-add.
    t = make_trip(car_seats=3)
    f = make_friend(name="Old Name", tier="full")
    client.post(f"/api/admin/trips/{t['id']}/participants",
                json={"friend_id": f["id"]}, headers=ADMIN)
    client.put(f"/api/admin/friends/{f['id']}", json={"name": "New Name"}, headers=ADMIN)
    # Owner roster reflects the new name.
    owner_trip = client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]
    assert [p["display_name"] for p in owner_trip["participants"]] == ["New Name"]
    # A full-tier friend viewing the trip sees it too.
    viewer = make_friend(name="Viewer", tier="full")
    seen = _trip_for(client, viewer["token"], t["id"])
    assert seen["participants"] == [{"name": "New Name"}]


def test_friend_rename_moves_pending_and_declined_rows(client, make_friend, make_trip):
    # The snapshot follows the rename on ANY linked row, not just confirmed ones.
    t = make_trip(car_seats=3)
    f = make_friend(name="Old", tier="full")
    client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(f["token"]))
    client.put(f"/api/admin/friends/{f['id']}", json={"name": "Renamed"}, headers=ADMIN)
    names = [r["display_name"] for r in
             client.get("/api/admin/requests", headers=ADMIN).get_json()["requests"]]
    assert names == ["Renamed"]                 # pending request row moved with the rename


def test_friend_rename_leaves_unlinked_guests_untouched(client, make_friend, make_trip):
    # An owner-added guest (no friend_id) sharing the old name is independent.
    t = make_trip(car_seats=3)
    f = make_friend(name="Sam", tier="full")
    client.post(f"/api/admin/trips/{t['id']}/participants",
                json={"friend_id": f["id"]}, headers=ADMIN)
    client.post(f"/api/admin/trips/{t['id']}/participants",
                json={"name": "Sam"}, headers=ADMIN)          # unlinked guest
    client.put(f"/api/admin/friends/{f['id']}", json={"name": "Samuel"}, headers=ADMIN)
    owner_trip = client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]
    assert sorted(p["display_name"] for p in owner_trip["participants"]) == ["Sam", "Samuel"]


def test_delete_trip_cascades_participants(client, make_trip):
    t = make_trip(car_seats=2)
    client.post(f"/api/admin/trips/{t['id']}/participants", json={"name": "Alex"}, headers=ADMIN)
    # Delete succeeds despite a child row -> ON DELETE CASCADE (FK enforcement on).
    assert client.delete(f"/api/admin/trips/{t['id']}", headers=ADMIN).status_code == 200
    assert client.get(f"/api/trips/{t['id']}", headers=ADMIN).status_code == 404


# ─── Owner-going toggle ("reserve a seat for me" on trip creation) ─────────
# owner_going defaults True: the owner is seated as a confirmed passenger on the
# new trip, taking one seat. Full tier sees the owner in the roster; every tier
# sees one fewer free seat.

import app as app_module


def _create(client, **body):
    b = {"destination": "Gudauri", "start_date": "2026-01-12", "end_date": "2026-01-12", "car_seats": 3}
    b.update(body)
    r = client.post("/api/admin/trips", json=b, headers=ADMIN)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["trip"]


def test_owner_going_defaults_on_and_seats_owner(client):
    # Omitting the flag → owner is seated (default on).
    t = _create(client, car_seats=3)
    assert t["confirmed_count"] == 1
    assert t["free_seats"] == 2
    names = [p["display_name"] for p in t["participants"]]
    assert names == [app_module.OWNER_NAME]


def test_owner_going_off_leaves_trip_empty(client):
    t = _create(client, car_seats=3, owner_going=False)
    assert t["confirmed_count"] == 0
    assert t["free_seats"] == 3
    assert t["participants"] == []


def test_owner_going_visible_to_full_friend_not_basic(client, make_friend):
    t = _create(client, car_seats=3, owner_going=True)
    full = make_friend(name="F", tier="full")
    basic = make_friend(name="B", tier="basic")
    ft = client.get(f"/api/trips/{t['id']}", headers=friend_headers(full["token"])).get_json()["trip"]
    bt = client.get(f"/api/trips/{t['id']}", headers=friend_headers(basic["token"])).get_json()["trip"]
    assert ft["participants"] == [{"name": app_module.OWNER_NAME}]   # full sees the owner by name
    assert ft["free_seats"] == 2
    assert "participants" not in bt and "free_seats" not in bt        # basic: neither names nor seat count


def test_owner_going_bumps_zero_seat_trip_to_one(client):
    # A 0-seat trip with the owner going gets his one seat; friends see 0 free.
    t = _create(client, car_seats=0, owner_going=True)
    assert t["car_seats"] == 1 and t["confirmed_count"] == 1 and t["free_seats"] == 0


def test_owner_going_off_keeps_zero_seats(client):
    t = _create(client, car_seats=0, owner_going=False)
    assert t["car_seats"] == 0 and t["participants"] == []


def test_owner_going_leaves_room_for_a_friend_request(client, make_friend):
    # Owner takes seat 1 of 2; a full friend can still request the remaining one.
    t = _create(client, car_seats=2, owner_going=True)
    f = make_friend(tier="full")
    r = client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(f["token"]))
    assert r.status_code == 201
    rid = client.get("/api/admin/requests", headers=ADMIN).get_json()["requests"][0]["id"]
    assert client.post(f"/api/admin/requests/{rid}/approve", headers=ADMIN).status_code == 200
    trip = client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]
    assert trip["free_seats"] == 0 and trip["confirmed_count"] == 2   # owner + friend


def test_owner_going_rejects_non_bool(client):
    r = client.post("/api/admin/trips", json={
        "destination": "X", "start_date": "2026-01-12", "end_date": "2026-01-12",
        "car_seats": 2, "owner_going": "yes",
    }, headers=ADMIN)
    assert r.status_code == 400 and r.get_json()["error"] == "bad_owner_going"


def test_owner_seat_is_removable_by_owner(client):
    # The reserved seat is an ordinary confirmed participant the owner can drop.
    t = _create(client, car_seats=2, owner_going=True)
    pid = t["participants"][0]["id"]
    assert client.delete(f"/api/admin/participants/{pid}", headers=ADMIN).status_code == 200
    trip = client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]
    assert trip["confirmed_count"] == 0 and trip["free_seats"] == 2


def _owner_names(trip):
    return [p["display_name"] for p in trip["participants"] if p["status"] == "confirmed"]


def test_edit_toggle_on_adds_owner_seat(client, make_trip):
    t = make_trip(car_seats=3)                       # made with owner_going=False
    assert client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]["owner_going"] is False
    r = client.put(f"/api/admin/trips/{t['id']}", json={"owner_going": True}, headers=ADMIN)
    trip = r.get_json()["trip"]
    assert trip["owner_going"] is True
    assert app_module.OWNER_NAME in _owner_names(trip)
    assert trip["confirmed_count"] == 1 and trip["free_seats"] == 2


def test_edit_toggle_off_removes_owner_seat(client):
    t = _create(client, car_seats=3, owner_going=True)   # owner seated
    assert t["owner_going"] is True
    r = client.put(f"/api/admin/trips/{t['id']}", json={"owner_going": False}, headers=ADMIN)
    trip = r.get_json()["trip"]
    assert trip["owner_going"] is False
    assert app_module.OWNER_NAME not in _owner_names(trip)
    assert trip["confirmed_count"] == 0 and trip["free_seats"] == 3


def test_edit_owner_going_absent_leaves_roster_untouched(client):
    t = _create(client, car_seats=3, owner_going=True)
    # A PUT that doesn't mention owner_going must not drop the owner's seat.
    r = client.put(f"/api/admin/trips/{t['id']}", json={"notes": "changed"}, headers=ADMIN)
    trip = r.get_json()["trip"]
    assert trip["owner_going"] is True and trip["confirmed_count"] == 1


def test_edit_toggle_on_is_idempotent(client):
    t = _create(client, car_seats=3, owner_going=True)
    r = client.put(f"/api/admin/trips/{t['id']}", json={"owner_going": True}, headers=ADMIN)
    trip = r.get_json()["trip"]
    assert _owner_names(trip).count(app_module.OWNER_NAME) == 1   # no duplicate seat


def test_edit_toggle_on_over_capacity_rejected(client, make_friend):
    # Fill the only seat with a friend, then the owner can't also take one.
    t = make_trip_full(client, car_seats=1)
    f = make_friend(tier="full")
    client.post(f"/api/admin/trips/{t['id']}/participants", json={"friend_id": f["id"]}, headers=ADMIN)
    r = client.put(f"/api/admin/trips/{t['id']}", json={"owner_going": True}, headers=ADMIN)
    assert r.status_code == 409 and r.get_json()["error"] == "seat_unavailable"


def test_edit_toggle_off_frees_room_to_shrink_seats(client, make_friend):
    # Owner + 1 friend on a 2-seat trip; dropping the owner AND shrinking to 1 is OK.
    t = _create(client, car_seats=2, owner_going=True)
    f = make_friend(tier="full")
    client.post(f"/api/admin/trips/{t['id']}/participants", json={"friend_id": f["id"]}, headers=ADMIN)
    r = client.put(f"/api/admin/trips/{t['id']}", json={"car_seats": 1, "owner_going": False}, headers=ADMIN)
    assert r.status_code == 200
    trip = r.get_json()["trip"]
    assert trip["car_seats"] == 1 and trip["confirmed_count"] == 1
    assert app_module.OWNER_NAME not in _owner_names(trip)


def test_edit_toggle_on_zero_seat_bumps_to_one(client, make_trip):
    t = make_trip(car_seats=0)
    r = client.put(f"/api/admin/trips/{t['id']}", json={"owner_going": True}, headers=ADMIN)
    trip = r.get_json()["trip"]
    assert trip["car_seats"] == 1 and trip["confirmed_count"] == 1 and trip["free_seats"] == 0


def test_edit_owner_going_rejects_non_bool(client, make_trip):
    t = make_trip(car_seats=2)
    r = client.put(f"/api/admin/trips/{t['id']}", json={"owner_going": "sure"}, headers=ADMIN)
    assert r.status_code == 400 and r.get_json()["error"] == "bad_owner_going"


def make_trip_full(client, **body):
    """A trip created with no owner seat (owner_going off), for capacity tests."""
    b = {"destination": "X", "start_date": "2026-01-12", "end_date": "2026-01-12", "owner_going": False}
    b.update(body)
    r = client.post("/api/admin/trips", json=b, headers=ADMIN)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["trip"]


# ─── Busy events (kind='busy': unavailable to all, context for full tier) ──

def _busy(client, **body):
    b = {"kind": "busy", "start_date": "2026-08-10", "end_date": "2026-08-12",
         "context": "Work conference"}
    b.update(body)
    r = client.post("/api/admin/trips", json=b, headers=ADMIN)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["trip"]


def test_busy_event_owner_sees_kind_and_context(client):
    t = _busy(client, context="Family wedding")
    assert t["kind"] == "busy" and t["status"] == "busy"
    assert t["context"] == "Family wedding"
    # None of the trip-detail fields exist on a busy event.
    for absent in ("destination", "car_seats", "free_seats", "participants", "category"):
        assert absent not in t, absent


def test_busy_event_full_friend_sees_context_only(client, make_friend):
    t = _busy(client, context="Exams all week")
    f = make_friend(tier="full")
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["status"] == "busy" and trip["kind"] == "busy"
    assert trip["context"] == "Exams all week"
    # Still no trip detail, and participation keys present (friend invariant).
    assert "destination" not in trip and "free_seats" not in trip
    assert trip["on_trip"] is False and trip["my_status"] is None


def test_busy_event_basic_and_busy_tiers_get_no_context(client, make_friend):
    t = _busy(client, context="secret reason")
    for tier in ("basic", "busy"):
        f = make_friend(name=f"F{tier}", tier=tier)
        trip = _trip_for(client, f["token"], t["id"])
        assert trip["status"] == "busy"
        assert "context" not in trip, tier


def test_busy_event_anon_and_raw_body_no_context_leak(client):
    _busy(client, context="TopSecretReason", start_date="2026-08-10", end_date="2026-08-12")
    r = client.get("/api/calendar?from=2026-08-01&to=2026-08-31")
    t = r.get_json()["trips"][0]
    assert t["status"] == "busy" and "context" not in t
    assert b"TopSecretReason" not in r.data


def test_busy_event_not_requestable(client, make_friend):
    t = _busy(client)
    f = make_friend(tier="full")
    r = client.post(f"/api/trips/{t['id']}/request-seat", headers=friend_headers(f["token"]))
    assert r.status_code == 403


def test_busy_event_add_participant_rejected(client):
    t = _busy(client)
    r = client.post(f"/api/admin/trips/{t['id']}/participants", json={"name": "Alex"}, headers=ADMIN)
    assert r.status_code == 400 and r.get_json()["error"] == "not_a_trip"


def test_busy_event_update_context_and_dates(client):
    t = _busy(client, context="old")
    r = client.put(f"/api/admin/trips/{t['id']}",
                   json={"context": "new reason", "end_date": "2026-08-14"}, headers=ADMIN)
    trip = r.get_json()["trip"]
    assert trip["context"] == "new reason" and trip["end_date"] == "2026-08-14"
    assert trip["kind"] == "busy"


def test_busy_event_partial_update_keeps_dates(client):
    t = _busy(client, start_date="2026-08-10", end_date="2026-08-12", context="a")
    client.put(f"/api/admin/trips/{t['id']}", json={"context": "b"}, headers=ADMIN)
    trip = client.get(f"/api/trips/{t['id']}", headers=ADMIN).get_json()["trip"]
    assert trip["start_date"] == "2026-08-10" and trip["end_date"] == "2026-08-12"
    assert trip["context"] == "b"


def test_busy_event_shows_in_calendar_overlap(client, make_friend):
    t = _busy(client, start_date="2026-08-10", end_date="2026-08-20")
    f = make_friend(tier="busy")
    j = client.get("/api/calendar?from=2026-08-15&to=2026-08-16",
                   headers=friend_headers(f["token"])).get_json()
    assert [x["id"] for x in j["trips"]] == [t["id"]]


def test_busy_event_context_optional(client, make_friend):
    t = _busy(client, context="")
    f = make_friend(tier="full")
    trip = _trip_for(client, f["token"], t["id"])
    assert trip["context"] == ""


def test_create_rejects_bad_kind(client):
    r = client.post("/api/admin/trips", json={
        "kind": "weird", "start_date": "2026-08-10", "end_date": "2026-08-12",
    }, headers=ADMIN)
    assert r.status_code == 400 and r.get_json()["error"] == "bad_kind"


def test_busy_event_rejects_bad_dates(client):
    r = client.post("/api/admin/trips", json={
        "kind": "busy", "start_date": "2026-08-20", "end_date": "2026-08-10",
    }, headers=ADMIN)
    assert r.status_code == 400 and r.get_json()["error"] == "end_before_start"


def test_busy_event_delete(client):
    t = _busy(client)
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


# ─── Owner "view as" preview (?view_as=) ──────────────────────────────────
# The owner may ask the calendar reads to project DOWN to a tier or to anon.
# It must be exactly the projection that audience gets, work only for the
# owner, and never function as an upgrade for anyone else.

def test_view_as_busy_downgrades_owner_to_busy_span(client, make_trip):
    make_trip(destination="SecretValley", car_seats=3, notes="leaving 6am",
              start_date="2026-01-12", end_date="2026-01-14")
    r = client.get("/api/calendar?from=2026-01-01&to=2026-01-31&view_as=busy", headers=ADMIN)
    assert r.status_code == 200
    t = r.get_json()["trips"][0]
    assert t["status"] == "busy"
    for forbidden in ("destination", "location_label", "car_seats", "free_seats",
                      "participants", "notes", "category", "privacy", "updated_at",
                      "pending_count"):
        assert forbidden not in t, forbidden
    assert b"SecretValley" not in r.data and b"leaving 6am" not in r.data


def test_view_as_anon_matches_real_anon_projection(client, make_trip):
    t = make_trip(destination="SecretValley", car_seats=3)
    real = client.get("/api/calendar?from=2026-01-01&to=2026-01-31").get_json()["trips"]
    preview = client.get(
        "/api/calendar?from=2026-01-01&to=2026-01-31&view_as=anon", headers=ADMIN
    ).get_json()["trips"]
    assert preview == real
    assert "on_trip" not in preview[0] and "my_status" not in preview[0]
    # The single-trip read honors it too (the owner can call it; anon itself can't).
    one = client.get(f"/api/trips/{t['id']}?view_as=anon", headers=ADMIN).get_json()["trip"]
    assert one == real[0]


def test_view_as_full_matches_a_real_full_friend(client, make_friend, make_trip):
    t = make_trip(destination=DEST, car_seats=3, notes="leaving 6am")
    client.post(f"/api/admin/trips/{t['id']}/participants", json={"name": "Alex"}, headers=ADMIN)
    f = make_friend(name="Watcher", tier="full")
    real = client.get(
        f"/api/trips/{t['id']}", headers=friend_headers(f["token"])
    ).get_json()["trip"]
    preview = client.get(f"/api/trips/{t['id']}?view_as=full", headers=ADMIN).get_json()["trip"]
    # Identical projection — same fields, same values (Watcher isn't on the trip,
    # and the synthetic preview friend never is).
    assert preview == real
    assert preview["can_request"] is True and preview["participants"] == [{"name": "Alex"}]


def test_view_as_is_ignored_for_non_owners(client, make_friend, make_trip):
    make_trip(destination="SecretValley", start_date="2026-01-12", end_date="2026-01-14")
    f = make_friend(tier="busy")
    # A busy friend can't use view_as to see more...
    r = client.get("/api/calendar?from=2026-01-01&to=2026-01-31&view_as=full",
                   headers=friend_headers(f["token"]))
    assert r.status_code == 200
    assert "destination" not in r.get_json()["trips"][0]
    # ...and neither can an anonymous caller (param ignored, not even a 400).
    r = client.get("/api/calendar?from=2026-01-01&to=2026-01-31&view_as=full")
    assert r.status_code == 200
    assert "destination" not in r.get_json()["trips"][0]
    assert b"SecretValley" not in r.data


def test_view_as_rejects_unknown_values(client, make_trip):
    t = make_trip()
    for bad in ("owner", "admin", "FULL", "x"):
        r = client.get(f"/api/calendar?from=2026-01-01&to=2026-01-31&view_as={bad}",
                       headers=ADMIN)
        assert r.status_code == 400 and r.get_json()["error"] == "bad_view_as"
        assert client.get(f"/api/trips/{t['id']}?view_as={bad}", headers=ADMIN).status_code == 400


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
