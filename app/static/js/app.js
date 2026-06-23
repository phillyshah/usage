/*
 * app.js — UI logic for Usage.
 *
 * Wires the four-step guided flow plus the secondary sections. Plain
 * vanilla JS, ES modules, no framework. All server calls go through api.js
 * and surface friendly errors via toasts and inline notices.
 */
import { api, ApiError } from "./api.js";

/* ------------------------------------------------------------------ utils */
const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.className = opts.class;
  if (opts.text != null) node.textContent = opts.text;
  if (opts.html != null) node.innerHTML = opts.html;
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
  for (const c of [].concat(children)) if (c) node.append(c);
  return node;
}

function fmtBytes(n) {
  if (!n && n !== 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

function pluralize(n, one, many) { return `${n} ${n === 1 ? one : (many || one + "s")}`; }

/* SVG icon snippets reused in notices/toasts. */
const ICONS = {
  check: `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="8 12 11 15 16 9"/></svg>`,
  warn: `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12" y2="17"/></svg>`,
  error: `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12" y2="16"/></svg>`,
  info: `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16"/><line x1="12" y1="8" x2="12" y2="8"/></svg>`,
};

/* --------------------------------------------------------------- toasts */
const toastRegion = $("#toast-region");

function toast(kind, title, text = "", { timeout = 6000 } = {}) {
  const icon = kind === "success" ? ICONS.check : kind === "error" ? ICONS.error : ICONS.info;
  const node = el("div", { class: `toast is-${kind}`, attrs: { role: "status" } });
  node.append(
    el("span", { class: "toast-icon", html: icon, attrs: { "aria-hidden": "true" } }),
    el("div", { class: "toast-body" }, [
      el("div", { class: "toast-title", text: title }),
      text ? el("div", { class: "toast-text", text }) : null,
    ]),
  );
  const close = el("button", { class: "toast-close", text: "×", attrs: { "aria-label": "Dismiss notification", type: "button" } });
  const remove = () => { node.remove(); };
  close.addEventListener("click", remove);
  node.append(close);
  toastRegion.append(node);
  if (timeout) setTimeout(remove, timeout);
}

/* ------------------------------------------------------------- notices */
/**
 * Render a friendly inline notice into a result container.
 * For errors we add a collapsible "Show technical detail" area.
 */
function renderNotice(container, kind, title, bodyNodes = [], detail = "") {
  container.hidden = false;
  container.replaceChildren();
  const icon = kind === "success" ? ICONS.check : kind === "warn" ? ICONS.warn
    : kind === "error" ? ICONS.error : ICONS.info;
  const body = el("div", { class: "notice-body" }, [
    el("div", { class: "notice-title", text: title }),
  ]);
  for (const n of [].concat(bodyNodes)) if (n) body.append(n);

  if (detail) {
    const pre = el("pre", { class: "detail-pre", text: detail });
    pre.hidden = true;
    const toggle = el("button", {
      class: "detail-toggle", text: "Show technical detail",
      attrs: { type: "button", "aria-expanded": "false" },
    });
    toggle.addEventListener("click", () => {
      const open = pre.hidden;
      pre.hidden = !open;
      toggle.textContent = open ? "Hide technical detail" : "Show technical detail";
      toggle.setAttribute("aria-expanded", String(open));
    });
    body.append(toggle, pre);
  }

  const notice = el("div", { class: `notice is-${kind}` }, [
    el("span", { class: "notice-icon", html: icon, attrs: { "aria-hidden": "true" } }),
    body,
  ]);
  container.append(notice);
}

function errorDetail(err) {
  if (err instanceof ApiError) return err.detail || `Status ${err.status}`;
  return String(err && err.message ? err.message : err);
}

/* --------------------------------------------------- file picker module */
/**
 * Wires a dropzone + hidden input + file list into a reusable picker.
 * Tracks selected File objects, renders removable rows (with thumbnails
 * for images), and toggles the submit/clear buttons. Returns helpers.
 */
function createPicker({ dropzone, input, listEl, submitBtn, clearBtn, multiple = true, accept = () => true }) {
  let files = [];

  function accepted(file) { return accept(file); }

  function setFiles(next) {
    files = multiple ? next : next.slice(0, 1);
    render();
  }

  function addFiles(fileLike) {
    const incoming = Array.from(fileLike).filter(accepted);
    if (incoming.length === 0) return;
    setFiles(multiple ? files.concat(incoming) : incoming);
  }

  function removeAt(i) { files.splice(i, 1); render(); }

  function clear() { files = []; render(); }

  function render() {
    listEl.replaceChildren();
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      const row = el("li", { class: "file-row" });

      if (f.type && f.type.startsWith("image/")) {
        const img = el("img", { class: "file-thumb", attrs: { alt: "", "aria-hidden": "true" } });
        const url = URL.createObjectURL(f);
        img.src = url;
        img.addEventListener("load", () => URL.revokeObjectURL(url), { once: true });
        row.append(img);
      }

      row.append(el("div", { class: "file-meta" }, [
        el("div", { class: "file-name", text: f.name }),
        el("div", { class: "file-size", text: fmtBytes(f.size) }),
      ]));

      const rm = el("button", {
        class: "file-remove", text: "×",
        attrs: { type: "button", "aria-label": `Remove ${f.name}` },
      });
      rm.addEventListener("click", () => removeAt(i));
      row.append(rm);
      listEl.append(row);
    }

    const has = files.length > 0;
    if (submitBtn) submitBtn.disabled = !has;
    if (clearBtn) clearBtn.hidden = !has;
  }

  // Click / keyboard on dropzone opens picker
  dropzone.addEventListener("click", (e) => {
    // The label's native behavior opens the input; avoid double-trigger.
    if (e.target === input) return;
  });
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });

  input.addEventListener("change", () => {
    addFiles(input.files);
    input.value = ""; // allow re-selecting the same file
  });

  // Drag & drop
  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("is-dragover"); }));
  ["dragleave", "dragend", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("is-dragover"); }));
  dropzone.addEventListener("drop", (e) => {
    if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
  });

  if (clearBtn) clearBtn.addEventListener("click", clear);

  return {
    get files() { return files.slice(); },
    clear,
    hasFiles: () => files.length > 0,
  };
}

