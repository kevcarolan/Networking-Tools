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
const canUpgrade = () => !!(state.user && state.user.can_upgrade);

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
  $$("[data-upgrader]").forEach((el) => (el.hidden = !canUpgrade()));
  state.platforms = await api("/api/platforms");
  switchView("backups");
  clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => {
    if (document.hidden) return;
    if (state.view === "backups") loadBackups();
    else if (state.view === "firmware" && fw.tab === "versions") loadFwVersions();
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
  ({ backups: loadBackups, firmware: loadFirmware, circuits: loadCircuits, credentials: loadCredentials,
    audit: loadAudit })[view]();
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
  $("#detail-circuits").hidden = tab !== "circuits";
  $("#detail-upgrades").hidden = tab !== "upgrades";
  $("#detail-text").hidden = true;
  ({ runs: loadRuns, versions: loadVersions, circuits: loadDeviceCircuits, upgrades: loadDeviceUpgrades })[tab]();
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

// ---------- firmware ----------

const fw = { tab: "versions", rows: [], standards: [], images: [] };

const COMPLIANCE_LABEL = {
  compliant: "On standard", behind: "Behind", ahead: "Ahead", no_standard: "No standard", unknown: "Unknown",
};

function fmtBytes(n) {
  if (n === null || n === undefined) return "—";
  if (n >= 2 ** 30) return `${(n / 2 ** 30).toFixed(1)} GB`;
  if (n >= 2 ** 20) return `${Math.round(n / 2 ** 20)} MB`;
  return `${Math.round(n / 1024)} KB`;
}

function loadFirmware() {
  selectFwTab(fw.tab);
}

function selectFwTab(tab) {
  fw.tab = tab;
  $$("#fw-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".fw-pane").forEach((p) => (p.hidden = p.id !== `fw-${tab}`));
  ({ versions: loadFwVersions, standards: loadFwStandards, images: loadFwImages, jobs: loadJobs })[tab]();
}
$$("#fw-tabs button").forEach((b) => b.addEventListener("click", () => selectFwTab(b.dataset.tab)));

async function loadFwVersions() {
  try {
    const [rows, summary, images] = await Promise.all([
      api("/api/firmware/devices"), api("/api/firmware/summary"), api("/api/firmware/images")]);
    fw.rows = rows;
    fw.images = images;
    renderFwSummary(summary);
    renderFwFilters();
    renderFwDevices();
  } catch (e) {
    toast(e.message);
  }
}

function renderFwSummary(s) {
  const tiles = [
    ["", "Devices", s.total],
    ["compliant", "On standard", s.compliant],
    ["behind", "Behind standard", s.behind],
    ["ahead", "Ahead of standard", s.ahead],
    ["no_standard", "No standard set", s.no_standard],
    ["unknown", "Version unknown", s.unknown],
  ];
  $("#fw-summary").replaceChildren(...tiles.map(([key, label, n]) =>
    h("button", { class: `tile ${key}`, onclick: () => { $("#fw-filter-compliance").value = key; renderFwDevices(); } },
      h("span", { class: "num" }, n ?? 0), h("span", { class: "lbl" }, label))));
}

function fillSelect(sel, allLabel, values) {
  const current = sel.value;
  sel.replaceChildren(h("option", { value: "" }, allLabel), ...values.map(([v, l]) => h("option", { value: v }, l)));
  sel.value = values.some(([v]) => v === current) ? current : "";
}

function renderFwFilters() {
  const platforms = new Map(fw.rows.map((r) => [r.device.platform, r.device.platform_label]));
  fillSelect($("#fw-filter-platform"), "All platforms", [...platforms].sort((a, b) => a[1].localeCompare(b[1])));
  const sites = [...new Set(fw.rows.map((r) => r.device.site).filter(Boolean))].sort();
  fillSelect($("#fw-filter-site"), "All sites", sites.map((x) => [x, x]));
}

function renderFwDevices() {
  const q = $("#fw-search").value.trim().toLowerCase();
  const comp = $("#fw-filter-compliance").value;
  const platform = $("#fw-filter-platform").value;
  const site = $("#fw-filter-site").value;
  const rows = fw.rows.filter((r) => {
    const d = r.device, f = r.facts;
    if (comp && r.compliance !== comp) return false;
    if (platform && d.platform !== platform) return false;
    if (site && d.site !== site) return false;
    if (q && ![d.name, d.address, d.site, f.model, f.version, f.serial].some((v) => (v || "").toLowerCase().includes(q))) return false;
    return true;
  });
  $("#fw-table tbody").replaceChildren(...rows.map(fwRow));
  const empty = $("#fw-empty");
  empty.hidden = rows.length > 0;
  empty.textContent = fw.rows.length ? "No devices match the filter." : "No devices yet - add them on the Config backups page.";
}

function fwRow(r) {
  const d = r.device, f = r.facts;
  const image = r.target_image ? fw.images.find((i) => i.filename === r.target_image) : null;
  const lowFlash = image && f.flash_free !== null && f.flash_free < image.size * 1.1;
  let checked;
  if (f.busy || f.status === "running") checked = h("span", { class: "badge running" }, "Checking…");
  else if (f.status === "failed") checked = [h("span", { class: "badge failed" }, "Check failed"),
    h("span", { class: "errtext" }, f.last_error || ""),
    f.last_success ? h("span", { class: "errtext" }, `Last good: ${fmtAgo(f.last_success)}`) : null];
  else checked = h("span", { title: fmtTime(f.last_success) }, fmtAgo(f.last_success));
  const actions = isAdmin()
    ? [h("button", { class: "small", disabled: f.busy, onclick: () => fwCheckNow(d) }, "Check now"),
      h("button", { class: "small", onclick: () => openUpgradeSettings(d, loadFwVersions) }, "Upgrade settings")] : [];
  const mode = [f.boot_mode, f.ha_role].filter(Boolean).join(" · ") || "—";
  return h("tr", { class: d.enabled ? "" : "disabled" },
    h("td", {}, h("span", { class: `dot ${r.compliance}`, title: COMPLIANCE_LABEL[r.compliance] })),
    h("td", { class: "name" }, d.name, h("small", {}, d.address)),
    h("td", {}, d.site || "—"),
    h("td", {}, d.platform_label),
    h("td", {}, f.model || "—", f.serial ? h("small", { class: "sub mono" }, f.serial) : null),
    h("td", {}, f.version ? h("code", {}, f.version) : "—",
      f.previous_version ? h("small", { class: "sub" }, `was ${f.previous_version} until ${fmtTime(f.version_changed_at)}`) : null),
    h("td", {}, h("span", { class: `badge ${r.compliance}` }, COMPLIANCE_LABEL[r.compliance]),
      r.target_version ? h("small", { class: "sub" }, `target ${r.target_version}`) : null),
    h("td", { class: lowFlash ? "lowflash" : "", title: lowFlash ? "Not enough space for the target image" : "" },
      fmtBytes(f.flash_free), f.flash_total ? h("small", { class: "sub" }, `of ${fmtBytes(f.flash_total)}`) : null),
    h("td", {}, mode),
    h("td", { class: "result" }, checked),
    h("td", { class: "actions" }, actions));
}

["input", "change"].forEach((evt) => {
  ["#fw-search", "#fw-filter-compliance", "#fw-filter-platform", "#fw-filter-site"]
    .forEach((sel) => $(sel).addEventListener(evt, renderFwDevices));
});

async function fwCheckNow(device) {
  try {
    const res = await api(`/api/firmware/devices/${device.id}/check`, { method: "POST" });
    toast(`${device.name}: ${res.message}`);
    loadFwVersions();
    setTimeout(loadFwVersions, 5000);
  } catch (e) {
    toast(e.message);
  }
}

$("#fw-check-all").addEventListener("click", async () => {
  if (!confirm("Log in to every enabled device now and read its software version?")) return;
  try {
    toast((await api("/api/firmware/check-all", { method: "POST" })).message);
    loadFwVersions();
    setTimeout(loadFwVersions, 5000);
  } catch (e) {
    toast(e.message);
  }
});

// Standards

async function loadFwStandards() {
  try {
    [fw.standards, fw.rows] = await Promise.all([api("/api/firmware/standards"), api("/api/firmware/devices")]);
  } catch (e) {
    return toast(e.message);
  }
  const counts = {};
  fw.rows.forEach((r) => { if (r.standard_id) counts[r.standard_id] = (counts[r.standard_id] || 0) + 1; });
  $("#fw-standard-table tbody").replaceChildren(...fw.standards.map((s) => h("tr", {},
    h("td", {}, s.platform_label),
    h("td", {}, h("code", {}, s.model_pattern)),
    h("td", {}, h("code", {}, s.target_version)),
    h("td", {}, s.image_filename || "—"),
    h("td", {}, counts[s.id] || 0),
    h("td", {}, s.notes || ""),
    h("td", {}, fmtTime(s.updated_at), h("small", { class: "sub" }, s.updated_by)),
    h("td", { class: "actions" }, isAdmin() ? [
      h("button", { class: "small", onclick: () => openStandardDialog(s) }, "Edit"),
      h("button", { class: "small danger", onclick: () => deleteStandard(s) }, "Delete")] : []))));
  $("#fw-standards-empty").hidden = fw.standards.length > 0;
}

async function openStandardDialog(std = null) {
  const form = $("#fw-standard-form");
  form.reset();
  $(".error", form).hidden = true;
  fw.images = await api("/api/firmware/images").catch(() => []);
  form.platform.replaceChildren(...state.platforms.map((p) => h("option", { value: p.key }, p.label)));
  const fillImages = () => {
    const current = form.image_id.value;
    form.image_id.replaceChildren(h("option", { value: "" }, "— none —"),
      ...fw.images.filter((i) => i.platform === form.platform.value)
        .map((i) => h("option", { value: i.id }, `${i.filename} (${i.version})`)));
    form.image_id.value = current;
  };
  form.platform.onchange = fillImages;
  form.image_id.onchange = () => {
    const img = fw.images.find((i) => String(i.id) === form.image_id.value);
    if (img) form.target_version.value = img.version;
  };
  $("#fw-standard-title").textContent = std ? "Edit standard" : "Add standard";
  if (std) {
    form.platform.value = std.platform;
    form.model_pattern.value = std.model_pattern === "*" ? "" : std.model_pattern;
    form.target_version.value = std.target_version;
    form.notes.value = std.notes;
  }
  fillImages();
  if (std && std.image_id) form.image_id.value = std.image_id;
  $("#fw-standard-dialog").dataset.stdId = std ? std.id : "";
  $("#fw-standard-dialog").showModal();
}
$("#fw-add-standard").addEventListener("click", () => openStandardDialog());

$("#fw-standard-form").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  const form = ev.target;
  const id = $("#fw-standard-dialog").dataset.stdId;
  const body = {
    platform: form.platform.value, model_pattern: form.model_pattern.value || "*",
    target_version: form.target_version.value, notes: form.notes.value,
    image_id: form.image_id.value ? Number(form.image_id.value) : null,
  };
  try {
    await api(id ? `/api/firmware/standards/${id}` : "/api/firmware/standards", { method: id ? "PUT" : "POST", body });
    $("#fw-standard-dialog").close();
    loadFwStandards();
  } catch (e) {
    const err = $(".error", form);
    err.textContent = e.message;
    err.hidden = false;
  }
});

