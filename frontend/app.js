// ─── app.js ─────────────────────────────────────────────────────────────
// Availability calendar UI. Three modes, decided by GET /api/me:
//   owner   → full calendar + edit controls + admin (trips, friends, requests)
//   friend  → tier-scoped calendar + (full tier) request-a-seat
//   anon    → the same calendar, but every trip shows only as a "Busy" span
//             (dates blocked out, no detail) — ask for a link to see more
// All visibility is enforced server-side; this file only renders whatever the
// projection returned and never assumes hidden fields exist.

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const state = {
  viewer: { role: "anon" },
  viewAs: null,            // owner preview: null (off) | 'anon' | 'busy' | 'basic' | 'full'
  cursor: null,            // Date: first of the displayed month
  tripsById: {},           // id -> projected trip
  tripsByDay: {},          // 'YYYY-MM-DD' -> [trip, ...]
  openDay: null,           // 'YYYY-MM-DD' of the open day modal, or null
};

// The viewer the UI should render for. Normally the real viewer; while the
// owner previews ("view as"), a synthetic friend/anon — matching the projected
// data the server returns for view_as, so chrome and data always agree.
// state.viewer stays the real identity (auth, header, pending badge).
function effViewer() {
  if (state.viewer.role === "owner" && state.viewAs) {
    return state.viewAs === "anon"
      ? { role: "anon", preview: true }
      : { role: "friend", tier: state.viewAs, name: "Preview", preview: true };
  }
  return state.viewer;
}

const ERR = {
  seat_unavailable: "That seat just filled up.",
  already_requested: "You've already asked for a seat on this trip.",
  already_decided: "That request was already handled.",
  seats_below_confirmed: "Can't set fewer seats than people already confirmed.",
  already_active: "That friend already has a seat or pending request here.",
  forbidden: "You don't have access to do that.",
  unauthorized: "Please sign in again.",
  not_found: "Not found.",
  bad_login: "That name and birthday don't match. Check with George.",
  too_many_attempts: "Too many tries. Wait a few minutes and try again.",
  bad_birthday: "Please enter a valid birthday.",
  bad_date: "Please use valid dates.",
  end_before_start: "End date can't be before the start date.",
  range_too_large: "Date range is too large.",
  bad_area: "That map location looks invalid.",
  bad_pins: "One of the map pins looks invalid.",
  bad_pin_activity: "A pin points at an activity that doesn't exist.",
  bad_activities: "Activities must be short text lines.",
  too_many_activities: "Too many activities (max 50).",
  too_many_pins: "Too many pins (max 100).",
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

// ─── BIRTHDAY PICKER (day-first, consistent on every device) ────────────────
// A native <input type="date"> orders its fields (day vs. month first) by the
// phone's region setting, with no way to override it from code — so birthdays use
// three explicit selects, Day · Month · Year with the month spelled out. That
// renders identically everywhere and removes all DD/MM ambiguity. dobPickerHTML()
// builds one from a 'YYYY-MM-DD' value; readDob() reads it back (or "" if blank).
const MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
                     "July", "August", "September", "October", "November", "December"];
const DOB_MIN_YEAR = 1920;

function dobPickerHTML(value, { id = "", cls = "" } = {}) {
  const [, yy = "", mm = "", dd = ""] = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "") || [];
  const sel = (v, cur) => (v === cur ? " selected" : "");
  let days = `<option value="">Day</option>`;
  for (let d = 1; d <= 31; d++) { const v = String(d).padStart(2, "0"); days += `<option value="${v}"${sel(v, dd)}>${d}</option>`; }
  let months = `<option value="">Month</option>`;
  MONTH_NAMES.forEach((name, i) => { const v = String(i + 1).padStart(2, "0"); months += `<option value="${v}"${sel(v, mm)}>${name}</option>`; });
  let years = `<option value="">Year</option>`;
  for (let y = new Date().getFullYear(); y >= DOB_MIN_YEAR; y--) years += `<option value="${y}"${sel(String(y), yy)}>${y}</option>`;
  return `<span class="dob${cls ? " " + cls : ""}"${id ? ` id="${id}"` : ""}>`
    + `<select class="dob-d" aria-label="Day of birth">${days}</select>`
    + `<select class="dob-m" aria-label="Month of birth">${months}</select>`
    + `<select class="dob-y" aria-label="Year of birth">${years}</select></span>`;
}

function readDob(container) {
  if (!container) return "";
  const d = container.querySelector(".dob-d")?.value || "";
  const m = container.querySelector(".dob-m")?.value || "";
  const y = container.querySelector(".dob-y")?.value || "";
  return d && m && y ? `${y}-${m}-${d}` : "";
}

// Keep the Day options valid for the chosen month/year (no 31 April, Feb 29 only
// on leap years). Call once after a picker is inserted into the DOM.
function wireDob(container) {
  const d = container?.querySelector(".dob-d");
  const m = container?.querySelector(".dob-m");
  const y = container?.querySelector(".dob-y");
  if (!d || !m || !y) return;
  const sync = () => {
    // Day 0 of month N+1 is the last day of month N; default year 2000 (a leap
    // year) is permissive until a year is picked, then re-clamps.
    const max = m.value ? new Date(Number(y.value) || 2000, Number(m.value), 0).getDate() : 31;
    for (const o of d.options) if (o.value) o.disabled = Number(o.value) > max;
    if (d.selectedOptions[0]?.disabled) d.value = "";
  };
  m.addEventListener("change", sync);
  y.addEventListener("change", sync);
  sync();
}