/* Accept helpers */
const isImage = (f) => /image\/(jpeg|png)/.test(f.type) || /\.(jpe?g|png)$/i.test(f.name);
// Step 1 accepts photos AND PDFs (a multi-page PDF = one ticket per page).
const isTicketUpload = (f) => isImage(f) || /application\/pdf/.test(f.type) || /\.pdf$/i.test(f.name);
const isXlsx = (f) => /\.xlsx$/i.test(f.name) || f.type.includes("spreadsheetml");
const isSheet = (f) => isXlsx(f) || /\.csv$/i.test(f.name) || f.type.includes("csv");

/* ----------------------------------------------------------- app state */
// `batchIds` tracks every batch created this session so "Start over" can delete
// them server-side.
const state = { lastBatchId: null, batchIds: new Set() };

function rememberBatch(id) {
  if (id) { state.lastBatchId = id; state.batchIds.add(id); }
}

/* ===================================================================== *
 *  Top-level tabs — two independent pipelines
 * ===================================================================== */
/**
 * Accessible tablist: one panel visible at a time, roving tabindex, and
 * arrow-key navigation per the WAI-ARIA tabs pattern.
 */
(function initTabs() {
  const tablist = $('[role="tablist"]');
  if (!tablist) return;
  const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));

  function select(tab, focus = true) {
    for (const t of tabs) {
      const active = t === tab;
      t.setAttribute("aria-selected", String(active));
      t.tabIndex = active ? 0 : -1;
      const panel = document.getElementById(t.getAttribute("aria-controls"));
      if (panel) panel.hidden = !active;
    }
    if (focus) tab.focus();
  }

  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => select(tab, false));
    tab.addEventListener("keydown", (e) => {
      let next = null;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") next = tabs[(i + 1) % tabs.length];
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = tabs[(i - 1 + tabs.length) % tabs.length];
      else if (e.key === "Home") next = tabs[0];
      else if (e.key === "End") next = tabs[tabs.length - 1];
      if (next) { e.preventDefault(); select(next); }
    });
  });
})();

/* ===================================================================== *
 *  STEP 1 — Upload tickets
 * ===================================================================== */
const uploadResult = $("#upload-result");
const uploadPicker = createPicker({
  dropzone: $("#upload-dropzone"),
  input: $("#upload-input"),
  listEl: $("#upload-file-list"),
  submitBtn: $("#upload-submit"),
  clearBtn: $("#upload-clear"),
  multiple: true,
  accept: isTicketUpload,
});