async function deleteStandard(std) {
  if (!confirm(`Delete the standard for ${std.platform_label} ${std.model_pattern}?`)) return;
  try {
    await api(`/api/firmware/standards/${std.id}`, { method: "DELETE" });
    loadFwStandards();
  } catch (e) {
    toast(e.message);
  }
}

// Image library

async function loadFwImages() {
  try {
    fw.images = await api("/api/firmware/images");
  } catch (e) {
    return toast(e.message);
  }
  const images = [...fw.images].sort((a, b) => a.vendor.localeCompare(b.vendor) || a.platform.localeCompare(b.platform));
  $("#fw-image-table tbody").replaceChildren(...images.map((i) => h("tr", {},
    h("td", {}, h("b", {}, i.vendor)),
    h("td", { class: "name" }, i.filename, i.recommended ? h("span", { class: "badge success" }, "Recommended") : null,
      i.release_ref ? h("small", {}, i.release_ref) : null, i.notes ? h("small", {}, i.notes.slice(0, 80)) : null,
      i.job_count ? h("small", {}, `Used by ${i.job_count} upgrade job(s)`) : null),
    h("td", {}, i.platform_label),
    h("td", {}, h("code", {}, i.version)),
    h("td", {}, h("code", {}, i.model_pattern)),
    h("td", {}, fmtBytes(i.size)),
    h("td", { title: `SHA-512: ${i.sha512}` }, h("code", {}, i.md5),
      h("small", { class: "sub" }, i.verified ? "✓ matched vendor checksum" : "not verified against vendor checksum")),
    h("td", {}, fmtTime(i.uploaded_at), h("small", { class: "sub" }, i.uploaded_by)),
    h("td", { class: "actions" }, isAdmin() ? [
      h("button", { class: "small", onclick: () => toggleRecommended(i) }, i.recommended ? "Unmark" : "Recommend"),
      h("button", { class: "small danger", disabled: i.job_count > 0, onclick: () => deleteImage(i) }, "Delete")] : []))));
  $("#fw-images-empty").hidden = fw.images.length > 0;
}

function openUploadDialog() {
  const form = $("#fw-upload-form");
  form.reset();
  $(".error", form).hidden = true;
  $("#fw-upload-progress").hidden = true;
  form.platform.replaceChildren(...state.platforms.map((p) => h("option", { value: p.key }, p.label)));
  $("#fw-upload-dialog").showModal();
}
$("#fw-upload").addEventListener("click", openUploadDialog);

// Guess the version from common image names, e.g. cat9k_iosxe.17.12.04.SPA.bin,
// nxos.9.3.10.bin, cisco-asa-fp2k.9.18.4.SPA, x930-5.5.4-1.1.rel
$("#fw-upload-form").file.addEventListener("change", (ev) => {
  const form = ev.target.form;
  const file = ev.target.files[0];
  if (!file || form.version.value) return;
  const m = file.name.match(/[.-](\d+\.\d+[.\d]*[a-z]?(?:-\d+\.\d+)?)(?:\.SPA)?\.(?:bin|rel|SPA|tar)$/i);
  if (m) form.version.value = m[1];
});

