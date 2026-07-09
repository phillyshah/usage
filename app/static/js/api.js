/*
 * api.js — thin fetch wrapper around the Usage backend.
 *
 * Every call returns a Promise that resolves to parsed data, or rejects
 * with an ApiError carrying a friendly message plus optional technical
 * detail (so the UI can show "Something went wrong" with a collapsible
 * detail area). All URLs are relative so the app works same-origin behind
 * a firewall with no external dependencies.
 */

export class ApiError extends Error {
  constructor(message, { status = 0, detail = "" } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

const FRIENDLY = "Something went wrong. Please try again.";

/**
 * Core request helper. Parses JSON when possible, builds an ApiError with
 * a human message and a technical detail string on failure.
 */
async function request(url, options = {}) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (networkErr) {
    // fetch only rejects on network-level failures (offline, DNS, CORS).
    throw new ApiError(
      "Can't reach the server. Check your connection and try again.",
      { detail: String(networkErr && networkErr.message ? networkErr.message : networkErr) }
    );
  }

  // Try to read the body once, as text, then attempt JSON.
  const raw = await res.text();
  let data = null;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = raw; // not JSON (e.g. a plain error string)
    }
  }

  if (!res.ok) {
    // Pull a server-provided message if there is one (FastAPI uses "detail").
    let serverMsg = "";
    if (data && typeof data === "object") {
      if (typeof data.detail === "string") serverMsg = data.detail;
      else if (typeof data.message === "string") serverMsg = data.message;
    } else if (typeof data === "string") {
      serverMsg = data;
    }
    throw new ApiError(FRIENDLY, {
      status: res.status,
      detail: serverMsg || `${res.status} ${res.statusText}`,
    });
  }

  return data;
}

/** Build a multipart FormData from a list of File objects under one field. */
function filesToForm(field, files) {
  const fd = new FormData();
  for (const f of files) fd.append(field, f, f.name);
  return fd;
}

export const api = {
  /** GET /health -> {status:"ok"} */
  health() {
    return request("/health", { method: "GET" });
  },

  /**
   * POST /images (multipart, field "files")
   * -> 202 {batch_id, tickets:[{ticket_id, status}]}
   */
  uploadImages(files) {
    return request("/images", {
      method: "POST",
      body: filesToForm("files", files),
    });
  },

  /**
   * POST /batches/run (json optional {batch_id})
   * -> 200 {batch_id, sheet_path, ticket_count}
   * This can take a while; caller should show indeterminate progress.
   */
  runBatch(batchId) {
    const body = batchId ? JSON.stringify({ batch_id: batchId }) : JSON.stringify({});
    return request("/batches/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
  },

  /** GET /batches -> [{batch_id, run_date, ticket_count, status}] */
  listBatches() {
    return request("/batches", { method: "GET" });
  },

  /** Relative URL for the review sheet download (used as an href / anchor). */
  sheetUrl(batchId) {
    return `/batches/${encodeURIComponent(batchId)}/sheet`;
  },

  /** DELETE /batches/{id} -> permanently remove a batch + its tickets/images. */
  deleteBatch(batchId) {
    return request(`/batches/${encodeURIComponent(batchId)}`, { method: "DELETE" });
  },

  /**
   * POST /corrections/upload (multipart, field "files")
   * -> 200 {processed, tickets_matched, tickets_unknown}
   */
  uploadCorrections(files) {
    return request("/corrections/upload", {
      method: "POST",
      body: filesToForm("files", files),
    });
  },

  /**
   * POST /reference/log (multipart, single file, field "file")
   * -> 200 {row_count, unique_parts, unique_lots}
   */
  uploadReferenceLog(file) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    return request("/reference/log", { method: "POST", body: fd });
  },

  /**
   * POST /reference/masters (multipart): field "files" (the file) + form
   * field "kind" = "gtin" | "part_info" | "surgeon".
   * -> 200 {gtin_rows, part_rows, surgeon_rows} (routed one int, others null)
   */
  uploadMaster(kind, file) {
    const fd = new FormData();
    fd.append("files", file, file.name);
    fd.append("kind", kind);
    return request("/reference/masters", { method: "POST", body: fd });
  },

  /**
   * GET /metrics/auto-resolve?weeks=N -> [{week, pct_confident}]
   */
  autoResolveMetrics(weeks = 8) {
    return request(`/metrics/auto-resolve?weeks=${encodeURIComponent(weeks)}`, {
      method: "GET",
    });
  },

  /**
   * GET /metrics/auto-resolve-daily?days=N (ascending date)
   * -> [{date, pct_confident, fields, confident}]
   */
  autoResolveDaily(days = 14) {
    return request(`/metrics/auto-resolve-daily?days=${encodeURIComponent(days)}`, {
      method: "GET",
    });
  },

  /**
   * GET /metrics/learning?days=N
   * -> {cumulative:{prices, part_descriptions, reps, gtin_links, surgeon_links},
   *     daily:[{date, corrections_made, blanks_filled, low_conf_fixed,
   *             facts_learned:{prices, part_descriptions, reps, gtin_links, surgeon_links}}]}
   *     (daily newest-first)
   */
  learningMetrics(days = 400) {
    return request(`/metrics/learning?days=${encodeURIComponent(days)}`, {
      method: "GET",
    });
  },

  /**
   * GET /corrections/uploads?limit=N
   * -> [{uploaded_at, sheets_processed, tickets_matched, tickets_unknown, status}]
   */
  correctionsUploads(limit = 1000) {
    return request(`/corrections/uploads?limit=${encodeURIComponent(limit)}`, {
      method: "GET",
    });
  },

  /**
   * GET /metrics/learning/{kind} — full rows for one learning store.
   * kind: "prices" | "part-descriptions" | "reps" | "gtin-links"
   */
  learningDetail(kind) {
    return request(`/metrics/learning/${encodeURIComponent(kind)}`, { method: "GET" });
  },

  /**
   * GET /metrics/learning/health
   * -> {status:"ok"|"stale"|"at_risk"|"empty", total, last_learned, tables:[...]}
   */
  learningHealth() {
    return request("/metrics/learning/health", { method: "GET" });
  },

  /**
   * POST /debug/trace (multipart, single file, field "file")
   * -> {ticket_id, filename, status, steps:[{stage,label,status,summary,detail}], result}
   * Runs the full extraction pipeline on one ticket with step-by-step trace.
   */
  debugTrace(file) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    return request("/debug/trace", { method: "POST", body: fd });
  },

  /**
   * POST /debug/trace/{ticket_id}/correct
   * body: {confirm_all?, header?: {field:value}, lines?: {line_id: {field:value}}}
   * -> {ticket_id, status:"verified", learned:{part_desc,rep,price,gtin_xref,surgeon_map}, audited_fields}
   */
  debugCorrect(ticketId, body) {
    return request(`/debug/trace/${encodeURIComponent(ticketId)}/correct`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  /** GET /version -> {version, changelog:[{version, date, notes:[]}]} */
  getVersion() {
    return request("/version", { method: "GET" });
  },

  /** GET /reference/status -> {loaded, updated_at, row_count, unique_parts, unique_lots} */
  referenceStatus() {
    return request("/reference/status", { method: "GET" });
  },
};