// "Clear list" should also clear the Step-1 result notice (the picker's own
// clear only empties the staged files). The global [hidden] CSS fix makes the
// button hide correctly when the list is empty.
$("#upload-clear").addEventListener("click", () => {
  uploadResult.hidden = true;
  uploadResult.replaceChildren();
});

$("#upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!uploadPicker.hasFiles()) return;
  const btn = $("#upload-submit");
  const files = uploadPicker.files;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Uploading…";
  try {
    const data = await api.uploadImages(files);
    renderUploadResult(data);
    toast("success", "Tickets received", `${pluralize(files.length, "photo")} uploaded.`);
    uploadPicker.clear();
    // Step 2 becomes available once tickets are in; the prior step-3 download
    // (if any) is now stale, so dim it until this batch is extracted again.
    if (data && Array.isArray(data.tickets) && data.tickets.some((t) => t.status !== "error")) {
      setStep2Ready(true);
      setDownloadReady(null);
    }
  } catch (err) {
    renderNotice(uploadResult, "error", "We couldn't upload your tickets",
      [el("p", { class: "notice-text", text: "Please check the files are photos (JPEG/PNG) or PDFs and try again." })],
      errorDetail(err));
    toast("error", "Upload failed", "Please try again.");
    btn.disabled = false;
  } finally {
    btn.textContent = original;
  }
});

function renderUploadResult(data) {
  const tickets = Array.isArray(data && data.tickets) ? data.tickets : [];
  if (data && data.batch_id) rememberBatch(data.batch_id);

  const manual = tickets.filter((t) => t.status === "manual_queue");
  const failed = tickets.filter((t) => t.status === "error");
  const ok = tickets.filter((t) => t.status !== "error");
  const body = [];
  body.push(el("p", { class: "notice-text",
    text: `${pluralize(ok.length, "ticket")} received and ready for step 2.` }));

  const list = el("ul", { class: "ticket-list" });
  for (const t of tickets) {
    const kind = t.status === "manual_queue" ? "manual"
      : t.status === "error" ? "error" : "review";
    const label = kind === "manual" ? "Needs a person"
      : kind === "error" ? "Couldn't upload" : "Ready to review";
    list.append(el("li", { class: "ticket-row" }, [
      el("span", { class: "ticket-id", text: t.ticket_id || t.filename || "Ticket" }),
      el("span", { class: `tag tag-${kind}`, text: label }),
    ]));
  }
  body.push(list);

  if (manual.length > 0) {
    body.push(el("p", { class: "notice-text", html:
      `<strong>${pluralize(manual.length, "ticket")}</strong> couldn't be cleared of patient information automatically, so ${manual.length === 1 ? "it has" : "they have"} been set aside for a person to handle. Nothing is lost — just let your team lead know.` }));
  }

  if (failed.length > 0) {
    const detail = failed.find((t) => t.error)?.error;
    body.push(el("p", { class: "notice-text", html:
      `<strong>${pluralize(failed.length, "photo")}</strong> couldn't be uploaded${detail ? ` — ${detail}` : "."} The other photos were unaffected.` }));
  }

  const kind = failed.length && !ok.length ? "error"
    : failed.length ? "warn" : ok.length ? "success" : "info";
  renderNotice(uploadResult, kind,
    ok.length ? "Tickets received" : failed.length ? "Upload had problems" : "No tickets were read",
    body);
}

/* ===================================================================== *
 *  STEP 2 — Extract data (run batch)
 * ===================================================================== */
const runBtn = $("#run-btn");
const runProgress = $("#run-progress");
const runResult = $("#run-result");

/** Step 2 is actionable only once tickets have been uploaded this session. */
function setStep2Ready(ready) {
  runBtn.disabled = !ready;
}

runBtn.addEventListener("click", async () => {
  if (runBtn.disabled) return;
  runBtn.disabled = true;
  runProgress.hidden = false;
  runResult.hidden = true;
  try {
    const data = await api.runBatch(state.lastBatchId || undefined);
    runProgress.hidden = true;
    if (data && data.batch_id) rememberBatch(data.batch_id);
    renderRunResult(data);
    setDownloadReady(data && data.batch_id);
    toast("success", "Data extracted", "Your review spreadsheet is ready in step 3.");
    loadBatches(); // refresh the history list
  } catch (err) {
    runProgress.hidden = true;
    renderNotice(runResult, "error", "We couldn't process the batch",
      [el("p", { class: "notice-text", text: "Please wait a moment and try again. If it keeps happening, tell your team lead." })],
      errorDetail(err));
    toast("error", "Processing failed", "Please try again.");
  } finally {
    runBtn.disabled = false; // re-enable so a re-run is possible
  }
});