$("#fw-upload-form").addEventListener("submit", (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  const form = ev.target;
  const err = $(".error", form);
  const bar = $("#fw-upload-progress");
  const file = form.file.files[0];
  const params = new URLSearchParams({
    filename: file.name, platform: form.platform.value, version: form.version.value,
    model_pattern: form.model_pattern.value || "*", checksum: form.checksum.value, notes: form.notes.value,
  });
  const submit = $("button[value=save]", form);
  err.hidden = true;
  bar.hidden = false;
  bar.value = 0;
  submit.disabled = true;
  // XMLHttpRequest rather than fetch, for upload progress on large images.
  const xhr = new XMLHttpRequest();
  xhr.open("PUT", `/api/firmware/images/upload?${params}`);
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.value = (100 * e.loaded) / e.total; };
  xhr.onloadend = () => {
    submit.disabled = false;
    if (xhr.status === 201) {
      $("#fw-upload-dialog").close();
      toast(`${file.name} uploaded`);
      loadFwImages();
      return;
    }
    if (xhr.status === 401) return showLogin();
    let msg = xhr.statusText || "Upload failed";
    try {
      const detail = JSON.parse(xhr.responseText).detail;
      msg = Array.isArray(detail) ? detail.map((e) => `${e.loc.slice(-1)[0]}: ${e.msg}`).join("; ") : detail;
    } catch (_) { /* not JSON */ }
    bar.hidden = true;
    err.textContent = msg;
    err.hidden = false;
  };
  xhr.send(file);
});

async function deleteImage(img) {
  if (!confirm(`Delete ${img.filename} from the server?`)) return;
  try {
    await api(`/api/firmware/images/${img.id}`, { method: "DELETE" });
    loadFwImages();
  } catch (e) {
    toast(e.message);
  }
}

// ---------- circuits ----------

const ct = { rows: [], imp: null, pending: null };

const CT_MATCH = { matched: "Linked", unmatched: "Switch not in NetOps", ambiguous: "Several devices match", none: "No switch" };

async function loadCircuits() {
  try {
    const data = await api("/api/circuits");
    ct.rows = data.circuits;
    ct.imp = data.import;
  } catch (e) {
    return toast(e.message);
  }
  const rows = ct.rows;
  const linked = rows.filter((r) => r.match === "matched").length;
  const tiles = [
    ["", "Circuits", rows.length, ""],
    ["success", "Linked to a switch", linked, "matched"],
    ["failed", "Not linked", rows.length - linked, "unlinked"],
    ["never", "With warnings", rows.filter((r) => r.warnings).length, "warnings"],
    ["", "Switches", new Set(rows.map((r) => r.switch).filter(Boolean)).size, ""],
  ];
  $("#ct-summary").replaceChildren(...tiles.map(([cls, label, n, filter]) =>
    h("button", { class: `tile ${cls}`, onclick: () => { $("#ct-filter-match").value = filter; renderCircuits(); } },
      h("span", { class: "num" }, n), h("span", { class: "lbl" }, label))));
  $("#ct-source").textContent = ct.imp
    ? `Live list: ${ct.imp.filename} (sheet "${ct.imp.sheet}"), ${ct.imp.row_count} rows, made live ${fmtTime(ct.imp.committed_at)} by ${ct.imp.committed_by}.`
    : "No circuit list has been uploaded yet.";
  const uniq = (key) => [...new Set(rows.map((r) => r[key]).filter(Boolean))].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  fillSelect($("#ct-filter-service"), "All services", uniq("service").map((x) => [x, x]));
  fillSelect($("#ct-filter-switch"), "All switches", uniq("switch").map((x) => [x, x]));
  fillSelect($("#ct-filter-vlan"), "All VLANs", uniq("vlan").map((x) => [x, `VLAN ${x}`]));
  renderCircuits();
}

function renderCircuits() {
  const q = $("#ct-search").value.trim().toLowerCase();
  const service = $("#ct-filter-service").value, sw = $("#ct-filter-switch").value;
  const vlan = $("#ct-filter-vlan").value, match = $("#ct-filter-match").value;
  const rows = ct.rows.filter((r) => {
    if (service && r.service !== service) return false;
    if (sw && r.switch !== sw) return false;
    if (vlan && r.vlan !== vlan) return false;
    if (match === "matched" && r.match !== "matched") return false;
    if (match === "unlinked" && r.match === "matched") return false;
    if (match === "warnings" && !r.warnings) return false;
    if (q && ![r.device_name, r.eng_prefix, r.ip, r.mac, r.port, r.switch, r.drawing_number, r.drawing_ref,
      r.room, r.device_type].some((v) => (v || "").toLowerCase().includes(q))) return false;
    return true;
  });
  const shown = rows.slice(0, 2000);
  $("#ct-table tbody").replaceChildren(...shown.map(circuitRow));
  const empty = $("#ct-empty");
  empty.hidden = rows.length > 0 && rows.length <= 2000;
  empty.textContent = !ct.rows.length ? (isAdmin() ? "No circuit list yet - click Upload spreadsheet." : "No circuit list uploaded yet.")
    : !rows.length ? "No circuits match the filter." : `Showing the first 2000 of ${rows.length} - narrow the filter to see the rest.`;
}

function circuitRow(r) {
  const linkCls = r.match === "matched" ? "success" : r.match === "none" ? "never" : "failed";
  return h("tr", {},
    h("td", {}, h("span", { class: `dot ${linkCls}`, title: CT_MATCH[r.match] })),
    h("td", {}, r.service || "—", r.ip_partition ? h("small", { class: "sub" }, r.ip_partition) : null),
    h("td", { class: "name" }, r.device_name || "—", r.eng_prefix ? h("small", {}, r.eng_prefix) : null,
      r.warnings ? h("span", { class: "errtext" }, `⚠ ${r.warnings}`) : null),
    h("td", {}, r.device_type || "—", r.manufacturer ? h("small", { class: "sub" }, r.manufacturer) : null),
    h("td", {}, h("code", {}, r.mac || "—")),
    h("td", {}, r.room || "—", r.floor ? h("small", { class: "sub" }, r.floor) : null),
    h("td", {}, h("code", {}, r.ip || "—"), r.gateway ? h("small", { class: "sub" }, `gw ${r.gateway} / ${r.mask}`) : null),
    h("td", {}, r.vlan || "—", r.conn_type ? h("small", { class: "sub" }, r.conn_type) : null),
    h("td", {}, h("code", {}, `${r.switch || "?"} ${r.port || ""}`),
      h("small", { class: "sub" }, r.match === "matched" ? `→ ${r.switch_device_name}` : CT_MATCH[r.match])),
    h("td", {}, r.drawing_number || "—", r.drawing_ref ? h("small", { class: "sub" }, r.drawing_ref) : null));
}

["input", "change"].forEach((evt) => {
  ["#ct-search", "#ct-filter-service", "#ct-filter-switch", "#ct-filter-vlan", "#ct-filter-match"]
    .forEach((sel) => $(sel).addEventListener(evt, renderCircuits));
});

