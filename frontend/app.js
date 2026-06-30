// ─── app.js ─────────────────────────────────────────────────────────────
// Availability calendar UI. Three modes, decided by GET /api/me:
//   owner   → full calendar + edit controls + admin (trips, friends, requests)
//   friend  → tier-scoped calendar + (full tier) request-a-seat
//   anon    → locked landing
// All visibility is enforced server-side; this file only renders whatever the
// projection returned and never assumes hidden fields exist.

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const state = {
  viewer: { role: "anon" },
  cursor: null,            // Date: first of the displayed month
  tripsById: {},           // id -> projected trip
  tripsByDay: {},          // 'YYYY-MM-DD' -> [trip, ...]
  openDay: null,           // 'YYYY-MM-DD' of the open day modal, or null
};

const ERR = {
  seat_unavailable: "That seat just filled up.",
  already_requested: "You've already asked for a seat on this trip.",
  already_decided: "That request was already handled.",
  seats_below_confirmed: "Can't set fewer seats than people already confirmed.",
  already_active: "That friend already has a seat or pending request here.",
  forbidden: "You don't have access to do that.",
  unauthorized: "Please sign in again.",
  not_found: "Not found.",
  bad_date: "Please use valid dates.",
  end_before_start: "End date can't be before the start date.",
  range_too_large: "Date range is too large.",
};
function msg(e) {
  return (e && (ERR[e.code] || ERR[e.message])) || (e && e.message) || "Something went wrong.";
}

// ─── DATE HELPERS (all local-time; the client owns "today") ───────────────

function startOfMonth(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }
function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
function addMonths(d, n) { return new Date(d.getFullYear(), d.getMonth() + n, 1); }
function ymd(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function parseYmd(s) { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); }
function mondayIndex(d) { return (d.getDay() + 6) % 7; }   // 0=Mon … 6=Sun
function monthGrid(cursor) {
  const first = startOfMonth(cursor);
  const gridStart = addDays(first, -mondayIndex(first));
  return { gridStart, gridEnd: addDays(gridStart, 41) };   // 6 weeks
}
function fmtRange(start, end) {
  const opts = { day: "numeric", month: "short", year: "numeric" };
  const a = parseYmd(start);
  if (start === end) return a.toLocaleDateString(undefined, { weekday: "short", ...opts });
  const b = parseYmd(end);
  const sameMonth = start.slice(0, 7) === end.slice(0, 7);
  const aStr = a.toLocaleDateString(undefined, sameMonth ? { day: "numeric" } : { day: "numeric", month: "short" });
  return `${aStr} – ${b.toLocaleDateString(undefined, opts)}`;
}
function monthLabel(d) { return d.toLocaleDateString(undefined, { month: "long", year: "numeric" }); }

function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ─── TOAST ────────────────────────────────────────────────────────────────

let toastTimer = null;
function toast(text, kind = "info") {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.className = "toast show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 2600);
}

// ─── MODAL SCAFFOLD ─────────────────────────────────────────────────────

function openModal(html, opts = {}) {
  closeModal();
  const back = document.createElement("div");
  back.className = "modal-backdrop";
  back.id = "modal";
  back.innerHTML = `<div class="modal ${opts.wide ? "wide" : ""}">${html}</div>`;
  document.body.appendChild(back);
  back.addEventListener("click", e => { if (e.target === back) closeModal(); });
  back.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", closeModal));
  document.addEventListener("keydown", escClose);
  return back;
}
function closeModal() {
  document.getElementById("modal")?.remove();
  document.removeEventListener("keydown", escClose);
}
function escClose(e) { if (e.key === "Escape") closeModal(); }

// ─── BOOT ─────────────────────────────────────────────────────────────────

async function init() {
  API.captureInviteToken();
  document.getElementById("logo").addEventListener("click", e => {
    e.preventDefault();
    if (state.cursor) { state.cursor = startOfMonth(new Date()); loadAndRender(); }
  });

  try {
    state.viewer = await API.whoami();
  } catch (e) {
    state.viewer = { role: "anon" };
    if (API.isOwner()) API.logoutOwner();   // stale admin key
  }

  renderHeader();
  if (state.viewer.role === "anon") { renderLocked(); return; }
  state.cursor = startOfMonth(new Date());
  await loadAndRender();
}

