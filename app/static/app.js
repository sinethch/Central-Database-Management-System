"use strict";

/* =========================================================================
   DBOps Control Plane — frontend controller
   ========================================================================= */

const APP_VERSION = "3.0.0";

/* Auth gate — client-side operator credentials */
const AUTH_USER_KEY = "dbops_auth_user";
const AUTH_TOKEN_KEY = "dbops_auth_token";
let currentRole = "user";
let currentUsername = "";
let pendingMfaChallenge = "";

const ENGINES = {
  postgres: { abbr: "PG", label: "PostgreSQL", color: "#5b8cff" },
  mongodb:  { abbr: "MG", label: "MongoDB",    color: "#3fb950" },
  mysql:    { abbr: "MY", label: "MySQL",      color: "#9b7bff" },
  redis:    { abbr: "RD", label: "Redis",      color: "#f0574d" },
};

function readActiveDatabaseId() {
  try { return localStorage.getItem("dbops_active_database") || "db-primary"; }
  catch { return "db-primary"; }
}

function readAutoRefreshPreference() {
  try { return localStorage.getItem("dbops_auto_refresh") !== "paused"; }
  catch { return true; }
}

/* Inline SVG icon reference (sprite is defined in index.html) */
function icon(name, cls) {
  return `<svg class="ico${cls ? " " + cls : ""}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
}

function engineBadge(engine, large) {
  const key = String(engine || "").toLowerCase();
  const meta = ENGINES[key] || { abbr: "DB" };
  return `<span class="engine-badge${large ? " engine-badge-lg" : ""}" data-engine="${escapeHtml(key)}">${escapeHtml(meta.abbr)}</span>`;
}

const state = {
  activeDatabaseId: readActiveDatabaseId(),
  databases: [],
  databaseReachability: {},
  reachabilityRefreshing: false,
  activityDbFilter: null,
  filter: "",
  autoRefresh: readAutoRefreshPreference(),
  refreshing: false,
  backupBusy: false,
  lastHealthConnected: true,
  lastSlowCount: 0,
  cachedBackups: [],
  restoreArchives: [],
  restoreBackupId: null,
  restores: [],
  remoteServers: [],
  provisionServerId: null,
  latencyHistory: [12, 14, 11, 15, 13, 16, 12, 10, 14, 11, 13, 12],
  cacheMetric: null,
  sizeMetric: null,
  slowMetric: null,
  deadlockMetric: null,
};
let targetCarouselOffset = 0;
let targetCarouselDrag = null;
let suppressTargetCarouselClick = false;

let refreshTimer = null;
let confirmResolver = null;
const fallbackModals = {};
const serverHealthRequests = new Set();

const commandState = { items: [], index: 0 };
const browseState = { databaseId: "", table: "", offset: 0, limit: 100 };

/* -------------------------------------------------------------------------
   Environment helpers
   ------------------------------------------------------------------------- */
function reducedMotion() {
  return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

function ready(fn) {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn, { once: true });
  else fn();
}

/* -------------------------------------------------------------------------
   Small utilities
   ------------------------------------------------------------------------- */
function engineMeta(engine) {
  return ENGINES[String(engine || "").toLowerCase()] || { abbr: "DB", label: String(engine || "DB").toUpperCase(), color: "#5b8cff" };
}

function maskedSshAddress(server) {
  const host = String(server?.host || "").trim();
  const port = String(server?.ssh_port || 22);
  const ipv4 = host.match(/^(?:\d{1,3}\.){3}(\d{1,3})$/);
  const safeHost = ipv4 ? `***.***.***.${ipv4[1]}` : "***";
  return `${safeHost}:${port}`;
}

function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function bump(el) {
  if (!el) return;
  el.classList.remove("bump");
  void el.offsetWidth;
  el.classList.add("bump");
}

function setValue(el, text, bumpEl = true) {
  if (!el) return;
  const next = String(text);
  if (el.textContent === next) return;

  if (bumpEl && !reducedMotion() && !el._counting) {
    const toMatch = next.match(/^(-?[\d,]+(?:\.\d+)?)(.*)$/);
    const fromMatch = (el.textContent || "").match(/^(-?[\d,]+(?:\.\d+)?)(.*)$/);
    if (toMatch && fromMatch && toMatch[2] === fromMatch[2]) {
      const to = parseFloat(toMatch[1].replace(/,/g, ""));
      const from = parseFloat(fromMatch[1].replace(/,/g, ""));
      const suffix = toMatch[2];
      const useComma = toMatch[1].includes(",");
      const decimals = (toMatch[1].split(".")[1] || "").length;
      if (isFinite(from) && isFinite(to) && from !== to) {
        el._counting = true;
        const start = performance.now();
        const dur = 500;
        const step = (now) => {
          const t = Math.min((now - start) / dur, 1);
          const eased = 1 - Math.pow(1 - t, 3);
          let val = from + (to - from) * eased;
          let s = decimals ? val.toFixed(decimals) : String(Math.round(val));
          if (useComma) s = Number(s).toLocaleString();
          el.textContent = s + suffix;
          if (t < 1) requestAnimationFrame(step);
          else { el.textContent = next; el._counting = false; }
        };
        requestAnimationFrame(step);
        bump(el);
        return;
      }
    }
  }

  el.textContent = next;
  if (bumpEl) bump(el);
}

function setBusy(btn, busy, busyLabel) {
  if (!btn) return;
  if (busy) {
    if (!btn.dataset.idleLabel) btn.dataset.idleLabel = btn.textContent.trim();
    if (busyLabel) btn.textContent = busyLabel;
    btn.classList.add("loading");
    btn.disabled = true;
  } else {
    btn.classList.remove("loading");
    btn.disabled = false;
    if (btn.dataset.idleLabel) btn.textContent = btn.dataset.idleLabel;
  }
}

/* -------------------------------------------------------------------------
   API layer
   ------------------------------------------------------------------------- */
function getHeaders() {
  const token = localStorage.getItem(AUTH_TOKEN_KEY);
  return { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function api(url, options = {}) {
  let res;
  try {
    const headers = { ...getHeaders(), ...(options.headers || {}) };
    if (typeof FormData !== "undefined" && options.body instanceof FormData) delete headers["Content-Type"];
    res = await fetch(url, {
      ...options,
      headers,
    });
  } catch (err) {
    throw new ApiError("Network error — cannot reach the control plane.", 0);
  }

  const text = await res.text();
  let data = {};
  if (text) {
    try { data = JSON.parse(text); } catch { data = { detail: text }; }
  }

  if (!res.ok) {
    let detail = data && (data.detail || data.message);
    if (typeof detail !== "string") detail = detail ? JSON.stringify(detail) : "";
    if (res.status === 401) detail = "Unauthorized — — check your API key.";
    throw new ApiError(detail || `Request failed (${res.status})`, res.status);
  }
  return data;
}

/* -------------------------------------------------------------------------
   Toasts
   ------------------------------------------------------------------------- */
const TOAST_ICONS = { success: icon("check"), error: icon("warning"), warning: icon("warning"), info: icon("info") };

function showToast(message, type = "info", { pinned = false, timeout = 5000 } = {}) {
  const container = document.getElementById("toastContainer");
  if (!container) return;
  while (container.children.length >= 5) container.firstElementChild.remove();

  const el = document.createElement("div");
  el.className = `toast ${type}${pinned ? " pinned" : ""}`;
  el.innerHTML = `
    <span class="toast-icon">${TOAST_ICONS[type] || TOAST_ICONS.info}</span>
    <div class="toast-body"><div class="toast-msg"></div></div>
    <button class="toast-close" data-action="close-toast" aria-label="Dismiss">${icon("x", "ico-sm")}</button>
    ${pinned ? "" : '<span class="toast-progress"></span>'}
  `;
  el.querySelector(".toast-msg").textContent = message;
  container.appendChild(el);
  if (!pinned) el._timer = setTimeout(() => dismissToast(el), timeout);
}

function dismissToast(el) {
  if (!el || el._closing) return;
  el._closing = true;
  clearTimeout(el._timer);
  el.classList.add("hide");
  setTimeout(() => el.remove(), 280);
}

/* -------------------------------------------------------------------------
   Operations activity feed (shared with a local fallback, capped)
   ------------------------------------------------------------------------- */
const ACTIVITY_KEY = "dbops_activity";
const ACTIVITY_MAX = 50;

const ACTIVITY_META = {
  add:      { icon: icon("plus"),     cls: "ok" },
  remove:   { icon: icon("trash"),    cls: "warn" },
  backup:   { icon: icon("snapshot"), cls: "accent" },
  ping:     { icon: icon("zap"),      cls: "ok" },
  verify:   { icon: icon("search"),   cls: "accent" },
  kill:     { icon: icon("x"),        cls: "err" },
  switch:   { icon: icon("swap"),     cls: "info" },
  schedule: { icon: icon("clock"),    cls: "accent" },
  copy:     { icon: icon("copy"),     cls: "info" },
  key:      { icon: icon("key"),      cls: "info" },
  live:     { icon: icon("pause"),    cls: "info" },
  theme:    { icon: icon("moon"),     cls: "accent" },
  restore:  { icon: icon("restore"),  cls: "accent" },
  notify:   { icon: icon("bell"),     cls: "info" },
  success:  { icon: icon("check"),    cls: "ok" },
  info:     { icon: icon("info"),     cls: "info" },
  warning:  { icon: icon("warning"),  cls: "warn" },
  error:    { icon: icon("x"),        cls: "err" },
  user:     { icon: icon("plus"),     cls: "info" },
  server:   { icon: icon("info"),     cls: "accent" },
  delete:   { icon: icon("trash"),    cls: "warn" },
};

let activityLog = [];
let activityExpanded = false;
let activityClearedAt = 0;
let activityLaneColors = [];
const ACTIVITY_VISIBLE_LIMIT = 8;
const ACTIVITY_GRAPH_COLORS = ["#58a6ff", "#3fb950", "#bc8cff", "#e3b341", "#f778ba", "#39c5cf", "#ff7b72", "#a5d6ff"];

function activityTypeLabel(kind) {
  return ({ add: "Target added", remove: "Target removed", backup: "Snapshot", ping: "Connection test", verify: "Snapshot check", kill: "Query stopped", switch: "Target switched", schedule: "Schedule", copy: "Copied", key: "Credentials", live: "Live refresh", theme: "Display", restore: "Restore", notify: "Notification", success: "Completed", info: "Information", warning: "Warning", error: "Error", auth: "Sign in", user: "User management", server: "Server management", delete: "Snapshot deleted" })[kind] || "Operation";
}

function loadActivity() {
  try { activityLog = JSON.parse(localStorage.getItem(ACTIVITY_KEY) || "[]") || []; }
  catch { activityLog = []; }
  if (!Array.isArray(activityLog)) activityLog = [];
}

function saveActivity() {
  try { localStorage.setItem(ACTIVITY_KEY, JSON.stringify(activityLog.slice(0, ACTIVITY_MAX))); }
  catch (e) { /* storage unavailable — keep in memory only */ }
}

function logActivity(kind, title, detail, dbId) {
  const item = {
    client_id: window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    kind, title, detail: detail || "", actor: currentUsername || authStoredUser() || "Unknown user",
    actorRole: currentRole || "user", db_id: dbId || null, ts: Date.now(),
  };
  activityLog.unshift(item);
  if (activityLog.length > ACTIVITY_MAX) activityLog.length = ACTIVITY_MAX;
  activityExpanded = false;
  saveActivity();
  renderActivity(true);
  api("/api/v1/auth/activity", { method: "POST", body: JSON.stringify(item) }).then((saved) => {
    if (!saved || !saved.client_id) { fetchActivity(); return; }
    const local = activityLog.find((entry) => entry.client_id === saved.client_id);
    if (!local) return;
    Object.assign(local, saved);
    activityLog.sort((a, b) => Number(b.ts) - Number(a.ts));
    saveActivity();
    renderActivity(false);
  }).catch(() => { /* Keep the browser copy when the activity service is temporarily unavailable. */ });
}

async function fetchActivity() {
  try {
    const qs = state.activityDbFilter ? `?db_id=${encodeURIComponent(state.activityDbFilter)}` : "";
    const result = await api(`/api/v1/auth/activity${qs}`);
    activityClearedAt = Number(result.cleared_at || 0);
    const serverEvents = Array.isArray(result.events) ? result.events : [];
    const serverIds = new Set(serverEvents.map((item) => item.client_id));
    const localOnly = activityLog.filter((item) => Number(item.ts) > activityClearedAt && (!item.client_id || !serverIds.has(item.client_id)));
    activityLog = [...serverEvents, ...localOnly].sort((a, b) => Number(b.ts) - Number(a.ts)).slice(0, ACTIVITY_MAX);
    saveActivity();
    renderActivity(false);
  } catch { /* Retain the locally saved feed if the shared activity service is offline. */ }
}

function relTime(ts) {
  const s = Math.floor((Date.now() - ts) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(ts).toLocaleDateString();
}

function activityActorKey(item) {
  return String(item.actor || "Unknown user").trim().toLocaleLowerCase();
}

function drawActivityGraph(laneColors) {
  const list = document.getElementById("activityList");
  const svg = document.getElementById("activityGraphSvg");
  if (!list || !svg) return;
  const rows = [...list.querySelectorAll(".timeline-item")];
  if (!rows.length) { svg.innerHTML = ""; svg.style.height = "0px"; return; }
  const laneCount = Math.max(1, laneColors.length);
  const width = Math.max(58, 14 + laneCount * 17);
  const xs = Array.from({ length: laneCount }, (_, lane) => 8 + lane * 17);
  const points = rows.map((row) => {
    const lane = Number(row.dataset.lane) || 0;
    return { x: xs[lane] || xs[0], y: row.offsetTop + 17, lane };
  });
  const height = Math.max(list.scrollHeight, rows.at(-1).offsetTop + rows.at(-1).offsetHeight);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.style.height = `${height}px`;
  svg.style.width = `${width}px`;
  const ranges = Array.from({ length: laneCount }, (_, lane) => points.flatMap((point, index) => point.lane === lane ? [index] : []));
  const rails = ranges.map((indexes, lane) => {
    if (!indexes.length) return "";
    const first = points[indexes[0]], last = points[indexes[indexes.length - 1]];
    const y1 = indexes.length === 1 ? first.y - 9 : first.y;
    const y2 = indexes.length === 1 ? last.y + 9 : last.y;
    return `<path class="activity-branch-rail" d="M ${first.x} ${y1} L ${last.x} ${y2}" stroke="${laneColors[lane]}"/>`;
  }).join("");
  const defs = points.slice(0, -1).map((point, index) => {
    const next = points[index + 1];
    return `<linearGradient id="activityLink${index}" gradientUnits="userSpaceOnUse" x1="${point.x}" y1="${point.y}" x2="${next.x}" y2="${next.y}"><stop stop-color="${laneColors[point.lane]}"/><stop offset="1" stop-color="${laneColors[next.lane]}"/></linearGradient>`;
  }).join("");
  const links = points.slice(0, -1).map((point, index) => {
    const next = points[index + 1];
    const dy = next.y - point.y;
    const d = point.x === next.x
      ? `M ${point.x} ${point.y} L ${next.x} ${next.y}`
      : `M ${point.x} ${point.y} C ${point.x} ${point.y + dy * .52}, ${next.x} ${next.y - dy * .52}, ${next.x} ${next.y}`;
    return `<path class="activity-branch-link" d="${d}" stroke="url(#activityLink${index})"/>`;
  }).join("");
  const nodes = points.map((point) => `<circle cx="${point.x}" cy="${point.y}" r="5" fill="${laneColors[point.lane]}"/>`).join("");
  svg.innerHTML = `<defs>${defs}</defs><g class="activity-branch-rails">${rails}</g><g class="activity-branch-links">${links}</g><g class="activity-branch-nodes">${nodes}</g>`;
}
window.addEventListener("resize", () => requestAnimationFrame(() => drawActivityGraph(activityLaneColors)));

function populateActivityDatabaseFilter() {
  const select = document.getElementById("activityDbFilter");
  if (!select) return;
  const currentValue = select.value;
  const options = state.databases
    .slice()
    .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")))
    .map((db) => `<option value="${escapeHtml(db.id)}">${escapeHtml(db.name || db.id)}</option>`);
  select.innerHTML = '<option value="">All databases</option>' + (options.length ? options.join("") : '<option value="" disabled>No databases registered</option>');
  // Restore previous selection if still available
  const option = select.querySelector(`option[value="${currentValue}"]`);
  if (option) select.value = currentValue;
  else select.value = "";
}

function renderActivity(animateFirst) {
  const list = document.getElementById("activityList");
  if (!list) return;

  const shownEvents = state.activityDbFilter
    ? activityLog.filter((item) => item.db_id === state.activityDbFilter)
    : activityLog;
  const today = new Date();
  const todayCount = shownEvents.filter((item) => {
    const eventDate = new Date(item.ts);
    return eventDate.getFullYear() === today.getFullYear() && eventDate.getMonth() === today.getMonth() && eventDate.getDate() === today.getDate();
  }).length;
  const todayEl = document.getElementById("activityTodayCount");
  const totalEl = document.getElementById("activityTotalCount");
  if (todayEl) todayEl.textContent = String(todayCount);
  if (totalEl) totalEl.textContent = String(shownEvents.length);
  const moreButton = document.getElementById("activityMoreBtn");
  if (moreButton) {
    moreButton.hidden = shownEvents.length <= ACTIVITY_VISIBLE_LIMIT;
    moreButton.textContent = activityExpanded ? "Show fewer events" : `Show all ${shownEvents.length} events`;
  }

  if (!shownEvents.length) {
    const legend = document.getElementById("activityBranchLegend");
    if (legend) legend.innerHTML = "";
    const emptyMessage = state.activityDbFilter
      ? "No activity recorded for this database yet."
      : "No activity recorded yet. Snapshots, pings, verifications and target switches will appear here.";
    list.innerHTML = `<div class="timeline-empty">${emptyMessage}</div>`;
    return;
  }

  const visibleItems = (activityExpanded ? shownEvents : shownEvents.slice(0, ACTIVITY_VISIBLE_LIMIT)).slice();
  const laneByActor = new Map();
  visibleItems.forEach((item) => { const key = activityActorKey(item); if (!laneByActor.has(key)) laneByActor.set(key, laneByActor.size); });
  const laneColors = [...laneByActor.keys()].map((_, lane) => ACTIVITY_GRAPH_COLORS[lane % ACTIVITY_GRAPH_COLORS.length]);
  list.style.setProperty("--activity-graph-width", `${Math.max(58, 14 + laneColors.length * 17)}px`);
  activityLaneColors = laneColors;
  const legend = document.getElementById("activityBranchLegend");
  if (legend) legend.innerHTML = [...laneByActor.entries()].map(([key, lane]) => {
    const actor = visibleItems.find((item) => activityActorKey(item) === key)?.actor || "Unknown user";
    return `<span class="activity-branch-key"><i style="--branch-color:${laneColors[lane]}"></i>${escapeHtml(actor)}</span>`;
  }).join("");
  list.innerHTML = visibleItems.map((item, idx) => {
    const meta = ACTIVITY_META[item.kind] || ACTIVITY_META.info;
    const typeLabel = activityTypeLabel(item.kind);
    const occurred = new Date(item.ts);
    const actor = item.actor || "Unknown user";
    const actorRole = item.actorRole || "legacy event";
    const lane = laneByActor.get(activityActorKey(item)) || 0;
    return `
      <div class="timeline-item${animateFirst && idx === 0 ? " new" : ""}" data-ts="${item.ts}" data-lane="${lane}" style="--branch-color:${laneColors[lane]}">
        <span class="timeline-graph-anchor" aria-hidden="true"></span>
        <div class="timeline-body">
          <div class="timeline-event-meta"><span class="timeline-event-label"><span class="timeline-step">${String(idx + 1).padStart(2, "0")}</span><span class="timeline-type ${meta.cls}">${typeLabel}</span></span><time class="timeline-time" title="${occurred.toLocaleString()}">${occurred.toLocaleString()} <span class="timeline-relative">· ${relTime(item.ts)}</span></time></div>
          <div class="timeline-title">${escapeHtml(item.title)}</div>
          <div class="timeline-actor"><span class="timeline-actor-avatar" style="--branch-color:${laneColors[lane]}">${escapeHtml(actor.slice(0, 1).toUpperCase())}</span><strong>${escapeHtml(actor)}</strong><span class="timeline-role">${escapeHtml(actorRole === "admin" ? "Administrator" : actorRole === "user" ? "User" : actorRole)}</span></div>
          ${item.detail ? `<div class="timeline-detail">${escapeHtml(item.detail)}</div>` : ""}
        </div>
      </div>`;
  }).join("");
  list.innerHTML = `<svg id="activityGraphSvg" class="activity-graph-svg" preserveAspectRatio="none" aria-hidden="true"></svg>${list.innerHTML}`;
  drawActivityGraph(laneColors);
}

function toggleActivityHistory() {
  activityExpanded = !activityExpanded;
  renderActivity(false);
}

function refreshActivityTimes() {
  document.querySelectorAll("#activityList .timeline-item").forEach((el) => {
    const ts = Number(el.dataset.ts);
    const timeEl = el.querySelector(".timeline-relative");
    if (ts && timeEl) timeEl.textContent = `· ${relTime(ts)}`;
  });
}

async function clearActivity() {
  if (currentRole !== "admin") return;
  let result;
  try { result = await api("/api/v1/auth/activity/clear", { method: "POST" }); }
  catch (err) { showToast(err.message, "error"); return; }
  activityClearedAt = Number(result.cleared_at || Date.now());
  activityLog = []; activityExpanded = false; saveActivity(); renderActivity(false);
  showToast("Activity log cleared.", "info");
}

/* -------------------------------------------------------------------------
   Modal helpers (native <dialog> with graceful fallback)
   ------------------------------------------------------------------------- */
function hasNativeDialog() {
  return typeof HTMLDialogElement !== "undefined" && typeof HTMLDialogElement.prototype.showModal === "function";
}

function openDialog(id) {
  const dialog = document.getElementById(id);
  if (!dialog) return;

  if (hasNativeDialog()) {
    try {
      if (!dialog.open) dialog.showModal();
      return;
    } catch (err) { /* fall through to fallback */ }
  }

  if (fallbackModals[id]) return;
  const overlay = document.createElement("div");
  overlay.className = "modal-fallback";
  const content = dialog.querySelector(".dialog-content");
  overlay.appendChild(content);
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) closeDialog(id); });
  document.body.appendChild(overlay);
  fallbackModals[id] = { overlay, dialog };
}

function closeDialog(id) {
  const dialog = document.getElementById(id);
  if (hasNativeDialog() && dialog && dialog.open && typeof dialog.close === "function") {
    dialog.close();
    return;
  }
  const fb = fallbackModals[id];
  if (fb) {
    const content = fb.overlay.querySelector(".dialog-content");
    if (content) fb.dialog.appendChild(content);
    fb.overlay.remove();
    delete fallbackModals[id];
  }
}

function confirmAction({ title = "Please confirm", message = "Are you sure?", confirmText = "Confirm" } = {}) {
  const titleEl = document.getElementById("confirmTitle");
  const msgEl = document.getElementById("confirmMessage");
  const yes = document.querySelector('[data-action="confirm-yes"]');
  if (titleEl) titleEl.textContent = title;
  if (msgEl) msgEl.textContent = message;
  if (yes) yes.textContent = confirmText;
  openDialog("confirmDialog");
  return new Promise((resolve) => { confirmResolver = resolve; });
}

function resolveConfirm(value) {
  closeDialog("confirmDialog");
  if (confirmResolver) { confirmResolver(value); confirmResolver = null; }
}

/* -------------------------------------------------------------------------
   Inline form status helpers
   ------------------------------------------------------------------------- */
function showFieldError(fieldId, message) {
  const input = document.getElementById(fieldId);
  const err = document.getElementById(`${fieldId}Error`);
  if (input) input.classList.add("invalid");
  if (err) { err.textContent = message; err.hidden = false; }
}

function clearFieldError(fieldId) {
  const input = document.getElementById(fieldId);
  const err = document.getElementById(`${fieldId}Error`);
  if (input) input.classList.remove("invalid");
  if (err) { err.textContent = ""; err.hidden = true; }
}

function showInlineStatus(message, kind) {
  const el = document.getElementById("addDbStatus");
  if (!el) return;
  el.hidden = false;
  el.className = `inline-status ${kind}`;
  const mark = kind === "ok" ? icon("check", "ico-sm") : kind === "err" ? icon("x", "ico-sm") : "";
  el.innerHTML = `${kind === "busy" ? '<span class="spinner"></span>' : `<span class="status-mark">${mark}</span>`}<span></span>`;
  el.lastElementChild.textContent = message;
}

function hideInlineStatus() {
  const el = document.getElementById("addDbStatus");
  if (el) el.hidden = true;
}

/* Generic inline status writer for the restore + notification dialogs */
function setStatusEl(el, message, kind) {
  if (!el) return;
  el.hidden = false;
  el.className = `inline-status ${kind}`;
  const mark = kind === "ok" ? icon("check", "ico-sm") : kind === "err" ? icon("x", "ico-sm") : "";
  el.innerHTML = `${kind === "busy" ? '<span class="spinner"></span>' : `<span class="status-mark">${mark}</span>`}<span></span>`;
  el.lastElementChild.textContent = message;
}

function hideStatusEl(el) {
  if (el) el.hidden = true;
}

/* -------------------------------------------------------------------------
   Chart primitives — polar maths and SVG helpers shared by every graph
   ------------------------------------------------------------------------- */
function polarPoint(cx, cy, radius, degrees) {
  const rad = ((degrees - 90) * Math.PI) / 180;
  return [cx + radius * Math.cos(rad), cy + radius * Math.sin(rad)];
}

function annularSector(cx, cy, inner, outer, startDeg, endDeg) {
  const large = endDeg - startDeg > 180 ? 1 : 0;
  const [ax, ay] = polarPoint(cx, cy, outer, startDeg);
  const [bx, by] = polarPoint(cx, cy, outer, endDeg);
  const [cx2, cy2] = polarPoint(cx, cy, inner, endDeg);
  const [dx, dy] = polarPoint(cx, cy, inner, startDeg);
  const n = (v) => Number(v).toFixed(2);
  return `M ${n(ax)} ${n(ay)} A ${outer} ${outer} 0 ${large} 1 ${n(bx)} ${n(by)} L ${n(cx2)} ${n(cy2)} A ${inner} ${inner} 0 ${large} 0 ${n(dx)} ${n(dy)} Z`;
}

function buildSparkline(values, { width = 96, height = 26, stroke = "var(--accent)", fillId = "sparkFill" } = {}) {
  const samples = values.filter((v) => Number.isFinite(Number(v)));
  if (samples.length < 2) return "";
  const min = Math.min(...samples);
  const max = Math.max(...samples);
  const range = Math.max(1, max - min);
  const pad = 2.5;
  const step = (width - pad * 2) / (samples.length - 1);
  const points = samples.map((value, index) => [
    pad + index * step,
    height - pad - ((Number(value) - min) / range) * (height - pad * 2),
  ]);
  const line = points.map((p, i) => `${i ? "L" : "M"} ${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const area = `${line} L ${points[points.length - 1][0].toFixed(1)} ${height} L ${points[0][0].toFixed(1)} ${height} Z`;
  const last = points[points.length - 1];
  let pathLength = 0;
  for (let i = 1; i < points.length; i += 1) {
    pathLength += Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
  }
  return `<svg class="viz-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
    <defs>
      <linearGradient id="${fillId}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" style="stop-color:${stroke};stop-opacity:.42" />
        <stop offset="100%" style="stop-color:${stroke};stop-opacity:0" />
      </linearGradient>
    </defs>
    <path class="spark-area" d="${area}" fill="url(#${fillId})" />
    <path class="spark-line viz-draw" style="--dash:${Math.max(24, Math.round(pathLength))};stroke:${stroke}" d="${line}" vector-effect="non-scaling-stroke" />
    <circle class="spark-dot" cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="2.4" style="fill:${stroke}" />
  </svg>`;
}

/* -------------------------------------------------------------------------
   Metric-specific visualizations with hover details
   ------------------------------------------------------------------------- */
function updateMetricVisualizations() {
  renderCacheVisualization();
  renderSizeVisualization();
  renderSlowQueryVisualization();
}

/* Live latency sparkline for the system status bar */
function renderLatencySparkline() {
  const el = document.getElementById("latencySparkline");
  if (!el) return;
  const values = (state.latencyHistory || []).slice(-14);
  if (values.length < 2) {
    el.innerHTML = "";
    el.removeAttribute("role");
    el.removeAttribute("aria-label");
    el.removeAttribute("title");
    return;
  }
  el.innerHTML = buildSparkline(values, { width: 96, height: 26, stroke: "var(--accent)", fillId: "latencySparkFill" });
  const label = `Recent connection latency samples: ${values.map((v) => `${v} ms`).join(", ")}`;
  el.title = label;
  el.setAttribute("role", "img");
  el.setAttribute("aria-label", label);
}

/* Fleet reachability donut shown beside the target selector health bar */
function renderTargetHealthDonut(counts, total) {
  const el = document.getElementById("targetHealthDonut");
  if (!el) return;
  if (!total) {
    el.innerHTML = "";
    el.removeAttribute("role");
    el.removeAttribute("aria-label");
    return;
  }

  const segments = [
    { key: "reachable", value: counts.reachable, color: "var(--ok)" },
    { key: "unreachable", value: counts.unreachable, color: "var(--bad)" },
    { key: "pending", value: counts.pending, color: "var(--text-faint)" },
  ];
  const circumference = 2 * Math.PI * 18;
  let cursor = 0;
  const arcs = segments.map((segment) => {
    const fraction = segment.value / total;
    const length = fraction * circumference;
    const offset = -cursor * circumference;
    cursor += fraction;
    if (segment.value <= 0) return "";
    return `<circle class="fleet-donut-seg" cx="24" cy="24" r="18" pathLength="100"
      style="stroke:${segment.color};--seg-dash:${(fraction * 100).toFixed(3)} 100;--seg-offset:${(offset * 100 / circumference).toFixed(3)}"
      stroke-dashoffset="${(offset * 100 / circumference).toFixed(3)}"
      transform="rotate(-90 24 24)" />`;
  }).join("");

  const reachablePct = Math.round((counts.reachable / total) * 100);
  el.innerHTML = `<svg viewBox="0 0 48 48" aria-hidden="true">
      <circle class="fleet-donut-track" cx="24" cy="24" r="18" />
      ${arcs}
    </svg>
    <span class="fleet-donut-core">${reachablePct}%</span>`;
  el.setAttribute("role", "img");
  el.setAttribute("aria-label", `${counts.reachable} reachable, ${counts.unreachable} unreachable, ${counts.pending} pending of ${total} targets`);
}

/* Snapshot size trend for the backups section header */
function renderBackupSizeTrend() {
  const el = document.getElementById("backupSizeTrend");
  if (!el) return;
  const panel = document.getElementById("backupTrendPanel");
  const counter = document.getElementById("backupTrendCount");
  const items = (state.cachedBackups || [])
    .filter((b) => b && b.status === "completed" && Number(b.size_bytes) > 0)
    .slice(0, 14)
    .reverse();
  if (items.length < 2) {
    el.hidden = true;
    el.innerHTML = "";
    if (counter) counter.textContent = "last 0";
    if (panel) panel.hidden = true;
    return;
  }
  el.hidden = false;
  if (panel) panel.hidden = false;
  if (counter) counter.textContent = `last ${items.length}`;

  const sizes = items.map((b) => Number(b.size_bytes) || 0);
  const max = Math.max(...sizes);
  const min = Math.min(...sizes);
  const avg = sizes.reduce((total, value) => total + value, 0) / sizes.length;
  const range = Math.max(1, max - min);

  const W = 560;
  const H = 210;
  const padL = 58;
  const padR = 20;
  const padT = 20;
  const padB = 34;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const baseY = padT + innerH;
  const step = items.length > 1 ? innerW / (items.length - 1) : innerW;
  const points = sizes.map((value, index) => [
    padL + index * step,
    baseY - ((value - min) / range) * innerH,
  ]);

  const line = points.map((p, i) => `${i ? "L" : "M"} ${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const area = `${line} L ${points[points.length - 1][0].toFixed(1)} ${baseY.toFixed(1)} L ${points[0][0].toFixed(1)} ${baseY.toFixed(1)} Z`;
  let pathLength = 0;
  for (let i = 1; i < points.length; i += 1) {
    pathLength += Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
  }

  const grid = [0, 0.25, 0.5, 0.75, 1].map((factor) => {
    const gy = (baseY - factor * innerH).toFixed(1);
    return `<line class="viz-grid-line" x1="${padL}" y1="${gy}" x2="${(W - padR).toFixed(1)}" y2="${gy}" />
      <text class="viz-axis-label" x="${padL - 9}" y="${gy}" text-anchor="end" dominant-baseline="middle">${escapeHtml(formatServerBytes(min + factor * range))}</text>`;
  }).join("");

  const shortDate = (value) => formatSriLankaBackupShortDate(value);
  const dots = items.map((b, index) => {
    const [px, py] = points[index];
    const isLast = index === points.length - 1;
    const label = b.size_human || formatServerBytes(sizes[index]);
    const when = shortDate(b.created_at);
    return `<g class="trend-point${isLast ? " is-latest" : ""}" tabindex="0">
      <circle class="trend-point-hit" cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="10" />
      ${isLast ? `<circle class="trend-pulse" cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="4.4" />` : ""}
      <circle class="trend-point-dot" cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="${isLast ? 4.4 : 3.2}" style="animation-delay:${(0.5 + index * 0.05).toFixed(2)}s" />
      <title>${escapeHtml(b.filename || b.backup_id || "Snapshot")} · ${escapeHtml(label)} · ${escapeHtml(when)}</title>
    </g>`;
  }).join("");

  const labelIndexes = items.length <= 4
    ? items.map((_, index) => index)
    : [...new Set([0, Math.floor((items.length - 1) / 2), items.length - 1])];
  const xLabels = labelIndexes.map((index) => {
    const anchor = index === 0 ? "start" : index === items.length - 1 ? "end" : "middle";
    return `<text class="viz-axis-label" x="${points[index][0].toFixed(1)}" y="${(baseY + 15).toFixed(1)}" text-anchor="${anchor}">${escapeHtml(shortDate(items[index].created_at) || "--")}</text>`;
  }).join("");

  const peakIndex = sizes.indexOf(max);
  const [peakX, peakY] = points[peakIndex];
  const peakAnchor = peakIndex === 0 ? "start" : peakIndex === items.length - 1 ? "end" : "middle";
  const peakLabel = `<text class="viz-value-label" x="${peakX.toFixed(1)}" y="${Math.max(padT + 6, peakY - 11).toFixed(1)}" text-anchor="${peakAnchor}">${escapeHtml(items[peakIndex].size_human || formatServerBytes(max))}</text>`;

  el.innerHTML = `<svg class="viz-svg backup-trend-chart" viewBox="0 0 ${W} ${H}" role="img"
           aria-label="Snapshot size trend across the last ${items.length} completed backups, ranging from ${escapeHtml(formatServerBytes(min))} to ${escapeHtml(formatServerBytes(max))}">
        <defs>
          <linearGradient id="backupTrendFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" style="stop-color:var(--accent);stop-opacity:.45" />
            <stop offset="100%" style="stop-color:var(--accent);stop-opacity:0" />
          </linearGradient>
        </defs>
        ${grid}
        <path class="trend-area" d="${area}" fill="url(#backupTrendFill)" />
        <path class="trend-line viz-draw" style="--dash:${Math.max(40, Math.round(pathLength))}" d="${line}" />
        ${dots}
        ${peakLabel}
        ${xLabels}
      </svg>
      <div class="backup-trend-stats">
        <span><small>Smallest</small><strong>${escapeHtml(formatServerBytes(min))}</strong></span>
        <span><small>Average</small><strong>${escapeHtml(formatServerBytes(avg))}</strong></span>
        <span><small>Peak</small><strong>${escapeHtml(formatServerBytes(max))}</strong></span>
      </div>`;
}

function renderCacheVisualization() {
  const el = document.getElementById("cacheVisualization");
  if (!el) return;
  const data = state.cacheMetric;
  if (!data) { el.innerHTML = `<div class="viz-empty">Waiting for cache metrics…</div>`; return; }
  const ratio = Math.max(0, Math.min(100, Number(data.buffer_cache_hit_ratio) || 0));
  const threshold = Number(data.threshold_percent) || 95;
  const hits = Number(data.blocks_hit) || 0;
  const reads = Number(data.blocks_read) || 0;
  const color = ratio >= threshold ? "var(--ok)" : "var(--warn)";
  el.innerHTML = `<div class="cache-visualization">
    <div class="cache-scale-head"><span>Cache hit rate</span><strong>${ratio.toFixed(2)}%</strong></div>
    <div class="cache-scale-labels" style="--cache-target:${Math.min(100, Math.max(0, threshold))}%"><span>0%</span><span>${threshold}% target</span><span>100%</span></div>
    <div class="cache-scale-track" role="img" tabindex="0" aria-label="Buffer cache hit ratio ${ratio.toFixed(2)} percent; target ${threshold} percent" title="${hits.toLocaleString()} shared-buffer hits and ${reads.toLocaleString()} read requests since statistics reset">
      <i class="cache-scale-fill" style="--cache-ratio:${ratio}%;--cache-color:${color}"></i>
      <b class="cache-scale-target" style="--cache-target:${Math.min(100, Math.max(0, threshold))}%"></b>
      <b class="cache-scale-point" style="--cache-ratio:${ratio}%;--cache-color:${color}"></b>
    </div>
    <div class="cache-counts"><span><i class="cache-count-dot hit"></i>Shared-buffer hits <strong>${hits.toLocaleString()}</strong></span><span><i class="cache-count-dot read"></i>Read requests <strong>${reads.toLocaleString()}</strong></span></div>
    <small class="cache-scale-note">Cumulative counts since PostgreSQL statistics were reset</small>
  </div>`;
}

function renderSizeVisualization() {
  const el = document.getElementById("sizeVisualization");
  if (!el) return;
  const data = state.sizeMetric;
  if (!data) { el.innerHTML = `<div class="viz-empty">Waiting for storage metrics…</div>`; return; }
  const relations = (data.tables || []).slice().sort((a, b) => Number(b.total_bytes) - Number(a.total_bytes)).slice(0, 5);
  if (!relations.length) { el.innerHTML = `<div class="viz-empty">No relation breakdown is available for this target.</div>`; return; }
  const largest = Math.max(...relations.map((row) => Number(row.total_bytes) || 0), 1);
  el.innerHTML = `<div class="relation-chart-caption">Largest ${relations.length} relation${relations.length === 1 ? "" : "s"} · bar length compares to the largest</div>
    <div class="relation-stack-legend">
      <span><i style="--legend-color:linear-gradient(135deg,#818cf8,#38bdf8)"></i>Table data</span>
      <span><i style="--legend-color:linear-gradient(135deg,rgba(155,123,255,.9),rgba(56,189,248,.5))"></i>Index data</span>
    </div>
    <div class="relation-bars">${relations.map((row) => {
      const bytes = Number(row.total_bytes) || 0;
      const tableBytes = Number(row.table_bytes) || 0;
      const pct = Math.max(bytes > 0 ? 2 : 0, (bytes / largest) * 100);
      const tableShare = bytes > 0 ? (tableBytes / bytes) * 100 : 100;
      const indexShare = bytes > 0 ? Math.max(0, 100 - tableShare) : 0;
      const name = `${row.schema_name ? `${row.schema_name}.` : ""}${row.table_name || "Relation"}`;
      return `<div class="relation-bar-row" tabindex="0" title="${escapeHtml(name)} · ${escapeHtml(row.total_human || formatServerBytes(bytes))} total, ${escapeHtml(row.table_human || "table data")} table data, ${escapeHtml(row.index_human || "index data")} indexes">
        <span class="relation-name">${escapeHtml(row.table_name || "Relation")}</span>
        <span class="relation-scale">
          <span class="relation-stack-track" style="--bar-width:${pct.toFixed(2)}%">
            <i class="seg-table" style="--seg-table:${tableShare.toFixed(2)}%"></i>
            <i class="seg-index" style="--seg-table:${tableShare.toFixed(2)}%;--seg-index:${indexShare.toFixed(2)}%"></i>
          </span>
        </span>
        <strong>${escapeHtml(row.total_human || formatServerBytes(bytes))}</strong>
      </div>`;
    }).join("")}</div>`;
}

function renderSlowQueryVisualization() {
  const el = document.getElementById("slowQueryVisualization");
  if (!el) return;
  const data = state.slowMetric;
  if (!data) { el.innerHTML = `<div class="viz-empty">Waiting for query activity…</div>`; return; }
  const queries = (data.slow_queries || []).slice().sort((a, b) => Number(b.duration_seconds) - Number(a.duration_seconds)).slice(0, 4);
  const events = state.deadlockMetric || {};
  const eventStats = `<div class="slow-event-stats">
    <span title="Deadlocks counted since PostgreSQL statistics were reset">Deadlocks <b>${Number(events.deadlocks_count) || 0}</b></span>
    <span title="Transactions rolled back since statistics were reset">Rollbacks <b>${Number(events.rollbacks_count) || 0}</b></span>
    <span title="Database conflicts counted since statistics were reset">Conflicts <b>${Number(events.conflicts_count) || 0}</b></span>
  </div>`;
  if (!queries.length) {
    el.innerHTML = `<div class="slow-empty"><span aria-hidden="true">✓</span><div><strong>No active slow queries</strong><small>Nothing is currently above the ${Number(data.threshold_seconds) || 0}s threshold.</small></div></div>${eventStats}`;
    return;
  }
  const threshold = Number(data.threshold_seconds) || 1;
  const maxDuration = Math.max(threshold, ...queries.map((query) => Number(query.duration_seconds) || 0));

  const cx = 70;
  const cy = 66;
  const inner = 17;
  const outer = 58;
  const span = outer - inner;
  const slice = 360 / queries.length;
  const colors = ["#fbbf24", "#f97316", "#f0574d", "#e879f9"];
  const thresholdRadius = inner + Math.min(1, threshold / maxDuration) * span;

  const rings = [0.25, 0.5, 0.75, 1].map((factor) => {
    const r = inner + factor * span;
    return `<circle class="polar-grid" cx="${cx}" cy="${cy}" r="${r.toFixed(2)}" />`;
  }).join("");

  const petals = queries.map((query, index) => {
    const duration = Number(query.duration_seconds) || 0;
    const ratio = Math.max(0.05, Math.min(1, duration / maxDuration));
    const radius = inner + ratio * span;
    const start = index * slice + slice * 0.14;
    const end = (index + 1) * slice - slice * 0.14;
    const [labelX, labelY] = polarPoint(cx, cy, inner + (radius - inner) / 2, (start + end) / 2);
    const queryText = String(query.query || "").replace(/\s+/g, " ").trim();
    const color = colors[index % colors.length];
    return `<path class="polar-petal" d="${annularSector(cx, cy, inner, radius, start, end)}" fill="${color}" fill-opacity=".72" stroke="${color}" stroke-width="1" style="animation-delay:${(index * 0.08).toFixed(2)}s"><title>PID ${escapeHtml(query.pid)} · ${duration.toFixed(2)}s · ${escapeHtml(queryText)}</title></path>
      <text class="viz-value-label" x="${labelX.toFixed(2)}" y="${labelY.toFixed(2)}" text-anchor="middle" dominant-baseline="middle">${duration.toFixed(1)}s</text>`;
  }).join("");

  const legend = queries.map((query, index) => {
    const duration = Number(query.duration_seconds) || 0;
    const color = colors[index % colors.length];
    return `<span><i style="--legend-color:${color}"></i>PID ${escapeHtml(query.pid)} <b>${duration.toFixed(1)}s</b></span>`;
  }).join("");

  el.innerHTML = `<div class="slow-chart-caption">Longest-running active queries <span>threshold ${threshold}s</span></div>
    <div class="polar-wrap">
      <svg class="polar-chart viz-svg" viewBox="0 0 140 132" role="img" aria-label="Radial chart of the longest running queries against the ${threshold} second threshold">
        ${rings}
        <circle class="polar-threshold" cx="${cx}" cy="${cy}" r="${thresholdRadius.toFixed(2)}" />
        ${petals}
        <circle class="polar-center-dot" cx="${cx}" cy="${cy}" r="2.6" />
      </svg>
      <div class="polar-legend">${legend}</div>
    </div>
    ${eventStats}`;
}

/* -------------------------------------------------------------------------
   Micro-interactions: pointer glow, ripple, reveal, scroll-spy
   ------------------------------------------------------------------------- */
function setupMouseGlow() {
  document.querySelectorAll(".interactive-card").forEach((card) => {
    card.addEventListener("mousemove", (e) => {
      const rect = card.getBoundingClientRect();
      card.style.setProperty("--mouse-x", `${e.clientX - rect.left}px`);
      card.style.setProperty("--mouse-y", `${e.clientY - rect.top}px`);
    });
  });
}

/* -------------------------------------------------------------------------
   Ambient background scene: constellation and parallax
   ------------------------------------------------------------------------- */
function setupBackground() {
  setupParallax();
  setupConstellation();
}

function setupParallax() {
  if (reducedMotion() || !window.matchMedia("(pointer: fine)").matches) return;
  const root = document.documentElement;
  let raf = null;
  window.addEventListener("mousemove", (e) => {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = null;
      root.style.setProperty("--par-x", ((e.clientX / window.innerWidth - 0.5) * 2).toFixed(3));
      root.style.setProperty("--par-y", ((e.clientY / window.innerHeight - 0.5) * 2).toFixed(3));
    });
  }, { passive: true });
}

function setupConstellation() {
  const canvasId = document.documentElement.dataset.auth === "locked" ? "authBgCanvas" : "bgCanvas";
  const canvas = document.getElementById(canvasId);
  if (!canvas || reducedMotion()) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  let w = 0;
  let h = 0;
  let dpr = 1;
  let particles = [];
  let raf = null;
  let running = true;
  let frame = 0;
  let color = "91, 140, 255";
  const pointer = { x: -9999, y: -9999, tx: -9999, ty: -9999, on: false };

  const readColor = () => {
    const v = (getComputedStyle(document.documentElement).getPropertyValue("--accent") || "").trim().replace("#", "");
    if (v.length === 6) {
      color = `${parseInt(v.slice(0, 2), 16)}, ${parseInt(v.slice(2, 4), 16)}, ${parseInt(v.slice(4, 6), 16)}`;
    }
  };

  const density = () => Math.max(32, Math.min(120, Math.round((w * h) / 12000)));

  const build = () => {
    particles = Array.from({ length: density() }, () => ({
      x: Math.random() * w,
      y: Math.random() * h,
      vx: (Math.random() - 0.5) * 0.22,
      vy: (Math.random() - 0.5) * 0.22,
      r: Math.random() * 1.4 + 0.6,
    }));
  };

  const resize = () => {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = canvas.clientWidth || window.innerWidth;
    h = canvas.clientHeight || window.innerHeight;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    build();
  };

  const draw = () => {
    raf = null;
    if (!running) return;
    if (++frame % 90 === 0) readColor();

    ctx.clearRect(0, 0, w, h);
    pointer.x += (pointer.tx - pointer.x) * 0.2;
    pointer.y += (pointer.ty - pointer.y) * 0.2;

    const maxLink = w < 720 ? 96 : 132;
    for (const p of particles) {
      p.x += p.vx; p.y += p.vy;
      if (p.x < -20) p.x = w + 20; else if (p.x > w + 20) p.x = -20;
      if (p.y < -20) p.y = h + 20; else if (p.y > h + 20) p.y = -20;
    }

    ctx.lineWidth = 1;
    for (let i = 0; i < particles.length; i++) {
      const a = particles[i];
      for (let j = i + 1; j < particles.length; j++) {
        const b = particles[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < maxLink * maxLink) {
          ctx.strokeStyle = `rgba(${color}, ${(1 - Math.sqrt(d2) / maxLink) * 0.16})`;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
      if (pointer.on) {
        const dx = a.x - pointer.x;
        const dy = a.y - pointer.y;
        const d2 = dx * dx + dy * dy;
        const reach = maxLink * 1.15;
        if (d2 < reach * reach) {
          ctx.strokeStyle = `rgba(${color}, ${(1 - Math.sqrt(d2) / reach) * 0.3})`;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(pointer.x, pointer.y);
          ctx.stroke();
        }
      }
    }

    ctx.fillStyle = `rgba(${color}, 0.5)`;
    for (const p of particles) {
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
    }

    raf = requestAnimationFrame(draw);
  };

  const onMove = (e) => { pointer.tx = e.clientX; pointer.ty = e.clientY; pointer.on = true; };
  const onLeave = () => { pointer.tx = -9999; pointer.ty = -9999; pointer.on = false; };
  const onVisibility = () => {
    if (document.hidden) {
      running = false;
      if (raf) { cancelAnimationFrame(raf); raf = null; }
    } else if (!raf) {
      running = true;
      raf = requestAnimationFrame(draw);
    }
  };

  let rt = null;
  const onResize = () => { clearTimeout(rt); rt = setTimeout(resize, 150); };

  readColor();
  resize();
  if (w && h) raf = requestAnimationFrame(draw);

  window.addEventListener("resize", onResize);
  window.addEventListener("mousemove", onMove, { passive: true });
  window.addEventListener("mouseleave", onLeave);
  document.addEventListener("visibilitychange", onVisibility);

  window.__bgRefreshColor = readColor;
}

function setupRipple() {
  if (reducedMotion()) return;
  document.addEventListener("pointerdown", (e) => {
    const btn = e.target.closest(".btn");
    if (!btn || btn.disabled) return;
    const rect = btn.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height);
    const span = document.createElement("span");
    span.className = "ripple";
    span.style.width = span.style.height = `${size}px`;
    span.style.left = `${e.clientX - rect.left - size / 2}px`;
    span.style.top = `${e.clientY - rect.top - size / 2}px`;
    btn.appendChild(span);
    span.addEventListener("animationend", () => span.remove());
  });
}

function setupScrollProgress() {
  const bar = document.getElementById("scrollProgress");
  if (!bar) return;
  let raf = null;
  const update = () => {
    raf = null;
    const doc = document.documentElement;
    const max = doc.scrollHeight - doc.clientHeight;
    const p = max > 0 ? doc.scrollTop / max : 0;
    bar.style.transform = `scaleX(${Math.max(0, Math.min(1, p)).toFixed(4)})`;
  };
  window.addEventListener("scroll", () => { if (!raf) raf = requestAnimationFrame(update); }, { passive: true });
  window.addEventListener("resize", update, { passive: true });
  update();
}

function setupFooterBeam() {
  const footer = document.getElementById("siteFooter");
  if (!footer) return;

  const yearEl = document.getElementById("footerYear");
  if (yearEl) yearEl.textContent = String(new Date().getFullYear());

}

function setupReveal() {
  const els = Array.from(document.querySelectorAll(".reveal"));
  if (!els.length) return;
  if (!("IntersectionObserver" in window) || reducedMotion()) {
    els.forEach((e) => e.classList.add("in"));
    return;
  }
  const io = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
    });
  }, { threshold: 0.08, rootMargin: "0px 0px -40px 0px" });
  els.forEach((e) => io.observe(e));
}

function setupScrollSpy() {
  const links = Array.from(document.querySelectorAll(".nav-link"));
  if (!links.length) return;
  const currentPath = window.location.pathname.replace(/\/$/, "") || "/console";
  links.forEach((link) => {
    const linkPath = new URL(link.href, window.location.origin).pathname.replace(/\/$/, "") || "/console";
    const active = linkPath === currentPath || (currentPath === "/" && linkPath === "/console");
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

function setupNavTransitions() {
  const links = Array.from(document.querySelectorAll(".nav-link"));
  const sections = Array.from(document.querySelectorAll("main section[id]"));
  if (!links.length) return;
  if (reducedMotion()) {
    links.forEach((link) => {
      link.addEventListener("click", (e) => {
        const url = new URL(link.href, window.location.origin);
        const targetHash = url.hash;
        if (!targetHash) return;
        const target = document.querySelector(targetHash);
        if (!target) return;
        e.preventDefault();
        window.history.replaceState(null, "", targetHash);
        links.forEach((l) => {
          const h = new URL(l.href, window.location.origin).hash || l.getAttribute("href");
          l.classList.toggle("active", h === targetHash);
          if (h === targetHash) l.setAttribute("aria-current", "page");
          else l.removeAttribute("aria-current");
        });
        target.scrollIntoView({ behavior: "auto", block: "start" });
      });
    });
    return;
  }

  const setActive = (hash) => {
    links.forEach((l) => {
      const h = new URL(l.href, window.location.origin).hash || l.getAttribute("href");
      l.classList.toggle("active", h === hash);
      if (h === hash) l.setAttribute("aria-current", "page");
      else l.removeAttribute("aria-current");
    });
  };

  let switching = false;
  links.forEach((link) => {
    link.addEventListener("click", (e) => {
      const url = new URL(link.href, window.location.origin);
      const targetHash = url.hash;
      if (!targetHash) return;
      const target = document.querySelector(targetHash);
      if (!target || switching) return;
      e.preventDefault();
      if (window.location.hash === targetHash) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }
      switching = true;
      document.documentElement.classList.add("nav-switching");
      sections.forEach((s) => {
        s.classList.remove("nav-fade-in");
        s.classList.add("nav-fade-out");
      });
      setTimeout(() => {
        window.history.replaceState(null, "", targetHash);
        setActive(targetHash);
        target.scrollIntoView({ behavior: "auto", block: "start" });
        sections.forEach((s) => {
          s.classList.remove("nav-fade-out");
        });
        requestAnimationFrame(() => {
          sections.forEach((s) => s.classList.add("nav-fade-in"));
        });
        setTimeout(() => {
          sections.forEach((s) => s.classList.remove("nav-fade-in"));
          document.documentElement.classList.remove("nav-switching");
          switching = false;
        }, 380);
      }, 200);
    });
  });

  window.addEventListener("hashchange", () => {
    const h = window.location.hash || "#command-center";
    setActive(h);
  });
}

function setupPageTransitions() {
  const mainEl = document.querySelector("main");
  if (mainEl) {
    requestAnimationFrame(() => {
      mainEl.classList.add("is-entering");
    });
  }
}

/* -------------------------------------------------------------------------
   Command palette
   ------------------------------------------------------------------------- */
function getCommands() {
  const base = [
    { label: "Add Database Target", hint: "Register and monitor a new database", icon: icon("plus"), run: () => openAddDbDialog() },
    { label: "Refresh Dashboard", hint: "Reload every metric now", icon: icon("refresh"), run: () => refreshDashboard() },
    { label: "Ping Active Target", hint: "Non-invasive connectivity test", icon: icon("zap"), run: () => quickPingActiveDb() },
    { label: "Take Snapshot", hint: "Create a PostgreSQL backup", icon: icon("snapshot"), run: () => openCreateBackupDialog() },
    { label: "Backup Schedule", hint: "Configure the cron schedule", icon: icon("clock"), run: () => openScheduleDialog() },
    { label: "Restore a Snapshot", hint: "Disaster recovery — overwrite or clone", icon: icon("restore"), run: () => openRestoreDialog() },
    { label: "Telegram Notifications", hint: "Configure alert delivery", icon: icon("bell"), run: () => openNotificationsDialog() },
    { label: "Toggle Live Refresh", hint: "Pause or resume auto refresh", icon: icon("pause"), run: () => toggleAutoRefresh() },
    { label: "Toggle Light / Dark Theme", hint: "Header theme button", icon: icon("moon"), run: () => toggleTheme() },
    ...(currentRole === "admin" ? [{ label: "Clear Activity Log", hint: "Remove the local operations history", icon: icon("trash"), run: () => clearActivity() }, { label: "Manage Users", hint: "Create or remove operator accounts", icon: icon("plus"), run: () => openUserManagement() }] : []),
  ];
  const dbs = state.databases.map((db) => {
    const meta = engineMeta(db.engine);
    return { label: db.name, hint: `${meta.label} · switch target`, icon: engineBadge(db.engine), tag: "DB", run: () => selectDatabase(db.id) };
  });
  return [...base, ...dbs];
}

function buildCommandList(query) {
  const list = document.getElementById("commandList");
  if (!list) return;
  const q = String(query || "").trim().toLowerCase();
  const items = getCommands().filter((it) => !q || `${it.label} ${it.hint}`.toLowerCase().includes(q));
  commandState.items = items;
  commandState.index = 0;

  if (!items.length) {
    list.innerHTML = `<div class="command-empty">No matching commands or targets.</div>`;
    return;
  }
  list.innerHTML = items.map((it, idx) => `
    <button class="command-item ${idx === 0 ? "active" : ""}" type="button" role="option"
      data-action="command-run" data-index="${idx}" aria-selected="${idx === 0}">
      <span class="command-item-ico">${it.icon}</span>
      <span class="command-item-text">
        <span class="command-item-label">${escapeHtml(it.label)}</span>
        <span class="command-item-hint">${escapeHtml(it.hint)}</span>
      </span>
      ${it.tag ? `<span class="command-item-tag">${escapeHtml(it.tag)}</span>` : ""}
    </button>`).join("");
}

function openCommandPalette() {
  const input = document.getElementById("commandInput");
  if (input) input.value = "";
  buildCommandList("");
  openDialog("commandDialog");
  if (input) setTimeout(() => input.focus(), 40);
}

function moveCommand(delta) {
  const list = document.getElementById("commandList");
  if (!list || !commandState.items.length) return;
  commandState.index = (commandState.index + delta + commandState.items.length) % commandState.items.length;
  list.querySelectorAll(".command-item").forEach((el, i) => {
    const active = i === commandState.index;
    el.classList.toggle("active", active);
    el.setAttribute("aria-selected", String(active));
    if (active) el.scrollIntoView({ block: "nearest" });
  });
}

function runCommand(index) {
  const it = commandState.items[index];
  if (!it) return;
  closeDialog("commandDialog");
  setTimeout(() => { try { it.run(); } catch (e) { console.error("[DBOps] command failed:", e); } }, 70);
}

/* -------------------------------------------------------------------------
   Engine auto-detection
   ------------------------------------------------------------------------- */
function autoDetectEngine(url) {
  const clean = String(url || "").trim().toLowerCase();
  const select = document.getElementById("newDbEngine");
  if (!select || !clean) return;
  if (clean.startsWith("mongodb://") || clean.startsWith("mongodb+srv://")) select.value = "mongodb";
  else if (clean.startsWith("mysql://")) select.value = "mysql";
  else if (clean.startsWith("redis://") || clean.startsWith("rediss://")) select.value = "redis";
  else if (clean.startsWith("postgresql://") || clean.startsWith("postgres://")) select.value = "postgres";
}

/* -------------------------------------------------------------------------
   Database registry & switcher
   ------------------------------------------------------------------------- */
function renderTargetHealthSummary() {
  const summary = document.getElementById("targetHealthSummary");
  if (!summary) return;

  const databases = state.databases;
  const total = databases.length;
  summary.hidden = total === 0;
  if (!total) return;

  const counts = databases.reduce((result, db) => {
    const status = state.databaseReachability[db.id]?.status;
    if (status === "reachable") result.reachable += 1;
    else if (status === "unreachable") result.unreachable += 1;
    else result.pending += 1;
    return result;
  }, { reachable: 0, unreachable: 0, pending: 0 });

  const setText = (id, value) => {
    const element = document.getElementById(id);
    if (element) element.textContent = String(value);
  };
  setText("targetHealthTotal", `${total} ${total === 1 ? "target" : "targets"}`);
  setText("targetHealthReachable", counts.reachable);
  setText("targetHealthUnreachable", counts.unreachable);
  setText("targetHealthPending", counts.pending);

  const bar = document.getElementById("targetHealthBar");
  if (bar) bar.setAttribute("aria-label", `${counts.reachable} reachable, ${counts.unreachable} unreachable, ${counts.pending} pending of ${total} targets`);
  [
    ["targetHealthReachableBar", counts.reachable],
    ["targetHealthUnreachableBar", counts.unreachable],
    ["targetHealthPendingBar", counts.pending],
  ].forEach(([id, count]) => {
    const segment = document.getElementById(id);
    if (segment) segment.style.width = `${total ? (count / total) * 100 : 0}%`;
  });

  renderTargetHealthDonut(counts, total);
}

function renderDatabasePills() {
  const container = document.getElementById("dbPillList");
  const searchWrap = document.getElementById("dbSearchWrap");
  if (!container) return;

  renderTargetHealthSummary();
  if (searchWrap) searchWrap.hidden = state.databases.length < 4;

  if (state.databases.length === 0) {
    container.innerHTML = `<span class="pill-placeholder">No databases registered yet — click “Add database instance”.</span>`;
    return;
  }

  const q = state.filter.trim().toLowerCase();
  const list = q
    ? state.databases.filter((d) => `${d.name} ${d.engine}`.toLowerCase().includes(q))
    : state.databases;

  if (list.length === 0) {
    container.innerHTML = `<span class="pill-placeholder">No targets match “${escapeHtml(state.filter)}”.</span>`;
    return;
  }

  const activeIndex = list.findIndex((db) => db.id === state.activeDatabaseId);
  targetCarouselOffset = activeIndex >= 0 ? activeIndex : 0;
  const cards = list.map((db, index) => {
    const meta = engineMeta(db.engine);
    const active = db.id === state.activeDatabaseId;
    const reachability = state.databaseReachability[db.id] || { status: "checking" };
    const label = reachability.status === "reachable" ? "Reachable" : reachability.status === "unreachable" ? "Unreachable" : reachability.status === "checking" ? "Checking" : "Unknown";
    const statusTitle = reachability.error ? `${label}: ${reachability.error}` : `${label}${reachability.latency_ms != null ? ` · ${reachability.latency_ms} ms` : ""}`;
    const engineKey = String(db.engine || "").toLowerCase();
    const requestedScene = ["postgres", "postgresql"].includes(engineKey) ? "postgres" : engineKey;
    const scene = ["postgres", "mongodb", "mysql", "redis"].includes(requestedScene) ? requestedScene : "default";
    return `<div class="target-carousel-card-wrap" data-index="${index}" data-active="${active}" role="listitem">
      <button class="target-polaroid" aria-pressed="${active}" data-action="select-db" data-id="${escapeHtml(db.id)}" type="button" title="Select ${escapeHtml(db.name)} — ${escapeHtml(statusTitle)}">
        <span class="target-polaroid-art target-scene-${escapeHtml(scene)}">
          ${engineBadge(db.engine, true)}
          ${icon("database")}
        </span>
        <span class="target-polaroid-name">${escapeHtml(db.name)}</span>
        <span class="target-polaroid-meta">
          <span>${escapeHtml(meta.label)}</span>
          <span class="target-polaroid-status ${escapeHtml(reachability.status)}" title="${escapeHtml(statusTitle)}">${escapeHtml(label)}</span>
        </span>
      </button>
      ${db.id !== "db-primary" ? `<button class="target-polaroid-remove" data-action="unregister-target" data-id="${escapeHtml(db.id)}" title="Remove ${escapeHtml(db.name)} from this application" aria-label="Remove ${escapeHtml(db.name)} from this application" type="button"><svg class="ico ico-sm" aria-hidden="true"><use href="#i-trash"/></svg></button>` : ""}
    </div>`;
  }).join("");
  const selectedPosition = activeIndex >= 0 ? activeIndex : 0;
  container.innerHTML = `<div class="target-carousel-stage" data-dragging="0" tabindex="0" aria-label="Drag to browse database targets. Use left and right arrow keys to switch targets.">
    <div class="target-carousel-track" role="list">${cards}</div>
  </div>
  <div class="target-carousel-controls">
    <button class="target-carousel-arrow" data-action="target-carousel-step" data-direction="-1" type="button" aria-label="Previous target" ${selectedPosition <= 0 ? "disabled" : ""}><span aria-hidden="true">←</span></button>
    <span class="target-carousel-count" aria-live="polite"><strong>${String(selectedPosition + 1).padStart(2, "0")}</strong> / ${String(list.length).padStart(2, "0")}</span>
    <button class="target-carousel-arrow" data-action="target-carousel-step" data-direction="1" type="button" aria-label="Next target" ${selectedPosition >= list.length - 1 ? "disabled" : ""}><span aria-hidden="true">→</span></button>
  </div>`;
  updateTargetCarouselPosition();
}

function updateTargetCarouselPosition() {
  const stage = document.querySelector(".target-carousel-stage");
  if (!stage) return;
  const width = stage.clientWidth;
  const y0 = 20;
  const spacing = Math.min(224, Math.max(174, width * 0.58));
  const cards = stage.querySelectorAll(".target-carousel-card-wrap");
  cards.forEach((card, index) => {
    const offset = index - targetCarouselOffset;
    const x = offset * spacing;
    const distance = Math.abs(offset);
    const y = y0 + Math.min(12, distance * 3);
    const angle = Math.max(-9, Math.min(9, offset * 3.5));
    const scale = Math.max(.72, 1 - distance * .12);
    card.style.transform = `translate3d(calc(-50% + ${x}px), ${y}px, 0) rotate(${angle}deg) scale(${scale})`;
    card.style.opacity = String(Math.max(0, 1 - Math.max(0, distance - 1) * .48));
    card.style.zIndex = String(100 - Math.round(distance * 10));
    card.style.visibility = distance > 2.4 ? "hidden" : "visible";
    card.setAttribute("aria-hidden", distance > 2.4 ? "true" : "false");
  });
}

function stepTargetCarousel(direction) {
  const cards = [...document.querySelectorAll(".target-carousel-card-wrap")];
  if (!cards.length) return;
  const nextIndex = Math.max(0, Math.min(cards.length - 1, Math.round(targetCarouselOffset) + direction));
  const target = state.databases.find((db) => db.id === cards[nextIndex]?.querySelector("[data-action='select-db']")?.dataset.id);
  if (target) {
    selectDatabase(target.id);
    document.querySelector(".target-carousel-stage")?.focus({ preventScroll: true });
  }
}

function initTargetCarousel() {
  const container = document.getElementById("dbPillList");
  if (!container) return;

  container.addEventListener("pointerdown", (event) => {
    const stage = event.target.closest(".target-carousel-stage");
    if (!stage || event.button !== 0 || event.target.closest(".target-carousel-controls, .target-polaroid-remove")) return;
    targetCarouselDrag = { stage, startX: event.clientX, startOffset: targetCarouselOffset, moved: false };
  });
  container.addEventListener("pointermove", (event) => {
    if (!targetCarouselDrag || !container.contains(targetCarouselDrag.stage)) return;
    const distance = event.clientX - targetCarouselDrag.startX;
    if (!targetCarouselDrag.moved && Math.abs(distance) > 5) {
      targetCarouselDrag.moved = true;
      targetCarouselDrag.stage.dataset.dragging = "1";
      targetCarouselDrag.stage.setPointerCapture(event.pointerId);
    }
    if (!targetCarouselDrag.moved) return;
    event.preventDefault();
    const spacing = Math.min(224, Math.max(174, targetCarouselDrag.stage.clientWidth * 0.58));
    const lastIndex = targetCarouselDrag.stage.querySelectorAll(".target-carousel-card-wrap").length - 1;
    targetCarouselOffset = Math.max(0, Math.min(lastIndex, targetCarouselDrag.startOffset - distance / spacing));
    updateTargetCarouselPosition();
  });
  const finishDrag = (cancelled = false) => {
    if (!targetCarouselDrag) return;
    const { stage, moved, startOffset } = targetCarouselDrag;
    targetCarouselDrag = null;
    stage.dataset.dragging = "0";
    if (!moved) return;
    if (cancelled) {
      targetCarouselOffset = startOffset;
      updateTargetCarouselPosition();
      return;
    }
    const index = Math.round(targetCarouselOffset);
    const targetId = stage.querySelector(`.target-carousel-card-wrap[data-index="${index}"] [data-action="select-db"]`)?.dataset.id;
    const target = state.databases.find((db) => db.id === targetId);
    suppressTargetCarouselClick = true;
    setTimeout(() => { suppressTargetCarouselClick = false; }, 0);
    if (target) {
      selectDatabase(target.id);
      document.querySelector(".target-carousel-stage")?.focus({ preventScroll: true });
    }
  };
  container.addEventListener("pointerup", () => finishDrag());
  container.addEventListener("pointercancel", () => finishDrag(true));
  container.addEventListener("keydown", (event) => {
    if (!event.target.closest(".target-carousel-stage")) return;
    if (event.key === "ArrowLeft") stepTargetCarousel(-1);
    else if (event.key === "ArrowRight") stepTargetCarousel(1);
    else if (event.key === "Home") stepTargetCarousel(-Math.round(targetCarouselOffset));
    else if (event.key === "End") stepTargetCarousel(state.databases.length);
    else return;
    event.preventDefault();
  });
  window.addEventListener("resize", updateTargetCarouselPosition);
}

async function refreshDatabaseReachability() {
  if (state.reachabilityRefreshing || !state.databases.length) return;
  state.reachabilityRefreshing = true;
  const targets = state.databases.map(({ id }) => id);
  targets.forEach((id) => {
    if (!state.databaseReachability[id]) state.databaseReachability[id] = { status: "checking" };
  });
  renderDatabasePills();
  try {
    await Promise.all(targets.map(async (dbId) => {
      try {
        const health = await api(`/api/v1/health?db_id=${encodeURIComponent(dbId)}`);
        state.databaseReachability[dbId] = {
          status: health.connected ? "reachable" : "unreachable",
          latency_ms: health.latency_ms,
          error: health.connected ? null : (health.error || "Connection failed"),
        };
      } catch (error) {
        state.databaseReachability[dbId] = { status: "unknown", error: error.message || "Health check failed" };
      }
    }));
  } finally {
    state.reachabilityRefreshing = false;
    state.databaseReachability = Object.fromEntries(
      Object.entries(state.databaseReachability).filter(([dbId]) => state.databases.some((db) => db.id === dbId))
    );
    renderDatabasePills();
  }
}

function updateActiveToolbar() {
  const target = state.databases.find((d) => d.id === state.activeDatabaseId) || state.databases[0];
  if (!target) {
    const titleEl = document.getElementById("activeDbTitle");
    const badgeEl = document.getElementById("activeEngineBadge");
    const urlEl = document.getElementById("activeDbUrlMasked");
    const backupTargetLabel = document.getElementById("backupTargetLabel");
    const unregisterBtn = document.getElementById("unregisterBtn");
    if (titleEl) titleEl.textContent = "No database registered";
    if (badgeEl) badgeEl.textContent = "NO TARGET";
    if (urlEl) { urlEl.textContent = "Add a database to start monitoring"; urlEl.removeAttribute("title"); }
    if (backupTargetLabel) backupTargetLabel.textContent = "No database selected";
    if (unregisterBtn) unregisterBtn.style.display = "none";
    document.querySelectorAll('[data-action="open-backup"]').forEach((button) => { button.disabled = true; });
    return;
  }

  const meta = engineMeta(target.engine);
  const iconEl = document.getElementById("activeEngineIcon");
  const titleEl = document.getElementById("activeDbTitle");
  const badgeEl = document.getElementById("activeEngineBadge");
  const urlEl = document.getElementById("activeDbUrlMasked");
  const unregisterBtn = document.getElementById("unregisterBtn");
  const backupTargetLabel = document.getElementById("backupTargetLabel");

  if (iconEl) iconEl.innerHTML = engineBadge(target.engine, true);
  if (titleEl) titleEl.textContent = target.name;
  if (badgeEl) {
    badgeEl.textContent = meta.label.toUpperCase();
    badgeEl.className = `badge ${(target.status || "online") === "online" ? "badge-info" : "badge-danger"}`;
  }
  if (urlEl) {
    urlEl.textContent = target.connection_url;
    urlEl.title = currentRole === "admin" ? "Click to copy the full connection URL" : "Only an administrator can copy the full connection URL";
    urlEl.disabled = currentRole !== "admin";
  }
  if (backupTargetLabel) backupTargetLabel.textContent = `${target.name} (${meta.label})`;
  const backupSupported = ["postgres", "postgresql"].includes(String(target.engine || "").toLowerCase());
  document.querySelectorAll('[data-action="open-backup"]').forEach((button) => {
    button.disabled = !backupSupported;
    button.title = backupSupported ? "Create a PostgreSQL snapshot" : "Snapshot creation currently supports PostgreSQL targets only";
  });
  if (unregisterBtn) unregisterBtn.style.display = target.id === "db-primary" ? "none" : "inline-flex";

  const relationsHeader = document.getElementById("relationsHeaderTitle");
  if (relationsHeader) {
    if (target.engine === "mongodb") relationsHeader.textContent = "Top Collections by Size";
    else if (target.engine === "redis") relationsHeader.textContent = "Keyspace & Memory Statistics";
    else relationsHeader.textContent = "Top Tables by Size";
  }
}

async function fetchRegisteredDatabases() {
  const container = document.getElementById("dbPillList");
  try {
    const data = await api("/api/v1/databases");
    state.databases = data.databases || [];
    state.databaseReachability = Object.fromEntries(
      Object.entries(state.databaseReachability).filter(([dbId]) => state.databases.some((db) => db.id === dbId))
    );
    if (state.databases.length && !state.databases.some((d) => d.id === state.activeDatabaseId)) {
      state.activeDatabaseId = state.databases[0].id;
    }
    try { if (state.activeDatabaseId) localStorage.setItem("dbops_active_database", state.activeDatabaseId); } catch (e) { /* keep selection in memory */ }
    renderBrowseDatabaseOptions();
    renderDatabasePills();
    updateActiveToolbar();
    refreshDatabaseReachability();
    populateActivityDatabaseFilter();
  } catch (err) {
    console.error("[DBOps] Failed to load databases:", err);
    if (container) {
      container.innerHTML = `<span class="pill-placeholder">Failed to load targets — ${escapeHtml(err.message)}</span>`;
    }
    populateActivityDatabaseFilter();
  }
}

async function selectDatabase(dbId) {
  state.cacheMetric = null; state.sizeMetric = null; state.slowMetric = null; state.deadlockMetric = null;
  updateMetricVisualizations();
  state.activeDatabaseId = dbId;
  try { localStorage.setItem("dbops_active_database", dbId); } catch (e) { /* keep selection in memory */ }
  const target = state.databases.find((d) => d.id === dbId);
  renderDatabasePills();
  updateActiveToolbar();
  refreshBrowserTables();
  if (target) { showToast(`Monitoring target: ${target.name}`, "info"); logActivity("switch", "Switched active target", target.name, target.id); }
  await refreshDashboard();
  await fetchMigrationHistory();
}

function renderBrowseMessage(message) {
  const body = document.getElementById("browseTableBody");
  const head = document.getElementById("browseTableHead");
  if (head) head.innerHTML = "<tr><th>Data</th></tr>";
  if (body) body.innerHTML = `<tr><td class="table-empty">${escapeHtml(message)}</td></tr>`;
}

function renderBrowseDatabaseOptions() {
  const select = document.getElementById("browseDatabaseSelect");
  if (!select) return;
  const previousId = state.databases.some((db) => db.id === browseState.databaseId)
    ? browseState.databaseId
    : state.activeDatabaseId;
  select.innerHTML = '<option value="">Select a database</option>' + state.databases.map((db) =>
    `<option value="${escapeHtml(db.id)}">${escapeHtml(db.name)} (${escapeHtml(engineMeta(db.engine).label)})</option>`
  ).join("");
  const selected = state.databases.some((db) => db.id === previousId) ? previousId : (state.databases[0]?.id || "");
  browseState.databaseId = selected;
  select.value = selected;
  refreshBrowserTables();
}

async function refreshBrowserTables() {
  const select = document.getElementById("browseTableSelect");
  if (!select) return;
  const dbSelect = document.getElementById("browseDatabaseSelect");
  const dbId = dbSelect ? dbSelect.value : (browseState.databaseId || state.activeDatabaseId);
  browseState.databaseId = dbId;
  const db = state.databases.find((item) => item.id === dbId);
  browseState.table = ""; browseState.offset = 0;
  if (!db) { select.disabled = true; select.innerHTML = '<option value="">Select a database first</option>'; renderBrowseMessage("Select a registered database to browse its data."); return; }
  select.disabled = true;
  select.innerHTML = '<option value="">Loading tables…</option>';
  try {
    const data = await api(`/api/v1/databases/${encodeURIComponent(db.id)}/browse/tables`);
    select.innerHTML = '<option value="">Select a table or collection</option>' + (data.tables || []).map((table) => {
      const value = `${table.schema}.${table.name}`;
      return `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`;
    }).join("");
    select.disabled = !(data.tables || []).length;
    document.getElementById("browseStatus").textContent = data.truncated ? "Showing up to 500 tables/collections." : `${(data.tables || []).length} tables/collections available.`;
    if (!data.tables?.length) renderBrowseMessage("No tables or collections are available to this account.");
  } catch (err) {
    select.disabled = true;
    select.innerHTML = '<option value="">Could not load tables</option>';
    document.getElementById("browseStatus").textContent = err.message;
    renderBrowseMessage(err.message);
  }
}

async function loadBrowserRows() {
  const select = document.getElementById("browseTableSelect");
  const dbSelect = document.getElementById("browseDatabaseSelect");
  const dbId = dbSelect ? dbSelect.value : browseState.databaseId;
  const db = state.databases.find((item) => item.id === dbId);
  if (!db || !select?.value) { showToast("Select a table or collection first.", "warning"); return; }
  browseState.table = select.value;
  const status = document.getElementById("browseStatus");
  status.textContent = "Loading rows…";
  try {
    const qs = new URLSearchParams({ table: browseState.table, offset: String(browseState.offset), limit: String(browseState.limit) });
    const data = await api(`/api/v1/databases/${encodeURIComponent(db.id)}/browse/rows?${qs}`);
    const columns = data.columns || [];
    document.getElementById("browseTableHead").innerHTML = columns.length ? `<tr>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr>` : "<tr><th>Data</th></tr>";
    const rows = data.rows || [];
    document.getElementById("browseTableBody").innerHTML = rows.length ? rows.map((row) => `<tr>${columns.map((column) => {
      const value = row[column];
      const display = value == null ? "NULL" : typeof value === "object" ? JSON.stringify(value) : String(value);
      return `<td title="${escapeHtml(display)}">${escapeHtml(display)}</td>`;
    }).join("")}</tr>`).join("") : `<tr><td colspan="${Math.max(columns.length, 1)}" class="table-empty">No rows on this page.</td></tr>`;
    const page = Math.floor(browseState.offset / browseState.limit) + 1;
    document.getElementById("browsePageLabel").textContent = `Page ${page} · ${rows.length} rows`;
    document.getElementById("browsePrev").disabled = browseState.offset === 0;
    document.getElementById("browseNext").disabled = !data.has_more;
    status.textContent = `Read-only view · ${db.name} · ${browseState.table}`;
  } catch (err) { status.textContent = err.message; renderBrowseMessage(err.message); }
}

function signalDatabaseRegistryChange() {
  try { localStorage.setItem("dbops_registry_changed", String(Date.now())); } catch (e) { /* current page still updates */ }
}

/* -------------------------------------------------------------------------
   Add-database flow
   ------------------------------------------------------------------------- */
function openAddDbDialog() {
  clearFieldError("newDbName");
  clearFieldError("newDbUrl");
  hideInlineStatus();
  openDialog("addDbDialog");
  const name = document.getElementById("newDbName");
  if (name) setTimeout(() => name.focus(), 60);
}

async function testAddDbConnection() {
  clearFieldError("newDbUrl");
  const url = document.getElementById("newDbUrl").value.trim();
  const engine = document.getElementById("newDbEngine").value;
  if (!url) { showFieldError("newDbUrl", "Enter a connection URL to test."); return; }
  if (!/^[a-z0-9+.-]+:\/\//i.test(url)) { showFieldError("newDbUrl", "URL must include a scheme, e.g. postgresql://"); return; }

  showInlineStatus("Testing connection…", "busy");
  try {
    const data = await api("/api/v1/databases/test", {
      method: "POST",
      body: JSON.stringify({ engine, connection_url: url }),
    });
    if (data.connected) {
      showInlineStatus(`Connected — ${data.latency_ms} ms${data.version ? ` · ${data.version}` : ""}`, "ok");
    } else {
      showInlineStatus(data.error || "Connection failed.", "err");
    }
  } catch (err) {
    showInlineStatus(err.message, "err");
  }
}

async function submitRegisterDb() {
  clearFieldError("newDbName");
  clearFieldError("newDbUrl");
  hideInlineStatus();

  const name = document.getElementById("newDbName").value.trim();
  const url = document.getElementById("newDbUrl").value.trim();
  const engine = document.getElementById("newDbEngine").value;

  let valid = true;
  if (!name) { showFieldError("newDbName", "Please enter a label."); valid = false; }
  if (!url) { showFieldError("newDbUrl", "Please enter a connection URL."); valid = false; }
  else if (!/^[a-z0-9+.-]+:\/\//i.test(url)) { showFieldError("newDbUrl", "URL must include a scheme, e.g. postgresql://"); valid = false; }
  if (!valid) return;

  const dup = state.databases.find((d) => d.name.toLowerCase() === name.toLowerCase());
  if (dup) {
    const proceed = await confirmAction({
      title: "Duplicate label",
      message: `A database named “${name}” already exists. Add another one anyway?`,
      confirmText: "Add anyway",
    });
    if (!proceed) return;
  }

  const btn = document.getElementById("submitDbBtn");
  setBusy(btn, true, "Adding…");
  try {
    const data = await api("/api/v1/databases", {
      method: "POST",
      body: JSON.stringify({ name, engine, connection_url: url, verify_connectivity: true }),
    });
    const offline = (data.status || "online") !== "online";
    showToast(
      offline ? `Added “${data.name}” but it is unreachable — check URL/credentials.` : `Database “${data.name}” added successfully.`,
      offline ? "warning" : "success"
    );
    logActivity(offline ? "warning" : "add", offline ? "Added unreachable target" : "Registered database", `${data.name} · ${engineMeta(engine).label}`, data.id);
    closeDialog("addDbDialog");
    document.getElementById("newDbName").value = "";
    document.getElementById("newDbUrl").value = "";
    state.activeDatabaseId = data.id;
    await fetchRegisteredDatabases();
    signalDatabaseRegistryChange();
    await refreshDashboard();
  } catch (err) {
    if (err.status === 401) showToast(err.message, "error");
    else { showInlineStatus(`Registration failed: ${err.message}`, "err"); showToast(`Could not add database: ${err.message}`, "error"); }
  } finally {
    setBusy(btn, false);
  }
}

async function confirmUnregisterActiveDb(dbId = state.activeDatabaseId) {
  const target = state.databases.find((d) => d.id === dbId);
  if (!target || target.id === "db-primary") return;

  const ok = await confirmAction({
    title: "Unregister target",
    message: `Stop monitoring “${target.name}”? The remote database is left completely untouched.`,
    confirmText: "Unregister",
  });
  if (!ok) return;

  try {
    await api(`/api/v1/databases/${encodeURIComponent(target.id)}`, { method: "DELETE" });
    showToast(`Unregistered “${target.name}”.`, "info");
    logActivity("remove", "Unregistered target", target.name, target.id);
    const removedActive = target.id === state.activeDatabaseId;
    state.databases = state.databases.filter((db) => db.id !== target.id);
    const fallback = state.databases.find((db) => db.id === "db-primary") || state.databases[0];
    if (removedActive) {
      state.activeDatabaseId = fallback ? fallback.id : "";
      state.cacheMetric = null; state.sizeMetric = null; state.slowMetric = null; state.deadlockMetric = null;
      try {
        if (fallback) localStorage.setItem("dbops_active_database", fallback.id);
        else localStorage.removeItem("dbops_active_database");
      } catch (e) { /* current page still updates */ }
    }
    renderDatabasePills();
    updateActiveToolbar();
    if (removedActive) updateMetricVisualizations();
    signalDatabaseRegistryChange();
    await fetchRegisteredDatabases();
    await fetchRestores();
    if (removedActive && fallback) await refreshDashboard();
  } catch (err) {
    showToast(`Unregister failed: ${err.message}`, "error");
  }
}

/* -------------------------------------------------------------------------
   Actions
   ------------------------------------------------------------------------- */
async function quickPingActiveDb() {
  const btn = document.querySelector('[data-action="ping"]');
  setBusy(btn, true, "Pinging…");
  try {
    const data = await api("/api/v1/actions/ping", {
      method: "POST",
      body: JSON.stringify({ db_id: state.activeDatabaseId }),
    });
    if (data.connected) { showToast(`Ping OK — ${data.latency_ms} ms${data.version ? ` (${data.version})` : ""}`, "success"); logActivity("ping", "Ping succeeded", `${data.latency_ms} ms${data.version ? ` · ${data.version}` : ""}`, state.activeDatabaseId); }
    else { showToast(`Ping failed — ${data.error || "unreachable"}`, "error"); logActivity("error", "Ping failed", data.error || "unreachable", state.activeDatabaseId); }
  } catch (err) {
    showToast(`Ping error: ${err.message}`, "error");
  } finally {
    setBusy(btn, false);
  }
}

async function copyActiveUrl() {
  const target = state.databases.find((d) => d.id === state.activeDatabaseId);
  if (!target) return;
  if (currentRole !== "admin") {
    showToast("Ask an administrator to copy the full connection URL.", "warning");
    return;
  }
  try {
    const data = await api(`/api/v1/databases/${encodeURIComponent(target.id)}/connection-url`);
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(data.connection_url);
    else {
      const ta = document.createElement("textarea");
      ta.value = data.connection_url; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta);
      try { ta.select(); if (!document.execCommand("copy")) throw new Error("Clipboard access is unavailable."); }
      finally { ta.remove(); }
    }
    showToast(`Full connection URL for ${data.database_name} copied. Share it using a secure channel.`, "success");
    logActivity("copy", "Copied connection URL", target.name, target.id);
  } catch (err) {
    showToast(`Could not copy connection URL: ${err.message}`, "error");
  }
}

/* -------------------------------------------------------------------------
   Command-center hero
   ------------------------------------------------------------------------- */
const HERO_STATE = {
  ready:      { cls: "ok" },
  connecting: { cls: "warn" },
  error:      { cls: "bad" },
};

function computeHeroMode() {
  const total = state.databases.length;
  if (!total) return { status: "connecting" };
  const unreachable = state.databases.filter((d) => d.status !== "online").length;
  if (unreachable === 0) return { status: "ready", text: "All systems operational" };
  return { status: "error", text: `${unreachable} of ${total} target${total === 1 ? "" : "s"} unreachable` };
}

function updateHeroStatus(status, text) {
  const meta = HERO_STATE[status] || HERO_STATE.connecting;
  const dot = document.getElementById("heroStatusDot");
  const el = document.getElementById("heroStatusText");
  if (dot) dot.className = `chip-dot ${meta.cls}`;
  if (el) el.textContent = text || meta.text || "—";
}

function computeSystemStatus() {
  const { status, text } = computeHeroMode();
  updateHeroStatus(status, text);
}

function setHeroChip(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function parseIso(iso) {
  if (!iso) return NaN;
  return Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : `${iso}Z`);
}

function formatSriLankaDateTime(iso) {
  const timestamp = parseIso(iso);
  if (Number.isNaN(timestamp)) return "--";
  const parts = new Intl.DateTimeFormat("en", {
    timeZone: "Asia/Colombo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(timestamp));
  const values = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}:${values.second}`;
}

function formatSriLankaBackupShortDate(iso) {
  const timestamp = parseIso(iso);
  if (Number.isNaN(timestamp)) return "--";
  const parts = new Intl.DateTimeFormat("en-LK", {
    timeZone: "Asia/Colombo",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(timestamp));
  const values = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${values.month}-${values.day} ${values.hour}:${values.minute}`;
}

function relFromIso(iso) {
  const t = parseIso(iso);
  return isNaN(t) ? "—" : relTime(t);
}

function untilFromIso(iso) {
  const t = parseIso(iso);
  if (isNaN(t)) return "—";
  const s = Math.floor((t - Date.now()) / 1000);
  if (s <= 0) return "any moment";
  if (s < 60) return `in ${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `in ${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `in ${h}h`;
  return `in ${Math.floor(h / 24)}d`;
}

async function fetchScheduleSummary() {
  try {
    const data = await api("/api/v1/backups/schedule");
    if (!data.enabled) setHeroChip("heroNextRun", "Paused");
    else setHeroChip("heroNextRun", data.next_run_time ? untilFromIso(data.next_run_time) : "Scheduled");
  } catch (err) {
    setHeroChip("heroNextRun", "—");
  }
}

async function refreshHeroNotify() {
  try {
    const cfg = await api("/api/v1/notifications/config");
    setHeroChip("heroNotify", cfg.enabled && cfg.telegram_configured ? "Configured" : (cfg.enabled ? "No token" : "Off"));
  } catch (err) {
    setHeroChip("heroNotify", "—");
  }
}

/* -------------------------------------------------------------------------
   Health & metrics
   ------------------------------------------------------------------------- */
async function fetchHealth() {
  try {
    const data = await api(`/api/v1/health?db_id=${encodeURIComponent(state.activeDatabaseId)}`);
    const dot = document.getElementById("statusDot");
    const text = document.getElementById("statusText");
    const lat = document.getElementById("latencyVal");
    const ver = document.getElementById("versionVal");
    const up = document.getElementById("uptimeVal");
    const target = state.databases.find((d) => d.id === state.activeDatabaseId);

    if (data.connected) {
      state.lastHealthConnected = true;
      if (dot) dot.className = "status-dot";
      if (text) { setValue(text, target ? `${target.name} (${engineMeta(target.engine).label})` : "Connected"); text.style.color = "var(--ok)"; }
      setValue(lat, `${data.latency_ms != null ? data.latency_ms : "--"} ms`);
      setValue(ver, data.version || "Online");
      setValue(up, data.uptime || "--");
      state.latencyHistory.push(data.latency_ms || 12);
      if (state.latencyHistory.length > 15) state.latencyHistory.shift();
      renderLatencySparkline();
    } else {
      state.lastHealthConnected = false;
      if (dot) dot.className = "status-dot unhealthy";
      if (text) { setValue(text, `${target ? target.name : "Target"} (Unreachable)`); text.style.color = "var(--bad)"; }
      setValue(lat, "-- ms"); setValue(ver, "N/A"); setValue(up, "Down");
      if (data.error) console.warn("[DBOps] Health:", data.error);
    }
  } catch (err) {
    console.error("[DBOps] Health check failed:", err);
    state.lastHealthConnected = false;
    const dot = document.getElementById("statusDot");
    if (dot) dot.className = "status-dot unhealthy";
    setValue(document.getElementById("statusText"), "Control plane offline");
  }
}

async function fetchMigrationHistory() {
  const note = document.getElementById("migrationHistoryNote");
  const body = document.getElementById("migrationHistoryBody");
  if (!note || !body) return;
  const summary = document.getElementById("recentMigrationVal");
  const summaryMeta = document.getElementById("recentMigrationMeta");
  const summaryNote = document.getElementById("recentMigrationFootnote");
  const badge = document.getElementById("recentMigrationBadge");
  const setSummary = (value, meta, status = "Read only") => {
    if (summary) summary.textContent = value;
    if (summaryMeta) summaryMeta.textContent = meta;
    if (badge) badge.textContent = status;
  };
  note.textContent = "Reading migration ledger metadata (read-only)…";
  setSummary("Checking…", "Looking for a recognized migration record");
  try {
    const data = await api(`/api/v1/metrics/migrations?db_id=${encodeURIComponent(state.activeDatabaseId)}`);
    note.textContent = data.message || "Migration history checked.";
    if (!data.supported || !data.migrations?.length) {
      body.innerHTML = `<tr><td colspan="5" class="table-empty">${escapeHtml(data.status === "not_detected" ? "No recognized migration entries found." : data.message || "No migration entries available.")}</td></tr>`;
      const label = data.status === "not_detected" ? "No ledger found" : data.status === "unsupported" ? "Not supported" : data.inaccessible?.length ? "History restricted" : "No entries found";
      setSummary(label, data.status === "not_detected" ? "No recognized migration table; custom or external tracking may be used." : data.message || "No migration records are available.", data.status === "not_detected" ? "Not detected" : data.status === "unsupported" ? "Unsupported" : "No entries");
      if (summaryNote) summaryNote.textContent = "Only recorded migration history is shown; current schema is not used to infer past changes.";
      return;
    }
    body.innerHTML = data.migrations.map((item) => {
      const applied = item.applied_at ? new Date(item.applied_at).toLocaleString() : "Not recorded by this migration system";
      return `<tr><td>${escapeHtml(item.framework)}</td><td>${escapeHtml(item.version)}</td><td>${escapeHtml(item.name)}</td><td>${escapeHtml(applied)}</td><td>${escapeHtml(item.status)}</td></tr>`;
    }).join("");
    const latest = data.migrations[0];
    setSummary(latest.name || latest.version || "Migration recorded", `${latest.framework} · ${latest.applied_at ? new Date(latest.applied_at).toLocaleString() : "timestamp not recorded"}`, latest.status || "Recorded");
    if (summaryNote) summaryNote.textContent = `${data.migrations.length} recent record${data.migrations.length === 1 ? "" : "s"} shown. Refresh history to check for newer entries.`;
  } catch (err) {
    note.textContent = `Could not read migration history: ${err.message}`;
    body.innerHTML = `<tr><td colspan="5" class="table-empty">History unavailable. Check the target connection and database user's read permissions.</td></tr>`;
    setSummary("History unavailable", "Check the target connection and database user's read permissions.", "Unavailable");
    if (summaryNote) summaryNote.textContent = "No schema inspection or migration changes were attempted.";
  }
}

async function fetchCacheHit() {
  try {
    const data = await api(`/api/v1/metrics/cache-hit-ratio?db_id=${encodeURIComponent(state.activeDatabaseId)}`);
    state.cacheMetric = data;
    setValue(document.getElementById("cacheHitVal"), `${data.buffer_cache_hit_ratio}%`);
    const badge = document.getElementById("cacheBadge");
    if (badge) {
      const threshold = Number(data.threshold_percent || 95);
      badge.className = `badge ${data.is_healthy ? "badge-success" : "badge-warning"}`;
      badge.innerHTML = data.is_healthy
        ? `${icon("check", "ico-sm")}<span>Cache healthy</span>`
        : `${icon("warning", "ico-sm")}<span>Below ${threshold}% cache target</span>`;
      badge.title = data.is_healthy
        ? `Cache hit ratio meets the ${threshold}% target. This describes cache efficiency, not overall database health.`
        : `Cache hit ratio is below the ${threshold}% target. This describes cache efficiency, not overall database health.`;
    }

    updateMetricVisualizations();
  } catch (err) {
    console.error("[DBOps] cache:", err);
    setValue(document.getElementById("cacheHitVal"), "--%");
    state.cacheMetric = null; updateMetricVisualizations();
  }
}

async function fetchDbSize() {
  try {
    const data = await api(`/api/v1/metrics/database-size?db_id=${encodeURIComponent(state.activeDatabaseId)}`);
    state.sizeMetric = data;
    setValue(document.getElementById("dbNameBadge"), data.database_name);
    setValue(document.getElementById("dbSizeVal"), data.total_human);
    setValue(document.getElementById("tableCountSummary"), `${data.tables.length} user relation${data.tables.length === 1 ? "" : "s"} monitored`, false);

    updateMetricVisualizations();

    const tbody = document.getElementById("tablesTableBody");
    if (!tbody) return;
    if (!data.tables || data.tables.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="table-empty">No relations found for this target.</td></tr>`;
      return;
    }
    tbody.innerHTML = data.tables.slice(0, 10).map((t) => `
      <tr>
        <td style="font-weight:600;">${escapeHtml(t.table_name)}</td>
        <td><span class="badge badge-info">${escapeHtml(t.total_human)}</span></td>
        <td>${escapeHtml(t.table_human)}</td>
        <td>${escapeHtml(t.index_human)}</td>
      </tr>`).join("");
  } catch (err) {
    console.error("[DBOps] size:", err);
    setValue(document.getElementById("dbSizeVal"), "--");
    state.sizeMetric = null; updateMetricVisualizations();
    const tbody = document.getElementById("tablesTableBody");
    if (tbody) tbody.innerHTML = `<tr><td colspan="4" class="table-empty">Storage metrics unavailable for this target.</td></tr>`;
  }
}

async function fetchSlowQueriesAndDeadlocks() {
  const body = document.getElementById("slowQueriesTableBody");
  const badge = document.getElementById("slowQueriesBadge");

  try {
    const slow = await api(`/api/v1/metrics/slow-queries?db_id=${encodeURIComponent(state.activeDatabaseId)}`);
    state.slowMetric = slow;
    state.lastSlowCount = Number(slow.count) || 0;
    setValue(document.getElementById("slowQueryVal"), `${slow.count} Slow`);
    updateMetricVisualizations();
    if (badge) {
      badge.className = `badge ${slow.count > 0 ? "badge-danger" : "badge-success"}`;
      badge.textContent = `${slow.count} Active Slow Quer${slow.count === 1 ? "y" : "ies"}`;
    }
    if (body) {
      if (slow.count > 0 && slow.slow_queries) {
        body.innerHTML = slow.slow_queries.map((q) => `
          <tr>
            <td><code>${escapeHtml(q.pid)}</code></td>
            <td>${escapeHtml(q.user || "app")}</td>
            <td>${escapeHtml(q.client_addr || "local")}</td>
            <td><strong style="color:var(--bad);">${escapeHtml(q.duration_seconds)}s</strong></td>
            <td class="truncate" title="${escapeHtml(q.query)}">${escapeHtml(q.query)}</td>
            <td class="col-action"><button class="btn btn-sm btn-danger" data-action="kill-query" data-id="${escapeHtml(q.pid)}" type="button">Kill</button></td>
          </tr>`).join("");
      } else {
        body.innerHTML = `<tr><td colspan="6" class="table-empty">No slow queries exceeding the threshold. Database executing smoothly.</td></tr>`;
      }
    }
  } catch (err) {
    console.error("[DBOps] slow queries:", err);
    setValue(document.getElementById("slowQueryVal"), "-- Slow");
    state.slowMetric = null; updateMetricVisualizations();
    if (body) body.innerHTML = `<tr><td colspan="6" class="table-empty">Query telemetry unavailable for this target.</td></tr>`;
  }

  try {
    const dead = await api(`/api/v1/metrics/deadlocks?db_id=${encodeURIComponent(state.activeDatabaseId)}`);
    state.deadlockMetric = dead;
    const badgeEl = document.getElementById("deadlockBadge");
    if (badgeEl) {
      badgeEl.textContent = `${dead.deadlocks_count} Deadlocks`;
      badgeEl.className = `badge ${dead.deadlocks_count > 0 ? "badge-danger" : "badge-success"}`;
    }
    updateMetricVisualizations();
  } catch (err) {
    console.error("[DBOps] deadlocks:", err);
    state.deadlockMetric = null; updateMetricVisualizations();
  }
}

async function killSlowQuery(pid, btn) {
  const ok = await confirmAction({
    title: "Terminate operation",
    message: `Terminate process / operation ${pid} on the active database?`,
    confirmText: "Terminate",
  });
  if (!ok) return;

  setBusy(btn, true, "Killing…");
  try {
    const data = await api("/api/v1/actions/kill-query", {
      method: "POST",
      body: JSON.stringify({ db_id: state.activeDatabaseId, pid: Number(pid) }),
    });
    if (data.success) { showToast(data.message, "success"); logActivity("kill", "Terminated operation", `PID ${pid}`, state.activeDatabaseId); await fetchSlowQueriesAndDeadlocks(); }
    else showToast(`Termination failed: ${data.message}`, "error");
  } catch (err) {
    showToast(`Error: ${err.message}`, "error");
  } finally {
    setBusy(btn, false);
  }
}


async function downloadBackup(backupId) {
  try {
    const response = await fetch(`/api/v1/backups/${encodeURIComponent(backupId)}/download`, { headers: getHeaders() });
    if (!response.ok) throw new Error("Download failed. Sign in again and retry.");
    const blob = await response.blob();
    const link = document.createElement("a"); link.href = URL.createObjectURL(blob);
    link.download = `backup-${backupId}`; link.click(); URL.revokeObjectURL(link.href);
  } catch (err) { showToast(err.message, "error"); }
}

function renderBackupsEmptyState(message, action = "") {
  const tbody = document.getElementById("backupsTableBody");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="9" class="table-empty">
    <div class="backup-empty-state">
      <span class="backup-empty-icon" aria-hidden="true">${icon("snapshot")}</span>
      <strong>No snapshots to show</strong>
      <span>${escapeHtml(message)}</span>
      ${action}
    </div>
  </td></tr>`;
}

async function fetchBackups() {
  const tbody = document.getElementById("backupsTableBody");
  if (!tbody) return;
  const target = state.databases.find((db) => db.id === state.activeDatabaseId);
  const supportsSnapshots = target && ["postgres", "postgresql"].includes(String(target.engine || "").toLowerCase());

  if (target && !supportsSnapshots) {
    const postgresTarget = state.databases.find((db) => ["postgres", "postgresql"].includes(String(db.engine || "").toLowerCase()));
    state.cachedBackups = [];
    renderBackupSizeTrend();
    setHeroChip("heroLastBackup", "PostgreSQL only");
    renderBackupsEmptyState(
      `“${target.name}” is a ${engineMeta(target.engine).label} target. Snapshot management is available for PostgreSQL targets.`,
      postgresTarget
        ? `<button class="btn btn-secondary btn-sm" data-action="select-db" data-id="${escapeHtml(postgresTarget.id)}" type="button">View ${escapeHtml(postgresTarget.name)} snapshots</button>`
        : ""
    );
    return;
  }

  try {
    const data = await api(`/api/v1/backups?db_id=${encodeURIComponent(state.activeDatabaseId)}`);
    state.cachedBackups = data.backups || [];
    renderBackupSizeTrend();
    if (!state.cachedBackups.length) {
      setHeroChip("heroLastBackup", "None yet");
      renderBackupsEmptyState(
        target ? `No historical snapshots are registered for “${target.name}” yet.` : "No historical snapshots are registered for the selected target.",
        supportsSnapshots
          ? `<button class="btn btn-primary-gradient btn-sm" data-action="open-backup" type="button">${icon("plus", "ico-sm")} Take first snapshot</button>`
          : ""
      );
      return;
    }
    const latestDone = state.cachedBackups.find((b) => b.status === "completed");
    setHeroChip("heroLastBackup", latestDone ? relFromIso(latestDone.completed_at || latestDone.created_at) : "In progress");

    tbody.innerHTML = state.cachedBackups.map((b) => {
      const statusBadge =
        b.status === "completed" ? `<span class="badge badge-success">Completed</span>` :
        b.status === "in_progress" ? `<span class="badge badge-warning">In progress…</span>` :
        `<span class="badge badge-danger">Failed</span>`;
      const restoreBtn = b.status === "completed"
        ? `<button class="btn btn-secondary btn-sm" data-action="open-restore" data-id="${escapeHtml(b.backup_id)}" type="button" title="Restore this snapshot">${icon("restore", "ico-sm")} Restore</button>`
        : `<button class="btn btn-secondary btn-sm" disabled title="Only completed backups can be restored" type="button">${icon("restore", "ico-sm")} Restore</button>`;
      return `
        <tr>
          <td><code>${escapeHtml(b.backup_id)}</code></td>
          <td class="truncate" title="${escapeHtml(b.filename)}" style="max-width:190px;">${escapeHtml(b.filename)} ${b.encrypted ? icon("lock", "ico-sm lock-mark") : ""}</td>
          <td class="truncate" title="${escapeHtml(b.description || "")}" style="max-width:220px;">${escapeHtml(b.description || "—")}</td>
          <td><span class="badge badge-info">${escapeHtml(b.format)}</span></td>
          <td>${escapeHtml(b.size_human)}</td>
          <td>${statusBadge}</td>
          <td>${b.age_days != null ? `${escapeHtml(b.age_days)} d` : "--"}</td>
          <td>${escapeHtml(formatSriLankaDateTime(b.created_at))}</td>
          <td class="col-action">
            <div class="row-actions">
              ${restoreBtn}
              <button class="btn btn-secondary btn-sm" data-action="download-backup" data-id="${escapeHtml(b.backup_id)}" type="button" title="Download archive">${icon("download", "ico-sm")}</button>
              <button class="btn btn-secondary btn-sm" data-action="verify-backup" data-id="${escapeHtml(b.backup_id)}" type="button" title="Verify integrity">${icon("search", "ico-sm")}</button>
              <button class="btn btn-sm btn-danger-ghost" data-action="delete-backup" data-id="${escapeHtml(b.backup_id)}" type="button" title="Delete local snapshot" ${b.status === "in_progress" ? "disabled" : ""}>Delete</button>
            </div>
          </td>
        </tr>`;
    }).join("");
  } catch (err) {
    console.error("[DBOps] backups:", err);
    tbody.innerHTML = `<tr><td colspan="9" class="table-empty">Backups unavailable — ${escapeHtml(err.message)}</td></tr>`;
  }
}

/* -------------------------------------------------------------------------
   Restore center
   ------------------------------------------------------------------------- */
async function fetchRestores() {
  const tbody = document.getElementById("restoresTableBody");
  const restoredDatabasesBody = document.getElementById("restoredDatabasesBody");
  if (!tbody && !restoredDatabasesBody) return;
  try {
    if (tbody) {
      try {
        const backups = await api("/api/v1/backups");
        state.restoreArchives = backups.backups || [];
      } catch (backupErr) {
        console.warn("[DBOps] Could not load databases for restore selector:", backupErr);
      }
    }
    const data = await api("/api/v1/restores?limit=30");
    state.restores = data.restores || [];
    if (!state.restores.length) {
      renderRestoredDatabases();
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="8" class="table-empty">No restores yet. Pick a completed snapshot and use “Restore”.</td></tr>`;
      }
      return;
    }
    if (tbody) tbody.innerHTML = state.restores.map((r) => {
      const destination = state.databases.find((db) => db.id === r.db_id);
      const statusBadge =
        r.status === "completed" ? `<span class="badge badge-success">Completed</span>` :
        r.status === "running" ? `<span class="badge badge-warning">Running…</span>` :
        `<span class="badge badge-danger">Failed</span>`;
      const modeBadge = r.mode === "clone"
        ? `<span class="badge badge-info">Clone</span>`
        : `<span class="badge badge-warning">Overwrite</span>`;
      const isRegisteredTarget = r.restored_database_id && state.databases.some((db) => db.id === r.restored_database_id);
      const targetStatus = isRegisteredTarget
        ? `<span class="badge badge-success" title="This database is available in Console Targets">In Targets</span><button class="btn btn-danger-ghost btn-sm" data-action="unregister-restored-target" data-id="${escapeHtml(r.restore_id)}" type="button" title="Remove this database from monitored Targets; keep its database and data">Remove from Targets</button>`
        : (r.status === "completed" && r.mode === "clone"
          ? `<button class="btn btn-secondary btn-sm" data-action="register-restored-target" data-id="${escapeHtml(r.restore_id)}" type="button" title="Add this restored database to monitored Targets">Add to Targets</button>`
          : "");
      return `
        <tr>
          <td><code>${escapeHtml(r.restore_id)}</code></td>
          <td><code>${escapeHtml(r.backup_id)}</code></td>
          <td>${modeBadge}</td>
          <td><strong>${escapeHtml(r.target_database)}</strong><div class="text-muted" style="font-size:0.82em;margin-top:3px;">On ${escapeHtml(destination?.name || "registered destination")}</div></td>
          <td>${statusBadge}</td>
          <td>${r.duration_seconds != null ? `${escapeHtml(r.duration_seconds)}s` : "—"}</td>
          <td>${escapeHtml((r.started_at || "").replace("T", " ").substring(0, 19))}</td>
          <td class="col-action">
            <div class="row-actions">${targetStatus}<button class="btn btn-secondary btn-sm" data-action="restore-detail" data-id="${escapeHtml(r.restore_id)}" type="button">Details</button></div>
          </td>
        </tr>`;
    }).join("");
    renderRestoredDatabases();
  } catch (err) {
    console.error("[DBOps] restores:", err);
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="8" class="table-empty">Restore history unavailable — ${escapeHtml(err.message)}</td></tr>`;
    }
    if (restoredDatabasesBody) {
      restoredDatabasesBody.innerHTML = `<tr><td colspan="4" class="table-empty">Restore history unavailable — ${escapeHtml(err.message)}</td></tr>`;
    }
  }
}

function renderRestoredDatabases() {
  const tbody = document.getElementById("restoredDatabasesBody");
  if (!tbody) return;
  const restored = state.restores.filter((r) => r.status === "completed" && r.mode === "clone"
    && r.restored_database_id && state.databases.some((db) => db.id === r.restored_database_id));
  const recordedDatabaseIds = new Set(restored.map((r) => r.restored_database_id).filter(Boolean));
  // Targets can outlive restore history (for example after metadata is reset).
  // Keep databases explicitly labeled as restored visible in this section too.
  const registeredRestores = state.databases
    .filter((db) => /\s\(restored\)$/i.test(db.name || "") && !recordedDatabaseIds.has(db.id))
    .map((db) => ({ db, databaseName: restoreDatabaseName(db) }));
  if (!restored.length) {
    if (!registeredRestores.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="table-empty">No restored databases yet. Completed clone restores will appear here.</td></tr>`;
      return;
    }
    tbody.innerHTML = registeredRestores.map(({ db, databaseName }) => `<tr>
      <td><strong>${escapeHtml(databaseName)}</strong></td>
      <td>Registered PostgreSQL target</td>
      <td><span class="badge badge-success">In Targets</span></td>
      <td class="col-action"><div><code class="restored-url-preview" title="Password is hidden">${escapeHtml(db.connection_url || "")}</code><div style="margin-top:6px;">${currentRole === "admin"
        ? `<button class="btn btn-secondary btn-sm" data-action="copy-registered-restored-url" data-id="${escapeHtml(db.id)}" type="button">Copy full URL</button>`
        : `<span class="text-muted" title="Connection URLs include credentials">Ask an administrator to copy the full URL</span>`}</div></div></td>
    </tr>`).join("");
    return;
  }
  tbody.innerHTML = restored.map((r) => {
    const db = state.databases.find((item) => item.id === r.restored_database_id);
    const destination = state.databases.find((item) => item.id === r.db_id);
    const urlAction = db
      ? `<div><code class="restored-url-preview" title="Password is hidden">${escapeHtml(db.connection_url || "")}</code><div style="margin-top:6px;">${currentRole === "admin"
        ? `<button class="btn btn-secondary btn-sm" data-action="copy-restored-url" data-id="${escapeHtml(r.restore_id)}" type="button">Copy full URL</button>`
        : `<span class="text-muted" title="Connection URLs include credentials">Ask an administrator to copy the full URL</span>`}</div></div>`
      : `<button class="btn btn-secondary btn-sm" data-action="register-restored-target" data-id="${escapeHtml(r.restore_id)}" type="button">Add to Targets</button>`;
    const monitoringAction = db
      ? `<span class="badge badge-success">In Targets</span><div style="margin-top:6px;"><button class="btn btn-danger-ghost btn-sm" data-action="unregister-restored-target" data-id="${escapeHtml(r.restore_id)}" type="button" title="Remove it from this app's Targets; keep the database and its data">Remove from Targets</button></div>`
      : `<span class="badge badge-warning">Not added</span>`;
    return `<tr>
      <td><strong>${escapeHtml(r.target_database)}</strong></td>
      <td>${escapeHtml(destination?.name || "Registered destination")}</td>
      <td>${monitoringAction}</td>
      <td class="col-action">${urlAction}</td>
    </tr>`;
  }).join("") + registeredRestores.map(({ db, databaseName }) => `<tr>
    <td><strong>${escapeHtml(databaseName)}</strong></td>
    <td>Registered PostgreSQL target</td>
    <td><span class="badge badge-success">In Targets</span></td>
    <td class="col-action"><div><code class="restored-url-preview" title="Password is hidden">${escapeHtml(db.connection_url || "")}</code><div style="margin-top:6px;">${currentRole === "admin"
      ? `<button class="btn btn-secondary btn-sm" data-action="copy-registered-restored-url" data-id="${escapeHtml(db.id)}" type="button">Copy full URL</button>`
      : `<span class="text-muted" title="Connection URLs include credentials">Ask an administrator to copy the full URL</span>`}</div></div></td>
  </tr>`).join("");
}

async function copyRegisteredRestoredUrl(dbId, button) {
  setBusy(button, true, "Copying…");
  try {
    const data = await api(`/api/v1/databases/${encodeURIComponent(dbId)}/connection-url`);
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(data.connection_url);
    else {
      const input = document.createElement("textarea");
      input.value = data.connection_url; input.style.position = "fixed"; input.style.opacity = "0";
      document.body.appendChild(input);
      try { input.select(); if (!document.execCommand("copy")) throw new Error("Clipboard access is unavailable."); }
      finally { input.remove(); }
    }
    showToast(`Full connection URL for ${data.database_name} copied. Share it using a secure channel.`, "success");
    logActivity("copy", "Copied restored database connection URL", data.database_name, dbId);
  } catch (err) {
    showToast(`Could not copy connection URL: ${err.message}`, "error");
  } finally {
    setBusy(button, false);
  }
}

async function copyRestoredDatabaseUrl(restoreId, button) {
  const restore = state.restores.find((item) => item.restore_id === restoreId);
  setBusy(button, true, "Copying…");
  try {
    const data = await api(`/api/v1/restores/${encodeURIComponent(restoreId)}/connection-url`);
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(data.connection_url);
    else {
      const input = document.createElement("textarea");
      input.value = data.connection_url; input.style.position = "fixed"; input.style.opacity = "0";
      document.body.appendChild(input);
      try {
        input.select();
        if (!document.execCommand("copy")) throw new Error("Clipboard access is unavailable.");
      } finally { input.remove(); }
    }
    showToast(`Connection URL for ${data.database_name} copied. Share it with the team using your approved secure channel.`, "success");
    logActivity("copy", "Copied restored database connection URL", data.database_name, restore?.db_id);
  } catch (err) {
    showToast(`Could not copy connection URL: ${err.message}`, "error");
  } finally {
    setBusy(button, false);
  }
}

async function openRestoreDialog(backupId, preferredSourceDbId) {
  // The main snapshot table is filtered to the active database. Fetch the full
  // archive list here so the restore selector can offer snapshots from any target.
  try {
    const data = await api("/api/v1/backups");
    state.restoreArchives = data.backups || [];
  } catch (err) {
    console.warn("[DBOps] Could not refresh restore archive list:", err);
    state.restoreArchives = state.cachedBackups || [];
  }
  const completed = state.restoreArchives.filter((b) => b.status === "completed");
  const preferredSource = preferredSourceDbId;
  if (!backupId && preferredSource && !completed.some((item) => item.db_id === preferredSource)) {
    const source = state.databases.find((item) => item.id === preferredSource);
    showToast(`No completed snapshots are available for ${source?.name || "the selected database"}.`, "warning");
    return;
  }
  const backup = (backupId && completed.find((b) => b.backup_id === backupId))
    || completed.find((b) => b.db_id === preferredSource)
    || completed[0];
  if (!backup) {
    showToast("No completed snapshot is available to restore. Take a snapshot first.", "warning");
    return;
  }

  const target = state.databases.find((d) => d.id === state.activeDatabaseId) || state.databases[0];
  state.restoreBackupId = backup.backup_id;
  const pgTargets = state.databases.filter((db) => ["postgres", "postgresql"].includes(String(db.engine || "").toLowerCase()));
  const sourceDbSelect = document.getElementById("restoreSourceDbSelect");
  if (sourceDbSelect) {
    const sourceIds = [...new Set(pgTargets.map((db) => db.id))];
    const availableSourceIds = new Set(completed.map((item) => item.db_id).filter(Boolean));
    sourceDbSelect.innerHTML = sourceIds.map((id) => {
      const db = state.databases.find((item) => item.id === id);
      const hasSnapshots = availableSourceIds.has(id);
      return `<option value="${escapeHtml(id)}" ${hasSnapshots ? "" : "disabled"}>${escapeHtml(db?.name || "Registered database")}${hasSnapshots ? "" : " (no completed snapshots)"}</option>`;
    }).join("") || `<option value="">No PostgreSQL database sources available</option>`;
    const firstAvailableSource = sourceIds.find((id) => availableSourceIds.has(id));
    sourceDbSelect.value = preferredSource && availableSourceIds.has(preferredSource)
      ? preferredSource
      : (availableSourceIds.has(backup.db_id) ? backup.db_id : firstAvailableSource || "");
  }

  const backupSelect = document.getElementById("restoreBackupSelect");
  if (backupSelect) {
    backupSelect.innerHTML = completed.length
      ? completed.map((item) => {
          const db = state.databases.find((entry) => entry.id === item.db_id);
          const targetName = db ? `${db.name} · ` : "";
          return `<option value="${escapeHtml(item.backup_id)}">${escapeHtml(targetName)}${escapeHtml(item.backup_id)} · ${escapeHtml(item.filename)} (${escapeHtml(item.size_human)})</option>`;
        }).join("")
      : `<option value="" disabled>No completed snapshots are available</option>`;
    backupSelect.value = backup.backup_id;
  }
  updateRestoreBackupSelection(backup.backup_id);
  renderRestoreBackupOptions(sourceDbSelect?.value || backup.db_id || "", backup.backup_id);

  const existingSelect = document.getElementById("restoreExistingDbId");
  if (existingSelect) {
    const existingTargets = pgTargets.filter((db) => {
      if (!(["postgres", "postgresql"].includes(String(db.engine || "").toLowerCase()))) return false;
      try {
        const dbName = decodeURIComponent(new URL(db.connection_url).pathname.split("/").filter(Boolean).pop() || "postgres").toLowerCase();
        return !["postgres", "template0", "template1"].includes(dbName);
      } catch (_) { return false; }
    });
    existingSelect.innerHTML = existingTargets.map((db) => {
      const databaseName = new URL(db.connection_url).pathname.split("/").filter(Boolean).pop() || "database";
      return `<option value="${escapeHtml(db.id)}">${escapeHtml(db.name)} — ${escapeHtml(databaseName)}</option>`;
    }).join("");
    existingSelect.value = existingTargets.some((db) => db.id === state.activeDatabaseId) ? state.activeDatabaseId : (existingTargets[0]?.id || "");
    const existingModeOption = document.querySelector('#restoreMode option[value="existing"]');
    if (existingModeOption) existingModeOption.disabled = existingTargets.length === 0;
  }
  const modeSelect = document.getElementById("restoreMode");
  if (modeSelect) modeSelect.value = "clone";

  const restoreTargetSelect = document.getElementById("restoreTargetDbId");
  if (restoreTargetSelect) {
    const usableTargets = pgTargets.filter((db) => !["postgres", "template0", "template1"].includes(restoreDatabaseName(db).toLowerCase()));
    restoreTargetSelect.innerHTML = usableTargets.map((db) => {
      let databaseName = "database";
      try { databaseName = decodeURIComponent(new URL(db.connection_url).pathname.split("/").filter(Boolean).pop() || "database"); } catch (_) {}
      return `<option value="${escapeHtml(db.id)}">${escapeHtml(db.name)} — ${escapeHtml(databaseName)}</option>`;
    }).join("") || `<option value="" disabled>No PostgreSQL targets are registered</option>`;
    restoreTargetSelect.value = usableTargets.some((db) => db.id === target?.id) ? target.id : (usableTargets[0]?.id || "");
    const existingModeOption = document.querySelector('#restoreMode option[value="existing"]');
    if (existingModeOption) existingModeOption.disabled = usableTargets.length === 0;
  }

  const label = document.getElementById("restoreBackupLabel");
  if (label) label.textContent = `${backup.backup_id} · ${backup.filename} (${backup.size_human})`;
  const targetLabel = document.getElementById("restoreTargetLabel");
  if (targetLabel) targetLabel.textContent = target ? `${target.name} — ${target.connection_url}` : "Primary Database";

  const newName = document.getElementById("restoreNewDbName");
  if (newName) newName.value = "";
  const confirm = document.getElementById("restoreConfirm");
  if (confirm) confirm.value = "";

  clearFieldError("restoreNewDbName");
  hideStatusEl(document.getElementById("restoreStatus"));
  toggleRestoreMode();

  openDialog("restoreDialog");
  setTimeout(() => { if (confirm) confirm.focus(); }, 60);
}

function renderRestoreBackupOptions(sourceDbId, preferredBackupId) {
  const select = document.getElementById("restoreBackupSelect");
  if (!select) return;
  const options = state.restoreArchives.filter((item) => item.status === "completed" && (!sourceDbId || item.db_id === sourceDbId));
  select.innerHTML = options.length
    ? options.map((item) => `<option value="${escapeHtml(item.backup_id)}">${escapeHtml(item.backup_id)} · ${escapeHtml(item.filename)} (${escapeHtml(item.size_human)})</option>`).join("")
    : `<option value="" disabled>No completed snapshots for this database</option>`;
  const selected = options.some((item) => item.backup_id === preferredBackupId) ? preferredBackupId : options[0]?.backup_id;
  if (selected) {
    select.value = selected;
    updateRestoreBackupSelection(selected);
  }
}

function updateRestoreBackupSelection(backupId) {
  const backup = state.restoreArchives.find((item) => item.backup_id === backupId && item.status === "completed")
    || state.cachedBackups.find((item) => item.backup_id === backupId && item.status === "completed");
  if (!backup) return;
  state.restoreBackupId = backup.backup_id;

  const sourceSelect = document.getElementById("restoreSourceServer");
  if (sourceSelect) {
    const options = (backup.remote_copies || []).map((copy) => {
      const server = state.remoteServers.find((item) => item.id === copy.server_id);
      return server ? `<option value="${escapeHtml(server.id)}">${escapeHtml(server.name)} (${escapeHtml(maskedSshAddress(server))})</option>` : "";
    }).join("");
    sourceSelect.innerHTML = `<option value="">Local or mounted repository</option>${options}`;
    sourceSelect.value = "";
  }
  toggleRestoreMode();
}

function restoreDatabaseName(db) {
  try { return decodeURIComponent(new URL(db.connection_url).pathname.split("/").filter(Boolean).pop() || "database"); }
  catch (_) { return "database"; }
}

function updateRestorePlan() {
  const backup = state.restoreArchives.find((item) => item.backup_id === state.restoreBackupId)
    || state.cachedBackups.find((item) => item.backup_id === state.restoreBackupId);
  const source = backup && state.databases.find((db) => db.id === backup.db_id);
  const mode = document.getElementById("restoreMode")?.value || "clone";
  const name = (document.getElementById("restoreNewDbName")?.value || "").trim();
  const selectedTargetId = document.getElementById("restoreTargetDbId")?.value;
  const target = state.databases.find((db) => db.id === selectedTargetId);
  const sourceText = source
    ? `${source.name} / ${restoreDatabaseName(source)} (snapshot ${backup.backup_id})`
    : `Unknown source (snapshot ${backup?.backup_id || "not selected"})`;
  let destinationText = "Select a destination";
  let effectText = "No database has been changed yet.";
  if (target && mode === "clone") {
    destinationText = `${target.name} / ${name || "new database name needed"}`;
    effectText = `Creates ${name || "a new database"} on ${target.name} and loads the snapshot into it. Existing databases, including the source, are left unchanged.`;
  } else if (target) {
    destinationText = target ? `${target.name} / ${restoreDatabaseName(target)}` : "Select an existing database";
    effectText = "Writes the snapshot into this selected database. It will be changed; the source is affected only if you select the source database here.";
  }
  const sourceEl = document.getElementById("restorePlanSource");
  const destinationEl = document.getElementById("restorePlanDestination");
  const effectEl = document.getElementById("restorePlanEffect");
  if (sourceEl) sourceEl.textContent = sourceText;
  if (destinationEl) destinationEl.textContent = destinationText;
  if (effectEl) effectEl.textContent = effectText;
}

function toggleRestoreMode() {
  const mode = document.getElementById("restoreMode");
  const source = document.getElementById("restoreSourceServer");
  const existingOption = document.querySelector('#restoreMode option[value="existing"]');
  const newGroup = document.getElementById("restoreNewDbGroup");
  const targetSelect = document.getElementById("restoreTargetDbId");
  const isRemote = !!(source && source.value);
  if (existingOption) existingOption.disabled = isRemote || !targetSelect?.options.length;
  if (isRemote && mode?.value === "existing") {
    mode.value = "clone";
    showToast("Remote snapshot restore is available only for a new database. Choose a local copy for an existing target.", "warning");
  }
  const isClone = !mode || mode.value !== "existing";
  const warnText = document.getElementById("restoreWarningText");
  const phrase = document.getElementById("restoreConfirmPhrase");
  const confirm = document.getElementById("restoreConfirm");

  if (newGroup) newGroup.hidden = !isClone;
  const targetLabel = document.getElementById("restoreTargetFieldLabel");
  const targetHint = document.getElementById("restoreTargetFieldHint");
  if (targetLabel) targetLabel.textContent = isClone ? "2. Destination PostgreSQL server" : "3. Existing database to receive the snapshot";
  if (targetHint) targetHint.textContent = isClone
    ? "Choose a registered PostgreSQL target. To restore into another existing database, choose “Write into an existing database” below; this list will show registered PostgreSQL databases you can select."
    : "Choose the exact registered PostgreSQL database to receive the snapshot. The snapshot source may be selected here too.";
  if (warnText) {
    warnText.textContent = isClone
      ? "Creates a separate database on the selected destination server and loads the snapshot into it. The source database and other existing databases are left unchanged."
      : "Drops and recreates the public schema in the selected database before restoring. All existing objects in that schema are removed, including objects absent from the snapshot. A failed restore rolls back the reset; make a separate backup first.";
  }
  if (phrase) phrase.textContent = isClone ? "RESTORE" : "RESTORE EXISTING";
  if (confirm) confirm.placeholder = isClone ? "RESTORE" : "RESTORE EXISTING";
  updateRestorePlan();
}

async function submitRestore() {
  const btn = document.getElementById("startRestoreBtn");
  const confirmVal = (document.getElementById("restoreConfirm").value || "").trim();
  const mode = document.getElementById("restoreMode")?.value || "clone";
  const expectedConfirmation = mode === "existing" ? "RESTORE EXISTING" : "RESTORE";
  const statusEl = document.getElementById("restoreStatus");
  clearFieldError("restoreNewDbName");

  if (confirmVal !== expectedConfirmation) {
    setStatusEl(statusEl, `Type ${expectedConfirmation} exactly to confirm the operation.`, "err");
    return;
  }
  let newDbName = null;
  let targetDbId = document.getElementById("restoreTargetDbId")?.value || state.activeDatabaseId;
  if (mode === "clone") {
    newDbName = (document.getElementById("restoreNewDbName").value || "").trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]{0,62}$/.test(newDbName)) {
      showFieldError("restoreNewDbName", "Enter a valid new database name.");
      setStatusEl(statusEl, "Provide a valid new database name.", "err");
      return;
    }
  } else {
    if (!targetDbId) {
      setStatusEl(statusEl, "Select the registered PostgreSQL database to change.", "err");
      return;
    }
  }

  setBusy(btn, true, "Restoring…");
  setStatusEl(statusEl, mode === "clone" ? "Cloning snapshot into a new database…" : "Applying snapshot to the selected existing database…", "busy");
  state.backupBusy = true;
  computeSystemStatus();

  try {
    const data = await api("/api/v1/restores", {
      method: "POST",
      body: JSON.stringify({
        backup_id: state.restoreBackupId,
        db_id: targetDbId,
        mode,
        new_db_name: newDbName || undefined,
        confirm: confirmVal,
        source_server_id: document.getElementById("restoreSourceServer").value || undefined,
      }),
    });
    if (data.status === "completed") {
      if (mode === "clone") await fetchRegisteredDatabases();
      setStatusEl(statusEl, data.message || "Restore completed.", "ok");
      showToast(data.message || "Restore completed successfully.", "success");
      logActivity("restore", "Restore completed", `${data.backup_id} → ${data.target_database} (${mode})`, targetDbId);
    } else {
      setStatusEl(statusEl, data.message || data.error || "Restore failed.", "err");
      showToast(data.message || "Restore failed.", "error");
      logActivity("error", "Restore failed", `${data.backup_id || ""} · ${data.error || ""}`, targetDbId);
    }
    await fetchRestores();
  } catch (err) {
    setStatusEl(statusEl, `Restore failed: ${err.message}`, "err");
    showToast(`Restore failed: ${err.message}`, "error");
    logActivity("error", "Restore error", err.message, targetDbId);
  } finally {
    state.backupBusy = false;
    computeSystemStatus();
    setBusy(btn, false);
  }
}

function showRestoreDetail(restoreId) {
  const r = state.restores.find((x) => x.restore_id === restoreId);
  if (!r) return;
  const detail = r.status === "completed"
    ? (r.message || "Restore completed successfully.")
    : (r.error || r.message || "No additional detail.");
  showToast(`Restore ${r.restore_id}: ${detail}`, r.status === "failed" ? "error" : "info", { timeout: 9000 });
}

async function registerRestoredTarget(restoreId, button) {
  setBusy(button, true, "Adding…");
  try {
    const data = await api(`/api/v1/restores/${encodeURIComponent(restoreId)}/register-target`, { method: "POST" });
    await fetchRegisteredDatabases();
    await fetchRestores();
    signalDatabaseRegistryChange();
    const target = state.databases.find((db) => db.id === data.restored_database_id);
    showToast(target ? `${target.name} is now available in Console Targets.` : "Restored database added to Console Targets.", "success");
    logActivity("restore", "Restored database added to Targets", data.target_database, data.restored_database_id);
  } catch (err) {
    showToast(`Could not add restored database to Targets: ${err.message}`, "error");
    setBusy(button, false);
  }
}

async function unregisterRestoredTarget(restoreId, button) {
  const restore = state.restores.find((item) => item.restore_id === restoreId);
  if (!restore) return;
  const ok = await confirmAction({
    title: "Remove restored database from Targets?",
    message: `This stops monitoring “${restore.target_database}” and removes it from this app's Targets list. The database and all its data will remain on ${state.databases.find((db) => db.id === restore.db_id)?.name || "the destination server"}.`,
    confirmText: "Remove from Targets",
  });
  if (!ok) return;
  setBusy(button, true, "Removing…");
  try {
    await api(`/api/v1/restores/${encodeURIComponent(restoreId)}/target`, { method: "DELETE" });
    await fetchRegisteredDatabases();
    await fetchRestores();
    signalDatabaseRegistryChange();
    showToast(`${restore.target_database} removed from Targets; its database and data are unchanged.`, "success");
    logActivity("remove", "Removed restored database from Targets", restore.target_database, restore.db_id);
  } catch (err) {
    showToast(`Could not remove restored database from Targets: ${err.message}`, "error");
    setBusy(button, false);
  }
}

/* -------------------------------------------------------------------------
   Telegram notifications
   ------------------------------------------------------------------------- */
const NOTIFY_EVENTS = [
  ["backup_success", "Backup completed"],
  ["backup_failure", "Backup failed"],
  ["restore_started", "Restore started"],
  ["restore_success", "Restore completed"],
  ["restore_failure", "Restore failed"],
  ["migration_detected", "New migration detected"],
  ["alert", "Health alerts"],
  ["health", "Routine health"],
  ["test", "Test messages"],
];

function renderNotifyEvents(events) {
  const wrap = document.getElementById("notifyEvents");
  if (!wrap) return;
  wrap.innerHTML = NOTIFY_EVENTS.map(([key, label]) => `
    <label class="notify-event">
      <input type="checkbox" data-event="${key}" ${events[key] ? "checked" : ""}>
      <span>${escapeHtml(label)}</span>
    </label>`).join("");
}

function collectNotifyEvents() {
  const result = {};
  document.querySelectorAll("#notifyEvents input[data-event]").forEach((cb) => {
    result[cb.dataset.event] = cb.checked;
  });
  return result;
}

function openNotificationsDialog() {
  openDialog("notificationsDialog");
  loadNotificationSettings();
}

async function loadNotificationSettings() {
  const statusEl = document.getElementById("notifyStatus");
  hideStatusEl(statusEl);
  try {
    const cfg = await api("/api/v1/notifications/config");
    document.getElementById("notifyEnabled").value = cfg.enabled ? "true" : "false";
    document.getElementById("notifySilent").value = cfg.silent ? "true" : "false";
    document.getElementById("notifyChatId").value = cfg.chat_id || "";
    const recipientHint = document.getElementById("notifyTelegramRecipients");
    if (recipientHint) {
      const recipients = cfg.telegram_recipients || [];
      recipientHint.innerHTML = recipients.length
        ? recipients.map((id) => `<li><code>${escapeHtml(id)}</code></li>`).join("")
        : '<li class="muted">No users have registered with /start yet.</li>';
    }
    const tokenInput = document.getElementById("notifyBotToken");
    tokenInput.value = "";
    tokenInput.placeholder = cfg.bot_token_masked ? `Saved: ${cfg.bot_token_masked}` : "123456789:AA…";
    document.getElementById("notifyTokenHint").textContent = cfg.telegram_configured
      ? "A token is stored server-side. Leave blank to keep it unchanged."
      : "Stored server-side. The saved token is shown masked.";
    renderNotifyEvents(cfg.events || {});
    setHeroChip("heroNotify", cfg.enabled && cfg.telegram_configured ? "Configured" : (cfg.enabled ? "No token" : "Off"));
  } catch (err) {
    setStatusEl(statusEl, `Could not load settings: ${err.message}`, "err");
  }
}

async function saveNotificationSettings() {
  const btn = document.getElementById("saveNotifyBtn");
  const statusEl = document.getElementById("notifyStatus");
  setBusy(btn, true, "Saving…");
  hideStatusEl(statusEl);
  try {
    const payload = {
      enabled: document.getElementById("notifyEnabled").value === "true",
      silent: document.getElementById("notifySilent").value === "true",
      chat_id: document.getElementById("notifyChatId").value.trim(),
      events: collectNotifyEvents(),
    };
    const token = document.getElementById("notifyBotToken").value.trim();
    if (token) payload.bot_token = token;

    const cfg = await api("/api/v1/notifications/config", { method: "POST", body: JSON.stringify(payload) });
    setStatusEl(statusEl, cfg.message || "Settings saved.", "ok");
    showToast("Telegram notification settings saved.", "success");
    logActivity("notify", "Notification settings saved", cfg.enabled ? "Delivery enabled" : "Delivery disabled");
    document.getElementById("notifyBotToken").value = "";
    document.getElementById("notifyBotToken").placeholder = cfg.bot_token_masked ? `Saved: ${cfg.bot_token_masked}` : "123456789:AA…";
    setHeroChip("heroNotify", cfg.enabled && cfg.telegram_configured ? "Configured" : (cfg.enabled ? "No token" : "Off"));
  } catch (err) {
    setStatusEl(statusEl, `Save failed: ${err.message}`, "err");
    showToast(`Could not save notifications: ${err.message}`, "error");
  } finally {
    setBusy(btn, false);
  }
}

async function sendTestNotification() {
  const btn = document.querySelector('[data-action="test-notification"]');
  const statusEl = document.getElementById("notifyStatus");
  setBusy(btn, true, "Sending…");
  setStatusEl(statusEl, "Dispatching a test message to Telegram…", "busy");
  try {
    const res = await api("/api/v1/notifications/test", {
      method: "POST",
      body: JSON.stringify({ message: "Operator initiated a connectivity test from the dashboard." }),
    });
    if (res.success) {
      setStatusEl(statusEl, "Test message delivered to Telegram.", "ok");
      showToast("Test notification sent.", "success");
      logActivity("notify", "Test notification sent", res.status || "sent");
    } else {
      setStatusEl(statusEl, `Test failed: ${res.details && res.details.message ? res.details.message : res.status}`, "err");
      showToast(`Test failed: ${res.status}`, "error");
    }
  } catch (err) {
    setStatusEl(statusEl, `Test failed: ${err.message}`, "err");
    showToast(`Test failed: ${err.message}`, "error");
  } finally {
    setBusy(btn, false);
  }
}

async function fetchLocks() {
  try {
    const data = await api(`/api/v1/diagnostics/locks?db_id=${encodeURIComponent(state.activeDatabaseId)}`);
    const badge = document.getElementById("locksCountBadge");
    const container = document.getElementById("locksContent");
    if (!container) return;

    if (!data.total_waiting_locks || data.total_waiting_locks === 0) {
      if (badge) { badge.className = "badge badge-success"; badge.textContent = "0 Waiting Locks"; }
      container.innerHTML = `<div class="empty-ok">${icon("check", "ico-sm")} All lock queues clear. No transactions are waiting on locks.</div>`;
    } else {
      if (badge) { badge.className = "badge badge-danger"; badge.textContent = `${data.total_waiting_locks} Lock Conflicts`; }
      container.innerHTML = `
        <div class="table-responsive">
          <table>
            <thead><tr><th>Blocked PID</th><th>Statement</th><th>Blocking PID</th><th>Duration</th></tr></thead>
            <tbody>
              ${data.locks.map((l) => `
                <tr>
                  <td><code>${escapeHtml(l.blocked_pid)}</code></td>
                  <td class="truncate" style="max-width:180px;">${escapeHtml(l.blocked_statement || "--")}</td>
                  <td><strong style="color:var(--bad);">${escapeHtml(l.blocking_pid)}</strong></td>
                  <td>${escapeHtml(l.waiting_duration_seconds)}s</td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>`;
    }
  } catch (err) {
    console.error("[DBOps] locks:", err);
    const badge = document.getElementById("locksCountBadge");
    if (badge) { badge.className = "badge badge-warning"; badge.textContent = "Lock data unavailable"; }
  }
}

/* -------------------------------------------------------------------------
   Backup / schedule dialogs
   ------------------------------------------------------------------------- */
function openCreateBackupDialog() {
  updateActiveToolbar();
  const target = state.databases.find((db) => db.id === state.activeDatabaseId) || state.databases[0];
  if (!target || !["postgres", "postgresql"].includes(String(target.engine || "").toLowerCase())) {
    showToast("Snapshot creation currently supports PostgreSQL targets only.", "warning");
    return;
  }
  fetchRemoteServers();
  openDialog("createBackupDialog");
}

async function fetchRemoteServers() {
  const cards = document.getElementById("serverCards");
  try {
    const data = await api("/api/v1/servers");
    state.remoteServers = data.servers || [];
    if (cards) cards.innerHTML = state.remoteServers.length ? state.remoteServers.map((server) => `
      <article class="server-card" id="server-card-${escapeHtml(server.id)}">
        <div class="server-card-head"><div class="server-identity"><strong>${escapeHtml(server.name)}</strong><button class="btn btn-secondary btn-sm" data-action="edit-server-name" data-id="${escapeHtml(server.id)}" type="button" aria-label="Edit server name">Edit name</button><button class="btn btn-secondary btn-sm" data-action="edit-server-paths" data-id="${escapeHtml(server.id)}" type="button" aria-label="Edit server storage directories">Edit directories</button><span class="badge ${server.status === "online" ? "badge-success" : "badge-warning"}">${escapeHtml(server.status)}</span><span class="server-ssh">SSH ${escapeHtml(maskedSshAddress(server))}</span></div></div>
        <div class="server-paths"><small>Database: <code>${escapeHtml(server.database_root)}</code></small><small>Snapshots: <code>${escapeHtml(server.backup_root)}</code></small></div>
        <div class="server-managed-containers" id="managed-containers-${escapeHtml(server.id)}"><small>Loading DBOps containers…</small></div>
        <div class="server-health" id="server-health-${escapeHtml(server.id)}"><span class="spinner"></span> Checking server health…</div>
      </article>`).join("") : `<div class="table-empty">No remote servers registered. Add a Linux Docker host to begin.</div>`;
    const backupList = document.getElementById("backupRemoteServers");
    if (backupList) backupList.innerHTML = state.remoteServers.length ? state.remoteServers.map((server) => `<label class="server-check-row"><input type="checkbox" value="${escapeHtml(server.id)}"> <span>${escapeHtml(server.name)} <small>SSH ${escapeHtml(maskedSshAddress(server))}</small></span></label>`).join("") : "No servers registered.";
    await Promise.allSettled(state.remoteServers.flatMap((server) => [refreshServerHealth(server.id), refreshManagedContainers(server.id)]));
  } catch (err) {
    if (cards) cards.innerHTML = `<div class="table-empty">Could not load server list: ${escapeHtml(err.message)}</div>`;
  }
}

async function refreshManagedContainers(serverId) {
  const panel = document.getElementById(`managed-containers-${serverId}`);
  if (!panel) return;
  try {
    const data = await api(`/api/v1/servers/${encodeURIComponent(serverId)}/containers`);
    const containers = data.containers || [];
    panel.innerHTML = containers.length ? `<strong>DBOps containers</strong>${containers.map((container) => `<div class="managed-container-row"><code>${escapeHtml(container.container_name)}</code><small>${escapeHtml(container.status)}</small><button class="btn btn-sm btn-danger-ghost" data-action="delete-managed-container" data-id="${escapeHtml(serverId)}" data-container="${escapeHtml(container.container_name)}" type="button">Delete</button></div>`).join("")}` : "<small>No DBOps-created containers.</small>";
  } catch (err) {
    panel.innerHTML = `<small class="server-health-error">Could not load containers: ${escapeHtml(err.message)}</small>`;
  }
}

async function deleteManagedContainer(serverId, containerName) {
  const ok = await confirmAction({
    title: "Delete PostgreSQL container",
    message: `Force remove the DBOps container “${containerName}”? Its database files under the configured data root will be kept. Other containers and databases will not be changed.`,
    confirmText: "Delete Container",
  });
  if (!ok) return;
  try {
    await api(`/api/v1/servers/${encodeURIComponent(serverId)}/containers/${encodeURIComponent(containerName)}`, { method: "DELETE" });
    showToast(`Container ${containerName} removed. Its data directory was kept.`, "success");
    logActivity("server", "Deleted DBOps PostgreSQL container", containerName);
    await Promise.all([refreshManagedContainers(serverId), fetchRegisteredDatabases()]);
  } catch (err) {
    showToast(`Could not delete container: ${err.message}`, "error");
  }
}

function formatServerBytes(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(value), unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function serverUsagePie(label, percentage, detail) {
  if (percentage == null || !Number.isFinite(Number(percentage))) {
    return `<div class="server-usage-item"><div class="server-usage-pie server-usage-pie-empty" role="img" aria-label="${escapeHtml(label)} usage unavailable"><span>—</span></div><div><strong>${escapeHtml(label)}</strong><small>${escapeHtml(detail || "Unavailable")}</small></div></div>`;
  }
  const used = Math.max(0, Math.min(100, Number(percentage)));
  return `<div class="server-usage-item"><div class="server-usage-pie" role="img" aria-label="${escapeHtml(label)} ${Math.round(used)} percent used" style="--usage:${used}%"><span>${Math.round(used)}%</span></div><div><strong>${escapeHtml(label)}</strong><small>${escapeHtml(detail || `${Math.round(used)}% used`)}</small></div></div>`;
}

function serverHealthActions(serverId) {
  const safeId = escapeHtml(serverId);
  return `<div class="server-health-actions">
    <button class="btn btn-secondary btn-sm" data-action="refresh-server-health" data-id="${safeId}" type="button">Refresh Health</button>
    <button class="btn btn-secondary btn-sm" data-action="open-provision-db" data-id="${safeId}" type="button">Create Database Container</button>
    <button class="btn btn-sm btn-danger-ghost" data-action="remove-server-registration" data-id="${safeId}" type="button">Remove Server</button>
  </div>`;
}

async function refreshServerHealth(serverId) {
  const panel = document.getElementById(`server-health-${serverId}`);
  if (!panel || serverHealthRequests.has(serverId)) return;
  serverHealthRequests.add(serverId);
  const hasCurrentMetrics = Boolean(panel.querySelector(".server-health-content"));
  panel.setAttribute("aria-busy", "true");
  if (hasCurrentMetrics) {
    const head = panel.querySelector(".server-health-head");
    if (head) {
      const updating = document.createElement("small");
      updating.className = "server-health-refreshing";
      updating.textContent = "Refreshing…";
      updating.setAttribute("aria-live", "polite");
      const badge = head.querySelector(".badge");
      head.insertBefore(updating, badge || null);
    }
  } else {
    panel.innerHTML = `<div class="server-health-loading"><span class="spinner"></span> Checking server health…</div>${serverHealthActions(serverId)}`;
  }
  try {
    const health = await api(`/api/v1/servers/${encodeURIComponent(serverId)}/health`);
    const statusLabel = health.status === "healthy" ? "Healthy" : health.status === "degraded" ? "Degraded" : "Unreachable";
    const memoryUsed = health.memory_total_bytes != null && health.memory_available_bytes != null
      ? formatServerBytes(health.memory_total_bytes - health.memory_available_bytes) : "—";
    const memoryTotal = formatServerBytes(health.memory_total_bytes);
    const memoryPercent = health.memory_total_bytes > 0 && health.memory_available_bytes != null
      ? ((health.memory_total_bytes - health.memory_available_bytes) / health.memory_total_bytes) * 100 : null;
    const load = (health.load_average || []).map((item) => Number(item).toFixed(2)).join(" / ") || "—";
    const disk = health.disk_used_percent == null ? "—" : `${health.disk_used_percent}% used`;
    const containers = health.managed_containers_running == null ? "—" : `${health.managed_containers_running} running · ${health.managed_containers_stopped} stopped`;
    panel.innerHTML = `<div class="server-health-head"><strong>Health</strong><span class="badge ${health.status === "healthy" ? "badge-success" : health.status === "degraded" ? "badge-warning" : "badge-danger"}">${statusLabel}</span></div>
      <div class="server-health-content"><div class="server-health-grid">
      <div class="server-health-metric"><span>Docker</span><strong>${escapeHtml(health.docker_version || (health.ssh_connected ? "Unavailable" : "Offline"))}</strong></div>
      <div class="server-health-metric"><span>Memory</span><strong>${escapeHtml(memoryUsed)} / ${escapeHtml(memoryTotal)}</strong></div>
      <div class="server-health-metric"><span>Disk (${escapeHtml(state.remoteServers.find((server) => server.id === serverId)?.database_root || "database path")})</span><strong>${escapeHtml(disk)}</strong></div>
      <div class="server-health-metric"><span>Load average</span><strong>${escapeHtml(load)}</strong></div>
      <div class="server-health-metric"><span>DBOps containers</span><strong>${escapeHtml(containers)}</strong></div></div>
      <div class="server-usage-charts" aria-label="Server resource usage">
        ${serverUsagePie("Memory", memoryPercent, `${memoryUsed} / ${memoryTotal}`)}
        ${serverUsagePie("Disk", health.disk_used_percent, disk)}
      </div></div>
      <div class="server-health-footer">${health.error ? `<small class="server-health-error">${escapeHtml(health.error)}</small>` : `<small>Checked ${escapeHtml(new Date(health.checked_at).toLocaleTimeString())}</small>`}</div>
      ${serverHealthActions(serverId)}`;
  } catch (err) {
    if (hasCurrentMetrics) {
      const updating = panel.querySelector(".server-health-refreshing");
      if (updating) updating.remove();
      const footer = panel.querySelector(".server-health-footer");
      if (footer) {
        footer.textContent = "";
        const warning = document.createElement("small");
        warning.className = "server-health-error";
        warning.textContent = `Refresh failed; showing previous metrics. ${err.message}`;
        footer.appendChild(warning);
      }
    } else {
      panel.innerHTML = `<div class="server-health-head"><strong>Health</strong><span class="badge badge-danger">Unavailable</span></div><div class="server-health-footer"><small class="server-health-error">${escapeHtml(err.message)}</small></div>${serverHealthActions(serverId)}`;
    }
  } finally {
    panel.removeAttribute("aria-busy");
    const updating = panel.querySelector(".server-health-refreshing");
    if (updating) updating.remove();
    serverHealthRequests.delete(serverId);
  }
}

async function removeServerRegistration(serverId) {
  const server = state.remoteServers.find((item) => item.id === serverId);
  if (!server) return;
  const ok = await confirmAction({
    title: "Remove server registration",
    message: `Remove ${server.name} from this app? This only removes its registration here. The remote server, containers, databases, files, and snapshots will not be changed.`,
    confirmText: "Remove Server",
  });
  if (!ok) return;
  try {
    await api(`/api/v1/servers/${encodeURIComponent(serverId)}`, { method: "DELETE" });
    state.remoteServers = state.remoteServers.filter((item) => item.id !== serverId);
    serverHealthRequests.delete(serverId);
    showToast("Server registration removed. The remote server was not changed.", "success");
    await fetchRemoteServers();
  } catch (err) {
    showToast(`Could not remove server registration: ${err.message}`, "error");
  }
}

async function editServerName(serverId) {
  const server = state.remoteServers.find((item) => item.id === serverId);
  if (!server) return;
  const entered = window.prompt("Display name for this server:", server.name);
  if (entered === null) return;
  const name = entered.trim();
  if (!name || name.length > 120) {
    showToast("Enter a server name between 1 and 120 characters.", "warning");
    return;
  }
  try {
    await api(`/api/v1/servers/${encodeURIComponent(serverId)}/name`, { method: "PATCH", body: JSON.stringify({ name }) });
    showToast("Server display name updated.", "success");
    await fetchRemoteServers();
  } catch (err) {
    showToast(`Could not update server name: ${err.message}`, "error");
  }
}

async function editServerPaths(serverId) {
  const server = state.remoteServers.find((item) => item.id === serverId);
  if (!server) return;
  const databaseRoot = window.prompt("Database data directory on this server:", server.database_root);
  if (databaseRoot === null) return;
  const backupRoot = window.prompt("Snapshot directory on this server:", server.backup_root);
  if (backupRoot === null) return;
  try {
    await api(`/api/v1/servers/${encodeURIComponent(serverId)}/paths`, {
      method: "PATCH",
      body: JSON.stringify({ database_root: databaseRoot.trim(), backup_root: backupRoot.trim() }),
    });
    showToast("Server directories updated.", "success");
    logActivity("server", "Updated server storage directories", server.name);
    await fetchRemoteServers();
  } catch (err) {
    showToast(`Could not update server directories: ${err.message}`, "error");
  }
}

async function registerRemoteServer() {
  const btn = document.querySelector('[data-action="register-server"]');
  setBusy(btn, true, "Checking SSH…");
  try {
    await api("/api/v1/servers", { method: "POST", body: JSON.stringify({
      name: document.getElementById("serverName").value.trim(),
      host: document.getElementById("serverHost").value.trim(),
      ssh_port: Number(document.getElementById("serverSshPort").value),
      username: document.getElementById("serverUsername").value.trim(),
      password: document.getElementById("serverPassword").value,
      database_root: document.getElementById("serverDatabaseRoot").value.trim(),
      backup_root: document.getElementById("serverBackupRoot").value.trim(),
    }) });
    document.getElementById("serverPassword").value = "";
    closeDialog("serverDialog");
    showToast("Remote server verified and registered.", "success");
    logActivity("server", "Registered remote server", document.getElementById("serverName").value.trim());
    await fetchRemoteServers();
  } catch (err) { showToast(`Server registration failed: ${err.message}`, "error"); }
  finally { setBusy(btn, false); }
}

function openProvisionDbDialog(serverId) {
  const server = state.remoteServers.find((item) => item.id === serverId);
  if (!server) return;
  state.provisionServerId = serverId;
  const engineSelect = document.getElementById("provisionEngine");
  const engineDefaults = { postgres: ["postgres:16-alpine", 55432, "dbops-postgres"], mongodb: ["mongo:7", 57017, "dbops-mongodb"], mysql: ["mysql:8", 53306, "dbops-mysql"], redis: ["redis:7-alpine", 56379, "dbops-redis"] };
  const updateEngine = () => {
    const [image, port, container] = engineDefaults[engineSelect.value] || engineDefaults.postgres;
    document.getElementById("provisionImage").value = image;
    document.getElementById("provisionPort").value = port;
    document.getElementById("provisionContainerName").value = container;
    document.getElementById("provisionDbTitle").textContent = `Create Isolated ${engineSelect.selectedOptions[0].text} Container`;
    document.getElementById("provisionImageArchive").closest(".form-group").hidden = engineSelect.value !== "postgres";
  };
  engineSelect.onchange = updateEngine;
  updateEngine();
  document.getElementById("provisionServerLabel").textContent = `${server.name} (${maskedSshAddress(server)}) · creates a new container and data directory only`;
  openDialog("provisionDbDialog");
}

async function provisionPostgresContainer() {
  const engine = document.getElementById("provisionEngine").value;
  const name = document.getElementById("provisionName").value.trim();
  const containerName = document.getElementById("provisionContainerName").value.trim();
  const databaseName = document.getElementById("provisionDbName").value.trim();
  const databaseUser = document.getElementById("provisionDbUser").value.trim();
  const databasePassword = document.getElementById("provisionDbPassword").value;
  const image = document.getElementById("provisionImage").value.trim();
  const imagePatterns = { postgres: /^postgres:(1[4-9]|[2-9][0-9])(-alpine)?$/, mongodb: /^mongo:(6|7|8)(\.\d+)?(-[a-z0-9.-]+)?$/, mysql: /^(mysql:8(\.\d+)?|mariadb:10\.\d+|mariadb:11\.\d+)(-[a-z0-9.-]+)?$/, redis: /^redis:([7-9]|[1-9][0-9])(\.\d+)?(-[a-z0-9.-]+)?$/ };
  if (!state.provisionServerId || !name || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/.test(containerName) || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(databaseName) || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(databaseUser) || databasePassword.length < 16 || !imagePatterns[engine]?.test(image)) {
    showToast("Provide valid names, a supported image for the selected engine, and a database password of at least 16 characters.", "warning"); return;
  }
  const btn = document.querySelector('[data-action="provision-database"]');
  setBusy(btn, true, "Creating container…");
  try {
    clearServerOperationOutput();
    setServerOperationStatus("Connecting", "badge-info");
    appendServerOperationLine(`Starting provision for ${name} using ${image}`, "info");
    const response = await fetch(`/api/v1/servers/${encodeURIComponent(state.provisionServerId)}/containers/${engine}/stream`, {
      method: "POST", headers: getHeaders(), body: JSON.stringify({
      server_id: state.provisionServerId, engine, name, container_name: containerName, database_name: databaseName, database_user: databaseUser,
      database_password: databasePassword, host_port: Number(document.getElementById("provisionPort").value), image,
      }),
    });
    if (!response.ok) {
      const problem = await response.json().catch(() => ({}));
      throw new Error(problem.detail || `Request failed (${response.status})`);
    }
    document.getElementById("provisionDbPassword").value = "";
    closeDialog("provisionDbDialog");
    if (!response.body) throw new Error("This browser does not support live server output.");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let completed = false;
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
        const line = frame.split("\n").find((part) => part.startsWith("data: "));
        if (!line) continue;
        const event = JSON.parse(line.slice(6));
        if (event.type === "progress") appendServerOperationLine(event.message, "info");
        if (event.type === "error") throw new Error(event.message);
        if (event.type === "complete") {
          completed = true;
          const data = event.result;
          setServerOperationStatus("Complete", "badge-success");
          appendServerOperationLine(`Database registered: ${data.database_name}`, "success");
          state.activeDatabaseId = data.database_id;
          showToast(`New database container registered (${data.status}) and selected.`, data.status === "online" ? "success" : "warning", { timeout: 9000 });
          logActivity("server", "Created isolated PostgreSQL container", `${data.container_name} · ${data.data_directory}`);
          await fetchRegisteredDatabases();
          signalDatabaseRegistryChange();
          await refreshDashboard();
        }
      }
      if (done) break;
    }
    if (!completed) throw new Error("The server closed the live output stream before provisioning completed.");
  } catch (err) {
    setServerOperationStatus("Failed", "badge-danger");
    appendServerOperationLine(err.message, "error");
    showToast(`Container creation failed: ${err.message}`, "error", { timeout: 10000 });
  }
  finally { setBusy(btn, false); }
}

function setServerOperationStatus(label, badgeClass) {
  const status = document.getElementById("serverOperationStatus");
  if (!status) return;
  status.textContent = label;
  status.className = `badge ${badgeClass}`;
}

function appendServerOperationLine(message, kind = "info") {
  const log = document.getElementById("serverOperationLog");
  if (!log) return;
  const line = document.createElement("div");
  line.className = `server-log-line ${kind}`;
  const timeLabel = new Date().toLocaleTimeString();
  line.textContent = `[${timeLabel}] ${String(message)}`;
  log.appendChild(line);
  while (log.children.length > 300) log.firstElementChild.remove();
  log.scrollTop = log.scrollHeight;
}

function clearServerOperationOutput() {
  showServerOperationOutput();
  const log = document.getElementById("serverOperationLog");
  if (log) log.replaceChildren();
}

function showServerOperationOutput() {
  const panel = document.getElementById("serverLiveConsole");
  if (panel) panel.hidden = false;
  panel?.parentElement?.classList.add("has-live-output");
}

function closeServerOperationOutput() {
  const panel = document.getElementById("serverLiveConsole");
  if (panel) panel.hidden = true;
  panel?.parentElement?.classList.remove("has-live-output");
}

async function uploadPostgresImageArchive() {
  const fileInput = document.getElementById("provisionImageArchive");
  const file = fileInput?.files?.[0];
  const image = document.getElementById("provisionImage")?.value.trim();
  if (!state.provisionServerId || !file) { showToast("Choose a Docker image archive first.", "warning"); return; }
  if (file.size > 2 * 1024 * 1024 * 1024) { showToast("The image archive exceeds the 2 GiB upload limit.", "warning"); return; }
  if (!/^postgres:(1[4-9]|[2-9][0-9])(-alpine)?$/.test(image)) { showToast("Enter a supported PostgreSQL image tag that matches the archive.", "warning"); return; }
  const button = document.querySelector('[data-action="upload-postgres-image"]');
  setBusy(button, true, "Uploading image…");
  clearServerOperationOutput();
  setServerOperationStatus("Uploading", "badge-info");
  appendServerOperationLine(`Uploading ${file.name} and loading ${image} on the remote server`, "info");
  try {
    const body = new FormData();
    body.append("archive", file, file.name);
    const query = new URLSearchParams({ image });
    const data = await api(`/api/v1/servers/${encodeURIComponent(state.provisionServerId)}/images/postgres?${query}`, { method: "POST", body });
    setServerOperationStatus("Complete", "badge-success");
    appendServerOperationLine(`Image ${data.image} loaded on the remote server`, "success");
    showToast(`Loaded ${data.image} on the remote server. You can now create the container.`, "success", { timeout: 8000 });
    fileInput.value = "";
  } catch (err) { setServerOperationStatus("Failed", "badge-danger"); appendServerOperationLine(err.message, "error"); showToast(`Image upload failed: ${err.message}`, "error", { timeout: 10000 }); }
  finally { setBusy(button, false); }
}

const scheduleWeekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

function sriLankaScheduleTime(utcTime, weekday = null) {
  const [hour, minute] = (utcTime || "02:00").split(":").map(Number);
  const weekdayOffset = weekday ? scheduleWeekdays.indexOf(weekday) : 0;
  const date = new Date(Date.UTC(2024, 0, 1 + Math.max(weekdayOffset, 0), hour || 0, minute || 0));
  const time = new Intl.DateTimeFormat("en-US", { timeZone: "Asia/Colombo", hour: "numeric", minute: "2-digit", hour12: true }).format(date);
  if (!weekday) return time;
  const day = new Intl.DateTimeFormat("en-US", { timeZone: "Asia/Colombo", weekday: "long" }).format(date);
  return `${day} ${time}`;
}

function formatScheduleNextRun(value) {
  if (!value) return "";
  const date = new Date(value);
  const utc = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", dateStyle: "medium", timeStyle: "short", hour12: false }).format(date);
  const slt = new Intl.DateTimeFormat("en-US", { timeZone: "Asia/Colombo", dateStyle: "medium", timeStyle: "short", hour12: true }).format(date);
  return `${utc} UTC · ${slt} Sri Lanka time`;
}

function scheduleDescription() {
  const frequency = document.getElementById("scheduleFrequency").value;
  const time = document.getElementById("scheduleTime").value || "02:00";
  let timing;
  if (frequency === "current") timing = "The current custom schedule will be kept.";
  else if (frequency === "weekly") {
    const day = document.getElementById("scheduleWeekday");
    timing = `Snapshots will run every ${day.options[day.selectedIndex].text} at ${time} UTC (Sri Lanka time: ${sriLankaScheduleTime(time, day.value)}).`;
  } else if (frequency === "monthly") {
    timing = `Snapshots will run on day ${document.getElementById("scheduleMonthDay").value} of each month at ${time} UTC (Sri Lanka time: ${sriLankaScheduleTime(time)}).`;
  } else timing = `Snapshots will run every day at ${time} UTC (Sri Lanka time: ${sriLankaScheduleTime(time)}).`;

  const dbSelect = document.getElementById("scheduleDbId");
  const databaseName = dbSelect?.selectedOptions[0]?.textContent || "the selected database";
  const destinations = Array.from(document.querySelectorAll("#scheduleRepositories input:checked, #scheduleServers input:checked"))
    .map((input) => input.closest("label")?.querySelector("span")?.textContent?.trim())
    .filter(Boolean);
  const destinationText = destinations.length ? ` Local copy plus verified copies to ${destinations.join(", ")}.` : " Saved to the local backup folder.";
  return `${timing} Source: ${databaseName}.${destinationText}`;
}

function syncScheduleControls() {
  const frequency = document.getElementById("scheduleFrequency").value;
  document.getElementById("scheduleTimeGroup").hidden = frequency === "current";
  document.getElementById("scheduleWeekdayGroup").hidden = frequency !== "weekly";
  document.getElementById("scheduleMonthDayGroup").hidden = frequency !== "monthly";
  if (frequency !== "current") {
    const [hour = "02", minute = "00"] = (document.getElementById("scheduleTime").value || "02:00").split(":");
    const day = frequency === "monthly" ? document.getElementById("scheduleMonthDay").value : "*";
    const weekday = frequency === "weekly" ? document.getElementById("scheduleWeekday").value : "*";
    document.getElementById("scheduleCron").value = `${Number(minute)} ${Number(hour)} ${day} * ${weekday}`;
  }
  document.getElementById("schedulePreview").textContent = scheduleDescription();
}

function loadScheduleControls(cron) {
  const normalized = String(cron || "").trim().toLowerCase().split(/\s+/);
  const [minute, hour, dayOfMonth, month, dayOfWeek] = normalized;
  const validTime = normalized.length === 5 && /^\d{1,2}$/.test(minute) && Number(minute) < 60 && /^\d{1,2}$/.test(hour) && Number(hour) < 24;
  let frequency = null;
  if (validTime && dayOfMonth === "*" && month === "*" && dayOfWeek === "*") frequency = "daily";
  else if (validTime && dayOfMonth === "*" && month === "*" && scheduleWeekdays.includes(dayOfWeek)) frequency = "weekly";
  else if (validTime && /^(?:[1-9]|1\d|2[0-8])$/.test(dayOfMonth) && month === "*" && dayOfWeek === "*") frequency = "monthly";

  const selector = document.getElementById("scheduleFrequency");
  const customOption = selector.querySelector('option[value="current"]');
  if (frequency) {
    customOption.hidden = true;
    selector.value = frequency;
    document.getElementById("scheduleTime").value = `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
    if (frequency === "weekly") document.getElementById("scheduleWeekday").value = dayOfWeek;
    if (frequency === "monthly") document.getElementById("scheduleMonthDay").value = dayOfMonth;
  } else {
    customOption.hidden = false;
    selector.value = "current";
    document.getElementById("scheduleCron").value = cron || "0 2 * * *";
  }
  syncScheduleControls();
}

async function openScheduleDialog() {
  openDialog("scheduleDialog");
  try {
    const [data, dbData, repositoryData, serverData] = await Promise.all([
      api("/api/v1/backups/schedule"),
      api("/api/v1/databases"),
      api("/api/v1/storage/targets"),
      api("/api/v1/servers"),
    ]);
    const databases = (dbData.databases || []).filter((db) => ["postgres", "postgresql"].includes(String(db.engine || "").toLowerCase()));
    const dbSelect = document.getElementById("scheduleDbId");
    dbSelect.innerHTML = databases.length
      ? databases.map((db) => `<option value="${escapeHtml(db.id)}">${escapeHtml(db.name)} · ${escapeHtml(engineMeta(db.engine).label)}</option>`).join("")
      : `<option value="">No databases registered</option>`;
    dbSelect.value = data.db_id || state.activeDatabaseId || databases[0]?.id || "";
    if (!databases.some((db) => db.id === dbSelect.value)) dbSelect.value = databases[0]?.id || "";

    const selectedRepositories = new Set(data.repository_ids || []);
    const repositories = repositoryData.targets || [];
    document.getElementById("scheduleRepositories").innerHTML = repositories.length
      ? repositories.map((target) => `<label class="server-check-row"><input type="checkbox" value="${escapeHtml(target.id)}" ${selectedRepositories.has(target.id) ? "checked" : ""} ${target.available ? "" : "disabled"}> <span>${escapeHtml(target.name)} <small>${target.available ? "Mounted" : "Unavailable"}</small></span></label>`).join("")
      : "No mounted repositories registered.";
    const selectedServers = new Set(data.destination_server_ids || []);
    const servers = serverData.servers || [];
    document.getElementById("scheduleServers").innerHTML = servers.length
      ? servers.map((server) => `<label class="server-check-row"><input type="checkbox" value="${escapeHtml(server.id)}" ${selectedServers.has(server.id) ? "checked" : ""}> <span>${escapeHtml(server.name)} <small>SSH ${escapeHtml(maskedSshAddress(server))}</small></span></label>`).join("")
      : "No remote servers registered.";

    loadScheduleControls(data.cron || "0 2 * * *");
    document.getElementById("scheduleEnabled").value = data.enabled ? "true" : "false";
    document.getElementById("scheduleNextRun").textContent = data.next_run_time
      ? formatScheduleNextRun(data.next_run_time)
      : (data.enabled ? "Pending" : "Not scheduled");
  } catch (err) {
    console.error("[DBOps] schedule:", err);
    showToast(`Could not load schedule: ${err.message}`, "warning");
  }
}

async function submitCreateBackup() {
  const btn = document.getElementById("startBackupBtn");
  setBusy(btn, true, "Creating…");
  state.backupBusy = true;
  computeSystemStatus();
  try {
    const data = await api("/api/v1/backups/create", {
      method: "POST",
      body: JSON.stringify({
        db_id: state.activeDatabaseId,
        format: document.getElementById("backupFormat").value,
        compress: document.getElementById("backupCompress").value === "true",
        encrypt: document.getElementById("backupEncrypt").value === "true",
        description: document.getElementById("backupDesc").value.trim() || undefined,
        destination_server_ids: Array.from(document.querySelectorAll("#backupRemoteServers input:checked")).map((el) => el.value),
      }),
    });
    const requestedServers = Array.from(document.querySelectorAll("#backupRemoteServers input:checked")).map((el) => el.value);
    const remoteErrors = data.remote_copy_errors || [];
    const verifiedCount = (data.remote_copies || []).filter((copy) => requestedServers.includes(copy.server_id)).length;
    const remoteSummary = requestedServers.length ? `; ${verifiedCount} remote copy/copies verified${remoteErrors.length ? `, ${remoteErrors.length} failed` : ""}` : "";
    showToast(`Snapshot ready ? ID: ${data.backup_id}${remoteSummary}`, remoteErrors.length ? "warning" : "success");
    logActivity("backup", "Snapshot started", `${document.getElementById("backupFormat").value} · ${data.backup_id}`, state.activeDatabaseId);
    closeDialog("createBackupDialog");
    await fetchBackups();
  } catch (err) {
    showToast(`Backup error: ${err.message}`, "error");
  } finally {
    state.backupBusy = false;
    computeSystemStatus();
    setBusy(btn, false);
  }
}

async function submitUpdateSchedule() {
  const btn = document.querySelector('[data-action="submit-schedule"]');
  const cron = document.getElementById("scheduleCron").value.trim();
  const description = scheduleDescription();
  const enabled = document.getElementById("scheduleEnabled").value === "true";
  setBusy(btn, true, "Saving…");
  try {
    const data = await api("/api/v1/backups/schedule", {
      method: "POST",
      body: JSON.stringify({
        cron,
        enabled,
        format: "custom",
        db_id: document.getElementById("scheduleDbId").value || null,
        repository_ids: Array.from(document.querySelectorAll("#scheduleRepositories input:checked")).map((el) => el.value),
        destination_server_ids: Array.from(document.querySelectorAll("#scheduleServers input:checked")).map((el) => el.value),
      }),
    });
    showToast(enabled ? `Schedule saved. ${description}` : "Automated snapshots are paused.", "success");
    logActivity("schedule", "Backup schedule saved", `${cron} · ${document.getElementById("scheduleEnabled").value === "true" ? "active" : "disabled"}`, document.getElementById("scheduleDbId").value || null);
    closeDialog("scheduleDialog");
  } catch (err) {
    showToast(`Schedule error: ${err.message}`, "error");
  } finally {
    setBusy(btn, false);
  }
}

async function verifyBackupArchive(backupId) {
  openDialog("verifyDialog");
  const detailsEl = document.getElementById("verifyDetails");
  detailsEl.innerHTML = `<span class="spinner"></span> Running non-destructive integrity check on <code>${escapeHtml(backupId)}</code>…`;
  try {
    const data = await api(`/api/v1/backups/verify/${encodeURIComponent(backupId)}`, { method: "POST" });
    if (data.valid) {
      detailsEl.innerHTML = `
        <div class="verify-ok">${icon("check", "ico-sm")} Archive verification succeeded</div>
        <p><strong>Format:</strong> ${escapeHtml(data.format)}</p>
        <p><strong>Details:</strong> ${escapeHtml(data.details)}</p>
        <p><strong>Verified at:</strong> ${escapeHtml(data.verified_at)}</p>`;
    } else {
      detailsEl.innerHTML = `
        <div class="verify-bad">${icon("x", "ico-sm")} Verification failed</div>
        <p><strong>Error:</strong> ${escapeHtml(data.error || "Corrupted or incomplete archive")}</p>
        <p>${escapeHtml(data.details || "")}</p>`;
    }
    const backup = state.cachedBackups.find((item) => item.backup_id === backupId);
    logActivity(data.valid ? "verify" : "error", data.valid ? "Archive verification passed" : "Archive verification failed", backupId, backup?.db_id);
  } catch (err) {
    detailsEl.innerHTML = `<p style="color:var(--bad);">Verification request failed: ${escapeHtml(err.message)}</p>`;
    const backup = state.cachedBackups.find((item) => item.backup_id === backupId);
    logActivity("error", "Archive verification error", `${backupId} · ${err.message}`, backup?.db_id);
  }
}

async function deleteBackup(backupId) {
  const confirmed = await confirmAction({
    title: "Delete local snapshot?",
    message: "This removes the snapshot archive from this application's local backup storage and removes its entry from the list. Copies in repositories and on remote servers are left untouched. This cannot be undone for the local copy.",
    confirmText: "Delete local copy",
  });
  if (!confirmed) return;

  try {
    await api(`/api/v1/backups/${encodeURIComponent(backupId)}`, { method: "DELETE" });
    showToast("Local snapshot deleted. Repository and remote copies were preserved.", "success");
    const backup = state.cachedBackups.find((item) => item.backup_id === backupId);
    logActivity("delete", "Deleted local snapshot", backupId, backup?.db_id);
    await fetchBackups();
  } catch (err) {
    showToast(`Snapshot deletion failed: ${err.message}`, "error");
  }
}

/* -------------------------------------------------------------------------
   Theme — header toggle button (replaces the old pull-lamp)
   ------------------------------------------------------------------------- */
function getStoredTheme() {
  try {
    const t = localStorage.getItem("dbops_theme");
    if (t === "light" || t === "dark") return t;
  } catch (e) { /* ignore */ }
  return "dark";
}

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

let themeAnimTimer = null;

function applyTheme(theme, { persist = true } = {}) {
  const t = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = t;
  if (persist) {
    try { localStorage.setItem("dbops_theme", t); } catch (e) { /* ignore */ }
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", t === "light" ? "#f1f4f8" : "#0a0c11");

  if (typeof window.__bgRefreshColor === "function") window.__bgRefreshColor();

  const btn = document.getElementById("themeToggle");
  if (btn) {
    btn.innerHTML = icon(t === "light" ? "sun" : "moon");
    btn.setAttribute("aria-label", t === "light" ? "Switch to dark theme" : "Switch to light theme");
    btn.title = t === "light" ? "Switch to dark theme" : "Switch to light theme";
  }

  document.documentElement.classList.add("theme-anim");
  clearTimeout(themeAnimTimer);
  themeAnimTimer = setTimeout(() => document.documentElement.classList.remove("theme-anim"), 700);
}

function toggleTheme() {
  const next = currentTheme() === "light" ? "dark" : "light";
  applyTheme(next);
  logActivity("theme", next === "light" ? "Day mode enabled" : "Night mode enabled", "Theme switched");
}

function initTheme() {
  applyTheme(getStoredTheme(), { persist: false });
}

/* -------------------------------------------------------------------------
   Refresh orchestration
   ------------------------------------------------------------------------- */
function markUpdated() {
  const el = document.getElementById("lastUpdated");
  if (el) el.textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

async function refreshDashboard() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    await Promise.allSettled([
      fetchHealth(),
      fetchCacheHit(),
      fetchDbSize(),
      fetchSlowQueriesAndDeadlocks(),
      fetchBackups(),
      fetchRestores(),
      fetchLocks(),
      fetchScheduleSummary(),
    ]);
    computeSystemStatus();
    updateMetricVisualizations();
    markUpdated();
  } finally {
    state.refreshing = false;
  }
}

function startAutoRefresh() {
  stopAutoRefresh();
  refreshTimer = setInterval(() => { if (state.autoRefresh) refreshDashboard(); }, 8000);
}

function stopAutoRefresh() {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
}

function toggleAutoRefresh() {
  state.autoRefresh = !state.autoRefresh;
  try { localStorage.setItem("dbops_auto_refresh", state.autoRefresh ? "enabled" : "paused"); } catch (_) { /* keep the current-page preference */ }
  const btn = document.getElementById("autoRefreshBtn");
  const dot = document.getElementById("liveDot");
  if (btn) {
    btn.innerHTML = icon(state.autoRefresh ? "pause" : "play");
    btn.title = state.autoRefresh ? "Pause live refresh" : "Resume live refresh";
  }
  if (dot) dot.classList.toggle("paused", !state.autoRefresh);
  showToast(state.autoRefresh ? "Live refresh resumed." : "Live refresh paused.", "info");
  logActivity("live", state.autoRefresh ? "Live refresh resumed" : "Live refresh paused");
}

async function manualRefresh(btn) {
  setBusy(btn, true, "Refreshing…");
  await refreshDashboard();
  setBusy(btn, false);
}

/* -------------------------------------------------------------------------
   Delegated event handling (works for dynamic content + no inline handlers)
   ------------------------------------------------------------------------- */
function handleAction(action, el) {
  switch (action) {
    case "target-carousel-step": stepTargetCarousel(Number(el.dataset.direction)); break;
    case "open-add-db": openAddDbDialog(); break;
    case "open-server-dialog": openDialog("serverDialog"); break;
    case "register-server": registerRemoteServer(); break;
    case "refresh-server-health": refreshServerHealth(el.dataset.id); break;
    case "remove-server-registration": removeServerRegistration(el.dataset.id); break;
    case "edit-server-name": editServerName(el.dataset.id); break;
    case "edit-server-paths": editServerPaths(el.dataset.id); break;
    case "delete-managed-container": deleteManagedContainer(el.dataset.id, el.dataset.container); break;
    case "open-provision-db": openProvisionDbDialog(el.dataset.id); break;
    case "provision-database": provisionPostgresContainer(); break;
    case "clear-server-output": clearServerOperationOutput(); break;
    case "close-server-output": closeServerOperationOutput(); break;
    case "upload-postgres-image": uploadPostgresImageArchive(); break;
    case "open-users": openUserManagement(); break;
    case "delete-user": deleteUserAccount(el.dataset.username); break;
    case "open-command": openCommandPalette(); break;
    case "command-run": runCommand(Number(el.dataset.index)); break;
    case "clear-activity": clearActivity(); break;
    case "toggle-activity-history": toggleActivityHistory(); break;
    case "toggle-autorefresh": toggleAutoRefresh(); break;
    case "refresh": manualRefresh(el); break;
    case "copy-url": copyActiveUrl(); break;
    case "ping": quickPingActiveDb(); break;
    case "open-schedule": openScheduleDialog(); break;
    case "open-backup": openCreateBackupDialog(); break;
    case "unregister": confirmUnregisterActiveDb(); break;
    case "unregister-target": confirmUnregisterActiveDb(el.dataset.id); break;
    case "select-db": selectDatabase(el.dataset.id); break;
    case "browse-refresh-tables": if (isAuthed()) refreshBrowserTables(); break;
    case "browse-fullscreen": toggleBrowseFullscreen(); break;
    case "browse-load": browseState.offset = 0; loadBrowserRows(); break;
    case "browse-prev": browseState.offset = Math.max(0, browseState.offset - browseState.limit); loadBrowserRows(); break;
    case "browse-next": browseState.offset += browseState.limit; loadBrowserRows(); break;
    case "close-dialog": closeDialog(el.dataset.dialog); break;
    case "submit-add-db": submitRegisterDb(); break;
    case "test-connection": testAddDbConnection(); break;
    case "submit-backup": submitCreateBackup(); break;
    case "submit-schedule": submitUpdateSchedule(); break;
    case "verify-backup": verifyBackupArchive(el.dataset.id); break;
    case "download-backup": downloadBackup(el.dataset.id); break;
    case "delete-backup": deleteBackup(el.dataset.id); break;
    case "kill-query": killSlowQuery(el.dataset.id, el); break;
    case "toggle-theme": toggleTheme(); break;
    case "open-notifications": openNotificationsDialog(); break;
    case "save-notifications": saveNotificationSettings(); break;
    case "test-notification": sendTestNotification(); break;
    case "open-restore": openRestoreDialog(el.dataset.id); break;
    case "open-restore-latest": openRestoreDialog(null); break;
    case "submit-restore": submitRestore(); break;
    case "refresh-restores": fetchRestores(); break;
    case "refresh-migrations": fetchMigrationHistory(); break;
    case "restore-detail": showRestoreDetail(el.dataset.id); break;
    case "register-restored-target": registerRestoredTarget(el.dataset.id, el); break;
    case "unregister-restored-target": unregisterRestoredTarget(el.dataset.id, el); break;
    case "copy-restored-url": copyRestoredDatabaseUrl(el.dataset.id, el); break;
    case "copy-registered-restored-url": copyRegisteredRestoredUrl(el.dataset.id, el); break;
    case "confirm-yes": resolveConfirm(true); break;
    case "confirm-no": resolveConfirm(false); break;
    case "close-toast": dismissToast(el.closest(".toast")); break;
    default: break;
  }
}

async function toggleBrowseFullscreen() {
  const panel = document.getElementById("database-browser");
  if (!panel) return;
  try {
    if (document.fullscreenElement === panel) await document.exitFullscreen();
    else if (panel.requestFullscreen) await panel.requestFullscreen();
    else showToast("Full screen is not available in this browser.", "warning");
  } catch (err) { showToast(`Could not change full screen: ${err.message}`, "error"); }
}

function registerEvents() {
  document.addEventListener("click", (ev) => {
    if (suppressTargetCarouselClick) {
      suppressTargetCarouselClick = false;
      ev.preventDefault();
      return;
    }
    const sectionLink = ev.target.closest(".nav-link");
    if (sectionLink) {
      const destination = new URL(sectionLink.href, window.location.href);
      const target = destination.hash && destination.pathname === window.location.pathname
        ? document.querySelector(destination.hash)
        : null;
      if (target) {
        ev.preventDefault();
        if (window.location.hash !== destination.hash) history.pushState(null, "", destination.hash);
        document.querySelectorAll(".nav-link").forEach((link) => {
          const active = link === sectionLink;
          link.classList.toggle("active", active);
          if (active) link.setAttribute("aria-current", "page");
          else link.removeAttribute("aria-current");
        });
        target.classList.add("in");
        target.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
      }
      return;
    }
    const el = ev.target.closest("[data-action]");
    if (!el) return;
    ev.preventDefault();
    handleAction(el.dataset.action, el);
  });

  const newUserForm = document.getElementById("newUserForm");
  if (newUserForm) newUserForm.addEventListener("submit", createUserAccount);
  initTargetCarousel();

  document.addEventListener("input", (ev) => {
    if (ev.target.matches("[data-autodetect]")) autoDetectEngine(ev.target.value);
    if (ev.target.id === "dbSearchInput") { state.filter = ev.target.value; renderDatabasePills(); }
    if (ev.target.id === "commandInput") buildCommandList(ev.target.value);
    if (["scheduleTime", "scheduleDbId"].includes(ev.target.id)) syncScheduleControls();
    if (ev.target.id === "restoreNewDbName") updateRestorePlan();
  });

  document.addEventListener("change", (ev) => {
    if (ev.target.id === "restoreBackupSelect") updateRestoreBackupSelection(ev.target.value);
    if (ev.target.id === "restoreSourceDbSelect") renderRestoreBackupOptions(ev.target.value, "");
    if (["restoreMode", "restoreSourceServer"].includes(ev.target.id)) toggleRestoreMode();
    if (ev.target.id === "restoreTargetDbId") updateRestorePlan();
    if (["scheduleFrequency", "scheduleWeekday", "scheduleMonthDay", "scheduleDbId"].includes(ev.target.id)
      || ev.target.matches("#scheduleRepositories input, #scheduleServers input")) syncScheduleControls();
    if (ev.target.id === "activityDbFilter") {
      state.activityDbFilter = ev.target.value || null;
      activityExpanded = false;
      renderActivity(false);
    }
  });

  document.addEventListener("keydown", (ev) => {
    if (!isAuthed()) return;

    if ((ev.ctrlKey || ev.metaKey) && String(ev.key).toLowerCase() === "k") {
      ev.preventDefault();
      openCommandPalette();
      return;
    }

    const cmdDialog = document.getElementById("commandDialog");
    if (cmdDialog && cmdDialog.open) {
      if (ev.key === "ArrowDown") { ev.preventDefault(); moveCommand(1); return; }
      if (ev.key === "ArrowUp") { ev.preventDefault(); moveCommand(-1); return; }
      if (ev.key === "Enter") { ev.preventDefault(); runCommand(commandState.index); return; }
    }

    if (ev.key === "Escape") {
      Object.keys(fallbackModals).forEach((id) => closeDialog(id));
      if (confirmResolver) { confirmResolver(false); confirmResolver = null; }
    }
  });

  const confirmDialog = document.getElementById("confirmDialog");
  if (confirmDialog) {
    confirmDialog.addEventListener("close", () => {
      if (confirmResolver) { confirmResolver(false); confirmResolver = null; }
    });
  }

  const commandDialog = document.getElementById("commandDialog");
  if (commandDialog) {
    commandDialog.addEventListener("close", () => {
      const input = document.getElementById("commandInput");
      if (input) input.value = "";
    });
  }

  window.addEventListener("error", (e) => console.error("[DBOps] Uncaught error:", e.error || e.message));
  window.addEventListener("unhandledrejection", (e) => console.error("[DBOps] Unhandled rejection:", e.reason));
}

/* -------------------------------------------------------------------------
   Auth gate — animated tagline login
   ------------------------------------------------------------------------- */
const AUTH_LINES = [
  "Initializing secure session\u2026",
  "Verifying operator clearance\u2026",
  "Establishing encrypted channel\u2026",
  "Control plane ready for operator",
];

let authLineTimer = null;
let authLinePos = 0;
let authLineIndex = 0;
let authLineDeleting = false;
let authTagTimer = null;

function authStoredUser() {
  try { return localStorage.getItem(AUTH_USER_KEY) || ""; }
  catch (e) { return ""; }
}

function isAuthed() {
  return !!authStoredUser();
}

function rememberAuth(user) {
  try { localStorage.setItem(AUTH_USER_KEY, user); } catch (e) { /* ignore */ }
}

function forgetAuth() {
  try { localStorage.removeItem(AUTH_USER_KEY); localStorage.removeItem(AUTH_TOKEN_KEY); } catch (e) { /* ignore */ }
}

function startAuthTypewriter() {
  const el = document.getElementById("authTypewrite");
  if (!el) return;
  if (reducedMotion()) { el.textContent = AUTH_LINES[0]; return; }

  let hold = 0;
  clearInterval(authLineTimer);
  authLineTimer = setInterval(() => {
    const line = AUTH_LINES[authLineIndex % AUTH_LINES.length];
    if (hold > 0) { hold--; return; }
    if (!authLineDeleting) {
      authLinePos++;
      el.textContent = line.slice(0, authLinePos);
      if (authLinePos >= line.length) { authLineDeleting = true; hold = 22; }
    } else {
      authLinePos--;
      el.textContent = line.slice(0, Math.max(0, authLinePos));
      if (authLinePos <= 0) { authLineDeleting = false; authLineIndex++; hold = 6; }
    }
  }, 55);
}

function typeAuthTagName(name) {
  const el = document.getElementById("authTagName");
  if (!el) return;
  const target = (name || "operator").toLowerCase();
  clearInterval(authTagTimer);
  if (reducedMotion()) { el.textContent = target; return; }
  el.textContent = "";
  el.classList.add("is-typing");
  let i = 0;
  authTagTimer = setInterval(() => {
    i++;
    el.textContent = target.slice(0, i);
    if (i >= target.length) {
      clearInterval(authTagTimer);
      el.classList.remove("is-typing");
    }
  }, 120);
}

function showAuthError(message) {
  const el = document.getElementById("authError");
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
  el.style.animation = "none";
  void el.offsetWidth;
  el.style.animation = "";
}

function hideAuthError() {
  const el = document.getElementById("authError");
  if (el) { el.hidden = true; el.textContent = ""; }
}

function toggleCapsWarn(show) {
  const el = document.getElementById("authCapsWarn");
  if (el) el.hidden = !show;
}

function setupCapsLock() {
  const pass = document.getElementById("authPass");
  if (!pass) return;
  pass.addEventListener("keydown", (e) => {
    if (e.getModifierState && e.getModifierState("CapsLock")) {
      toggleCapsWarn(true);
    } else {
      toggleCapsWarn(false);
    }
  });
}

function setUserChip(user) {
  currentUsername = user || "";
  const chip = document.getElementById("userChip");
  const nameEl = document.getElementById("userChipName");
  const avatarEl = document.getElementById("userChipAvatar");
  const label = user || "operator";
  if (nameEl) nameEl.textContent = label;
  if (avatarEl) avatarEl.textContent = label.charAt(0);
  if (chip) chip.title = `Signed in as ${label}`;
}

async function submitAuth(event) {
  event.preventDefault();
  hideAuthError();
  const passEl = document.getElementById("authPass");
  const mfaCode = document.getElementById("authMfaCode");
  const user = document.getElementById("authUser").value.trim();
  if (pendingMfaChallenge && (!mfaCode.value || !/^\d{6}$/.test(mfaCode.value.trim()))) {
    showAuthError("Enter the current 6-digit code from Microsoft Authenticator."); return;
  }
  if (!pendingMfaChallenge && (!user || !passEl.value)) { showAuthError("Enter both username and password."); return; }
  const btn = document.getElementById("authSubmit"); if (btn) btn.classList.add("is-busy");
  try {
    const endpoint = pendingMfaChallenge ? "/api/v1/auth/login/verify-mfa" : "/api/v1/auth/login";
    const payload = pendingMfaChallenge
      ? { challenge: pendingMfaChallenge, code: mfaCode.value.trim() }
      : { username: user, password: passEl.value };
    const response = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Invalid username or password.");
    if (data.mfa_required) {
      pendingMfaChallenge = data.challenge;
      document.getElementById("authCredentials").hidden = true;
      document.getElementById("authMfaStep").hidden = false;
      document.getElementById("authMfaSetup").hidden = !data.setup_required;
      document.getElementById("authMfaInstructions").textContent = data.setup_required
        ? `Set up Microsoft Authenticator for ${data.username}. Scan the QR code below, then enter the code it generates.`
        : `Enter the current 6-digit code for ${data.username} from Microsoft Authenticator.`;
      document.getElementById("authMfaSecret").value = data.manual_secret || "";
      document.getElementById("authMfaQr").src = data.qr_data_url || "";
      document.getElementById("authSubmitLabel").textContent = data.setup_required ? "Verify and enable" : "Verify code";
      document.getElementById("authBack").hidden = false;
      passEl.value = "";
      if (btn) btn.classList.remove("is-busy");
      mfaCode.value = "";
      mfaCode.focus();
      return;
    }
    localStorage.setItem(AUTH_TOKEN_KEY, data.token); rememberAuth(data.user.username); currentRole = data.user.role;
    if (btn) btn.classList.remove("is-busy"); passEl.value = ""; onAuthSuccess();
  } catch (err) {
    if (btn) btn.classList.remove("is-busy");
    showAuthError(err.message);
    if (pendingMfaChallenge) { mfaCode.value = ""; mfaCode.focus(); }
    else { passEl.value = ""; passEl.focus(); }
  }
}

function resetAuthMfaStep() {
  pendingMfaChallenge = "";
  document.getElementById("authCredentials").hidden = false;
  document.getElementById("authMfaStep").hidden = true;
  document.getElementById("authMfaSetup").hidden = true;
  document.getElementById("authMfaSecret").value = "";
  document.getElementById("authMfaQr").removeAttribute("src");
  document.getElementById("authMfaCode").value = "";
  document.getElementById("authSubmitLabel").textContent = "Authenticate";
  document.getElementById("authBack").hidden = true;
  hideAuthError();
  document.getElementById("authUser").focus();
}


function applyRoleUI() {
  document.querySelectorAll(".admin-only").forEach((el) => { el.hidden = currentRole !== "admin"; });
  const clearButton = document.querySelector('[data-action="clear-activity"]');
  if (clearButton) clearButton.hidden = currentRole !== "admin";
}

async function openUserManagement() {
  if (currentRole !== "admin") return;
  try {
    document.getElementById("newUserForm").reset();
    const data = await api("/api/v1/auth/users");
    const list = document.getElementById("usersList");
    list.innerHTML = data.users.map((u) => `<div class="user-row"><span><strong>${escapeHtml(u.username)}</strong><small>${escapeHtml(u.role)} ? added ${escapeHtml(new Date(u.created_at + "Z").toLocaleDateString())}</small></span>${u.role === "user" ? `<button class="btn btn-danger-ghost btn-sm" data-action="delete-user" data-username="${escapeHtml(u.username)}">Delete</button>` : ""}</div>`).join("");
    document.getElementById("userMfaResult").hidden = true;
    openDialog("usersDialog");
  } catch (err) { showToast(err.message, "error"); }
}

async function createUserAccount(event) {
  event.preventDefault();
  const username = document.getElementById("newUsername").value.trim();
  const password = document.getElementById("newUserPassword").value;
  const role = document.getElementById("newUserRole").value;
  const enableMfa = document.getElementById("newUserMfa").checked;
  try {
    const result = await api("/api/v1/auth/users", { method: "POST", body: JSON.stringify({ username, password, role, enable_mfa: enableMfa }) });
    logActivity("user", `Created ${role} account`, username);
    event.target.reset();
    document.getElementById("userMfaResult").hidden = true;
    if (result.mfa_enabled) {
      document.getElementById("userMfaQr").src = result.qr_data_url || "";
      document.getElementById("userMfaSecret").value = result.manual_secret || "";
      document.getElementById("userMfaResult").hidden = false;
      showToast(`${role === "admin" ? "Administrator" : "User"} account created for ${username}. Microsoft Authenticator is enabled — share the QR code with the user.`, "success");
    } else {
      showToast(`${role === "admin" ? "Administrator" : "User"} account created for ${username}.`, "success");
    }
    await openUserManagement();
  } catch (err) { showToast(err.message, "error"); }
}

async function deleteUserAccount(username) {
  if (!await confirmAction({ message: `Delete the account for ${username}?`, title: "Delete user", confirmText: "Delete" })) return;
  try { await api(`/api/v1/auth/users/${encodeURIComponent(username)}`, { method: "DELETE" }); logActivity("user", "Deleted user account", username); showToast(`Account ${username} deleted.`, "success"); await openUserManagement(); }
  catch (err) { showToast(err.message, "error"); }
}

function onAuthSuccess() {
  document.documentElement.dataset.auth = "in";
  setUserChip(authStoredUser());
  hideAuthError();
  applyRoleUI();
  startApp();
  showWelcomeExperience(authStoredUser());
  logActivity("auth", "Operator authenticated", "Signed in");
  showToast(`Welcome back, ${authStoredUser()}.`, "success");
}

async function logout(revoke = false) {
  if (revoke) {
    try { await api("/api/v1/auth/logout", { method: "POST" }); } catch (_) { /* clear local session even if server is unavailable */ }
  }
  forgetAuth();
  stopAutoRefresh();
  document.documentElement.dataset.auth = "locked";
  window.location.reload();
}

function initAuth() {
  const userEl = document.getElementById("authUser");
  const passEl = document.getElementById("authPass");
  const mfaCode = document.getElementById("authMfaCode");
  const form = document.getElementById("authForm");
  const reveal = document.getElementById("authReveal");
  const logoutBtn = document.getElementById("logoutBtn");
  const welcomeContinue = document.getElementById("welcomeContinue");

  if (form) form.addEventListener("submit", submitAuth);
  if (welcomeContinue) welcomeContinue.addEventListener("click", finishWelcomeExperience);
  document.addEventListener("keydown", (event) => {
    if (document.documentElement.dataset.welcome === "active" && event.key === "Escape") {
      event.preventDefault();
      finishWelcomeExperience();
    }
  });
  const backBtn = document.getElementById("authBack");
  if (backBtn) backBtn.addEventListener("click", resetAuthMfaStep);
  if (mfaCode) mfaCode.addEventListener("input", () => { mfaCode.value = mfaCode.value.replace(/\D/g, "").slice(0, 6); });

  if (reveal) {
    reveal.addEventListener("click", () => {
      if (!passEl) return;
      const show = passEl.type === "password";
      passEl.type = show ? "text" : "password";
      reveal.innerHTML = icon(show ? "eye-off" : "eye");
      reveal.setAttribute("aria-label", show ? "Hide password" : "Show password");
      reveal.title = show ? "Hide password" : "Show password";
    });
  }

  if (userEl) {
    userEl.addEventListener("input", () => {
      const val = userEl.value.trim();
      if (!val) return;
      const tag = document.getElementById("authTagName");
      if (tag) tag.textContent = val.toLowerCase();
    });
  }

  if (logoutBtn) logoutBtn.addEventListener("click", () => logout(true));

  setupCapsLock();
  setupParallax();
  setupConstellation();
  startAuthTypewriter();
  typeAuthTagName("admin");

  if (isAuthed() && localStorage.getItem(AUTH_TOKEN_KEY)) {
    api("/api/v1/auth/me").then((user) => { currentRole = user.role; setUserChip(user.username); startApp(); applyRoleUI(); /* showWelcomeExperience skipped on restore */ }).catch(() => logout());
  } else {
    setTimeout(() => { if (userEl) userEl.focus(); }, 700);
  }
}

/* -------------------------------------------------------------------------
   Init
   ------------------------------------------------------------------------- */
function init() {
  registerEvents();
  initAuth();
  const browseSelect = document.getElementById("browseTableSelect");
  if (browseSelect) browseSelect.addEventListener("change", () => {
    browseState.offset = 0;
    if (browseSelect.value) loadBrowserRows();
  });
  const browseDatabaseSelect = document.getElementById("browseDatabaseSelect");
  if (browseDatabaseSelect) browseDatabaseSelect.addEventListener("change", () => {
    browseState.databaseId = browseDatabaseSelect.value;
    browseState.table = "";
    refreshBrowserTables();
  });
  // A stored display name alone never grants access; the server validates the session.
}

let appStarted = false;
let registrySyncTimer = null;
let welcomeFrame = null;
let welcomeTimer = null;
let welcomeDismissTimer = null;
let welcomeInertElements = [];

window.addEventListener("storage", (event) => {
  if (!appStarted || !["dbops_active_database", "dbops_registry_changed"].includes(event.key)) return;
  if (event.key === "dbops_active_database") {
    state.activeDatabaseId = event.newValue || "db-primary";
    state.cacheMetric = null; state.sizeMetric = null; state.slowMetric = null; state.deadlockMetric = null;
    updateMetricVisualizations();
  }
  clearTimeout(registrySyncTimer);
  registrySyncTimer = setTimeout(async () => {
    await fetchRegisteredDatabases();
    await Promise.allSettled([refreshDashboard(), fetchMigrationHistory()]);
  }, 80);
});

function renderWelcomeTunnel(progress, cards, motionReduced) {
  const total = cards.length;
  const goldenAngle = 137.5 * (Math.PI / 180);
  cards.forEach((card, i) => {
    const start = (i / total) * 0.6;
    const end = start + 0.6;
    const local = motionReduced ? 0.58 : Math.max(0, Math.min(1, (progress - start) / (end - start)));
    const farZ = window.innerWidth < 768 ? -900 : -1400;
    const nearZ = window.innerWidth < 768 ? 220 : 380;
    const z = motionReduced ? -30 : farZ + (nearZ - farZ) * local;
    const opacity = motionReduced ? 0.42 : local <= 0 ? 0 : local < 0.18 ? local / 0.18 : local > 0.82 ? (1 - local) / 0.18 : 1;
    const angle = i * goldenAngle;
    const radiusX = window.innerWidth * (window.innerWidth < 768 ? 0.28 : 0.3 + (i % 3) * 0.045);
    const radiusY = window.innerHeight * (window.innerWidth < 768 ? 0.22 : 0.24 + (i % 2) * 0.055);
    const x = Math.cos(angle) * radiusX;
    const y = Math.sin(angle) * radiusY;
    const rotation = (i % 2 === 0 ? 1 : -1) * (2 + (i % 4) * 2);
    card.style.transform = `translate3d(calc(-50% + ${x}px), calc(-50% + ${y}px), ${z}px) rotateZ(${rotation}deg)`;
    card.style.opacity = String(opacity);
    card.style.zIndex = String(Math.round(local * 2000));
  });
}

function startWelcomeTunnel(duration) {
  const container = document.getElementById("welcomeTunnelCards");
  if (!container) return;
  const nodes = [
    { icon: "database", title: "POSTGRESQL", detail: "Protected targets" },
    { icon: "snapshot", title: "SNAPSHOTS", detail: "Verified archives" },
    { icon: "restore", title: "RECOVERY", detail: "Restore workflows" },
    { icon: "gauge", title: "TELEMETRY", detail: "Live health signals" },
    { icon: "storage", title: "STORAGE", detail: "Space & contention" },
    { icon: "activity", title: "AUDIT TRAIL", detail: "Operator activity" },
    { icon: "clock", title: "SCHEDULES", detail: "Reliable cadence" },
    { icon: "terminal", title: "INFRASTRUCTURE", detail: "Managed servers" },
  ];

  container.replaceChildren();
  nodes.forEach((item, i) => {
    const card = document.createElement("div");
    card.className = "tunnel-hero__card";
    card.dataset.index = String(i);
    card.innerHTML = `
      <div class="tunnel-hero__card-inner">
        <span class="tunnel-hero__card-icon" aria-hidden="true">${icon(item.icon)}</span>
        <div class="tunnel-hero__card-overlay">
          <span class="tunnel-hero__card-title">${escapeHtml(item.title)}</span>
          <span class="tunnel-hero__card-detail">${escapeHtml(item.detail)}</span>
        </div>
      </div>
    `;
    container.appendChild(card);
  });

  const cards = Array.from(container.querySelectorAll(".tunnel-hero__card"));
  const motionReduced = reducedMotion();
  if (motionReduced) {
    renderWelcomeTunnel(0, cards, true);
    return;
  }

  const startedAt = performance.now();
  const animate = (now) => {
    const progress = Math.min(1, (now - startedAt) / duration);
    renderWelcomeTunnel(progress, cards, false);
    if (progress < 1) welcomeFrame = requestAnimationFrame(animate);
    else welcomeFrame = null;
  };
  welcomeFrame = requestAnimationFrame(animate);
}

function finishWelcomeExperience() {
  const overlay = document.getElementById("welcomeExperience");
  if (!overlay || overlay.hidden || overlay.classList.contains("is-exiting")) return;
  clearTimeout(welcomeTimer);
  clearTimeout(welcomeDismissTimer);
  welcomeTimer = null;
  if (welcomeFrame) cancelAnimationFrame(welcomeFrame);
  welcomeFrame = null;

  overlay.classList.add("is-exiting");
  welcomeDismissTimer = setTimeout(() => {
    overlay.hidden = true;
    overlay.setAttribute("aria-hidden", "true");
    document.documentElement.removeAttribute("data-welcome");
    welcomeInertElements.forEach((element) => { element.inert = false; });
    welcomeInertElements = [];
    document.getElementById("command-center")?.focus({ preventScroll: true });
    welcomeDismissTimer = null;
  }, reducedMotion() ? 0 : 420);
}

function showWelcomeExperience(username) {
  const overlay = document.getElementById("welcomeExperience");
  if (!overlay) return;
  clearTimeout(welcomeTimer);
  clearTimeout(welcomeDismissTimer);
  if (welcomeFrame) cancelAnimationFrame(welcomeFrame);
  welcomeFrame = null;

  const name = document.getElementById("welcomeUser");
  if (name) name.textContent = username || "operator";
  overlay.hidden = false;
  overlay.setAttribute("aria-hidden", "false");
  overlay.classList.remove("is-exiting");
  document.documentElement.dataset.welcome = "active";
  welcomeInertElements = [document.querySelector(".app-header"), document.querySelector("main"), document.querySelector(".bwf")].filter(Boolean);
  welcomeInertElements.forEach((element) => { element.inert = true; });
  startWelcomeTunnel(reducedMotion() ? 900 : 2800);
  document.getElementById("welcomeContinue")?.focus({ preventScroll: true });
  welcomeTimer = setTimeout(finishWelcomeExperience, reducedMotion() ? 900 : 2800);
}

function startApp() {
  if (appStarted) return;
  appStarted = true;

  const autoRefreshButton = document.getElementById("autoRefreshBtn");
  const liveDot = document.getElementById("liveDot");
  if (autoRefreshButton) {
    autoRefreshButton.innerHTML = icon(state.autoRefresh ? "pause" : "play");
    autoRefreshButton.title = state.autoRefresh ? "Pause live refresh" : "Resume live refresh";
  }
  if (liveDot) liveDot.classList.toggle("paused", !state.autoRefresh);

  document.documentElement.dataset.auth = "in";
  setupBackground();
  setupMouseGlow();
  setupRipple();
  setupScrollProgress();
  setupReveal();
  setupScrollSpy();
  setupNavTransitions();
  setupPageTransitions();
  setupFooterBeam();
  updateMetricVisualizations();
  renderLatencySparkline();
  loadActivity();
  renderActivity(false);
  fetchActivity();
  setInterval(() => { if (!document.hidden && state.autoRefresh) fetchActivity(); }, 12000);
  initTheme();
  refreshHeroNotify();
  setInterval(refreshActivityTimes, 30000);

  const versionEl = document.getElementById("uiVersion");
  if (versionEl) versionEl.textContent = `v${APP_VERSION.split(".")[0]}`;

  fetchRemoteServers();
  setInterval(() => {
    if (!document.hidden && state.autoRefresh) state.remoteServers.forEach((server) => refreshServerHealth(server.id));
  }, 30000);
  setInterval(() => { if (!document.hidden && state.autoRefresh) refreshDatabaseReachability(); }, 30000);
  fetchRegisteredDatabases().then(async () => {
    await Promise.allSettled([state.autoRefresh ? refreshDashboard() : Promise.resolve(), fetchMigrationHistory()]);
  });
  startAutoRefresh();
}

ready(init);