// Upload + preview

$("#ct-upload").addEventListener("click", () => {
  const form = $("#ct-upload-form");
  form.reset();
  $(".error", form).hidden = true;
  form.hidden = false;
  $("#ct-preview").hidden = true;
  $("#ct-upload-dialog").showModal();
});
$("#ct-upload-close").addEventListener("click", () => $("#ct-upload-dialog").close());

$("#ct-upload-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target, err = $(".error", form), file = form.file.files[0];
  const btn = $("button", form);
  err.hidden = true;
  btn.disabled = true;
  btn.textContent = "Checking…";
  try {
    const res = await fetch(`/api/circuits/imports/upload?filename=${encodeURIComponent(file.name)}`, {
      method: "PUT", credentials: "same-origin", body: file, headers: { "Content-Type": "application/octet-stream" },
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) return showLogin();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    ct.pending = data;
    form.hidden = true;
    renderPreview(data);
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Check file";
  }
});

function renderPreview(imp) {
  const s = imp.summary, d = s.diff;
  const list = (items, more) => items.length
    ? h("ul", { class: "plain" }, items.map((x) => h("li", {}, x)), more > items.length ? h("li", { class: "muted" }, `… and ${more - items.length} more`) : null)
    : h("p", { class: "muted" }, "None.");
  const pane = $("#ct-preview");
  pane.replaceChildren(
    h("div", { class: "tiles" },
      [["", "Rows read", imp.row_count], ["success", "New", d.added], ["never", "Changed", d.changed],
        ["failed", "Removed", d.removed], ["", "Unchanged", d.unchanged], ["failed", "Rows not linked", s.rows_unmatched],
        ["never", "Rows with warnings", imp.warning_count]]
        .map(([cls, label, n]) => h("div", { class: `tile ${cls}` }, h("span", { class: "num" }, n), h("span", { class: "lbl" }, label)))),
    h("p", { class: "muted" }, `${imp.filename}, sheet "${imp.sheet}", header on row ${s.header_row}. ` +
      (s.replaces_import ? "Compared with the current live list." : "There is no live list yet; every row is new.") +
      (s.missing_columns.length ? ` Columns not found (left empty): ${s.missing_columns.join(", ")}.` : "")),
    h("h3", {}, "Switches not found in NetOps"),
    h("p", { class: "muted" }, "Rows on these switches can't be linked to upgrade jobs until a NetOps device with that name (or ending in -name) exists."),
    list([...s.unmatched_switches, ...s.ambiguous_switches.map((x) => `${x} (matches more than one device)`)],
      s.unmatched_switches.length + s.ambiguous_switches.length),
    h("h3", {}, "Changes"),
    d.changed_sample.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Row", "Field", "Before", "After"].map((t) => h("th", {}, t)))),
      h("tbody", {}, d.changed_sample.flatMap((c) => c.fields.map((f, i) => h("tr", {},
        h("td", {}, i === 0 ? c.row : ""), h("td", {}, f), h("td", {}, h("code", {}, c.before[f] || "—")),
        h("td", {}, h("code", {}, c.after[f] || "—"))))))) : h("p", { class: "muted" }, "No changed rows."),
    h("p", {}, h("b", {}, "New: "), d.added_sample.slice(0, 20).join(", ") || "none", d.added > 20 ? ` … (+${d.added - 20})` : ""),
    h("p", {}, h("b", {}, "Removed: "), d.removed_sample.slice(0, 20).join(", ") || "none", d.removed > 20 ? ` … (+${d.removed - 20})` : ""),
    h("h3", {}, "Rows with warnings"),
    list(s.warnings_sample.map((w) => `Row ${w.row}: ${w.warnings}`), imp.warning_count),
    h("div", { class: "dialog-actions" },
      h("button", { class: "ghost", onclick: discardPending }, "Discard"),
      h("button", { class: "primary", onclick: commitPending }, "Make this the live list")));
  pane.hidden = false;
}

async function commitPending() {
  try {
    await api(`/api/circuits/imports/${ct.pending.id}/commit`, { method: "POST" });
    $("#ct-upload-dialog").close();
    toast("Circuit list updated");
    loadCircuits();
  } catch (e) {
    toast(e.message);
  }
}

async function discardPending() {
  try {
    await api(`/api/circuits/imports/${ct.pending.id}`, { method: "DELETE" });
  } catch (e) { /* already gone */ }
  $("#ct-upload-dialog").close();
}

// History

$("#ct-history").addEventListener("click", async () => {
  const body = $("#ct-history-body");
  body.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  $("#ct-history-dialog").showModal();
  try {
    const imports = await api("/api/circuits/imports");
    if (!imports.length) return body.replaceChildren(h("p", { class: "empty" }, "No imports yet."));
    body.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, ["Uploaded", "File", "Rows", "Changes", "Status"].map((t) => h("th", {}, t)))),
      h("tbody", {}, imports.map((i) => {
        const d = (i.summary || {}).diff || {};
        return h("tr", {},
          h("td", {}, fmtTime(i.uploaded_at), h("small", { class: "sub" }, i.uploaded_by)),
          h("td", {}, i.filename, h("small", { class: "sub" }, `sheet "${i.sheet}"`)),
          h("td", {}, i.row_count),
          h("td", {}, `+${d.added ?? 0} ~${d.changed ?? 0} −${d.removed ?? 0}`),
          h("td", {}, h("span", { class: `badge ${i.status === "active" ? "success" : i.status === "pending" ? "running" : "never"}` },
            i.status), i.committed_at ? h("small", { class: "sub" }, `${fmtTime(i.committed_at)} ${i.committed_by}`) : null));
      }))));
  } catch (e) {
    body.replaceChildren(h("p", { class: "error" }, e.message));
  }
});
$("#ct-history-close").addEventListener("click", () => $("#ct-history-dialog").close());

// Device detail: circuits on this switch

async function loadDeviceCircuits() {
  const pane = $("#detail-circuits");
  pane.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  try {
    const data = await api(`/api/circuits/for-device/${state.detailDevice.id}`);
    if (!data.import) return pane.replaceChildren(h("p", { class: "empty" }, "No circuit list has been uploaded yet."));
    if (!data.count) return pane.replaceChildren(h("p", { class: "empty" }, "No circuits in the list use this device."));
    pane.replaceChildren(h("p", { class: "muted" }, `${data.count} circuit(s) on ${data.device}, from ${data.import.filename}.`),
      ...data.groups.map((g) => h("div", {},
        h("h3", {}, `${g.service} — VLAN ${g.vlan} (${g.circuits.length})`),
        h("table", {}, h("thead", {}, h("tr", {}, ["Port", "Device", "Type", "IP", "MAC", "Location", "Drawing"].map((t) => h("th", {}, t)))),
          h("tbody", {}, g.circuits.map((c) => h("tr", {},
            h("td", {}, h("code", {}, c.port)), h("td", { class: "name" }, c.device_name || "—"), h("td", {}, c.device_type || "—"),
            h("td", {}, h("code", {}, c.ip || "—")), h("td", {}, h("code", {}, c.mac || "—")),
            h("td", {}, [c.floor, c.room].filter(Boolean).join(" / ") || "—"), h("td", {}, c.drawing_number || "—"))))))));
  } catch (e) {
    pane.replaceChildren(h("p", { class: "error" }, e.message));
  }
}

