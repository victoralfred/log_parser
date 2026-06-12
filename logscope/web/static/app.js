/* logscope dashboard — vanilla JS, no build step.
   One global `filters` object drives every widget; any widget that changes
   it calls refresh(). */

const filters = {};        // service, level, component, fingerprint, since, until, q
let page = 0;
const PAGE_SIZE = 100;
let scanPoll = null;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${path}: ${res.status} ${await res.text()}`);
  return res.json();
}

function fmtTs(epoch) {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toISOString().replace("T", " ").slice(0, 19);
}

function setFilter(key, value) {
  if (value === undefined || value === null || value === "") delete filters[key];
  else filters[key] = value;
  page = 0;
  refresh();
}

function clearFilters() {
  for (const k of Object.keys(filters)) delete filters[k];
  $("search-input").value = "";
  $("search-error").textContent = "";
  page = 0;
  refresh();
}

function filterQuery(extra = {}) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries({...filters, ...extra}))
    if (v !== undefined && v !== null && v !== "") params.set(k, v);
  return params.toString();
}

/* ---------- modal ---------- */

let modalRawText = "";   // what the copy button copies

function openModal(title, {console: consoleSkin = false, extraHTML = ""} = {}) {
  $("modal-title").textContent = title;
  $("modal-extra").innerHTML = extraHTML;
  const body = $("modal-body");
  body.className = consoleSkin ? "modal-console" : "";
  body.innerHTML = "";
  $("modal-overlay").hidden = false;
  document.body.style.overflow = "hidden";
  return body;
}

function closeModal() {
  $("modal-overlay").hidden = true;
  document.body.style.overflow = "";
  modalRawText = "";
}

$("modal-close").onclick = closeModal;
$("modal-overlay").addEventListener("click", (ev) => {
  if (ev.target === $("modal-overlay")) closeModal();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !$("modal-overlay").hidden) closeModal();
});
$("modal-copy").onclick = async () => {
  try {
    await navigator.clipboard.writeText(modalRawText);
    $("modal-copy").textContent = "copied ✓";
  } catch {
    $("modal-copy").textContent = "copy failed";
  }
  setTimeout(() => { $("modal-copy").textContent = "copy"; }, 1200);
};

function renderFilterBar() {
  const parts = Object.entries(filters).map(([k, v]) =>
    `${k}=${String(v).slice(0, 30)}`);
  $("active-filters").textContent = parts.length ? `filters: ${parts.join("  ")}` : "";
  $("clear-filters").hidden = parts.length === 0;
}

/* ---------- filter state <-> URL hash ---------- */

let applyingHash = false;

function writeHash() {
  if (applyingHash) return;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) params.set(k, v);
  if (page) params.set("page", page);
  const hash = params.toString();
  if (hash !== location.hash.slice(1))
    history.replaceState(null, "", hash ? "#" + hash : location.pathname);
}

function readHash() {
  for (const k of Object.keys(filters)) delete filters[k];
  page = 0;
  for (const [k, v] of new URLSearchParams(location.hash.slice(1))) {
    if (k === "page") page = Math.max(0, parseInt(v, 10) || 0);
    else if (k === "since" || k === "until") filters[k] = parseFloat(v);
    else filters[k] = v;
  }
  // sync widgets from restored state
  if (filters.q) { $("search-input").value = filters.q; $("search-regex").checked = false; }
  if (filters.regex) { $("search-input").value = filters.regex; $("search-regex").checked = true; }
}

window.addEventListener("hashchange", () => {
  applyingHash = true;
  readHash();
  refresh().finally(() => { applyingHash = false; });
});

/* ---------- scanners sidebar ---------- */

async function loadScanners() {
  const data = await api("/api/scanners");
  const div = $("scanner-list");
  div.innerHTML = "";
  for (const s of data.scanners) {
    const card = document.createElement("div");
    card.className = "scanner" + (s.ok ? "" : " broken");
    card.innerHTML = s.ok
      ? `<label><input type="checkbox" class="scanner-pick" value="${esc(s.name)}" checked>
           <span class="name">${esc(s.name)}</span></label>
         <span class="badge channel">${esc(s.channel)}</span>
         <span class="badge ok">ok</span>
         <div class="meta">${esc(s.description)}</div>`
      : `<span class="name">${esc(s.name)}</span>
         <span class="badge broken">broken</span>
         <div class="err">${esc(s.error || "")}</div>`;
    div.appendChild(card);
  }
}

function pickedScanners() {
  return [...document.querySelectorAll(".scanner-pick:checked")].map(c => c.value);
}

/* ---------- scan trigger + progress ---------- */

function scanLevels() {
  const picked = [...document.querySelectorAll("#scan-levels input:checked")]
    .map(c => c.value);
  return picked.includes("ALL") || !picked.length ? null : picked;
}

// "ALL" is mutually exclusive with specific levels
document.querySelectorAll("#scan-levels input").forEach(box => {
  box.addEventListener("change", () => {
    const all = document.querySelector('#scan-levels input[value="ALL"]');
    if (box.value === "ALL" && box.checked)
      document.querySelectorAll("#scan-levels input").forEach(c => {
        if (c.value !== "ALL") c.checked = false;
      });
    else if (box.checked) all.checked = false;
    if (![...document.querySelectorAll("#scan-levels input")].some(c => c.checked))
      all.checked = true;
  });
});

async function startScan() {
  const root = $("root-input").value.trim() || ".";
  localStorage.setItem("logscope-root", root);
  $("scan-status").textContent = "starting…";
  try {
    const {run_id} = await api("/api/scan", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({scanners: pickedScanners(), root,
                            levels: scanLevels()}),
    });
    if (scanPoll) clearInterval(scanPoll);
    scanPoll = setInterval(() => pollScan(run_id), 1000);
    pollScan(run_id);
  } catch (e) {
    $("scan-status").textContent = e.message;
  }
}

async function pollScan(runId) {
  const run = await api(`/api/scan/${runId}`);
  const done = run.sources.filter(s => s.status === "done").length;
  const failed = run.sources.filter(s => s.status === "failed");
  const skipped = run.sources.reduce((a, s) => a + (s.skipped || 0), 0);
  $("scan-status").textContent =
    run.status === "running"
      ? `scanning… ${done}/${run.sources.length} sources, ${run.record_count} records`
      : `scan ${run.status}: ${run.record_count} records from ${done} sources` +
        (skipped ? ` (${skipped.toLocaleString()} skipped by level filter)` : "") +
        (failed.length ? `, ${failed.length} failed (${failed.map(f => f.scanner).join(", ")})` : "");
  if (run.status !== "running") {
    clearInterval(scanPoll);
    scanPoll = null;
    refresh();
  }
}

/* ---------- summary matrix ---------- */

const LEVEL_ORDER = ["CRITICAL", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"];

async function loadMatrix() {
  const data = await api("/api/summary");
  const services = [...new Set(data.matrix.map(r => r.service))].sort();
  const levels = LEVEL_ORDER.filter(l => data.matrix.some(r => r.level === l));
  const counts = {};
  for (const r of data.matrix) counts[`${r.service}|${r.level}`] = r.n;

  let html = `<p class="muted">${data.total.toLocaleString()} records
    (${data.unparsed} unparsed) · ${fmtTs(data.first_ts)} → ${fmtTs(data.last_ts)}</p>
    <table><tr><th>service</th>${levels.map(l =>
      `<th class="lvl lvl-${l}">${l}</th>`).join("")}<th>total</th></tr>`;
  for (const svc of services) {
    let total = 0;
    const cells = levels.map(l => {
      const n = counts[`${svc}|${l}`] || 0;
      total += n;
      return `<td class="num cell" data-svc="${esc(svc)}" data-lvl="${l}">
        ${n ? n.toLocaleString() : ""}</td>`;
    }).join("");
    html += `<tr><td>${esc(svc)}</td>${cells}<td class="num">${total.toLocaleString()}</td></tr>`;
  }
  html += "</table>";
  $("matrix").innerHTML = html;
  for (const cell of document.querySelectorAll("#matrix .cell"))
    cell.onclick = () => {
      filters.service = cell.dataset.svc;
      filters.level = cell.dataset.lvl;
      delete filters.fingerprint;
      syncLevelChecks();
      page = 0;
      refresh();
    };
}

/* ---------- timeline canvas ---------- */

const LEVEL_COLORS = {INFO: "#1a7f37", WARN: "#bf8700", ERROR: "#cf222e",
                      CRITICAL: "#a40e26", DEBUG: "#6e7781", TRACE: "#6e7781"};
const BUCKET_STEPS = [60, 300, 900, 3600, 14400, 86400];
let tlBuckets = [];
let tlBucket = 900;

function pickBucket(firstTs, lastTs) {
  const span = Math.max(60, (lastTs || 0) - (firstTs || 0));
  for (const step of BUCKET_STEPS)
    if (span / step <= 180) return step;
  return BUCKET_STEPS[BUCKET_STEPS.length - 1];
}

async function loadTimeline() {
  // auto-size buckets to the visible span (zoomed window or full data span)
  let first = filters.since, last = filters.until;
  if (first === undefined || last === undefined) {
    const s = await api("/api/summary");
    first = filters.since ?? s.first_ts;
    last = filters.until ?? s.last_ts;
  }
  tlBucket = pickBucket(first, last);
  $("tl-reset").hidden = filters.since === undefined && filters.until === undefined;

  const level = $("tl-level").value;
  const params = new URLSearchParams();
  if (level) params.set("level", level);
  if (filters.service) params.set("service", filters.service);
  if (filters.fingerprint) params.set("fingerprint", filters.fingerprint);
  if (filters.regex) params.set("regex", filters.regex);
  if (filters.since !== undefined) params.set("since", filters.since);
  if (filters.until !== undefined) params.set("until", filters.until);
  const data = await api(`/api/timeline?bucket=${tlBucket}&${params}`);

  const canvas = $("timeline");
  const ctx = canvas.getContext("2d");
  canvas.width = canvas.clientWidth * devicePixelRatio;
  canvas.height = 120 * devicePixelRatio;
  ctx.scale(devicePixelRatio, devicePixelRatio);
  const W = canvas.clientWidth, H = 120;
  ctx.clearRect(0, 0, W, H);
  if (!data.points.length) {
    ctx.fillStyle = "#84718e";
    ctx.fillText("no data — run a scan", 10, 60);
    tlBuckets = [];
    return;
  }

  // aggregate stacked counts per bucket
  const byBucket = new Map();
  for (const p of data.points) {
    if (!byBucket.has(p.bucket_ts)) byBucket.set(p.bucket_ts, {});
    byBucket.get(p.bucket_ts)[p.level] = p.n;
  }
  const tsList = [...byBucket.keys()].sort((a, b) => a - b);
  const t0 = tsList[0], t1 = tsList[tsList.length - 1] + tlBucket;
  const nSlots = Math.max(1, Math.round((t1 - t0) / tlBucket));
  const barW = Math.max(1, W / nSlots);
  const maxTotal = Math.max(...tsList.map(ts =>
    Object.values(byBucket.get(ts)).reduce((a, b) => a + b, 0)));

  tlBuckets = [];
  for (const ts of tsList) {
    const x = ((ts - t0) / (t1 - t0)) * W;
    let y = H - 14;
    const stack = byBucket.get(ts);
    for (const lvl of ["INFO", "DEBUG", "TRACE", "WARN", "ERROR", "CRITICAL"]) {
      const n = stack[lvl];
      if (!n) continue;
      const h = Math.max(1, (n / maxTotal) * (H - 22));
      ctx.fillStyle = LEVEL_COLORS[lvl] || "#84718e";
      ctx.fillRect(x, y - h, Math.max(barW - 1, 1), h);
      y -= h;
    }
    tlBuckets.push({x, w: barW, ts});
  }
  // axis labels
  ctx.fillStyle = "#84718e";
  ctx.font = "10px sans-serif";
  ctx.fillText(fmtTs(t0), 4, H - 3);
  const endLabel = fmtTs(t1);
  ctx.fillText(endLabel, W - ctx.measureText(endLabel).width - 4, H - 3);
}

$("timeline").addEventListener("click", (ev) => {
  const rect = ev.target.getBoundingClientRect();
  const x = ev.clientX - rect.left;
  const hit = tlBuckets.find(b => x >= b.x && x <= b.x + b.w);
  if (hit) {
    filters.since = hit.ts;
    filters.until = hit.ts + tlBucket;
    page = 0;
    refresh();
  }
});

/* ---------- fingerprints ---------- */

async function loadFingerprints() {
  const params = new URLSearchParams({limit: 25});
  if (filters.service) params.set("service", filters.service);
  if (filters.level) params.set("level", filters.level);
  const data = await api(`/api/fingerprints?${params}`);
  let html = `<table><tr><th>count</th><th>worst</th><th>services</th>
    <th>template</th><th>first</th><th>last</th></tr>`;
  for (const f of data.fingerprints) {
    html += `<tr class="clickable" data-fp="${esc(f.fingerprint)}">
      <td class="num">${f.count.toLocaleString()}</td>
      <td class="lvl lvl-${esc(f.worst_level)}">${esc(f.worst_level)}</td>
      <td>${esc(f.services)}</td>
      <td class="tmpl">${esc(f.template.slice(0, 140))}</td>
      <td>${fmtTs(f.first_ts)}</td><td>${fmtTs(f.last_ts)}</td></tr>`;
  }
  html += "</table>";
  $("fingerprints").innerHTML = html;
  for (const row of document.querySelectorAll("#fingerprints .clickable"))
    row.onclick = () => setFilter("fingerprint", row.dataset.fp);
}

/* ---------- records ---------- */

async function loadRecords() {
  let data;
  try {
    data = await api(`/api/records?${filterQuery(
      {limit: PAGE_SIZE, offset: page * PAGE_SIZE})}`);
    $("search-error").textContent = "";
  } catch (e) {
    // bad regex etc. — show inline, leave the current table untouched
    const m = e.message.match(/invalid regex[^"}]*/);
    $("search-error").textContent = m ? m[0] : e.message;
    return;
  }
  $("rec-count").textContent = `(${data.total.toLocaleString()} match)`;
  let html = `<table><tr><th>time</th><th>level</th><th>service</th>
    <th>component</th><th>message</th></tr>`;
  for (const r of data.records) {
    html += `<tr class="clickable rec-row" data-id="${r.id}">
      <td>${esc(r.time)}</td>
      <td class="lvl lvl-${esc(r.level)}">${esc(r.level || "?")}</td>
      <td>${esc(r.service)}</td><td>${esc(r.component)}</td>
      <td class="rec-msg">${esc(r.msg.slice(0, 220))}</td></tr>`;
  }
  html += "</table>";
  $("records").innerHTML = html;
  $("page-info").textContent =
    `page ${page + 1} / ${Math.max(1, Math.ceil(data.total / PAGE_SIZE))}`;
  for (const row of document.querySelectorAll(".rec-row"))
    row.onclick = () => openRecordModal(+row.dataset.id);
}

async function openRecordModal(id) {
  const r = await api(`/api/records/${id}`);
  const headerLine = r.logger
    ? `${r.time} | ${r.logger} | ${r.level} | (${r.file}:${r.line} in ${r.func})`
    : r.time;
  const lines = [headerLine, "", r.msg, ...(r.continuation || [])];
  const metaPairs = [
    ["service", r.service], ["component", r.component],
    ["channel", r.channel], ["source", `${r.source}:${r.lineno}`],
    ["fingerprint", r.fingerprint],
    ...Object.entries(r.extra || {}),
  ].filter(([, v]) => v);
  modalRawText = lines.join("\n") + "\n\n" +
    metaPairs.map(([k, v]) => `${k}: ${v}`).join("\n");

  const body = openModal(`${r.level || "log"} record #${r.id}`, {console: true});
  body.innerHTML = `<pre><span class="console-meta">${esc(r.time)} | ${esc(r.logger)} | </span><span class="console-level-${esc(r.level)}">${esc(r.level)}</span><span class="console-meta"> | (${esc(r.file)}:${r.line} in ${esc(r.func)})</span>

${esc(r.msg)}${(r.continuation || []).length ? "\n" + esc(r.continuation.join("\n")) : ""}

<span class="console-meta">${metaPairs.map(([k, v]) => `${esc(k)}: ${esc(v)}`).join("\n")}</span></pre>`;
}