// ─── HEADER ─────────────────────────────────────────────────────────────

function renderHeader() {
  const c = document.getElementById("nav-controls");
  const v = state.viewer;
  if (v.role === "owner") {
    c.innerHTML = `
      <button class="btn btn-ghost" id="btn-requests">Requests<span id="pending-badge" class="count-badge" hidden>0</span></button>
      <button class="btn btn-ghost" id="btn-friends">Friends</button>
      <button class="btn btn-primary" id="btn-new-trip">＋ Trip</button>
      <span class="badge owner" id="btn-signout" title="Sign out">Owner ✕</span>`;
    c.querySelector("#btn-new-trip").onclick = () => openTripForm();
    c.querySelector("#btn-friends").onclick = openFriends;
    c.querySelector("#btn-requests").onclick = openRequests;
    c.querySelector("#btn-signout").onclick = () => { API.logoutOwner(); location.reload(); };
  } else if (v.role === "friend") {
    c.innerHTML = `
      <span class="badge tier-${escapeHtml(v.tier)}">${escapeHtml(v.name)} · ${escapeHtml(v.tier)}</span>
      <button class="btn btn-ghost" id="btn-owner" title="Owner sign-in">⚙</button>`;
    c.querySelector("#btn-owner").onclick = openOwnerLogin;
  } else {
    c.innerHTML = `<button class="btn btn-ghost" id="btn-owner">Owner</button>`;
    c.querySelector("#btn-owner").onclick = openOwnerLogin;
  }
}

// ─── LOCKED LANDING (anon) ────────────────────────────────────────────────

function renderLocked() {
  document.getElementById("main-content").innerHTML = `
    <section class="locked">
      <div class="locked-card">
        <div class="locked-icon">🔒</div>
        <h1>This calendar is private</h1>
        <p>Ask George for your personal link to see when he's around and where there's a free seat.</p>
        <button class="btn btn-ghost" id="locked-owner">I'm the owner</button>
      </div>
    </section>`;
  document.getElementById("locked-owner").onclick = openOwnerLogin;
}

// ─── OWNER LOGIN ──────────────────────────────────────────────────────────

function openOwnerLogin() {
  openModal(`
    <h2>Owner sign-in</h2>
    <p class="modal-sub">Enter the admin key to manage trips, friends, and seat requests.</p>
    <label class="field"><span>Admin key</span>
      <input type="password" id="ok-input" autocomplete="off" placeholder="ADMIN_KEY" /></label>
    <div class="modal-error" id="ok-error"></div>
    <div class="modal-actions">
      <button class="btn" data-close>Cancel</button>
      <button class="btn btn-primary" id="ok-go">Sign in</button>
    </div>`);
  const input = document.getElementById("ok-input");
  input.focus();
  const go = async () => {
    const err = document.getElementById("ok-error");
    err.textContent = "";
    try {
      await API.loginOwner(input.value);
      location.reload();
    } catch (e) {
      err.textContent = e.status === 401 ? "That key isn't right." : msg(e);
    }
  };
  document.getElementById("ok-go").onclick = go;
  input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); go(); } });
}

// ─── CALENDAR DATA ────────────────────────────────────────────────────────

function indexTrips(trips, gridStart, gridEnd) {
  state.tripsById = {};
  state.tripsByDay = {};
  const gs = ymd(gridStart), ge = ymd(gridEnd);
  for (const t of trips) {
    state.tripsById[t.id] = t;
    let cur = t.start_date < gs ? gs : t.start_date;
    const last = t.end_date > ge ? ge : t.end_date;
    let d = parseYmd(cur);
    while (ymd(d) <= last) {
      const k = ymd(d);
      (state.tripsByDay[k] || (state.tripsByDay[k] = [])).push(t);
      d = addDays(d, 1);
    }
  }
}

async function loadAndRender() {
  const { gridStart, gridEnd } = monthGrid(state.cursor);
  let trips = [];
  try {
    trips = await API.getCalendar(ymd(gridStart), ymd(gridEnd));
  } catch (e) {
    if (e.status === 401) { API.logoutOwner(); return location.reload(); }
    toast(msg(e), "error");
  }
  indexTrips(trips, gridStart, gridEnd);
  renderCalendar();
  if (state.viewer.role === "owner") refreshPendingBadge();
  if (state.openDay) renderDayModal(state.openDay);
}