// ---------- upgrade jobs ----------

const JOB_STATUS = {
  planned: "Planned", pre_checking: "Pre-checking…", blocked: "Blocked", ready: "Ready to start",
  staging: "Staging image…", running: "Running…", post_checking: "Post-checking…", paused: "Paused",
  completed: "Completed", completed_overrides: "Completed with overrides", failed: "Failed",
  cancelled: "Cancelled", rolling_back: "Rolling back…", rolled_back: "Rolled back",
  needs_attention: "Needs attention",
};
const JOB_OPEN = ["planned", "pre_checking", "blocked", "ready", "staging", "running", "post_checking",
  "paused", "failed", "rolling_back", "needs_attention"];
const JOB_WORKING = ["pre_checking", "staging", "running", "post_checking", "rolling_back"];
const UNIT_STATUS = { pending: "not changed yet", activated: "upgrading", checked: "upgraded, checked",
  failed: "post-checks failed", rolled_back: "rolled back" };
const jobs = { list: [], current: null, tab: "checks", timer: null, paths: null, worker: null };

async function upgradePaths() {
  if (!jobs.paths) jobs.paths = await api("/api/firmware/upgrade-paths");
  return jobs.paths;
}

async function toggleRecommended(img) {
  const ref = img.recommended ? img.release_ref : (prompt(`Why is ${img.filename} recommended? (e.g. "Cisco suggested release")`, img.release_ref || "") ?? null);
  if (ref === null) return;
  try {
    await api(`/api/firmware/images/${img.id}/info`, { method: "PUT", body: { recommended: !img.recommended, release_ref: ref } });
    loadFwImages();
  } catch (e) {
    toast(e.message);
  }
}

function workerBanner(w) {
  if (!w || w.alive) return "";
  return h("p", { class: "banner bad" }, h("b", {}, "The upgrade worker isn't running"),
    " (service netops-worker). Jobs can be viewed, but no checks or upgrade steps can run until it is started.");
}

async function loadJobs() {
  try {
    [jobs.list, jobs.worker] = await Promise.all([api("/api/firmware/jobs"), api("/api/firmware/worker")]);
  } catch (e) {
    return toast(e.message);
  }
  $("#worker-banner").replaceChildren(workerBanner(jobs.worker) || "");
  const count = (st) => jobs.list.filter((j) => st.includes(j.status)).length;
  $("#job-summary").replaceChildren(...[
    ["", "Open jobs", count(JOB_OPEN)], ["success", "Ready to start", count(["ready", "paused"])],
    ["failed", "Blocked / failed", count(["blocked", "failed", "needs_attention"])],
    ["success", "Completed", count(["completed", "completed_overrides"])],
    ["never", "Rolled back / cancelled", count(["rolled_back", "cancelled"])],
  ].map(([cls, label, n]) => h("div", { class: `tile ${cls}` }, h("span", { class: "num" }, n), h("span", { class: "lbl" }, label))));
  renderJobs();
}

function modeBadge(j) {
  return j.dry_run ? h("small", { class: "sub" }, "dry run") : h("span", { class: "badge live" }, "LIVE");
}

function renderJobs() {
  const f = $("#job-filter").value;
  const rows = jobs.list.filter((j) => !f || (f === "open" ? JOB_OPEN.includes(j.status) : !JOB_OPEN.includes(j.status)));
  $("#job-table tbody").replaceChildren(...rows.map((j) => h("tr", {},
    h("td", {}, `#${j.id}`),
    h("td", { class: "name" }, j.units.map((u) => u.name).join(" + "), h("small", {}, `${j.device.platform_label} · ${j.path_label}`)),
    h("td", {}, h("code", {}, `${j.from_version || "?"} → ${j.target_version}`), h("small", { class: "sub" }, j.image.filename)),
    h("td", {}, h("span", { class: `badge ${j.status}` }, JOB_STATUS[j.status] || j.status), " ", modeBadge(j)),
    h("td", {}, j.change_ref || "—"),
    h("td", {}, j.planned_start ? fmtTime(j.planned_start) : "—"),
    h("td", {}, fmtTime(j.created_at), h("small", { class: "sub" }, j.created_by)),
    h("td", { class: "actions" }, h("button", { class: "small", onclick: () => openJob(j.id) }, "Open")))));
  $("#job-empty").hidden = rows.length > 0;
}
$("#job-filter").addEventListener("change", renderJobs);

// New job

$("#job-new").addEventListener("click", async () => {
  const form = $("#job-new-form");
  form.reset();
  $(".error", form).hidden = true;
  try {
    [fw.rows, fw.images] = await Promise.all([api("/api/firmware/devices"), api("/api/firmware/images"), upgradePaths()]);
  } catch (e) {
    return toast(e.message);
  }
  form.device_id.replaceChildren(h("option", { value: "" }, "— choose a device —"),
    ...fw.rows.map((r) => h("option", { value: r.device.id }, `${r.device.name} (${r.device.platform_label}, ${r.facts.version || "version unknown"})`)));
  form.device_id.onchange = () => fillJobForm(form);
  form.path.onchange = () => describePath(form);
  $$("input[name=mode]", form).forEach((r) => (r.onchange = () => describePath(form)));
  fillJobForm(form);
  $("#job-new-dialog").showModal();
});

$("#job-new-settings").addEventListener("click", () => {
  const form = $("#job-new-form");
  const row = fw.rows.find((r) => String(r.device.id) === form.device_id.value);
  if (!row) return toast("Choose a device first");
  openUpgradeSettings(row.device, () => fillJobForm(form));
});

function globMatch(pattern, value) {
  return pattern === "*" || !value || new RegExp("^" + pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".") + "$", "i").test(value);
}

async function fillJobForm(form) {
  const row = fw.rows.find((r) => String(r.device.id) === form.device_id.value);
  const info = $("#job-new-device");
  form.image_id.replaceChildren();
  form.path.replaceChildren();
  $("#job-new-path").textContent = "";
  if (!row) return (info.textContent = "");
  const matches = fw.images.filter((i) => i.platform === row.device.platform && globMatch(i.model_pattern, row.facts.model));
  form.image_id.replaceChildren(...(matches.length ? matches.map((i) => h("option", { value: i.id },
    `${i.version} — ${i.filename}${i.recommended ? " (recommended)" : ""}`)) : [h("option", { value: "" }, "No image in the repository suits this device")]));
  const [settings, circ] = await Promise.all([
    api(`/api/firmware/devices/${row.device.id}/settings`).catch(() => ({ paths: [] })),
    api(`/api/circuits/for-device/${row.device.id}`).catch(() => ({ count: 0 }))]);
  form._settings = settings;
  const catalogue = (jobs.paths.find((p) => p.platform === row.device.platform) || { paths: [] }).paths;
  const preferred = settings.default_path || (row.device.platform === "cisco_ios" ? (row.facts.boot_mode === "install" ? "install" : "bundle")
    : (settings.peer ? (catalogue.find((p) => p.units === 2) || {}).id : catalogue[0] && catalogue[0].id));
  form.path.replaceChildren(...catalogue.map((p) => h("option", { value: p.id, disabled: p.units === 2 && !settings.peer },
    p.label + (p.units === 2 ? (settings.peer ? ` — with ${settings.peer}` : " — needs a peer (Upgrade settings)") : ""))));
  if (preferred && catalogue.some((p) => p.id === preferred && !(p.units === 2 && !settings.peer))) form.path.value = preferred;
  info.textContent = `Now running ${row.facts.version || "unknown"} on ${row.facts.model || "unknown model"}` +
    ` · ${circ.count || 0} affected circuit(s) in the circuit list` +
    (settings.upgrade_credential ? ` · upgrade account ${settings.upgrade_credential}` : " · no upgrade account set yet (an admin sets it in Upgrade settings)");
  describePath(form);
}