// ─── MAPS (Leaflet, loaded from a CDN — every use is guarded so the app
//     still works fully, minus maps, if it didn't load) ─────────────────────

const DEFAULT_VIEW = { center: [42.1, 43.75], zoom: 7 };   // Georgia, the country

function mapsAvailable() { return typeof L !== "undefined"; }

function makeMap(el, opts = {}) {
  const map = L.map(el, { scrollWheelZoom: !!opts.scroll });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  return map;
}

// What a pin should be called in tooltips/popups: its label, else the linked
// activity's name, else a generic "Spot".
function pinName(p, activities) {
  const act = p.activity != null && Array.isArray(activities) ? activities[p.activity] : null;
  return p.label || act || "Spot";
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

// ─── DRAG-SORT (pointer events — mouse and touch alike) ───────────────────
// Rows drag by their .drag-handle: DOM order follows the pointer live, and
// onDone(from, to) fires on release. One delegated listener per container, so
// rows can be re-rendered freely between drags. (HTML5 drag-and-drop is
// deliberately avoided — it doesn't work on mobile touch.)

function moveItem(arr, from, to) { const [x] = arr.splice(from, 1); arr.splice(to, 0, x); }

function makeSortable(container, onDone) {
  if (!container) return;
  container.addEventListener("pointerdown", e => {
    const handle = e.target.closest(".drag-handle");
    if (!handle || e.button > 0 || !container.contains(handle)) return;
    let row = handle;
    while (row.parentElement && row.parentElement !== container) row = row.parentElement;
    if (row.parentElement !== container) return;
    e.preventDefault();   // no text selection; touch-action:none on the handle stops scroll
    const from = [...container.children].indexOf(row);
    row.classList.add("dragging");

    const move = ev => {
      ev.preventDefault();
      // Insert before the first sibling whose midpoint is below the pointer.
      for (const sib of container.children) {
        if (sib === row) continue;
        const b = sib.getBoundingClientRect();
        if (ev.clientY < b.top + b.height / 2) { container.insertBefore(row, sib); return; }
      }
      container.appendChild(row);
    };
    const up = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", up);
      row.classList.remove("dragging");
      const to = [...container.children].indexOf(row);
      if (to !== from) onDone(from, to);
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", up);
  });
}

// ─── BOOT ─────────────────────────────────────────────────────────────────

// The platform homepage lives on the apex domain; this app is a subdomain
// (availability.<domain>). Strip the leading "availability." label to get back
// there, keeping protocol/port. Overridable via window.PROJECTS_URL; falls back
// to "/" when there's no subdomain to strip (e.g. bare localhost dev).
function projectsUrl() {
  if (typeof window !== "undefined" && window.PROJECTS_URL) return window.PROJECTS_URL;
  const { protocol, hostname, port } = window.location;
  const apex = hostname.replace(/^availability\./, "");
  if (apex === hostname) return "/";
  return `${protocol}//${apex}${port ? ":" + port : ""}`;
}

// ─── THEME ──────────────────────────────────────────────────────────────
// Dark is the default; light is opt-in and persisted. The saved theme is also
// applied by an inline <head> script so there's no flash on load.
const THEME_KEY = "theme";

function applyTheme(theme) {
  const light = theme === "light";
  if (light) document.documentElement.setAttribute("data-theme", "light");
  else document.documentElement.removeAttribute("data-theme");
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    btn.textContent = light ? "☾" : "☀";   // shows the mode you'd switch to
    const label = light ? "Switch to dark mode" : "Switch to light mode";
    btn.title = label;
    btn.setAttribute("aria-label", label);
  }
}

function initTheme() {
  let saved;
  try { saved = localStorage.getItem(THEME_KEY); } catch (e) {}
  applyTheme(saved === "light" ? "light" : "dark");
  const btn = document.getElementById("theme-toggle");
  if (btn) btn.onclick = () => {
    const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    applyTheme(next);
  };
}

async function init() {
  initTheme();
  API.captureInviteToken();
  const back = document.getElementById("back-to-projects");
  if (back) back.href = projectsUrl();
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
  // Anon included: the calendar itself is public, projected down to busy spans.
  state.cursor = startOfMonth(new Date());
  await loadAndRender();
}

// ─── HEADER ─────────────────────────────────────────────────────────────