// ─── CALENDAR RENDER ──────────────────────────────────────────────────────

function renderCalendar() {
  const { gridStart } = monthGrid(state.cursor);
  const today = ymd(new Date());
  const curMonth = state.cursor.getMonth();

  let cells = "";
  for (let i = 0; i < 42; i++) {
    const day = addDays(gridStart, i);
    const key = ymd(day);
    const trips = state.tripsByDay[key] || [];
    const otherMonth = day.getMonth() !== curMonth;
    const isToday = key === today;
    const weekStart = mondayIndex(day) === 0;

    const busy = trips.length > 0;
    const detailTrips = trips.filter(t => t.destination);   // tier shows detail

    let chips = "";
    const shown = trips.slice(0, 2);
    for (const t of shown) {
      const startsHere = key === t.start_date;
      const endsHere = key === t.end_date;
      const span = (t.start_date === t.end_date) ? "single"
        : startsHere ? "start" : endsHere ? "end" : "mid";
      const label = (startsHere || weekStart);
      const mine = t.on_trip ? " mine" : "";   // green: a trip the viewer is on
      if (t.destination) {
        const seat = (label && t.car_seats > 0)
          ? `<span class="chip-seat">${t.free_seats}/${t.car_seats}</span>` : "";
        chips += `<div class="chip detail span-${span} cat-${escapeHtml(t.category || "other")}${mine}">
          ${label ? `<span class="chip-label">${escapeHtml(t.destination)}</span>${seat}` : ""}</div>`;
      } else {
        chips += `<div class="chip busy span-${span}${mine}">${label ? `<span class="chip-label">Busy</span>` : ""}</div>`;
      }
    }
    if (trips.length > 2) chips += `<div class="chip-more">+${trips.length - 2}</div>`;

    cells += `<div class="cell ${otherMonth ? "other" : ""} ${busy ? "busy-day" : "free-day"} ${isToday ? "today" : ""}"
                   data-day="${key}" ${trips.length || state.viewer.role === "owner" ? "" : ""}>
      <div class="cell-num">${day.getDate()}</div>
      <div class="cell-chips">${chips}</div>
    </div>`;
  }

  const legend = state.viewer.role === "owner"
    ? `<span class="legend-hint">Click a day to add a trip · click a trip to manage it</span>`
    : `<span class="legend-hint">Click a marked day for details${state.viewer.tier === "full" ? " · request a seat" : ""}</span>`;

  document.getElementById("main-content").innerHTML = `
    <section class="cal">
      <div class="cal-header">
        <div class="cal-title">
          <button class="nav-btn" id="prev">‹</button>
          <h1>${monthLabel(state.cursor)}</h1>
          <button class="nav-btn" id="next">›</button>
          <button class="btn btn-ghost today-btn" id="today">Today</button>
        </div>
        ${legend}
      </div>
      <div class="cal-grid head">${WEEKDAYS.map(w => `<div class="wd">${w}</div>`).join("")}</div>
      <div class="cal-grid body">${cells}</div>
    </section>`;

  document.getElementById("prev").onclick = () => { state.cursor = addMonths(state.cursor, -1); loadAndRender(); };
  document.getElementById("next").onclick = () => { state.cursor = addMonths(state.cursor, 1); loadAndRender(); };
  document.getElementById("today").onclick = () => { state.cursor = startOfMonth(new Date()); loadAndRender(); };
  document.querySelectorAll(".cell").forEach(c => c.addEventListener("click", () => onCellClick(c.dataset.day)));
}

function onCellClick(day) {
  const trips = state.tripsByDay[day] || [];
  if (trips.length) { state.openDay = day; renderDayModal(day); }
  else if (state.viewer.role === "owner") openTripForm(null, { start_date: day, end_date: day });
}

// ─── DAY DETAIL MODAL ──────────────────────────────────────────────────────

function renderDayModal(day) {
  const trips = (state.tripsByDay[day] || []).map(t => state.tripsById[t.id]);
  if (!trips.length) { state.openDay = null; closeModal(); return; }
  const heading = parseYmd(day).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  const cards = trips.map(renderTripCard).join("");
  const back = openModal(`
    <div class="modal-head">
      <h2>${escapeHtml(heading)}</h2>
      <button class="icon-btn" data-close>✕</button>
    </div>
    <div class="trip-cards">${cards}</div>`, { wide: state.viewer.role === "owner" });
  back.addEventListener("click", e => { if (e.target === back) state.openDay = null; });
  back.querySelector("[data-close]").addEventListener("click", () => { state.openDay = null; });
  wireTripCards();
}