function describePath(form) {
  const row = fw.rows.find((r) => String(r.device.id) === form.device_id.value);
  if (!row) return;
  const p = ((jobs.paths.find((x) => x.platform === row.device.platform) || { paths: [] }).paths).find((x) => x.id === form.path.value);
  $("#job-new-path").textContent = p ? p.description : "";
  const liveRadio = $("input[name=mode][value=live]", form);
  liveRadio.disabled = !(p && p.live_allowed);
  if (liveRadio.disabled && liveRadio.checked) $("input[name=mode][value=dry]", form).checked = true;
  const live = liveRadio.checked;
  form.planned_start.required = form.planned_end.required = live;
  $("#job-new-live").textContent = !p ? "" : !p.live_allowed
    ? "Live upgrades are switched off for this platform (NETOPS_UPGRADE_LIVE_PLATFORMS). Dry run only."
    : live ? "A live upgrade needs a change window, and only starts inside it." : "";
}

$("#job-new-form").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  const form = ev.target, err = $(".error", form);
  const toIso = (v) => (v ? new Date(v).toISOString().slice(0, 19) : null);
  try {
    const job = await api("/api/firmware/jobs", { method: "POST", body: {
      device_id: Number(form.device_id.value), image_id: Number(form.image_id.value), path: form.path.value,
      live: $("input[name=mode][value=live]", form).checked, change_ref: form.change_ref.value,
      notes: form.notes.value, planned_start: toIso(form.planned_start.value), planned_end: toIso(form.planned_end.value) } });
    $("#job-new-dialog").close();
    loadJobs();
    openJob(job.id);
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  }
});

// Upgrade settings (admin)

async function openUpgradeSettings(device, after) {
  const form = $("#ups-form");
  form.reset();
  $(".error", form).hidden = true;
  let settings, creds, devices;
  try {
    [settings, creds, devices] = await Promise.all([api(`/api/firmware/devices/${device.id}/settings`),
      api("/api/credentials"), api("/api/devices")]);
  } catch (e) {
    return toast(e.message);
  }
  $("#ups-title").textContent = `Upgrade settings — ${device.name}`;
  form.upgrade_credential_id.replaceChildren(h("option", { value: "" }, "— none —"),
    ...creds.map((c) => h("option", { value: c.id }, `${c.name} (${c.username})`)));
  form.upgrade_credential_id.value = settings.upgrade_credential_id ?? "";
  form.default_path.replaceChildren(h("option", { value: "" }, "— decide per job —"),
    ...settings.paths.map((p) => h("option", { value: p.id }, p.label)));
  form.default_path.value = settings.default_path || "";
  const peers = devices.filter((d) => d.platform === device.platform && d.id !== device.id);
  form.peer_device_id.replaceChildren(h("option", { value: "" }, "— none (standalone) —"),
    ...peers.map((d) => h("option", { value: d.id }, `${d.name} (${d.address})`)));
  form.peer_device_id.value = settings.peer_device_id ?? "";
  form.file_system.value = settings.file_system || "";
  form.fdm_fingerprint.value = settings.fdm_fingerprint || "";
  $("#ups-fdm").hidden = device.platform !== "cisco_ftd";
  $("#ups-fetch").onclick = async () => {
    try {
      const r = await api(`/api/firmware/devices/${device.id}/fdm-fingerprint`, { method: "POST" });
      if (confirm(`${device.name} presents this certificate:\n\n${r.fingerprint}\n\nDoes it match the fingerprint shown in FDM? Only then trust it.`)) {
        form.fdm_fingerprint.value = r.fingerprint;
      }
    } catch (e) {
      toast(e.message);
    }
  };
  form.onsubmit = async (ev) => {
    if (ev.submitter && ev.submitter.value === "cancel") return;
    ev.preventDefault();
    const num = (v) => (v ? Number(v) : null);
    try {
      await api(`/api/firmware/devices/${device.id}/settings`, { method: "PUT", body: {
        upgrade_credential_id: num(form.upgrade_credential_id.value), default_path: form.default_path.value,
        peer_device_id: num(form.peer_device_id.value), file_system: form.file_system.value.trim(),
        fdm_fingerprint: form.fdm_fingerprint.value.trim() } });
      $("#ups-dialog").close();
      toast("Upgrade settings saved");
      if (after) after();
    } catch (e) {
      $(".error", form).textContent = e.message;
      $(".error", form).hidden = false;
    }
  };
  $("#ups-dialog").showModal();
}

// Job window

