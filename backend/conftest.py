"""Pytest fixtures for the availability-calendar backend.

Each test gets a fresh app bound to a throwaway SQLite file and a known admin
key. The app reads both the DB path and the admin key from the environment at
import time, so we set them before (re)loading the module.

Because this app has TWO credentials (owner admin key + per-friend tokens), the
fixtures also provide factories to mint friends and trips through the real admin
API, so tier/seat tests don't carry boilerplate.
"""

import os
import sys
import importlib

import pytest

# Allow `import app` regardless of where pytest is invoked from.
sys.path.insert(0, os.path.dirname(__file__))

ADMIN_KEY = "test-admin-key"
ADMIN = {"X-Admin-Key": ADMIN_KEY}


def friend_headers(token):
    return {"X-User-Token": token}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AVAILABILITY_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)

    import app as app_module
    importlib.reload(app_module)  # re-read env -> temp DB + admin key
    app_module.app.config.update(TESTING=True)

    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def make_friend(client):
    """Create a friend via the admin API; returns the friend dict (incl token)."""
    def _make(name="Friend", tier="full"):
        r = client.post("/api/admin/friends", json={"name": name, "tier": tier}, headers=ADMIN)
        assert r.status_code == 201, r.get_json()
        return r.get_json()["friend"]
    return _make


@pytest.fixture
def make_trip(client):
    """Create a trip via the admin API; returns the owner-projected trip dict."""
    def _make(**overrides):
        body = {
            "destination": "Gudauri",
            "location_label": "Gudauri ski resort",
            "start_date": "2026-01-12",
            "end_date": "2026-01-12",
            "car_seats": 2,
            "category": "ski",
        }
        body.update(overrides)
        r = client.post("/api/admin/trips", json=body, headers=ADMIN)
        assert r.status_code == 201, r.get_json()
        return r.get_json()["trip"]
    return _make