function renderRunResult(data) {
  const count = data && typeof data.ticket_count === "number" ? data.ticket_count : null;
  const body = [el("p", { class: "notice-text",
    text: count != null
      ? `Done. ${pluralize(count, "ticket")} processed. Head to step 3 to download and review.`
      : "Done. Head to step 3 to download and review." })];
  renderNotice(runResult, "success", "Your spreadsheet is ready", body);
}

/* ===================================================================== *
 *  STEP 3 — Download review spreadsheet
 * ===================================================================== */
const downloadLink = $("#download-sheet");
const downloadEmpty = $("#download-empty");

/**
 * Step 3 download stays visibly dimmed (aria-disabled) until extraction has
 * produced a batch. Passing a batchId activates it; passing null re-dims it.
 */
function setDownloadReady(batchId) {
  const ready = Boolean(batchId);
  if (ready) {
    downloadLink.href = api.sheetUrl(batchId);
    downloadLink.removeAttribute("aria-disabled");
    downloadLink.tabIndex = 0;
  } else {
    downloadLink.href = "#";
    downloadLink.setAttribute("aria-disabled", "true");
    downloadLink.tabIndex = -1;
  }
  downloadEmpty.hidden = ready;
}

// Block clicks while the download is disabled (anchors aren't natively disabled).
downloadLink.addEventListener("click", (e) => {
  if (downloadLink.getAttribute("aria-disabled") === "true") e.preventDefault();
});

/* ===================================================================== *
 *  STEP 4 — Send back corrections
 * ===================================================================== */
const correctionsResult = $("#corrections-result");
const correctionsPicker = createPicker({
  dropzone: $("#corrections-dropzone"),
  input: $("#corrections-input"),
  listEl: $("#corrections-file-list"),
  submitBtn: $("#corrections-submit"),
  clearBtn: $("#corrections-clear"),
  multiple: true,
  accept: isXlsx,
});

$("#corrections-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!correctionsPicker.hasFiles()) return;
  const btn = $("#corrections-submit");
  const files = correctionsPicker.files;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Sending…";
  try {
    const data = await api.uploadCorrections(files);
    renderCorrectionsResult(data);
    toast("success", "Corrections received", "Thanks — the tool just got a little smarter.");
    correctionsPicker.clear();
    loadMetrics();
  } catch (err) {
    renderNotice(correctionsResult, "error", "We couldn't read your corrections",
      [el("p", { class: "notice-text", text: "Make sure you're uploading the saved spreadsheet (.xlsx) and try again." })],
      errorDetail(err));
    toast("error", "Upload failed", "Please try again.");
    btn.disabled = false;
  } finally {
    btn.textContent = original;
  }
});

function renderCorrectionsResult(data) {
  const processed = num(data, "processed");
  const matched = num(data, "tickets_matched");
  const unknown = num(data, "tickets_unknown");

  const grid = el("div", { class: "stat-grid" }, [
    stat(processed, "files processed"),
    stat(matched, "tickets matched"),
    stat(unknown, "unknown tickets"),
  ]);
  const body = [grid];

  if (unknown > 0) {
    body.push(el("p", { class: "notice-text", html:
      `An <strong>unknown ticket</strong> just means a row in your spreadsheet didn't line up with a ticket the tool knows about — usually a typo in the ticket number or a row that was added by hand. It's safe to ignore, or double-check those rows.` }));
  }

  renderNotice(correctionsResult, unknown > 0 ? "warn" : "success",
    "Corrections received", body);
}

function num(obj, key) { return obj && typeof obj[key] === "number" ? obj[key] : 0; }
function stat(n, label) {
  return el("div", { class: "stat" }, [
    el("div", { class: "stat-num", text: String(n) }),
    el("div", { class: "stat-label", text: label }),
  ]);
}

/* ===================================================================== *
 *  REFERENCE DATA — four independent lookup-sheet upload tiles
 * ===================================================================== */
function fmtWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) +
    " at " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/**
 * Paint a tile's freshness line: "Updated <date> · N rows" when a sheet has
 * been uploaded, or a friendly "Not uploaded yet" state when it never has.
 * `extra` (optional) appends e.g. parts/lots counts for the Expiry Log.
 */
function renderFreshness(node, rows, updatedAt, extra = "") {
  if (!node) return;
  node.classList.remove("is-empty");
  const n = Number(rows || 0);
  if (!updatedAt && n === 0) {
    node.classList.add("is-empty");
    node.replaceChildren(el("span", { text: "Not uploaded yet" }));
    return;
  }
  const children = [
    el("span", { class: "ref-status-when", text: `Updated ${fmtWhen(updatedAt)}` }),
    el("span", { class: "ref-status-counts", text:
      `· ${n.toLocaleString()} ${n === 1 ? "row" : "rows"}${extra}` }),
  ];
  node.replaceChildren(...children);
}

/**
 * Wire one reference-data tile: picker + submit handler. `upload` is the api
 * call (returns the server payload); `onSuccess(data)` builds the success
 * notice body. Refreshes all freshness lines after a successful upload.
 */
function wireRefTile({ prefix, accept, label, upload, onSuccess }) {
  const resultEl = $(`#${prefix}-result`);
  const picker = createPicker({
    dropzone: $(`#${prefix}-dropzone`),
    input: $(`#${prefix}-input`),
    listEl: $(`#${prefix}-file-list`),
    submitBtn: $(`#${prefix}-submit`),
    clearBtn: null,
    multiple: false,
    accept,
  });

  $(`#${prefix}-form`).addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!picker.hasFiles()) return;
    const btn = $(`#${prefix}-submit`);
    const file = picker.files[0];
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "Updating…";
    try {
      const data = await upload(file);
      renderNotice(resultEl, "success", `${label} updated`, onSuccess(data));
      toast("success", `${label} updated`, "Takes effect immediately for new batches.");
      picker.clear();
      loadReferenceStatus(); // refresh every tile's freshness line
    } catch (err) {
      renderNotice(resultEl, "error", `We couldn't update ${label}`,
        [el("p", { class: "notice-text", text: "Please check the file and try again." })],
        errorDetail(err));
      toast("error", "Update failed", "Please try again.");
      btn.disabled = false;
    } finally {
      btn.textContent = original;
    }
  });
}

/* GTIN / Part Info / Surgeon — routed through the masters endpoint. */
wireRefTile({
  prefix: "gtin", accept: isSheet, label: "GTIN codes",
  upload: (f) => api.uploadMaster("gtin", f),
  onSuccess: (d) => [el("div", { class: "stat-grid stat-grid-1" }, [stat(num(d, "gtin_rows"), "rows")])],
});
wireRefTile({
  prefix: "part", accept: isSheet, label: "Part info",
  upload: (f) => api.uploadMaster("part_info", f),
  onSuccess: (d) => [el("div", { class: "stat-grid stat-grid-1" }, [stat(num(d, "part_rows"), "rows")])],
});
wireRefTile({
  prefix: "surgeon", accept: isSheet, label: "Surgeon info",
  upload: (f) => api.uploadMaster("surgeon", f),
  onSuccess: (d) => [el("div", { class: "stat-grid stat-grid-1" }, [stat(num(d, "surgeon_rows"), "rows")])],
});

/* Expiry Log — its own endpoint, .xlsx only, with parts/lots stats. */
wireRefTile({
  prefix: "reference", accept: isXlsx, label: "Expiry Log",
  upload: (f) => api.uploadReferenceLog(f),
  onSuccess: (d) => [el("div", { class: "stat-grid" }, [
    stat(num(d, "row_count"), "rows"),
    stat(num(d, "unique_parts"), "unique parts"),
    stat(num(d, "unique_lots"), "unique lots"),
  ])],
});

/* Freshness elements, one per tile. */
const gtinStatus = $("#gtin-status");
const partStatus = $("#part-status");
const surgeonStatus = $("#surgeon-status");
const referenceStatus = $("#reference-status");

/**
 * Read the /reference/status payload (new shape) and populate every tile's
 * freshness line. Handles never-uploaded sheets (rows 0 / updated_at null).
 */
