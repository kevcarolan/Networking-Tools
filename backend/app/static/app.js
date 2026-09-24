// NetOps Tools - browser frontend (plain JavaScript, no build step).
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  user: null,
  rows: [],
  platforms: [],
  credentials: [],
  view: "backups",
  refreshTimer: null,
  detailDevice: null,
};

// ---------- helpers ----------

/** Create an element: h("td", {class: "x"}, "text", childNode, ...) */
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c !== null && c !== undefined && c !== false) el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

async function api(path, options = {}) {
  const opts = { credentials: "same-origin", headers: {}, ...options };
  if (opts.body !== undefined && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, opts);
  if (res.status === 401 && path !== "/api/auth/login") {
    showLogin();
    throw new Error("Session expired - please sign in again");
  }
  const type = res.headers.get("content-type") || "";
  const data = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    let msg = data && data.detail !== undefined ? data.detail : data;
    if (Array.isArray(msg)) msg = msg.map((e) => `${e.loc.slice(-1)[0]}: ${e.msg}`).join("; ");
    throw new Error(msg || res.statusText);
  }
  return data;
}

function fmtTime(value) {
  if (!value) return "—";
  const d = new Date(value.endsWith("Z") || value.includes("+") ? value : value + "Z");
  return d.toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
}

function fmtAgo(value) {
  if (!value) return "never";
  const d = new Date(value.endsWith("Z") || value.includes("+") ? value : value + "Z");
  const mins = Math.round((Date.now() - d) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)} h ago`;
  return `${Math.round(mins / 1440)} days ago`;
}

function fmtFreq(mins) {
  const named = { 15: "15 min", 60: "Hourly", 240: "4 hours", 720: "12 hours", 1440: "Daily", 10080: "Weekly" };
  if (named[mins]) return named[mins];
  return mins % 1440 === 0 ? `${mins / 1440} days` : mins % 60 === 0 ? `${mins / 60} hours` : `${mins} min`;
}

let toastTimer;
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 4000);
}

const isAdmin = () => state.user && state.user.role === "admin";

// ---------- auth ----------

function showLogin() {
  state.user = null;
  clearInterval(state.refreshTimer);
  $("#app-view").hidden = true;
  $("#login-view").hidden = false;
}

async function showApp(user) {
  state.user = user;
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  $("#whoami").textContent = `${user.display_name || user.username} (${user.role})`;
  $$("[data-admin]").forEach((el) => (el.hidden = !isAdmin()));
  state.platforms = await api("/api/platforms");
  switchView("backups");
  clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => {
    if (state.view === "backups" && !document.hidden) loadBackups();
  }, 15000);
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const err = $("#login-error");
  err.hidden = true;
  try {
    const user = await api("/api/auth/login", {
      method: "POST",
      body: { username: form.username.value, password: form.password.value },
    });
    form.password.value = "";
    await showApp(user);
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

// ---------- navigation ----------

function switchView(view) {
  state.view = view;
  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((s) => (s.hidden = s.id !== `view-${view}`));
  ({ backups: loadBackups, credentials: loadCredentials, audit: loadAudit })[view]();
}
$$("#nav button").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));

// ---------- config backups ----------

function effectiveStatus(row) {
  if (row.backup.busy) return "running";
  if (!row.device.enabled || !row.backup.enabled) return "disabled";
  return row.backup.status;
}

const STATUS_LABEL = { success: "OK", failed: "Failed", running: "Running", never: "Never", disabled: "Disabled" };

async function loadBackups() {
  try {
    const [rows, summary] = await Promise.all([api("/api/backup/devices"), api("/api/backup/summary")]);
    state.rows = rows;
    renderSummary(summary);
    renderSiteFilter();
    renderDevices();
  } catch (e) {
    toast(e.message);
  }
}

function renderSummary(s) {
  const tiles = [
    ["", "Devices", s.total],
    ["success", "Backed up OK", s.success],
    ["failed", "Failing", s.failed],
    ["never", "Never backed up", s.never],
    ["disabled", "Disabled", s.disabled],
  ];
  $("#summary").replaceChildren(...tiles.map(([key, label, n]) =>
    h("button", { class: `tile ${key}`, onclick: () => { $("#filter-status").value = key; renderDevices(); } },
      h("span", { class: "num" }, n ?? 0), h("span", { class: "lbl" }, label))));
}

function renderSiteFilter() {
  const sel = $("#filter-site");
  const current = sel.value;
  const sites = [...new Set(state.rows.map((r) => r.device.site).filter(Boolean))].sort();
  sel.replaceChildren(h("option", { value: "" }, "All sites"), ...sites.map((s) => h("option", { value: s }, s)));
  sel.value = sites.includes(current) ? current : "";
  $("#site-list").replaceChildren(...sites.map((s) => h("option", { value: s })));
}

function renderDevices() {
  const q = $("#search").value.trim().toLowerCase();
  const status = $("#filter-status").value;
  const site = $("#filter-site").value;
  const rows = state.rows.filter((r) => {
    const d = r.device;
    if (site && d.site !== site) return false;
    if (status && effectiveStatus(r) !== status) return false;
    if (q && ![d.name, d.address, d.site, d.platform_label, d.notes].some((v) => (v || "").toLowerCase().includes(q))) return false;
    return true;
  });
  $("#device-table tbody").replaceChildren(...rows.map(deviceRow));
  $("#empty-devices").hidden = rows.length > 0;
  $("#empty-devices").textContent = state.rows.length ? "No devices match the filter." :
    isAdmin() ? "No devices yet - add a credential profile, then add your first device." : "No devices yet.";
}

function deviceRow(r) {
  const d = r.device, b = r.backup;
  const st = effectiveStatus(r);
  let result;
  if (st === "failed") {
    result = [h("span", { class: "badge failed" }, `Failed${b.consecutive_failures > 1 ? ` ×${b.consecutive_failures}` : ""}`),
      h("span", { class: "errtext" }, b.last_error || "")];
  } else {
    result = h("span", { class: `badge ${st}` }, STATUS_LABEL[st] || st);
  }
  const actions = [h("button", { class: "small", onclick: () => openDetail(r) }, "History")];
  if (isAdmin()) {
    actions.push(
      h("button", { class: "small", disabled: b.busy, onclick: () => runNow(d) }, "Backup now"),
      h("button", { class: "small", onclick: () => openDeviceDialog(r) }, "Edit"),
      h("button", { class: "small danger", onclick: () => deleteDevice(d) }, "Delete"));
  }
  return h("tr", { class: st === "disabled" ? "disabled" : "" },
    h("td", {}, h("span", { class: `dot ${st}`, title: STATUS_LABEL[st] })),
    h("td", { class: "name" }, d.name, d.notes ? h("small", {}, d.notes.slice(0, 80)) : null),
    h("td", {}, d.address),
    h("td", {}, d.site || "—"),
    h("td", {}, d.platform_label),
    h("td", {}, fmtFreq(b.frequency_minutes)),
    h("td", { title: fmtTime(b.last_success) }, fmtAgo(b.last_success)),
    h("td", { title: fmtTime(b.last_change) }, b.last_change ? fmtAgo(b.last_change) : "—"),
    h("td", { class: "result" }, result),
    h("td", { class: "actions" }, actions));
}

["input", "change"].forEach((evt) => {
  $("#search").addEventListener(evt, renderDevices);
  $("#filter-status").addEventListener(evt, renderDevices);
  $("#filter-site").addEventListener(evt, renderDevices);
});

async function runNow(device) {
  try {
    const res = await api(`/api/backup/devices/${device.id}/run`, { method: "POST" });
    toast(`${device.name}: ${res.message}`);
    loadBackups();
    setTimeout(loadBackups, 5000);
  } catch (e) {
    toast(e.message);
  }
}

async function deleteDevice(device) {
  if (!confirm(`Delete ${device.name}?\n\nIts backup history in the GUI is removed. Stored config versions stay in the git archive.`)) return;
  try {
    await api(`/api/devices/${device.id}`, { method: "DELETE" });
    toast(`${device.name} deleted`);
    loadBackups();
  } catch (e) {
    toast(e.message);
  }
}

// ---------- device dialog ----------

async function openDeviceDialog(row = null) {
  const dlg = $("#device-dialog");
  const form = $("#device-form");
  form.reset();
  $(".error", form).hidden = true;
  state.credentials = await api("/api/credentials").catch(() => []);
  form.platform.replaceChildren(...state.platforms.map((p) => h("option", { value: p.key }, p.label)));
  form.credential_id.replaceChildren(h("option", { value: "" }, "— none —"),
    ...state.credentials.map((c) => h("option", { value: c.id }, `${c.name} (${c.username})`)));
  $("#device-dialog-title").textContent = row ? `Edit ${row.device.name}` : "Add device";
  if (row) {
    const d = row.device;
    form.name.value = d.name;
    form.address.value = d.address;
    form.platform.value = d.platform;
    form.site.value = d.site;
    form.credential_id.value = d.credential_id ?? "";
    form.notes.value = d.notes;
    form.enabled.checked = d.enabled;
    form.backup_enabled.checked = row.backup.enabled;
    const freq = String(row.backup.frequency_minutes);
    if (![...form.frequency_minutes.options].some((o) => o.value === freq)) {
      form.frequency_minutes.append(h("option", { value: freq }, fmtFreq(row.backup.frequency_minutes)));
    }
    form.frequency_minutes.value = freq;
  } else if (state.credentials.length === 1) {
    form.credential_id.value = state.credentials[0].id;
  }
  dlg.dataset.deviceId = row ? row.device.id : "";
  dlg.showModal();
}

$("#add-device").addEventListener("click", () => openDeviceDialog());

$("#device-form").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  const form = ev.target;
  const dlg = $("#device-dialog");
  const id = dlg.dataset.deviceId;
  const body = {
    name: form.name.value, address: form.address.value, platform: form.platform.value,
    site: form.site.value, notes: form.notes.value, enabled: form.enabled.checked,
    credential_id: form.credential_id.value ? Number(form.credential_id.value) : null,
  };
  try {
    const device = await api(id ? `/api/devices/${id}` : "/api/devices", { method: id ? "PUT" : "POST", body });
    await api(`/api/backup/devices/${device.id}/settings`, {
      method: "PUT",
      body: { frequency_minutes: Number(form.frequency_minutes.value), enabled: form.backup_enabled.checked },
    });
    dlg.close();
    toast(`${device.name} saved`);
    loadBackups();
  } catch (e) {
    const err = $(".error", form);
    err.textContent = e.message;
    err.hidden = false;
  }
});

// ---------- device detail (history, versions, diffs) ----------

async function openDetail(row) {
  state.detailDevice = row.device;
  $("#detail-title").textContent = `${row.device.name} — ${row.device.address}`;
  $("#detail-text").hidden = true;
  selectDetailTab("runs");
  $("#detail-dialog").showModal();
}

function selectDetailTab(tab) {
  $$("#detail-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("#detail-runs").hidden = tab !== "runs";
  $("#detail-versions").hidden = tab !== "versions";
  $("#detail-text").hidden = true;
  (tab === "runs" ? loadRuns : loadVersions)();
}
$$("#detail-tabs button").forEach((b) => b.addEventListener("click", () => selectDetailTab(b.dataset.tab)));
$("#detail-close").addEventListener("click", () => $("#detail-dialog").close());

async function loadRuns() {
  const pane = $("#detail-runs");
  pane.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  try {
    const runs = await api(`/api/backup/devices/${state.detailDevice.id}/runs`);
    if (!runs.length) return pane.replaceChildren(h("p", { class: "empty" }, "No backup attempts yet."));
    pane.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, ["Started", "Trigger", "Result", "Detail"].map((t) => h("th", {}, t)))),
      h("tbody", {}, runs.map((r) => h("tr", {},
        h("td", {}, fmtTime(r.started_at)),
        h("td", {}, r.trigger),
        h("td", {}, h("span", { class: `badge ${r.status === "success" && r.changed ? "changed" : r.status}` },
          r.status === "success" ? (r.changed ? "Changed" : "OK") : STATUS_LABEL[r.status] || r.status)),
        h("td", {}, r.error_type ? `[${r.error_type}] ${r.message}` : r.message))))));
  } catch (e) {
    pane.replaceChildren(h("p", { class: "error" }, e.message));
  }
}

async function loadVersions() {
  const pane = $("#detail-versions");
  const id = state.detailDevice.id;
  pane.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  try {
    const versions = await api(`/api/backup/devices/${id}/versions`);
    if (!versions.length) return pane.replaceChildren(h("p", { class: "empty" }, "No stored versions yet."));
    pane.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, ["Saved", "Version", "Note", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, versions.map((v, i) => h("tr", {},
        h("td", {}, fmtTime(v.date)),
        h("td", {}, h("code", {}, v.commit.slice(0, 8))),
        h("td", {}, v.message),
        h("td", { class: "actions" },
          h("button", { class: "small", onclick: () => showConfig(id, v.commit) }, "View"),
          i < versions.length - 1
            ? h("button", { class: "small", onclick: () => showDiff(id, v.commit) }, "Changes") : null,
          h("button", { class: "small", onclick: () => downloadConfig(id, v.commit) }, "Download")))))));
  } catch (e) {
    pane.replaceChildren(h("p", { class: "error" }, e.message));
  }
}

async function showConfig(id, commit) {
  const pre = $("#detail-text");
  try {
    pre.textContent = await api(`/api/backup/devices/${id}/config?version=${commit}`);
    pre.hidden = false;
  } catch (e) {
    toast(e.message);
  }
}

async function showDiff(id, commit) {
  const pre = $("#detail-text");
  try {
    const diff = await api(`/api/backup/devices/${id}/diff?to=${commit}`);
    pre.replaceChildren(...(diff || "No differences.\n").split("\n").map((line) => {
      const cls = line.startsWith("@@") ? "hunk" : line.startsWith("+") && !line.startsWith("+++") ? "add"
        : line.startsWith("-") && !line.startsWith("---") ? "del" : null;
      return cls ? h("span", { class: cls }, line) : line + "\n";
    }));
    pre.hidden = false;
  } catch (e) {
    toast(e.message);
  }
}

async function downloadConfig(id, commit) {
  try {
    const text = await api(`/api/backup/devices/${id}/config?version=${commit}`);
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const a = h("a", { href: url, download: `${state.detailDevice.name}_${commit.slice(0, 8)}.cfg` });
    document.body.append(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    toast(e.message);
  }
}

// ---------- credentials ----------

async function loadCredentials() {
  try {
    state.credentials = await api("/api/credentials");
  } catch (e) {
    return toast(e.message);
  }
  $("#credential-table tbody").replaceChildren(...state.credentials.map((c) => h("tr", {},
    h("td", { class: "name" }, c.name),
    h("td", {}, c.username),
    h("td", {}, c.has_enable_secret ? "set" : "—"),
    h("td", {}, c.device_count),
    h("td", {}, fmtTime(c.updated_at)),
    h("td", { class: "actions" },
      h("button", { class: "small", onclick: () => openCredentialDialog(c) }, "Edit"),
      h("button", { class: "small danger", disabled: c.device_count > 0, onclick: () => deleteCredential(c) }, "Delete")))));
}

function openCredentialDialog(cred = null) {
  const form = $("#credential-form");
  form.reset();
  $(".error", form).hidden = true;
  $("#credential-dialog-title").textContent = cred ? `Edit ${cred.name}` : "Add credential";
  $("#credential-edit-hint").hidden = !cred;
  form.password.required = !cred;
  if (cred) {
    form.name.value = cred.name;
    form.username.value = cred.username;
  }
  $("#credential-dialog").dataset.credId = cred ? cred.id : "";
  $("#credential-dialog").showModal();
}
$("#add-credential").addEventListener("click", () => openCredentialDialog());

$("#credential-form").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  const form = ev.target;
  const id = $("#credential-dialog").dataset.credId;
  const body = { name: form.name.value, username: form.username.value };
  if (form.password.value) body.password = form.password.value;
  if (form.enable_secret.value) body.enable_secret = form.enable_secret.value;
  try {
    await api(id ? `/api/credentials/${id}` : "/api/credentials", { method: id ? "PUT" : "POST", body });
    $("#credential-dialog").close();
    loadCredentials();
  } catch (e) {
    const err = $(".error", form);
    err.textContent = e.message;
    err.hidden = false;
  }
});

async function deleteCredential(cred) {
  if (!confirm(`Delete credential profile ${cred.name}?`)) return;
  try {
    await api(`/api/credentials/${cred.id}`, { method: "DELETE" });
    loadCredentials();
  } catch (e) {
    toast(e.message);
  }
}

// ---------- audit ----------

async function loadAudit() {
  try {
    const rows = await api("/api/audit");
    $("#audit-table tbody").replaceChildren(...rows.map((r) => h("tr", {},
      h("td", {}, fmtTime(r.timestamp)), h("td", {}, r.username), h("td", {}, r.action), h("td", {}, r.detail))));
  } catch (e) {
    toast(e.message);
  }
}

// ---------- start ----------

api("/api/auth/me").then(showApp).catch(showLogin);