function seatPips(free, total) {
  let s = "";
  const filled = Math.max(0, total - free);
  for (let i = 0; i < total; i++) s += `<span class="pip ${i < filled ? "filled" : "free"}"></span>`;
  return `<span class="pips">${s}</span>`;
}

function renderTripCard(t) {
  const owner = state.viewer.role === "owner";

  if (!t.destination) {
    // busy-only projection (busy tier, or a busy_only trip seen by a friend)
    return `<div class="trip-card busy ${t.on_trip ? "mine" : ""}">
      <div class="tc-title"><span class="tc-dot ${t.on_trip ? "mine" : "busy"}"></span>${t.on_trip ? "You're away this day" : "Unavailable"}</div>
      <div class="tc-dates">${escapeHtml(fmtRange(t.start_date, t.end_date))}</div>
      ${t.on_trip ? `<div class="tc-actions"><span class="you-in">✓ You're on this trip</span></div>` : ""}
    </div>`;
  }

  const seats = (t.car_seats > 0)
    ? `<div class="tc-seats"><span class="seat-count">${t.free_seats} of ${t.car_seats} seat${t.car_seats === 1 ? "" : "s"} free</span>${seatPips(t.free_seats, t.car_seats)}</div>`
    : `<div class="tc-seats muted">No car seats offered</div>`;

  let people = "";
  if (owner) {
    people = renderOwnerRoster(t);
  } else if (Array.isArray(t.participants) && t.participants.length) {
    people = `<div class="tc-people">${t.participants.map(p => `<span class="person">${escapeHtml(p.name)}</span>`).join("")}</div>`;
  }

  let actions = "";
  if (owner) {
    actions = `<div class="tc-actions">
      <button class="btn btn-ghost" data-edit="${t.id}">Edit</button>
      <button class="btn btn-danger" data-del="${t.id}">Delete</button>
    </div>`;
  } else if (state.viewer.role === "friend") {
    if (t.on_trip) {
      // Confirmed by seat request OR matched by name to a guest you added.
      actions = `<div class="tc-actions"><span class="you-in">✓ You're on this trip</span></div>`;
    } else if (t.my_status === "pending") {
      actions = `<div class="tc-actions"><span class="pending-tag">⏳ Seat requested — pending</span>
        ${t.my_request_id ? `<button class="btn btn-ghost" data-cancel="${t.my_request_id}">Cancel</button>` : ""}</div>`;
    } else if (t.can_request) {
      const waitlist = t.free_seats <= 0;
      actions = `<div class="tc-actions"><button class="btn btn-primary" data-request="${t.id}">${waitlist ? "Join waitlist" : "Request a seat"}</button></div>`;
    }
  }

  return `<div class="trip-card ${t.on_trip ? "mine" : ""}">
    <div class="tc-title"><span class="tc-dot cat-${escapeHtml(t.category || "other")}"></span>${escapeHtml(t.destination)}
      ${t.category && t.category !== "other" ? `<span class="tag">${escapeHtml(t.category)}</span>` : ""}
      ${owner && t.privacy === "busy_only" ? `<span class="tag private">private</span>` : ""}</div>
    <div class="tc-dates">${escapeHtml(fmtRange(t.start_date, t.end_date))}</div>
    ${t.location_label ? `<div class="tc-loc">📍 ${escapeHtml(t.location_label)}</div>` : ""}
    ${seats}
    ${t.notes ? `<div class="tc-notes">${escapeHtml(t.notes)}</div>` : ""}
    ${people}
    ${actions}
  </div>`;
}