function renderHeader() {
  const c = document.getElementById("nav-controls");
  const v = state.viewer;
  if (v.role === "owner") {
    // While previewing, the edit controls disappear (a friend wouldn't have
    // them); the select doubles as the exit — pick "Owner" to come back.
    const opts = [
      ["", "Owner"], ["anon", "Visitor (signed out)"],
      ["busy", "Friend · busy"], ["basic", "Friend · basic"], ["full", "Friend · full"],
    ].map(([val, label]) =>
      `<option value="${val}" ${(state.viewAs || "") === val ? "selected" : ""}>${label}</option>`).join("");
    const viewAsSel = `<label class="view-as ${state.viewAs ? "active" : ""}" title="Preview the calendar as a friend would see it">
      <span aria-hidden="true">👁</span><select id="view-as" aria-label="View calendar as">${opts}</select></label>`;
    c.innerHTML = state.viewAs ? `
      ${viewAsSel}
      <span class="badge owner" id="btn-signout" title="Sign out">Owner ✕</span>` : `
      <button class="btn btn-ghost" id="btn-requests">Requests<span id="pending-badge" class="count-badge" hidden>0</span></button>
      <button class="btn btn-ghost" id="btn-friends">Friends</button>
      <button class="btn btn-primary" id="btn-new-trip">＋ Trip</button>
      ${viewAsSel}
      <span class="badge owner" id="btn-signout" title="Sign out">Owner ✕</span>`;
    if (!state.viewAs) {
      c.querySelector("#btn-new-trip").onclick = () => openTripForm();
      c.querySelector("#btn-friends").onclick = openFriends;
      c.querySelector("#btn-requests").onclick = openRequests;
    }
    c.querySelector("#view-as").onchange = e => setViewAs(e.target.value || null);
    c.querySelector("#btn-signout").onclick = () => { API.logoutOwner(); location.reload(); };
  } else if (v.role === "friend") {
    c.innerHTML = `
      <span class="badge tier-${escapeHtml(v.tier)}">${escapeHtml(v.name)} · ${escapeHtml(v.tier)}</span>
      <button class="btn btn-ghost" id="btn-signout-friend" title="Sign out">Sign out</button>
      <button class="btn btn-ghost" id="btn-owner" title="Owner sign-in">⚙</button>`;
    c.querySelector("#btn-signout-friend").onclick = () => { API.forgetUser(); location.reload(); };
    c.querySelector("#btn-owner").onclick = openOwnerLogin;
  } else {
    c.innerHTML = `
      <button class="btn btn-primary" id="btn-login">Sign in</button>
      <button class="btn btn-ghost" id="btn-owner">Owner</button>`;
    c.querySelector("#btn-login").onclick = openFriendLogin;
    c.querySelector("#btn-owner").onclick = openOwnerLogin;
  }
}

// Owner "view as" preview: swap the projection the server returns and re-render
// everything (an open day modal included, so its cards flip too).
async function setViewAs(mode) {
  state.viewAs = mode;
  renderHeader();
  await loadAndRender();
  if (mode) toast(`Previewing as ${mode === "anon" ? "a signed-out visitor" : `a ${mode}-tier friend`}.`, "info");
}

// ─── FRIEND LOGIN ─────────────────────────────────────────────────────────

function openFriendLogin() {
  openModal(`
    <h2>Sign in</h2>
    <p class="modal-sub">Enter your name and birthday, exactly as George set them up, to see trip details and request a seat.</p>
    <label class="field"><span>Name</span>
      <input type="text" id="fl-name" autocomplete="off" placeholder="Your name" maxlength="80" /></label>
    <label class="field"><span>Birthday</span>
      ${dobPickerHTML("", { id: "fl-bday" })}</label>
    <div class="modal-error" id="fl-error"></div>
    <div class="modal-actions">
      <button class="btn" data-close>Cancel</button>
      <button class="btn btn-primary" id="fl-go">Sign in</button>
    </div>`);
  const name = document.getElementById("fl-name");
  name.focus();
  wireDob(document.getElementById("fl-bday"));
  const go = async () => {
    const err = document.getElementById("fl-error");
    err.textContent = "";
    const bday = readDob(document.getElementById("fl-bday"));
    if (!name.value.trim() || !bday) { err.textContent = "Enter your name and birthday."; return; }
    try {
      await API.loginFriend(name.value, bday);
      location.reload();
    } catch (e) {
      err.textContent = msg(e);
    }
  };
  document.getElementById("fl-go").onclick = go;
  name.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); go(); }
  });
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
    trips = await API.getCalendar(ymd(gridStart), ymd(gridEnd), state.viewAs);
  } catch (e) {
    if (e.status === 401) { API.logoutOwner(); return location.reload(); }
    toast(msg(e), "error");
  }
  indexTrips(trips, gridStart, gridEnd);
  renderCalendar();
  // Real role, not effViewer(): the badge is owner chrome (and its element is
  // absent while previewing — refreshPendingBadge no-ops then).
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

  const ev = effViewer();
  const legend = ev.role === "owner"
    ? `<span class="legend-hint">Click a day to add a trip · click a trip to manage it</span>`
    : ev.role === "anon"
    ? `<span class="legend-hint">Marked days are unavailable · sign in with your name and birthday to see details</span>`
    : `<span class="legend-hint">Click a marked day for details${ev.tier === "full" ? " · request a seat" : ""}</span>`;

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
  else if (effViewer().role === "owner") openTripForm(null, { start_date: day, end_date: day });
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
    <div class="trip-cards">${cards}</div>`, { wide: effViewer().role === "owner" });
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
  const owner = effViewer().role === "owner";

  if (!t.destination) {
    // busy-only projection (anon, busy tier, or a busy_only trip seen by a friend)
    return `<div class="trip-card busy ${t.on_trip ? "mine" : ""}">
      <div class="tc-title"><span class="tc-dot ${t.on_trip ? "mine" : "busy"}"></span>${t.on_trip ? "You're away this day" : "Unavailable"}</div>
      <div class="tc-dates">${escapeHtml(fmtRange(t.start_date, t.end_date))}</div>
      ${friendActions(t)}
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
  } else {
    actions = friendActions(t);
  }

  // Activities (basic tier +) and the map (area for basic, + labeled pins for
  // full/owner). The map div is instantiated by wireTripCards() after insert.
  // An activity with a linked pin renders clickable (jump the map to the pin) —
  // only when the viewer's projection actually includes pins AND the map will
  // render, so basic-tier bullets stay plain text.
  const hasGeo = t.area || (Array.isArray(t.pins) && t.pins.length);
  const mapBox = hasGeo && mapsAvailable() ? `<div class="tc-map" data-map="${t.id}"></div>` : "";
  const pinnedActs = new Set(
    (mapBox && Array.isArray(t.pins) ? t.pins : [])
      .map(p => p.activity).filter(i => i != null));
  const activities = Array.isArray(t.activities) && t.activities.length
    ? `<div class="tc-acts"><span class="tc-acts-head">Activities</span>
        <ul>${t.activities.map((a, i) => pinnedActs.has(i)
          ? `<li class="linked" data-act="${i}" title="Show on the map">${escapeHtml(a)}<span class="act-pin">📍</span></li>`
          : `<li>${escapeHtml(a)}</li>`).join("")}</ul></div>`
    : "";

  return `<div class="trip-card ${t.on_trip ? "mine" : ""}">
    <div class="tc-title"><span class="tc-dot cat-${escapeHtml(t.category || "other")}"></span>${escapeHtml(t.destination)}
      ${t.category && t.category !== "other" ? `<span class="tag">${escapeHtml(t.category)}</span>` : ""}
      ${owner && t.privacy === "busy_only" ? `<span class="tag private">private</span>` : ""}</div>
    <div class="tc-dates">${escapeHtml(fmtRange(t.start_date, t.end_date))}</div>
    ${t.location_label ? `<div class="tc-loc">📍 ${escapeHtml(t.location_label)}</div>` : ""}
    ${seats}
    ${activities}
    ${mapBox}
    ${t.notes ? `<div class="tc-notes">${escapeHtml(t.notes)}</div>` : ""}
    ${people}
    ${actions}
  </div>`;
}