async function openJob(id) {
  jobs.tab = "procedure";
  $$("#job-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === jobs.tab));
  $("#job-dialog").showModal();
  await refreshJob(id);
}
$("#job-close").addEventListener("click", () => {
  clearTimeout(jobs.timer);
  $("#job-dialog").close();
  if (state.view === "firmware" && fw.tab === "jobs") loadJobs();
});
$$("#job-tabs button").forEach((b) => b.addEventListener("click", () => {
  jobs.tab = b.dataset.tab;
  $$("#job-tabs button").forEach((x) => x.classList.toggle("active", x === b));
  renderJobPane();
}));

async function refreshJob(id) {
  clearTimeout(jobs.timer);
  try {
    [jobs.current, jobs.worker] = await Promise.all([api(`/api/firmware/jobs/${id}`), api("/api/firmware/worker")]);
  } catch (e) {
    return toast(e.message);
  }
  renderJob();
  const j = jobs.current;
  if ($("#job-dialog").open && (j.busy || JOB_WORKING.includes(j.status) || j.pending_commit)) {
    jobs.timer = setTimeout(() => refreshJob(id), j.busy || JOB_WORKING.includes(j.status) ? 2000 : 15000);
  }
}

function minutesUntil(t) {
  return Math.round((new Date(t + (t.endsWith("Z") ? "" : "Z")) - Date.now()) / 60000);
}

function renderJob() {
  const j = jobs.current;
  $("#job-title").textContent = `Upgrade job #${j.id} — ${j.units.map((u) => u.name).join(" + ")}`;
  const working = (j.busy && !j.queued) || JOB_WORKING.includes(j.status);
  const cls = ["blocked", "failed", "needs_attention"].includes(j.status) ? "bad"
    : ["ready", "completed"].includes(j.status) ? "good" : "";
  const hint = {
    blocked: " — fix the device and Re-check, or override each blocker with a reason.",
    failed: j.mode === "rollback" ? " — the roll back's post-checks failed: check the device, then Re-run post-checks or override with a note."
      : " — investigate, then Re-run post-checks, override with a note, or Roll back.",
    paused: " — Continue carries on from the next step.",
    needs_attention: " — the procedure stopped in an unclear state: check the device, then re-run the post-checks or roll back.",
  }[j.status] || "";
  $("#job-banner").replaceChildren(
    workerBanner(jobs.worker) || "",
    h("p", { class: `banner ${cls}` }, h("b", {}, JOB_STATUS[j.status] || j.status),
      j.queued ? ` — ${j.queued} queued for the upgrade worker…` : working ? [" — ", h("span", { class: "spin" }), " ", j.current_step || "working", " (updates by itself)"] : "",
      j.stop_requested && working ? " — will stop after this step" : "", j.busy ? "" : hint),
    unresolvedList(j),
    j.pending_commit && j.abort_deadline ? h("p", { class: "banner bad" }, h("b", {}, "Not committed: "),
      `the device goes back to ${j.from_version} by itself at ${fmtTime(j.abort_deadline)} (in about ${minutesUntil(j.abort_deadline)} min) unless the new version is committed (Continue after the post-checks pass or are overridden).`) : "",
    j.dry_run ? h("p", { class: "banner" }, "DRY RUN: checks really read the device; anything that would change it is only recorded.")
      : h("p", { class: "banner bad" }, h("b", {}, "LIVE upgrade: "), `copies, reloads and commits for real, only inside the change window (${j.in_window ? "now open" : "not open now"}).`));
  const item = (label, value, sub) => h("div", {}, h("small", {}, label), value, sub ? h("small", {}, sub) : null);
  $("#job-info").replaceChildren(
    item("Upgrade", h("code", {}, `${j.from_version || "?"} → ${j.target_version}`), `${j.image.vendor} · ${j.image.filename}`),
    item("Path", j.path_label, `${j.device.platform_label} · site ${j.device.site || "—"}`),
    item("Change", j.change_ref || "—", j.planned_start ? `${fmtTime(j.planned_start)} – ${fmtTime(j.planned_end)}` : "no window set"),
    item("Created", fmtTime(j.created_at), j.created_by),
    item("Started", j.started_at ? fmtTime(j.started_at) : "—", j.started_by || ""),
    h("div", { class: "units" }, j.units.map((u) => h("div", { class: "unit" },
      h("b", {}, u.name), h("small", {}, `${u.address}${u.role ? " · " + u.role : ""}`),
      h("small", {}, `was ${u.from_version || "?"} · ${j.dry_run && u.status === "checked" ? "rehearsed (dry run)" : UNIT_STATUS[u.status] || u.status}${u.staged ? " · image staged" : ""}`),
      h("small", {}, `account: ${u.upgrade_credential || "— not set —"}`)))));
  const can = (a) => j.allowed.includes(a) && canUpgrade();
  const act = (label, action, cls = "", confirmText = null) => h("button", { class: cls, onclick: () => jobAction(action, confirmText) }, label);
  const live = !j.dry_run;
  $("#job-actions").replaceChildren(
    can("precheck") ? act(j.pre_run ? "Re-check" : "Run pre-checks", "precheck", j.status === "planned" ? "primary" : "") : "",
    can("stage") ? act("Stage image", "stage", "", `Copy ${j.image.filename} to ${j.units.map((u) => u.name).join(" and ")} and check its MD5 there?\n\nNothing is reloaded.`) : "",
    can("start") ? act(live ? "Start LIVE upgrade" : "Start (dry run)", "start", live ? "danger" : "primary",
      live ? null : `Start the dry run for ${j.device.name}?\n\nPre-checks run again first; nothing is changed on the device.`) : "",
    can("continue") ? act("Continue", "continue", "primary") : "",
    can("postcheck") ? act("Re-run post-checks", "postcheck") : "",
    can("rollback") ? act("Roll back", "rollback", "danger", live ? null : `Roll ${j.device.name} back to ${j.from_version}? (dry run)`) : "",
    can("stop") ? h("button", { class: "ghost", onclick: stopJob }, "Stop after this step") : "",
    can("cancel") ? h("button", { class: "ghost", onclick: cancelJob }, "Cancel job") : "",
    h("span", { class: "spacer" }),
    h("a", { class: "button", href: `/api/firmware/jobs/${j.id}/report.html`, target: "_blank", rel: "noopener" }, "Open report"),
    h("a", { class: "button", href: `/api/firmware/jobs/${j.id}/report.csv`, download: true }, "Checks CSV"));
  renderJobPane();
}

// Failed blockers without an override, named at the top of the job window.
function unresolvedList(j) {
  if (!["blocked", "failed", "needs_attention"].includes(j.status)) return "";
  const open = ["post", "stage", "pre"].map((ph) => j.checks[ph].filter((c) => c.severity === "blocker" && ["fail", "error"].includes(c.status) && !c.overridden_by))
    .find((list) => list.length && j.status !== "blocked") || j.checks.pre.filter((c) => c.severity === "blocker" && ["fail", "error"].includes(c.status) && !c.overridden_by);
  if (!open.length) return "";
  return h("ul", { class: "banner bad unresolved" }, open.map((c) =>
    h("li", {}, h("b", {}, (j.units.length > 1 && c.device ? `${c.device}: ` : "") + c.label), ": ", c.value || "", c.detail ? ` — ${c.detail.split("\n")[0]}` : "")));
}

function checksTable(rows, phase) {
  const j = jobs.current;
  if (!rows.length) return h("p", { class: "muted" }, { pre: "Pre-checks haven't run yet.", stage: "The image hasn't been staged yet.", post: "Post-checks run after each unit is upgraded." }[phase]);
  const canOverride = canUpgrade() && !j.busy &&
    ((phase === "pre" && ["blocked", "ready"].includes(j.status)) || (phase === "post" && ["failed", "paused", "completed_overrides", "needs_attention"].includes(j.status)));
  const multi = j.units.length > 1;
  return h("table", {},
    h("thead", {}, h("tr", {}, [multi ? "Unit" : null, "Check", "Result", "Value", "Detail", ""].filter((t) => t !== null).map((t) => h("th", {}, t)))),
    h("tbody", {}, rows.map((c) => {
      const failed = ["fail", "error"].includes(c.status);
      const sev = c.severity === "blocker" ? "" : c.severity === "warning" ? " (warning)" : " (info)";
      return h("tr", {},
        multi ? h("td", {}, c.device || "—") : null,
        h("td", { class: "name" }, c.label, h("small", {}, c.severity)),
        h("td", {}, h("span", { class: `badge ${failed && c.severity !== "blocker" ? "warnsev" : c.status}` },
          (failed && c.overridden_by ? "overridden" : c.status) + (failed ? sev : ""))),
        h("td", {}, c.value || "—"),
        h("td", {}, c.detail ? h("pre", { class: "detail" }, c.detail) : "",
          c.overridden_by ? h("div", { class: "override" }, `Override by ${c.overridden_by}: ${c.override_reason}`) : null),
        h("td", { class: "actions" }, canOverride && failed && c.severity === "blocker" && !c.overridden_by && c.overridable
          ? h("button", { class: "small", onclick: () => overrideCheck(c) }, "Override…") : ""));
    })));
}

function renderJobPane() {
  const j = jobs.current, pane = $("#job-pane");
  if (jobs.tab === "procedure") {
    if (!j.steps.length) return pane.replaceChildren(h("p", { class: "muted" }, "The procedure is worked out from the pre-checks when the job starts."));
    const working = (j.busy && !j.queued) || JOB_WORKING.includes(j.status);
    pane.replaceChildren(h("p", { class: "muted" }, j.mode === "rollback" ? "Roll back procedure" : `Upgrade procedure — ${j.path_label}`),
      h("ol", { class: "steps" }, j.steps.map((s, i) => h("li", { class: i < j.step_index ? "done" : i === j.step_index ? (working ? "current" : "next") : "" },
        h("span", { class: "n" }, i < j.step_index ? "" : i + 1),
        h("span", {}, s.label, i === j.step_index && working ? [" ", h("span", { class: "spin" })] : ""),
        h("span", {}, s.reload ? h("span", { class: "tag", title: "Takes the device down; only inside the change window" }, "reload") : "",
          s.gate ? h("span", { class: "tag gate", title: "Failures stop the job here" }, "check") : "")))));
  } else if (jobs.tab === "checks") {
    pane.replaceChildren(h("h3", {}, `Pre-checks${j.pre_run ? ` (run ${j.pre_run})` : ""}`), checksTable(j.checks.pre, "pre"),
      h("h3", {}, "Image on the device"), checksTable(j.checks.stage, "stage"),
      h("h3", {}, "Post-checks"), checksTable(j.checks.post, "post"));
  } else if (jobs.tab === "circuits") {
    const c = j.circuits.started || j.circuits.planned;
    if (!c || !c.count) return pane.replaceChildren(h("p", { class: "empty" }, "No circuits in the circuit list use this device."));
    pane.replaceChildren(h("p", { class: "muted" }, `${c.count} circuit(s) recorded ${j.circuits.started ? "at start" : "when the job was planned"} (${fmtTime(c.created_at)}), from ${c.source}.`),
      ...c.groups.map((g) => h("div", {}, h("h3", {}, `${g.service} — VLAN ${g.vlan} (${g.circuits.length})`),
        h("table", {}, h("thead", {}, h("tr", {}, ["Port", "Device", "Type", "IP", "Location", "Drawing"].map((t) => h("th", {}, t)))),
          h("tbody", {}, g.circuits.map((x) => h("tr", {}, h("td", {}, h("code", {}, x.port)), h("td", { class: "name" }, x.device_name || "—"),
            h("td", {}, x.device_type || "—"), h("td", {}, h("code", {}, x.ip || "—")),
            h("td", {}, [x.floor, x.room].filter(Boolean).join(" / ") || "—"), h("td", {}, x.drawing_number || "—"))))))));
  } else {
    pane.replaceChildren(h("table", {}, h("thead", {}, h("tr", {}, ["Time", "Unit", "Step", "Event"].map((t) => h("th", {}, t)))),
      h("tbody", {}, [...j.events].reverse().map((e) => h("tr", { class: `evt-${e.level}` },
        h("td", {}, fmtTime(e.time)), h("td", {}, e.device || ""), h("td", {}, e.step), h("td", {}, h("pre", { class: "detail" }, e.message)))))));
  }
}

async function jobAction(action, confirmText) {
  const j = jobs.current;
  let body;
  if (!j.dry_run && ["start", "continue", "rollback"].includes(action)) {
    const words = { start: "start the LIVE upgrade", continue: "continue the LIVE upgrade", rollback: "roll back for real" }[action];
    const typed = prompt(`To ${words} of ${j.units.map((u) => u.name).join(" and ")} to ${action === "rollback" ? j.from_version : j.target_version}, type the device name (${j.device.name}):`);
    if (typed === null) return;
    body = { confirm: typed };
  } else if (confirmText && !confirm(confirmText)) {
    return;
  }
  try {
    await api(`/api/firmware/jobs/${j.id}/${action}`, { method: "POST", body });
    setTimeout(() => refreshJob(j.id), 300);
  } catch (e) {
    toast(e.message);
  }
}

async function stopJob() {
  if (!confirm("Stop after the step that is running now? The job pauses and Continue carries on later. A step that is already running (e.g. a reload) is never interrupted.")) return;
  try {
    jobs.current = await api(`/api/firmware/jobs/${jobs.current.id}/stop`, { method: "POST" });
    renderJob();
  } catch (e) {
    toast(e.message);
  }
}

async function overrideCheck(c) {
  const reason = prompt(`Override "${c.label}"${c.device ? ` on ${c.device}` : ""}\n\n${c.detail || c.value}\n\nWhy is it safe to continue? (at least 10 characters; this goes in the job record)`);
  if (reason === null) return;
  try {
    jobs.current = await api(`/api/firmware/jobs/${jobs.current.id}/checks/${c.id}/override`, { method: "POST", body: { reason } });
    renderJob();
  } catch (e) {
    toast(e.message);
  }
}

async function cancelJob() {
  const changed = jobs.current.units.filter((u) => ["activated", "checked", "failed"].includes(u.status));
  const reason = prompt((changed.length ? `Warning: ${changed.map((u) => u.name).join(", ")} already changed. Consider Roll back instead.\n\n` : "") +
    "Why is this job being cancelled? (at least 10 characters)");
  if (reason === null) return;
  try {
    jobs.current = await api(`/api/firmware/jobs/${jobs.current.id}/cancel`, { method: "POST", body: { reason } });
    renderJob();
  } catch (e) {
    toast(e.message);
  }
}

// Device detail: upgrade history

async function loadDeviceUpgrades() {
  const pane = $("#detail-upgrades");
  pane.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  try {
    const list = await api(`/api/firmware/devices/${state.detailDevice.id}/jobs`);
    if (!list.length) return pane.replaceChildren(h("p", { class: "empty" }, "No upgrade jobs for this device yet."));
    pane.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, ["#", "Upgrade", "Status", "Change", "Started", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, list.map((j) => h("tr", {},
        h("td", {}, `#${j.id}`), h("td", {}, h("code", {}, `${j.from_version || "?"} → ${j.target_version}`), h("small", { class: "sub" }, j.path_label)),
        h("td", {}, h("span", { class: `badge ${j.status}` }, JOB_STATUS[j.status] || j.status), " ", modeBadge(j)),
        h("td", {}, j.change_ref || "—"), h("td", {}, j.started_at ? fmtTime(j.started_at) : "—"),
        h("td", { class: "actions" },
          h("a", { class: "button small", href: `/api/firmware/jobs/${j.id}/report.html`, target: "_blank", rel: "noopener" }, "Report"),
          h("button", { class: "small", onclick: () => { $("#detail-dialog").close(); openJob(j.id); } }, "Open")))))));
  } catch (e) {
    pane.replaceChildren(h("p", { class: "error" }, e.message));
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