/* ---------- gaps ---------- */

async function loadGaps() {
  const data = await api("/api/gaps?threshold=300");
  const div = $("gaps");
  if (!data.gaps.length) { div.innerHTML = '<div class="muted">none detected</div>'; return; }
  div.innerHTML = data.gaps.slice(0, 10).map(g =>
    `<div class="gap-item"><b>${esc(g.service)}</b> silent
     ${Math.round(g.duration / 60)} min<br>${fmtTs(g.from_ts)} → ${fmtTs(g.to_ts)}</div>`).join("");
}

/* ---------- plugin panels ---------- */

async function loadPanels() {
  const data = await api("/api/panels");
  const container = $("plugin-panels");
  container.innerHTML = "";
  for (const p of data.panels) {
    const card = document.createElement("div");
    card.className = "panel-card";
    card.innerHTML = `<h2>${esc(p.title)} <span class="badge channel">${esc(p.scanner)}</span></h2>
      <div class="panel-body muted">loading…</div>`;
    container.appendChild(card);
    api(p.data_url).then(d => {
      const body = card.querySelector(".panel-body");
      body.classList.remove("muted");
      if (p.kind === "table" && d.columns) {
        body.innerHTML = `<table><tr>${d.columns.map(c => `<th>${esc(c)}</th>`).join("")}</tr>
          ${(d.rows || []).map(r => `<tr>${r.map(v => `<td>${esc(v)}</td>`).join("")}</tr>`).join("")}</table>`;
        if (!(d.rows || []).length) body.innerHTML += '<div class="muted">no entries</div>';
      } else if (p.kind === "stat" && d.stats) {
        body.innerHTML = `<div class="stat-row">${d.stats.map(s =>
          `<div><div class="v">${esc(s.value)}</div><div class="l">${esc(s.label)}</div></div>`).join("")}</div>`;
      } else if (p.kind === "html" && d.html) {
        body.innerHTML = d.html;   // plugin author is responsible for safety
      } else {
        body.textContent = JSON.stringify(d).slice(0, 500);
      }
    }).catch(e => { card.querySelector(".panel-body").textContent = e.message; });
  }
}