async function loadReferenceStatus() {
  try {
    const s = await api.referenceStatus();
    const m = (s && s.masters) || {};
    const gtin = m.gtin || {};
    const part = m.part_info || {};
    const surgeon = m.surgeon || {};
    const log = (s && s.log) || s || {};

    renderFreshness(gtinStatus, gtin.rows, gtin.updated_at);
    renderFreshness(partStatus, part.rows, part.updated_at);
    renderFreshness(surgeonStatus, surgeon.rows, surgeon.updated_at);

    const parts = Number(log.unique_parts || 0);
    const lots = Number(log.unique_lots || 0);
    const extra = (parts || lots)
      ? ` · ${parts.toLocaleString()} parts, ${lots.toLocaleString()} lots` : "";
    renderFreshness(referenceStatus, log.row_count, log.updated_at, extra);
  } catch {
    // Leave whatever is shown; tiles stay usable even if status fails.
  }
}

/* ===================================================================== *
 *  SECONDARY — Past batches
 * ===================================================================== */
const batchesList = $("#batches-list");

async function loadBatches() {
  batchesList.replaceChildren(el("p", { class: "muted-note", text: "Loading…" }));
  try {
    const batches = await api.listBatches();
    renderBatches(Array.isArray(batches) ? batches : []);
  } catch (err) {
    batchesList.replaceChildren(
      el("div", { class: "empty-state" }, [
        el("strong", { text: "Couldn't load past batches" }),
        el("span", { text: "Press Refresh to try again." }),
      ]));
  }
}

function renderBatches(batches) {
  if (batches.length === 0) {
    batchesList.replaceChildren(
      el("div", { class: "empty-state" }, [
        el("strong", { text: "No batches yet" }),
        el("span", { text: "Once you finish step 2, your spreadsheets will appear here." }),
      ]));
    return;
  }
  // newest first if run_date is comparable
  const sorted = batches.slice().sort((a, b) =>
    String(b.run_date || "").localeCompare(String(a.run_date || "")));
  batchesList.replaceChildren(...sorted.map((b) => {
    const row = el("div", { class: "batch-row" }, [
      el("div", { class: "batch-meta" }, [
        el("div", { class: "batch-date", text: friendlyDate(b.run_date) || "Batch" }),
        el("div", { class: "batch-sub", text:
          `${pluralize(num(b, "ticket_count"), "ticket")}${b.status ? " · " + humanStatus(b.status) : ""}` }),
      ]),
    ]);
    const link = el("a", {
      class: "btn btn-secondary btn-sm",
      text: "Download",
      attrs: { href: api.sheetUrl(b.batch_id), download: "" },
    });
    row.append(link);
    return row;
  }));
}