// Read-only trip map on a card: trip spot (+ area circle) and labeled activity
// pins with popups. Zoom/pan enabled; scroll-wheel off so the modal scrolls.
function buildTripMap(el, t) {
  if (!mapsAvailable()) return;
  const map = makeMap(el);
  const layers = [];
  if (t.area) {
    layers.push(L.marker([t.area.lat, t.area.lng])
      .bindPopup(`<b>${escapeHtml(t.destination || "Trip")}</b>`));
    if (t.area.radius_m) {
      layers.push(L.circle([t.area.lat, t.area.lng], {
        radius: t.area.radius_m, weight: 2, color: "#4f8cff", fillOpacity: .12,
      }));
    }
  }
  const byActivity = new Map();   // activity index -> [marker, ...]
  for (const p of (t.pins || [])) {
    const act = p.activity != null && t.activities ? t.activities[p.activity] : null;
    const popup = `<b>${escapeHtml(pinName(p, t.activities))}</b>`
      + (act && p.label ? `<br><span class="small">${escapeHtml(act)}</span>` : "");
    const marker = L.marker([p.lat, p.lng], { opacity: .92 })
      .bindTooltip(escapeHtml(pinName(p, t.activities)), { direction: "top" })
      .bindPopup(popup);
    layers.push(marker);
    if (p.activity != null) {
      if (!byActivity.has(p.activity)) byActivity.set(p.activity, []);
      byActivity.get(p.activity).push(marker);
    }
  }
  const group = L.featureGroup(layers).addTo(map);
  map.fitBounds(group.getBounds().pad(0.25), { maxZoom: 13 });
  // The modal was appended this tick; Leaflet needs a size pass once laid out.
  setTimeout(() => map.invalidateSize(), 0);

  // Clickable activity bullets → jump to that activity's pin (one pin: center
  // + popup; several: frame them all).
  el.closest(".trip-card")?.querySelectorAll(".tc-acts li.linked").forEach(li => {
    li.onclick = () => {
      const markers = byActivity.get(+li.dataset.act);
      if (!markers || !markers.length) return;
      el.scrollIntoView({ behavior: "smooth", block: "nearest" });
      if (markers.length === 1) {
        map.setView(markers[0].getLatLng(), Math.max(map.getZoom(), 14), { animate: true });
        markers[0].openPopup();
      } else {
        map.fitBounds(L.featureGroup(markers).getBounds().pad(0.3), { maxZoom: 15 });
      }
    };
  });
}