/* ---------- flare documents browser ---------- */

const DOC_TAB_LABELS = {config: "Configurations", metadata: "Metadata",
                        "log-other": "Other logs", other: "Other"};
let docCategory = "config";
let docSearchTimer = null;

async function loadDocuments() {
  const params = new URLSearchParams({category: docCategory});
  const q = $("doc-search").value.trim();
  if (q) params.set("q", q);
  const data = await api(`/api/documents?${params}`);
  const counts = data.counts || {};
  const totalDocs = Object.values(counts).reduce((a, b) => a + b, 0);
  $("flare-section").hidden = totalDocs === 0;
  if (totalDocs === 0) return;

  // tabs
  const tabs = Object.keys(DOC_TAB_LABELS).filter(c => counts[c]);
  if (!tabs.includes(docCategory)) docCategory = tabs[0];
  $("doc-tabs").innerHTML = tabs.map(c =>
    `<label><input type="radio" name="doc-tab" value="${c}"
       ${c === docCategory ? "checked" : ""}>
       ${DOC_TAB_LABELS[c]} (${counts[c]})</label>`).join("");
  document.querySelectorAll('#doc-tabs input').forEach(r =>
    r.addEventListener("change", () => { docCategory = r.value; loadDocuments(); }));

  // file list grouped by top-level dir
  const groups = new Map();
  for (const d of data.documents) {
    const top = d.path.includes("/") ? d.path.split("/")[0] + "/" : "(root)";
    if (!groups.has(top)) groups.set(top, []);
    groups.get(top).push(d);
  }
  let html = "";
  for (const [group, docs] of [...groups.entries()].sort()) {
    html += `<div class="doc-group">${esc(group)} · ${docs.length}</div><div class="doc-items">`;
    for (const d of docs)
      html += `<div class="doc-item" data-id="${d.id}">
        <span class="path">${esc(d.path)}${d.scrubbed ? " 🔒" : ""}</span>
        <span class="size">${fmtSize(d.size)}</span></div>`;
    html += `</div>`;
  }
  $("doc-list").innerHTML = html || '<div class="muted" style="padding:10px">no matches</div>';
  document.querySelectorAll(".doc-item").forEach(el =>
    el.addEventListener("click", () => openDocument(+el.dataset.id)));
}