function renderOwnerRoster(t) {
  const list = t.participants || [];
  const confirmed = list.filter(p => p.status === "confirmed");
  const pending = list.filter(p => p.status === "pending");
  const rows = [];
  for (const p of confirmed) {
    rows.push(`<div class="roster-row confirmed">
      <span class="person">${escapeHtml(p.display_name)}</span>
      <span class="src">${p.source === "request" ? "joined" : "added"}</span>
      <button class="mini-btn danger" data-remove="${p.id}" title="Remove">✕</button></div>`);
  }
  for (const p of pending) {
    rows.push(`<div class="roster-row pending">
      <span class="person">${escapeHtml(p.display_name)}</span>
      <span class="src">wants a seat</span>
      <button class="mini-btn ok" data-approve="${p.id}">Approve</button>
      <button class="mini-btn" data-decline="${p.id}">Decline</button></div>`);
  }
  return `<div class="tc-roster">
    ${rows.join("") || `<div class="muted small">No passengers yet</div>`}
    <div class="roster-add">
      <input type="text" class="roster-add-input" data-addguest="${t.id}" placeholder="Add a guest by name…" maxlength="80" />
    </div>
  </div>`;
}

function wireTripCards() {
  const root = document.getElementById("modal");
  if (!root) return;
  root.querySelectorAll("[data-request]").forEach(b => b.onclick = () => act(() => API.requestSeat(+b.dataset.request), "Seat requested."));
  root.querySelectorAll("[data-cancel]").forEach(b => b.onclick = () => act(() => API.cancelRequest(+b.dataset.cancel), "Request cancelled."));
  root.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => openTripForm(state.tripsById[+b.dataset.edit]));
  root.querySelectorAll("[data-del]").forEach(b => b.onclick = () => delTrip(+b.dataset.del));
  root.querySelectorAll("[data-approve]").forEach(b => b.onclick = () => act(() => API.approveRequest(+b.dataset.approve), "Approved."));
  root.querySelectorAll("[data-decline]").forEach(b => b.onclick = () => act(() => API.declineRequest(+b.dataset.decline), "Declined."));
  root.querySelectorAll("[data-remove]").forEach(b => b.onclick = () => act(() => API.removeParticipant(+b.dataset.remove), "Removed."));
  root.querySelectorAll("[data-addguest]").forEach(inp => inp.addEventListener("keydown", e => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const name = inp.value.trim();
    if (name) act(() => API.addParticipant(+inp.dataset.addguest, { name }), "Added.");
  }));
}

// Run a mutation, then refresh the calendar + open day modal + pending badge.
async function act(fn, okMsg) {
  try {
    await fn();
    if (okMsg) toast(okMsg, "ok");
    await loadAndRender();
  } catch (e) {
    toast(msg(e), "error");
  }
}

function delTrip(id) {
  const t = state.tripsById[id];
  openConfirm(`Delete “${t.destination || "this trip"}”? This removes it and any seat requests.`, async () => {
    closeModal();
    state.openDay = null;
    await act(() => API.deleteTrip(id), "Trip deleted.");
  });
}

// ─── TRIP FORM (owner) ─────────────────────────────────────────────────────