// The friend-facing action row for a trip — shared by the busy and detail cards.
//   confirmed (a linked seat)  → "you're on this trip" + Leave
//   pending  (a seat request)  → pending + Cancel
//   on_trip via name match only → "you're on this trip" (George's guest; no Leave)
//   otherwise, full tier        → Request a seat / Join waitlist
function friendActions(t) {
  if (effViewer().role !== "friend") return "";
  if (t.my_status === "confirmed") {
    return `<div class="tc-actions"><span class="you-in">✓ You're on this trip</span>
      <button class="btn btn-ghost" data-leave="${t.id}">Leave trip</button></div>`;
  }
  if (t.my_status === "pending") {
    return `<div class="tc-actions"><span class="pending-tag">⏳ Seat requested — pending</span>
      <button class="btn btn-ghost" data-leave="${t.id}">Cancel</button></div>`;
  }
  if (t.on_trip) {   // name-matched guest the owner added — not the friend's to remove
    return `<div class="tc-actions"><span class="you-in">✓ You're on this trip</span></div>`;
  }
  if (t.can_request) {
    const waitlist = t.free_seats <= 0;
    return `<div class="tc-actions"><button class="btn btn-primary" data-request="${t.id}">${waitlist ? "Join waitlist" : "Request a seat"}</button></div>`;
  }
  return "";
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
      <input type="text" class="roster-add-input" data-addguest="${t.id}" placeholder="Add a friend or guest by name…" maxlength="80" autocomplete="off" />
      <div class="guest-suggest" data-suggest="${t.id}" hidden></div>
    </div>
  </div>`;
}

function wireTripCards() {
  const root = document.getElementById("modal");
  if (!root) return;
  // Owner previewing as a friend: the buttons render (that's the point of the
  // preview) but must not fire real friend actions from the owner's session.
  const previewBlock = effViewer().preview
    ? () => { toast("Preview only — switch back to Owner to make changes.", "info"); return true; }
    : () => false;
  root.querySelectorAll("[data-request]").forEach(b => b.onclick = () => { if (!previewBlock()) act(() => API.requestSeat(+b.dataset.request), "Seat requested."); });
  root.querySelectorAll("[data-leave]").forEach(b => b.onclick = () => { if (!previewBlock()) act(() => API.leaveTrip(+b.dataset.leave), "Removed you from the trip."); });
  root.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => openTripForm(state.tripsById[+b.dataset.edit]));
  root.querySelectorAll("[data-del]").forEach(b => b.onclick = () => delTrip(+b.dataset.del));
  root.querySelectorAll("[data-approve]").forEach(b => b.onclick = () => act(() => API.approveRequest(+b.dataset.approve), "Approved."));
  root.querySelectorAll("[data-decline]").forEach(b => b.onclick = () => act(() => API.declineRequest(+b.dataset.decline), "Declined."));
  root.querySelectorAll("[data-remove]").forEach(b => b.onclick = () => act(() => API.removeParticipant(+b.dataset.remove), "Removed."));
  root.querySelectorAll("[data-addguest]").forEach(inp => {
    const tripId = +inp.dataset.addguest;
    const box = root.querySelector(`.guest-suggest[data-suggest="${tripId}"]`);
    setupGuestAutocomplete(inp, box, tripId);
  });
  root.querySelectorAll(".tc-map").forEach(el => {
    const t = state.tripsById[+el.dataset.map];
    if (t) buildTripMap(el, t);
  });
}

// ─── GUEST AUTOCOMPLETE (owner) ────────────────────────────────────────────
// The "add" box suggests friends NOT already on this trip and filters them by
// what's typed. Picking one links by friend_id (so the friend sees it as their
// own trip and can't double-request); free text still adds a plain guest.

let friendsCache = null;
async function getFriends() {
  if (!friendsCache) friendsCache = await API.listFriends();
  return friendsCache;
}
function invalidateFriends() { friendsCache = null; }

function setupGuestAutocomplete(inp, box, tripId) {
  if (!box) return;
  const trip = state.tripsById[tripId];
  // Friends already on the trip (confirmed or pending) — exclude from suggestions.
  const taken = new Set((trip.participants || [])
    .filter(p => p.friend_id && (p.status === "confirmed" || p.status === "pending"))
    .map(p => p.friend_id));

  async function candidates() {
    const q = inp.value.trim().toLowerCase();
    const friends = await getFriends();
    return friends.filter(f => !taken.has(f.id) && f.name.toLowerCase().includes(q));
  }

  async function refresh() {
    const list = await candidates();
    if (!list.length) { box.hidden = true; box.innerHTML = ""; return; }
    box.innerHTML = list.slice(0, 8).map(f =>
      `<button type="button" class="suggest-item" data-fid="${f.id}">
        <span class="suggest-name">${escapeHtml(f.name)}</span>
        <span class="suggest-tier tier-${escapeHtml(f.tier)}">${escapeHtml(f.tier)}${f.enabled ? "" : " · off"}</span>
      </button>`).join("");
    box.hidden = false;
    box.querySelectorAll("[data-fid]").forEach(b =>
      // mousedown fires before the input's blur, so the click isn't swallowed
      b.addEventListener("mousedown", e => {
        e.preventDefault();
        box.hidden = true;
        act(() => API.addParticipant(tripId, { friend_id: +b.dataset.fid }), "Added.");
      }));
  }

  inp.addEventListener("focus", refresh);
  inp.addEventListener("input", refresh);
  inp.addEventListener("blur", () => setTimeout(() => { box.hidden = true; }, 150));
  inp.addEventListener("keydown", async e => {
    if (e.key === "Escape") { box.hidden = true; return; }
    if (e.key !== "Enter") return;
    e.preventDefault();
    const typed = inp.value.trim();
    if (!typed) return;
    // Enter on a name that exactly matches an available friend links them by id;
    // otherwise it's added as a plain named guest.
    const list = await candidates();
    const exact = list.find(f => f.name.toLowerCase() === typed.toLowerCase());
    if (exact) act(() => API.addParticipant(tripId, { friend_id: exact.id }), "Added.");
    else act(() => API.addParticipant(tripId, { name: typed }), "Added.");
  });
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
      <div class="field span2"><span>Activities <em>(what's on offer — basic tier and up)</em></span>
        <div id="f-acts" class="act-rows"></div>
        <button type="button" class="mini-btn" id="f-act-add">＋ Add activity</button>
      </div>
      <div class="field span2"><span>Map <em>(trip spot &amp; area for basic+ · labeled pins for full tier)</em></span>
        ${mapsAvailable() ? `
        <div class="map-modes">
          <button type="button" class="mini-btn mode active" id="f-mode-trip">📍 Trip spot</button>
          <button type="button" class="mini-btn mode" id="f-mode-pin">➕ Activity pins</button>
          <button type="button" class="mini-btn" id="f-area-clear">Clear spot</button>
          <span class="small muted" id="f-map-hint">Click the map to drop the trip spot</span>
        </div>
        <div id="f-map" class="map-editor"></div>
        <div class="map-radius" id="f-radius-row" hidden>
          <span class="small muted">Area radius</span>
          <input type="range" id="f-radius" min="0" max="50000" step="500" value="0" />
          <span class="small muted" id="f-radius-val">pin only</span>
        </div>
        <div id="f-pins" class="pin-rows"></div>
        ` : `<div class="muted small">Map unavailable — Leaflet didn't load. Activities still save.</div>`}
      </div>
    </div>
    <div class="modal-error" id="f-error"></div>
    <div class="modal-actions">
      <button class="btn" data-close>Cancel</button>
      <button class="btn btn-primary" id="f-save">${trip ? "Save changes" : "Create trip"}</button>
    </div>`, { wide: true });

  // ── Editor state: activities + geo, saved wholesale with the trip. Pins link
  // to activities by INDEX into the acts array; empty act rows are dropped at
  // save time with the indexes remapped to match.
  const geo = {
    acts: (t.activities || []).slice(),
    area: t.area ? { ...t.area } : null,
    pins: (t.pins || []).map(p => ({ ...p })),
  };

  // ── Activities rows ──
  const actsBox = document.getElementById("f-acts");
  function actRemove(i) {
    geo.acts.splice(i, 1);
    // Keep pin links pointing at the same activity: drop links to the removed
    // row, shift the ones after it.
    geo.pins.forEach(p => {
      if (p.activity == null) return;
      if (p.activity === i) p.activity = null;
      else if (p.activity > i) p.activity--;
    });
    redrawActs(); redrawPins(); syncPinMarkers();
  }
  function redrawActs() {
    actsBox.innerHTML = geo.acts.map((a, i) => `
      <div class="act-row">
        <span class="drag-handle" title="Drag to reorder">⠿</span>
        <input type="text" data-act="${i}" value="${escapeHtml(a)}" maxlength="120" placeholder="Skiing, sauna, board games…" />
        <button type="button" class="mini-btn danger" data-act-del="${i}" title="Remove">✕</button>
      </div>`).join("");
    actsBox.querySelectorAll("[data-act]").forEach(inp => {
      inp.oninput = () => { geo.acts[+inp.dataset.act] = inp.value; };
      // Renames show up in pin dropdowns once the field settles.
      inp.onchange = () => { redrawPins(); syncPinMarkers(); };
    });
    actsBox.querySelectorAll("[data-act-del]").forEach(b => b.onclick = () => actRemove(+b.dataset.actDel));
  }
  document.getElementById("f-act-add").onclick = () => {
    geo.acts.push("");
    redrawActs();
    actsBox.querySelector(`[data-act="${geo.acts.length - 1}"]`)?.focus();
  };
  redrawActs();
  // Reorder activities by dragging. Pin links follow their activity: build the
  // old-index → new-index map for the same move and rewrite p.activity.
  makeSortable(actsBox, (from, to) => {
    const order = geo.acts.map((_, i) => i);
    moveItem(order, from, to);
    moveItem(geo.acts, from, to);
    const newPos = new Map(order.map((oldIdx, pos) => [oldIdx, pos]));
    geo.pins.forEach(p => { if (p.activity != null) p.activity = newPos.get(p.activity); });
    redrawActs(); redrawPins();
  });

  // ── Map editor ──
  let fmap = null, tripMarker = null, tripCircle = null, pinMarkers = [];
  const pinsBox = document.getElementById("f-pins");
  // Reorder pins by dragging their number chip; rows and marker tooltips renumber.
  makeSortable(pinsBox, (from, to) => {
    moveItem(geo.pins, from, to);
    redrawPins(); syncPinMarkers();
  });

  function redrawPins() {
    if (!pinsBox) return;
    const options = i => [`<option value="">— no linked activity —</option>`]
      .concat(geo.acts.map((a, j) =>
        `<option value="${j}" ${geo.pins[i].activity === j ? "selected" : ""}>${escapeHtml(a.trim() || `(activity ${j + 1})`)}</option>`))
      .join("");
    pinsBox.innerHTML = geo.pins.map((p, i) => `
      <div class="pin-row">
        <span class="pin-num drag-handle" title="Drag to reorder">${i + 1}</span>
        <input type="text" data-pin-label="${i}" value="${escapeHtml(p.label || "")}" maxlength="120" placeholder="Label (e.g. lift base, chalet)" />
        <select data-pin-act="${i}" title="Linked activity">${options(i)}</select>
        <button type="button" class="mini-btn danger" data-pin-del="${i}" title="Remove pin">✕</button>
      </div>`).join("");
    pinsBox.querySelectorAll("[data-pin-label]").forEach(inp => {
      inp.oninput = () => { geo.pins[+inp.dataset.pinLabel].label = inp.value; };
      inp.onchange = () => syncPinMarkers();   // tooltip picks up the new label
    });
    pinsBox.querySelectorAll("[data-pin-act]").forEach(sel => {
      sel.onchange = () => {
        geo.pins[+sel.dataset.pinAct].activity = sel.value === "" ? null : +sel.value;
        syncPinMarkers();
      };
    });
    pinsBox.querySelectorAll("[data-pin-del]").forEach(b => b.onclick = () => {
      geo.pins.splice(+b.dataset.pinDel, 1);
      redrawPins(); syncPinMarkers();
    });
  }

  const r6 = x => Math.round(x * 1e6) / 1e6;   // ~0.1 m precision is plenty

  function radiusLabelText() {
    const m = geo.area && geo.area.radius_m;
    return !m ? "pin only" : m < 1000 ? `${m} m` : `${(m / 1000).toFixed(m % 1000 ? 1 : 0)} km`;
  }

  function updateCircle() {
    if (!fmap) return;
    if (tripCircle) { fmap.removeLayer(tripCircle); tripCircle = null; }
    if (geo.area && geo.area.radius_m) {
      tripCircle = L.circle([geo.area.lat, geo.area.lng], {
        radius: geo.area.radius_m, weight: 2, color: "#4f8cff", fillOpacity: .1, interactive: false,
      }).addTo(fmap);
    }
    const val = document.getElementById("f-radius-val");
    if (val) val.textContent = radiusLabelText();
  }

  function syncArea() {
    if (!fmap) return;
    if (tripMarker) { fmap.removeLayer(tripMarker); tripMarker = null; }
    const row = document.getElementById("f-radius-row");
    if (row) row.hidden = !geo.area;
    if (geo.area) {
      tripMarker = L.marker([geo.area.lat, geo.area.lng], { draggable: true })
        .bindTooltip("Trip spot", { direction: "top" }).addTo(fmap);
      tripMarker.on("dragend", () => {
        const ll = tripMarker.getLatLng();
        geo.area.lat = r6(ll.lat); geo.area.lng = r6(ll.lng);
        updateCircle();
      });
      const slider = document.getElementById("f-radius");
      if (slider) slider.value = geo.area.radius_m || 0;
    }
    updateCircle();
  }

  function syncPinMarkers() {
    if (!fmap) return;
    pinMarkers.forEach(m => fmap.removeLayer(m));
    pinMarkers = geo.pins.map((p, i) => {
      const m = L.marker([p.lat, p.lng], { draggable: true, opacity: .92 })
        .bindTooltip(`${i + 1} · ${escapeHtml(pinName(p, geo.acts))}`, { direction: "top" })
        .addTo(fmap);
      m.on("dragend", () => {
        const ll = m.getLatLng();
        p.lat = r6(ll.lat); p.lng = r6(ll.lng);
      });
      return m;
    });
  }

  if (mapsAvailable() && document.getElementById("f-map")) {
    fmap = makeMap(document.getElementById("f-map"), { scroll: true });
    // Start where the trip already is; else show the whole default region.
    if (geo.area) fmap.setView([geo.area.lat, geo.area.lng], 10);
    else if (geo.pins.length) fmap.fitBounds(L.latLngBounds(geo.pins.map(p => [p.lat, p.lng])).pad(0.3), { maxZoom: 12 });
    else fmap.setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);

    let mode = "trip";
    const hint = document.getElementById("f-map-hint");
    const modeBtns = { trip: document.getElementById("f-mode-trip"), pin: document.getElementById("f-mode-pin") };
    function setMode(m) {
      mode = m;
      modeBtns.trip.classList.toggle("active", m === "trip");
      modeBtns.pin.classList.toggle("active", m === "pin");
      if (hint) hint.textContent = m === "trip"
        ? "Click the map to drop the trip spot"
        : "Each click adds an activity pin";
    }
    modeBtns.trip.onclick = () => setMode("trip");
    modeBtns.pin.onclick = () => setMode("pin");
    document.getElementById("f-area-clear").onclick = () => { geo.area = null; syncArea(); };
    document.getElementById("f-radius").oninput = e => {
      if (!geo.area) return;
      geo.area.radius_m = +e.target.value || null;
      updateCircle();
    };
    fmap.on("click", e => {
      if (mode === "trip") {
        geo.area = { lat: r6(e.latlng.lat), lng: r6(e.latlng.lng), radius_m: geo.area ? geo.area.radius_m : null };
        syncArea();
      } else {
        geo.pins.push({ lat: r6(e.latlng.lat), lng: r6(e.latlng.lng), label: "", activity: null });
        redrawPins(); syncPinMarkers();
        pinsBox.querySelector(`[data-pin-label="${geo.pins.length - 1}"]`)?.focus();
      }
    });

    syncArea(); syncPinMarkers(); redrawPins();
    setTimeout(() => fmap.invalidateSize(), 0);
  } else {
    redrawPins();
  }

  document.getElementById("f-save").onclick = async () => {
    const err = document.getElementById("f-error");
    err.textContent = "";
    // Drop blank activity rows, remapping pin links so they stay on the same
    // activity (removing a blank row above shifts the indexes below it).
    const idxMap = new Map();
    const activities = [];
    geo.acts.forEach((s, i) => {
      const val = s.trim();
      if (val) { idxMap.set(i, activities.length); activities.push(val); }
    });
    const body = {
      destination: document.getElementById("f-dest").value.trim(),
      location_label: document.getElementById("f-loc").value.trim(),
      start_date: document.getElementById("f-start").value,
      end_date: document.getElementById("f-end").value,
      car_seats: parseInt(document.getElementById("f-seats").value, 10) || 0,
      category: document.getElementById("f-cat").value,
      privacy: document.getElementById("f-priv").value,
      notes: document.getElementById("f-notes").value.trim(),
      activities,
      area: geo.area,
      pins: geo.pins.map(p => ({
        lat: p.lat, lng: p.lng, label: (p.label || "").trim(),
        activity: p.activity != null && idxMap.has(p.activity) ? idxMap.get(p.activity) : null,
      })),
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
        <input class="fr-name" value="${escapeHtml(f.name)}" maxlength="80" placeholder="Name" />
        ${dobPickerHTML(f.birthday, { cls: "fr-bday" })}
        <select class="fr-tier">${tierOpts(f.tier)}</select>
        <label class="fr-enabled"><input type="checkbox" class="fr-on" ${f.enabled ? "checked" : ""}/> active</label>
      </div>
      ${f.birthday ? "" : `<div class="fr-warn">No birthday set — this friend can't sign in yet.</div>`}
      <div class="fr-actions">
        <button class="mini-btn" data-copy="${escapeHtml(f.invite_link)}" title="Legacy invite link (still works)">Copy link</button>
        <button class="mini-btn" data-rotate="${f.id}" title="Invalidate the old link, make a new one">Rotate</button>
        <button class="mini-btn ok" data-save="${f.id}">Save</button>
        <button class="mini-btn danger" data-delf="${f.id}">Delete</button>
      </div>
    </div>`).join("");

  openModal(`
    <div class="modal-head"><h2>Friends</h2><button class="icon-btn" data-close>✕</button></div>
    <p class="modal-sub">Each friend signs in with their <b>name + birthday</b>, so set both.
      Tier sets how much they see:
      <b>busy</b> = only “unavailable”, <b>basic</b> = destination, free seats, activities &amp; map area,
      <b>full</b> = + who's coming, notes, exact map pins, and can request a seat.</p>
    <div class="friend-list">${rows || `<div class="muted">No friends yet.</div>`}</div>
    <div class="friend-add">
      <input id="nf-name" placeholder="New friend's name" maxlength="80" />
      ${dobPickerHTML("", { id: "nf-bday" })}
      <select id="nf-tier">${tierOpts("basic")}</select>
      <button class="btn btn-primary" id="nf-add">Add friend</button>
    </div>`, { wide: true });

  const root = document.getElementById("modal");
  root.querySelectorAll(".dob").forEach(wireDob);   // clamp Day to the chosen month/year
  root.querySelectorAll("[data-copy]").forEach(b => b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast("Invite link copied.", "ok"); }
    catch { toast(b.dataset.copy, "info"); }
  });
  root.querySelectorAll("[data-save]").forEach(b => b.onclick = async () => {
    const row = b.closest(".friend-row");
    const body = {
      name: row.querySelector(".fr-name").value.trim(),
      birthday: readDob(row),                        // "" clears it server-side
      tier: row.querySelector(".fr-tier").value,
      enabled: row.querySelector(".fr-on").checked,
    };
    try {
      await API.updateFriend(+b.dataset.save, body);
      invalidateFriends(); toast("Saved.", "ok"); openFriends();
      // A rename/tier change alters participant names & seat math on trips, so
      // refresh the calendar underneath the drawer — the edit shows up at once.
      if (state.cursor) loadCalendarSilently();
    }
    catch (e) { toast(msg(e), "error"); }
  });
  root.querySelectorAll("[data-rotate]").forEach(b => b.onclick = async () => {
    openConfirm("Rotate this link? The old link stops working immediately.", async () => {
      try { await API.updateFriend(+b.dataset.rotate, { rotate: true }); invalidateFriends(); toast("New link generated.", "ok"); openFriends(); }
      catch (e) { toast(msg(e), "error"); }
    });
  });
  root.querySelectorAll("[data-delf]").forEach(b => b.onclick = () => {
    openConfirm("Delete this friend? Their link stops working; confirmed seats stay as named guests.", async () => {
      try { await API.deleteFriend(+b.dataset.delf); invalidateFriends(); toast("Friend deleted.", "ok"); openFriends(); }
      catch (e) { toast(msg(e), "error"); }
    });
  });
  document.getElementById("nf-add").onclick = async () => {
    const name = document.getElementById("nf-name").value.trim();
    const birthday = readDob(document.getElementById("nf-bday"));
    const tier = document.getElementById("nf-tier").value;
    if (!name) return toast("Enter a name.", "error");
    if (!birthday) return toast("Set a birthday so they can sign in.", "error");
    try { await API.createFriend({ name, birthday, tier }); invalidateFriends(); toast("Friend added.", "ok"); openFriends(); }
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
    const trips = await API.getCalendar(ymd(gridStart), ymd(gridEnd), state.viewAs);
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