function friendlyDate(raw) {
  if (!raw) return "";
  const d = new Date(raw);
  if (isNaN(d.getTime())) return String(raw);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
function humanStatus(s) {
  return String(s).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

$("#batches-refresh").addEventListener("click", loadBatches);

/* ===================================================================== *
 *  SECONDARY — Auto-resolve metric
 * ===================================================================== */
const metricsChart = $("#metrics-chart");

async function loadMetrics() {
  metricsChart.replaceChildren(el("p", { class: "muted-note", text: "Loading…" }));
  try {
    const data = await api.autoResolveMetrics(8);
    renderMetrics(Array.isArray(data) ? data : []);
  } catch (err) {
    metricsChart.replaceChildren(
      el("div", { class: "empty-state" }, [
        el("strong", { text: "Couldn't load this yet" }),
        el("span", { text: "It will show up once there's some history." }),
      ]));
  }
}

function renderMetrics(points) {
  if (points.length === 0) {
    metricsChart.replaceChildren(
      el("div", { class: "empty-state" }, [
        el("strong", { text: "Nothing to show just yet" }),
        el("span", { text: "As you process batches and send corrections, you'll watch this number climb. Keep going!" }),
      ]));
    return;
  }
  const bars = el("div", { class: "metric-bars", attrs: { role: "img",
    "aria-label": "Weekly share of labels read confidently" } });
  for (const p of points) {
    const pct = clampPct(p.pct_confident);
    const col = el("div", { class: "metric-col" });
    col.append(el("div", { class: "metric-val", text: `${Math.round(pct)}%` }));
    const wrap = el("div", { class: "metric-bar-wrap" });
    const bar = el("div", { class: "metric-bar" });
    bar.style.height = `${pct}%`;
    wrap.append(bar);
    col.append(wrap);
    col.append(el("div", { class: "metric-week", text: weekLabel(p.week) }));
    bars.append(col);
  }
  metricsChart.replaceChildren(bars);
}

function clampPct(v) {
  let n = Number(v);
  if (!isFinite(n)) n = 0;
  if (n <= 1) n *= 100; // accept either 0..1 or 0..100
  return Math.max(0, Math.min(100, n));
}
function weekLabel(w) {
  if (w == null) return "";
  const d = new Date(w);
  if (!isNaN(d.getTime())) return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return String(w);
}

/* ===================================================================== *
 *  Health check (header status)
 * ===================================================================== */
async function checkHealth() {
  const dot = $("#health-dot");
  const label = $("#health-label");
  try {
    const data = await api.health();
    const ok = data && data.status === "ok";
    dot.className = `health-dot ${ok ? "is-ok" : "is-down"}`;
    label.textContent = ok ? "Connected" : "Service issue";
  } catch {
    dot.className = "health-dot is-down";
    label.textContent = "Offline";
  }
}

/* ===================================================================== *
 *  What's New modal
 * ===================================================================== */
const whatsNewBtn   = $("#whats-new-btn");
const whatsNewModal = $("#whats-new-modal");
const whatsNewClose = $("#whats-new-close");
const changelogBody = $("#changelog-body");
const footerVersion = $("#footer-version");

let changelogLoaded = false;

function renderChangelog(changelog) {
  changelogBody.replaceChildren();
  for (const entry of changelog) {
    const section = el("div", { class: "cl-entry" });
    section.append(
      el("div", { class: "cl-header" }, [
        el("span", { class: "cl-version", text: `v${entry.version}` }),
        el("span", { class: "cl-date", text: entry.date }),
      ]),
      el("ul", { class: "cl-notes" },
        (entry.notes || []).map((n) => el("li", { text: n }))
      )
    );
    changelogBody.append(section);
  }
}

async function loadChangelog() {
  if (changelogLoaded) return;
  changelogBody.replaceChildren(el("p", { class: "muted-note", text: "Loading…" }));
  try {
    const data = await api.getVersion();
    if (data && data.changelog) renderChangelog(data.changelog);
    if (data && data.version) {
      whatsNewBtn.textContent = `What's New · v${data.version}`;
      if (footerVersion) footerVersion.textContent = `v${data.version}`;
    }
    changelogLoaded = true;
  } catch {
    changelogBody.replaceChildren(el("p", { class: "muted-note", text: "Couldn't load changelog." }));
  }
}

function openWhatsNew() {
  loadChangelog();
  whatsNewModal.hidden = false;
}
function closeWhatsNew() {
  whatsNewModal.hidden = true;
}

whatsNewBtn.addEventListener("click", openWhatsNew);
whatsNewClose.addEventListener("click", closeWhatsNew);

// Click the dimmed backdrop (but not the card) to dismiss.
whatsNewModal.addEventListener("click", (e) => {
  if (e.target === whatsNewModal) closeWhatsNew();
});

// ESC closes it.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !whatsNewModal.hidden) closeWhatsNew();
});

/* ===================================================================== *
 *  Start over — wipe this session's work and return to step 1
 * ===================================================================== */
$("#start-over-btn").addEventListener("click", async () => {
  const ok = window.confirm(
    "Start over? This permanently deletes the tickets you uploaded and the " +
    "spreadsheet generated in this session, and returns you to step 1. " +
    "This cannot be undone."
  );
  if (!ok) return;

  const ids = Array.from(state.batchIds);
  // Delete server-side work (best-effort; UI resets regardless).
  await Promise.all(ids.map((id) => api.deleteBatch(id).catch(() => {})));

  // Reset the UI back to step 1.
  uploadPicker.clear();
  correctionsPicker.clear();
  for (const node of [uploadResult, runResult, correctionsResult]) {
    if (node) { node.hidden = true; node.replaceChildren(); }
  }
  runProgress.hidden = true;
  state.lastBatchId = null;
  state.batchIds.clear();
  setStep2Ready(false);
  setDownloadReady(null);
  loadBatches();   // Past batches list no longer includes the deleted ones
  toast("info", "Started over", "You're back at step 1.");
});

/* ===================================================================== *
 *  Boot
 * ===================================================================== */
checkHealth();
loadBatches();
loadMetrics();
loadReferenceStatus();
loadChangelog(); // prefetch version + populate footer quietly