function openTripForm(trip, prefill = {}) {
  const t = trip || {};
  const v = (k, d) => escapeHtml(t[k] != null ? t[k] : (prefill[k] != null ? prefill[k] : d));
  const cats = ["ski", "summer", "travel", "other"];
  openModal(`
    <div class="modal-head"><h2>${trip ? "Edit trip" : "New trip"}</h2><button class="icon-btn" data-close>✕</button></div>
    <div class="form-grid">
      <label class="field span2"><span>Destination</span>
        <input id="f-dest" value="${v("destination", "")}" maxlength="120" placeholder="Gudauri" /></label>
      <label class="field span2"><span>Location label <em>(optional)</em></span>
        <input id="f-loc" value="${v("location_label", "")}" maxlength="120" placeholder="Gudauri ski resort" /></label>
      <label class="field"><span>Start date</span>
        <input id="f-start" type="date" value="${v("start_date", "")}" /></label>
      <label class="field"><span>End date</span>
        <input id="f-end" type="date" value="${v("end_date", "")}" /></label>
      <label class="field"><span>Car seats free</span>
        <input id="f-seats" type="number" min="0" max="50" value="${v("car_seats", "0")}" /></label>
      <label class="field"><span>Category</span>
        <select id="f-cat">${cats.map(c => `<option value="${c}" ${(t.category || "other") === c ? "selected" : ""}>${c}</option>`).join("")}</select></label>
      <label class="field span2"><span>Privacy</span>
        <select id="f-priv">
          <option value="normal" ${t.privacy !== "busy_only" ? "selected" : ""}>Normal — friends see per their tier</option>
          <option value="busy_only" ${t.privacy === "busy_only" ? "selected" : ""}>Private — everyone sees only “unavailable”</option>
        </select></label>
      <label class="field span2"><span>Notes <em>(full-tier only)</em></span>
        <textarea id="f-notes" maxlength="2000" rows="2" placeholder="Leaving 6am, back Sunday night…">${v("notes", "")}</textarea></label>
    </div>
    <div class="modal-error" id="f-error"></div>
    <div class="modal-actions">
      <button class="btn" data-close>Cancel</button>
      <button class="btn btn-primary" id="f-save">${trip ? "Save changes" : "Create trip"}</button>
    </div>`, { wide: true });

  document.getElementById("f-save").onclick = async () => {
    const err = document.getElementById("f-error");
    err.textContent = "";
    const body = {
      destination: document.getElementById("f-dest").value.trim(),
      location_label: document.getElementById("f-loc").value.trim(),
      start_date: document.getElementById("f-start").value,
      end_date: document.getElementById("f-end").value,
      car_seats: parseInt(document.getElementById("f-seats").value, 10) || 0,
      category: document.getElementById("f-cat").value,
      privacy: document.getElementById("f-priv").value,
      notes: document.getElementById("f-notes").value.trim(),
    };
    if (!body.destination) { err.textContent = "Destination is required."; return; }
    if (!body.start_date || !body.end_date) { err.textContent = "Pick start and end dates."; return; }
    if (body.end_date < body.start_date) { err.textContent = ERR.end_before_start; return; }
    try {
      if (trip) await API.updateTrip(trip.id, body);
      else await API.createTrip(body);
      closeModal();
      toast(trip ? "Trip updated." : "Trip created.", "ok");
      await loadAndRender();
    } catch (e) {
      err.textContent = msg(e);
    }
  };
}

// ─── FRIENDS DRAWER (owner) ────────────────────────────────────────────────

async function openFriends() {
  let friends = [];
  try { friends = await API.listFriends(); } catch (e) { return toast(msg(e), "error"); }
  const tierOpts = t => ["busy", "basic", "full"].map(x => `<option value="${x}" ${t === x ? "selected" : ""}>${x}</option>`).join("");
  const rows = friends.map(f => `
    <div class="friend-row ${f.enabled ? "" : "disabled"}" data-fid="${f.id}">
      <div class="fr-main">
        <input class="fr-name" value="${escapeHtml(f.name)}" maxlength="80" />
        <select class="fr-tier">${tierOpts(f.tier)}</select>
        <label class="fr-enabled"><input type="checkbox" class="fr-on" ${f.enabled ? "checked" : ""}/> active</label>
      </div>
      <div class="fr-actions">
        <button class="mini-btn" data-copy="${escapeHtml(f.invite_link)}">Copy link</button>
        <button class="mini-btn" data-rotate="${f.id}" title="Invalidate the old link, make a new one">Rotate</button>
        <button class="mini-btn ok" data-save="${f.id}">Save</button>
        <button class="mini-btn danger" data-delf="${f.id}">Delete</button>
      </div>
    </div>`).join("");

  openModal(`
    <div class="modal-head"><h2>Friends</h2><button class="icon-btn" data-close>✕</button></div>
    <p class="modal-sub">Each friend gets their own link. Tier sets how much they see:
      <b>busy</b> = only “unavailable”, <b>basic</b> = destination + free seats,
      <b>full</b> = + who's coming, notes, and can request a seat.</p>
    <div class="friend-list">${rows || `<div class="muted">No friends yet.</div>`}</div>
    <div class="friend-add">
      <input id="nf-name" placeholder="New friend's name" maxlength="80" />
      <select id="nf-tier">${tierOpts("basic")}</select>
      <button class="btn btn-primary" id="nf-add">Add friend</button>
    </div>`, { wide: true });

  const root = document.getElementById("modal");
  root.querySelectorAll("[data-copy]").forEach(b => b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast("Invite link copied.", "ok"); }
    catch { toast(b.dataset.copy, "info"); }
  });
  root.querySelectorAll("[data-save]").forEach(b => b.onclick = async () => {
    const row = b.closest(".friend-row");
    const body = {
      name: row.querySelector(".fr-name").value.trim(),
      tier: row.querySelector(".fr-tier").value,
      enabled: row.querySelector(".fr-on").checked,
    };
    try { await API.updateFriend(+b.dataset.save, body); toast("Saved.", "ok"); openFriends(); }
    catch (e) { toast(msg(e), "error"); }
  });
  root.querySelectorAll("[data-rotate]").forEach(b => b.onclick = async () => {
    openConfirm("Rotate this link? The old link stops working immediately.", async () => {
      try { await API.updateFriend(+b.dataset.rotate, { rotate: true }); toast("New link generated.", "ok"); openFriends(); }
      catch (e) { toast(msg(e), "error"); }
    });
  });
  root.querySelectorAll("[data-delf]").forEach(b => b.onclick = () => {
    openConfirm("Delete this friend? Their link stops working; confirmed seats stay as named guests.", async () => {
      try { await API.deleteFriend(+b.dataset.delf); toast("Friend deleted.", "ok"); openFriends(); }
      catch (e) { toast(msg(e), "error"); }
    });
  });
  document.getElementById("nf-add").onclick = async () => {
    const name = document.getElementById("nf-name").value.trim();
    const tier = document.getElementById("nf-tier").value;
    if (!name) return toast("Enter a name.", "error");
    try { await API.createFriend({ name, tier }); toast("Friend added.", "ok"); openFriends(); }
    catch (e) { toast(msg(e), "error"); }
  };
}