function fmtSize(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}

async function openDocument(id) {
  const d = await api(`/api/documents/${id}`);
  modalRawText = d.content;
  const hasVars = Array.isArray(d.parsed) && d.parsed.length;
  const extraHTML =
    `<span class="badge channel">${esc(d.format)}</span>` +
    (d.scrubbed ? '<span class="badge scrubbed">scrubbed</span>' : "") +
    (d.truncated ? '<span class="badge truncated">truncated</span>' : "") +
    (hasVars ? `<button id="doc-toggle">show raw</button>` : "") +
    `<a href="/api/documents/${id}/raw" target="_blank"><button>raw ↗</button></a>`;
  const body = openModal(d.path, {extraHTML});

  const renderVars = () => {
    const filter = ($("doc-search").value || "").toLowerCase();
    const rows = d.parsed
      .filter(([k, v]) => !filter || (k + "=" + v).toLowerCase().includes(filter))
      .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(String(v).slice(0, 300))}</td></tr>`);
    body.innerHTML = `<table class="var-table">
      <tr><th>variable</th><th>value</th></tr>${rows.join("")}</table>` +
      (rows.length === 0 ? '<div class="muted">no matching variables</div>' : "");
  };
  const renderRaw = () => {
    body.innerHTML = `<pre>${esc(d.content)}</pre>`;
  };

  if (hasVars) {
    let showingVars = true;
    renderVars();
    $("doc-toggle").onclick = () => {
      showingVars = !showingVars;
      $("doc-toggle").textContent = showingVars ? "show raw" : "show variables";
      showingVars ? renderVars() : renderRaw();
    };
  } else {
    renderRaw();
  }
}

$("doc-search").addEventListener("input", () => {
  clearTimeout(docSearchTimer);
  docSearchTimer = setTimeout(loadDocuments, 300);
});

/* ---------- flare health report ---------- */

async function loadReport() {
  const data = await api("/api/flare/sources");
  const sources = data.sources || [];
  $("report-section").hidden = sources.length === 0;
  if (!sources.length) return;

  const sel = $("report-source");
  sel.hidden = sources.length < 2;
  if (sel.options.length !== sources.length) {
    sel.innerHTML = sources.map(s =>
      `<option value="${esc(s.source)}">${esc(s.source.split("/").pop())}</option>`).join("");
    sel.onchange = renderReport;
  }
  await renderReport();
}

async function renderReport() {
  const source = $("report-source").value;
  const r = await api(`/api/flare/report?source=${encodeURIComponent(source)}`);
  const verdictClass = r.verdict === "healthy" ? "healthy"
    : r.verdict === "needs attention" ? "warn" : "bad";
  const cards = [];

  if (r.diagnose) {
    const d = r.diagnose;
    cards.push(`<div class="report-card"><h3>Diagnose · ${d.success}/${d.total} pass</h3>
      ${d.entries.length ? `<ul>${d.entries.map(e =>
        `<li><span class="lvl lvl-${e.status === "WARNING" ? "WARN" : "ERROR"}">${esc(e.status)}</span>
         ${esc(e.name)}<div class="diag">${esc(e.diagnosis)}</div></li>`).join("")}</ul>`
        : '<div class="muted">all checks passed</div>'}</div>`);
  }
  if (r.health) {
    cards.push(`<div class="report-card"><h3>Components</h3>
      ${r.health.unhealthy.length
        ? `<ul>${r.health.unhealthy.map(u =>
            `<li><span class="lvl lvl-ERROR">UNHEALTHY</span> ${esc(u)}</li>`).join("")}</ul>`
        : `<div class="muted">all ${r.health.healthy_count} components healthy</div>`}</div>`);
  }
  if (r.config_errors.length) {
    cards.push(`<div class="report-card"><h3>Configuration errors</h3>
      <ul>${r.config_errors.map(e =>
        `<li><b>${esc(e.name)}</b><div class="diag">${esc(e.error)}</div></li>`).join("")}</ul></div>`);
  }
  if (r.top_errors.length) {
    cards.push(`<div class="report-card"><h3>Top error templates</h3>
      <ul>${r.top_errors.map(f =>
        `<li class="report-err" data-fp="${esc(f.fingerprint)}">
          <b>${f.count.toLocaleString()}×</b> [${esc(f.services)}]
          <span class="tmpl">${esc(f.template.slice(0, 110))}</span></li>`).join("")}</ul></div>`);
  }
  if (Object.keys(r.lifecycle).length) {
    cards.push(`<div class="report-card"><h3>Lifecycle (journald)</h3>
      <div class="stat-row">${Object.entries(r.lifecycle).map(([k, v]) =>
        `<div><div class="v">${v}</div><div class="l">${esc(k)}s</div></div>`).join("")}</div></div>`);
  }
  if (r.agent && (r.agent.version || (r.agent.version_history || []).length)) {
    const hist = (r.agent.version_history || []).map(h =>
      `<li>${esc(h.version)} <span class="diag">${esc((h.timestamp || "").slice(0, 10))} via ${esc(h.tool || "?")}</span></li>`).join("");
    cards.push(`<div class="report-card"><h3>Agent</h3>
      <div>v${esc(r.agent.version || "?")} · ${esc(r.agent.os || "")}
        ${r.agent.install_method ? `· installed via ${esc(r.agent.install_method)}` : ""}</div>
      ${hist ? `<ul>${hist}</ul>` : ""}</div>`);
  }
  const levelBits = Object.entries(r.log_levels || {})
    .map(([l, n]) => `<span class="lvl lvl-${esc(l)}">${esc(l)}</span> ${n.toLocaleString()}`)
    .join(" · ");

  $("report-body").innerHTML =
    `<div class="verdict ${verdictClass}">${esc(r.verdict.toUpperCase())}
       — ${r.problems} problem${r.problems === 1 ? "" : "s"}, ${r.warnings} warning${r.warnings === 1 ? "" : "s"}
       ${levelBits ? `<span class="diag" style="float:right">${levelBits}</span>` : ""}</div>
     <div class="report-grid">${cards.join("")}</div>`;
  document.querySelectorAll(".report-err").forEach(el =>
    el.addEventListener("click", () => setFilter("fingerprint", el.dataset.fp)));
}

/* ---------- orchestration ---------- */

async function refresh() {
  renderFilterBar();
  syncLevelChecks();
  writeHash();
  await Promise.allSettled([
    loadMatrix(), loadTimeline(), loadFingerprints(), loadRecords(),
    loadGaps(), loadPanels(), loadDocuments(), loadReport(),
  ]);
}

/* ---------- search + view-level controls ---------- */

function applySearch() {
  const text = $("search-input").value.trim();
  delete filters.q;
  delete filters.regex;
  if (text) filters[$("search-regex").checked ? "regex" : "q"] = text;
  page = 0;
  refresh();
}

function syncLevelChecks() {
  const active = new Set((filters.level || "").split(",").filter(Boolean));
  document.querySelectorAll("#view-levels input").forEach(c => {
    c.checked = active.has(c.value);
  });
}

document.querySelectorAll("#view-levels input").forEach(box => {
  box.addEventListener("change", () => {
    const picked = [...document.querySelectorAll("#view-levels input:checked")]
      .map(c => c.value);
    setFilter("level", picked.join(","));
  });
});

$("search-btn").onclick = applySearch;
$("search-input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") applySearch();
});

/* ---------- tar archive upload ---------- */

$("upload-btn").onclick = () => $("upload-input").click();
$("upload-input").addEventListener("change", async () => {
  const f = $("upload-input").files[0];
  if (!f) return;
  $("scan-status").textContent = `uploading ${f.name}…`;
  const form = new FormData();
  form.append("file", f);
  try {
    const res = await fetch("/api/upload", {method: "POST", body: form});
    if (!res.ok) {
      const detail = (await res.json()).detail || res.statusText;
      $("scan-status").textContent = `upload failed: ${detail}`;
      return;
    }
    const {root, files, deduplicated} = await res.json();
    $("root-input").value = root;
    localStorage.setItem("logscope-root", root);
    $("scan-status").textContent =
      `${deduplicated ? "already uploaded — reusing" : "extracted"} ${files} files — scanning…`;
    startScan();
  } catch (e) {
    $("scan-status").textContent = `upload failed: ${e.message}`;
  } finally {
    $("upload-input").value = "";
  }
});

$("scan-btn").onclick = startScan;
$("clear-filters").onclick = clearFilters;
$("tl-level").onchange = loadTimeline;
$("prev-page").onclick = () => { if (page > 0) { page--; loadRecords(); } };
$("next-page").onclick = () => { page++; loadRecords(); };
$("tl-reset").onclick = () => {
  delete filters.since;
  delete filters.until;
  page = 0;
  refresh();
};

$("root-input").value = localStorage.getItem("logscope-root") || "datadog";

readHash();   // restore filters/page from the URL before first render
loadScanners().then(refresh);
