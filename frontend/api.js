// ─── api.js ───────────────────────────────────────────────────────────
// Client-side module that talks to the Flask backend. Exposes a global `API`.
//
// Two credentials, mirroring the backend:
//   - Owner: an admin key entered in the "Owner" modal, kept in localStorage,
//     sent as X-Admin-Key.
//   - Friend: a per-friend token that arrives once in the invite link
//     (?u=<token>), is captured into localStorage, stripped from the URL, and
//     sent as X-User-Token thereafter.
// The owner key takes precedence when both are present (so George can open a
// friend's link and still act as owner).

const API = (() => {
  const ADMIN_STORE = "avail-admin-key";
  const TOKEN_STORE = "avail-user-token";
  const BASE = (typeof window !== "undefined" && window.API_BASE) || "";

  let adminKey = localStorage.getItem(ADMIN_STORE) || null;
  let userToken = localStorage.getItem(TOKEN_STORE) || null;

  // ─── Invite-token capture ──────────────────────────────────────────────
  // Pull ?u=<token> out of the URL on first load, store it, and scrub it from
  // the address bar (and history) so it isn't shoulder-surfed or leaked.
  function captureInviteToken() {
    const params = new URLSearchParams(window.location.search);
    const t = params.get("u");
    if (t) {
      userToken = t.trim();
      localStorage.setItem(TOKEN_STORE, userToken);
      params.delete("u");
      const qs = params.toString();
      const clean = window.location.pathname + (qs ? "?" + qs : "") + window.location.hash;
      window.history.replaceState({}, "", clean);
    }
  }

  function authHeaders(extra) {
    const h = Object.assign({ "Content-Type": "application/json" }, extra || {});
    if (adminKey) h["X-Admin-Key"] = adminKey;
    else if (userToken) h["X-User-Token"] = userToken;
    return h;
  }

  async function request(method, path, body) {
    const resp = await fetch(BASE + path, {
      method,
      headers: authHeaders(),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (resp.status === 204) return null;
    let data = null;
    try { data = await resp.json(); } catch (e) { /* non-JSON */ }
    if (!resp.ok) {
      const err = new Error((data && data.error) || ("HTTP " + resp.status));
      err.status = resp.status;
      err.code = data && data.error;
      throw err;
    }
    return data;
  }

  // ─── Identity ──────────────────────────────────────────────────────────

  async function whoami() {
    // → { role: "owner" } | { role: "friend", name, tier } ; throws on 401 (anon)
    return request("GET", "/api/me");
  }

  function isOwner() { return !!adminKey; }
  function hasUserToken() { return !!userToken; }

  // Friend sign-in: name + birthday → a bearer token we store and send as
  // X-User-Token thereafter (same credential the legacy ?u= link carried).
  async function loginFriend(name, birthday) {
    const resp = await fetch(BASE + "/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: (name || "").trim(), birthday: (birthday || "").trim() }),
    });
    let data = null;
    try { data = await resp.json(); } catch (e) { /* non-JSON */ }
    if (!resp.ok) {
      const e = new Error((data && data.error) || ("HTTP " + resp.status));
      e.status = resp.status; e.code = data && data.error;
      throw e;
    }
    userToken = data.token;
    localStorage.setItem(TOKEN_STORE, userToken);
    return data;   // { token, name, tier }
  }

  async function loginOwner(candidate) {
    const key = (candidate || "").trim();
    if (!key) throw new Error("Enter the admin key.");
    const resp = await fetch(BASE + "/api/admin/verify", {
      method: "POST",
      headers: { "X-Admin-Key": key },
    });
    if (!resp.ok) { const e = new Error("Invalid key"); e.status = resp.status; throw e; }
    adminKey = key;
    localStorage.setItem(ADMIN_STORE, key);
  }

  function logoutOwner() {
    adminKey = null;
    localStorage.removeItem(ADMIN_STORE);
  }

  function forgetUser() {
    userToken = null;
    localStorage.removeItem(TOKEN_STORE);
  }

  // ─── Reads (tier-projected server-side) ────────────────────────────────

  function getCalendar(from, to) {
    return request("GET", `/api/calendar?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`)
      .then(d => d.trips || []);
  }
  function getTrip(id) { return request("GET", `/api/trips/${id}`).then(d => d.trip); }

  // ─── Friend actions ────────────────────────────────────────────────────

  function requestSeat(tripId) {
    return request("POST", `/api/trips/${tripId}/request-seat`).then(d => d.trip);
  }
  // Remove the caller's own participation (cancel a pending request OR leave a
  // confirmed seat). Resolved server-side by friend_id — no id needed.
  function leaveTrip(tripId) { return request("DELETE", `/api/trips/${tripId}/me`); }

  // ─── Owner: trips ──────────────────────────────────────────────────────

  function adminTrips() { return request("GET", "/api/admin/trips").then(d => d.trips || []); }
  function createTrip(t) { return request("POST", "/api/admin/trips", t).then(d => d.trip); }
  function updateTrip(id, t) { return request("PUT", `/api/admin/trips/${id}`, t).then(d => d.trip); }
  function deleteTrip(id) { return request("DELETE", `/api/admin/trips/${id}`); }

  // ─── Owner: participants & requests ────────────────────────────────────

  function addParticipant(tripId, payload) {
    return request("POST", `/api/admin/trips/${tripId}/participants`, payload).then(d => d.trip);
  }
  function removeParticipant(pid) { return request("DELETE", `/api/admin/participants/${pid}`); }
  function listRequests() { return request("GET", "/api/admin/requests").then(d => d.requests || []); }
  function approveRequest(id) { return request("POST", `/api/admin/requests/${id}/approve`).then(d => d.trip); }
  function declineRequest(id) { return request("POST", `/api/admin/requests/${id}/decline`).then(d => d.trip); }

  // ─── Owner: friends ────────────────────────────────────────────────────

  function listFriends() { return request("GET", "/api/admin/friends").then(d => d.friends || []); }
  function createFriend(f) { return request("POST", "/api/admin/friends", f).then(d => d.friend); }
  function updateFriend(id, f) { return request("PUT", `/api/admin/friends/${id}`, f).then(d => d.friend); }
  function deleteFriend(id) { return request("DELETE", `/api/admin/friends/${id}`); }

  return {
    captureInviteToken, whoami, isOwner, hasUserToken,
    loginFriend, loginOwner, logoutOwner, forgetUser,
    getCalendar, getTrip,
    requestSeat, leaveTrip,
    adminTrips, createTrip, updateTrip, deleteTrip,
    addParticipant, removeParticipant, listRequests, approveRequest, declineRequest,
    listFriends, createFriend, updateFriend, deleteFriend,
  };
})();