// ─── REQUESTS QUEUE (owner) ────────────────────────────────────────────────

async function openRequests() {
  let reqs = [];
  try { reqs = await API.listRequests(); } catch (e) { return toast(msg(e), "error"); }
  const rows = reqs.map(r => `
    <div class="req-row" data-rid="${r.id}">
      <div class="req-main">
        <b>${escapeHtml(r.display_name)}</b> wants a seat on
        <b>${escapeHtml(r.destination)}</b>
        <span class="muted">${escapeHtml(fmtRange(r.start_date, r.end_date))} · ${r.free_seats}/${r.car_seats} free</span>
      </div>
      <div class="req-actions">
        <button class="mini-btn ok" data-approve="${r.id}">Approve</button>
        <button class="mini-btn" data-decline="${r.id}">Decline</button>
      </div>
    </div>`).join("");
  openModal(`
    <div class="modal-head"><h2>Seat requests</h2><button class="icon-btn" data-close>✕</button></div>
    <div class="req-list">${rows || `<div class="muted">No pending requests 🎉</div>`}</div>`, { wide: true });
  const root = document.getElementById("modal");
  root.querySelectorAll("[data-approve]").forEach(b => b.onclick = async () => {
    try { await API.approveRequest(+b.dataset.approve); toast("Approved.", "ok"); refreshPendingBadge(); openRequests(); if (state.cursor) loadCalendarSilently(); }
    catch (e) { toast(msg(e), "error"); }
  });
  root.querySelectorAll("[data-decline]").forEach(b => b.onclick = async () => {
    try { await API.declineRequest(+b.dataset.decline); toast("Declined.", "ok"); refreshPendingBadge(); openRequests(); if (state.cursor) loadCalendarSilently(); }
    catch (e) { toast(msg(e), "error"); }
  });
}

async function loadCalendarSilently() {
  const { gridStart, gridEnd } = monthGrid(state.cursor);
  try {
    const trips = await API.getCalendar(ymd(gridStart), ymd(gridEnd));
    indexTrips(trips, gridStart, gridEnd);
    renderCalendar();
  } catch (e) { /* ignore */ }
}

async function refreshPendingBadge() {
  try {
    const reqs = await API.listRequests();
    const badge = document.getElementById("pending-badge");
    if (!badge) return;
    if (reqs.length) { badge.hidden = false; badge.textContent = reqs.length; }
    else badge.hidden = true;
  } catch (e) { /* ignore */ }
}

// ─── CONFIRM DIALOG ────────────────────────────────────────────────────────

function openConfirm(text, onYes) {
  const back = openModal(`
    <h2>Are you sure?</h2>
    <p class="modal-sub">${escapeHtml(text)}</p>
    <div class="modal-actions">
      <button class="btn" data-close>Cancel</button>
      <button class="btn btn-danger" id="cf-yes">Yes</button>
    </div>`);
  document.getElementById("cf-yes").onclick = onYes;
}

document.addEventListener("DOMContentLoaded", init);
