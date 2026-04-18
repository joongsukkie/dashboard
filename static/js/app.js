/* AI Analytics Agent — frontend */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  analysisReady: false,
  lastData: null,
  tableRows: [],
  tableCols: [],
  tableSort: { col: null, dir: 1 },
};

// ---------------------------------------------------------------------------
// Setup: save API key
// ---------------------------------------------------------------------------
$("#save-key").addEventListener("click", connectKey);
$("#api-key").addEventListener("keydown", (e) => { if (e.key === "Enter") connectKey(); });

async function connectKey() {
  const api_key = $("#api-key").value.trim();
  const s = $("#key-status");
  s.textContent = ""; s.className = "status-line";
  if (!api_key) { s.textContent = "Enter an API key to continue."; s.classList.add("err"); return; }
  s.textContent = "Connecting…";
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({api_key}),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Failed");
    s.innerHTML = `✓ Connected · <b>${j.label}</b>`;
    s.classList.add("ok");
  } catch (e) {
    s.textContent = e.message;
    s.classList.add("err");
  }
}

// ---------------------------------------------------------------------------
// Upload
// ---------------------------------------------------------------------------
const uploadZone = $("#upload-zone");
const fileInput = $("#file-input");
uploadZone.addEventListener("click", () => fileInput.click());
["dragenter","dragover"].forEach(ev => uploadZone.addEventListener(ev, (e) => {
  e.preventDefault(); uploadZone.classList.add("dragover");
}));
["dragleave","drop"].forEach(ev => uploadZone.addEventListener(ev, (e) => {
  e.preventDefault(); uploadZone.classList.remove("dragover");
}));
uploadZone.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length) handleUpload(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", (e) => {
  if (e.target.files.length) handleUpload(e.target.files[0]);
});

async function handleUpload(file) {
  const s = $("#upload-status");
  s.textContent = `Uploading ${file.name}...`; s.className = "status-line";
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/upload", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Upload failed");
    s.textContent = `✓ ${j.filename} — ${j.rows.toLocaleString()} rows × ${j.cols} columns`;
    s.classList.add("ok");
  } catch (e) {
    s.textContent = e.message; s.classList.add("err");
  }
}

// ---------------------------------------------------------------------------
// Mode selection + custom KPI show/hide
// ---------------------------------------------------------------------------
$$('input[name="mode"]').forEach(r => {
  r.addEventListener("change", () => {
    const isCustom = $('input[name="mode"]:checked').value === "custom";
    $("#custom-kpi-wrap").classList.toggle("hidden", !isCustom);
  });
});

// Benchmarks
$("#add-benchmark").addEventListener("click", () => {
  const rows = $("#benchmark-rows");
  if (rows.children.length >= 5) return;
  const row = document.createElement("div");
  row.className = "benchmark-row";
  row.innerHTML = `
    <input placeholder="Metric name" class="bm-metric" />
    <input placeholder="Benchmark value" class="bm-value" type="number" step="any" />
    <button class="btn btn-ghost small" type="button">×</button>
  `;
  row.querySelector("button").onclick = () => row.remove();
  rows.appendChild(row);
});

// ---------------------------------------------------------------------------
// Run analysis
// ---------------------------------------------------------------------------
$("#run-analysis").addEventListener("click", runAnalysis);

async function runAnalysis() {
  const status = $("#run-status");
  status.textContent = ""; status.className = "status-line center";
  const btn = $("#run-analysis"); btn.disabled = true;
  const prog = $("#progress"); prog.classList.remove("hidden");
  setStep(1);

  const mode = $('input[name="mode"]:checked').value;
  const custom = $("#custom-kpi").value.trim();
  const benchmarks = $$(".benchmark-row").map(r => ({
    metric: r.querySelector(".bm-metric").value.trim(),
    value: parseFloat(r.querySelector(".bm-value").value),
  })).filter(b => b.metric && !isNaN(b.value));

  try {
    setStep(2);
    const r = await fetch("/api/analyze", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({mode, custom, benchmarks}),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Analysis failed");
    setStep(3);
    state.lastData = j;
    renderDashboard(j);
    state.analysisReady = true;
    $("#setup-screen").classList.add("hidden");
    $("#dashboard").classList.remove("hidden");
  } catch (e) {
    status.textContent = e.message; status.classList.add("err");
  } finally {
    btn.disabled = false;
    prog.classList.add("hidden");
    $$(".progress-step").forEach(s => s.classList.remove("active", "done"));
  }
}

function setStep(n) {
  $$(".progress-step").forEach(el => {
    const i = parseInt(el.dataset.step);
    el.classList.remove("active", "done");
    if (i < n) el.classList.add("done");
    if (i === n) el.classList.add("active");
  });
}

// ---------------------------------------------------------------------------
// Dashboard rendering
// ---------------------------------------------------------------------------
function renderDashboard(d) {
  // Title bar
  $("#db-title").textContent = d.filename || "Dataset";
  $("#db-sub").textContent =
    `${d.rows.toLocaleString()} rows × ${d.cols} columns · Mode: ${d.mode}`;

  // Executive summary
  $("#exec-summary").textContent = d.executive_summary || "No summary returned.";

  // KPI cards
  const kpiRow = $("#kpi-row"); kpiRow.innerHTML = "";
  (d.kpi_cards || []).forEach(k => {
    const div = document.createElement("div");
    div.className = "kpi";
    div.innerHTML = `
      <div class="kpi-label">${esc(k.label || "")}</div>
      <div class="kpi-value">${esc(k.value || "")}</div>
      <div class="kpi-sub">${esc(k.subtext || "")}</div>
    `;
    kpiRow.appendChild(div);
  });

  // Cleaning card
  renderClean(d.clean_summary);

  // Data quality notes
  $("#dq-list").innerHTML = (d.data_quality_notes || []).map(n => `<li>${esc(n)}</li>`).join("");

  // Follow-ups
  $("#followup-list").innerHTML = (d.followup_questions || []).map(n => `<li>${esc(n)}</li>`).join("");

  // Charts
  renderCharts(d.charts || [], d.benchmarks || []);

  // Correlation
  const corrCard = $("#corr-card");
  if (d.correlation) {
    corrCard.classList.remove("hidden");
    Plotly.newPlot("corr-chart", d.correlation.data, d.correlation.layout, {responsive: true, displayModeBar: false});
  } else { corrCard.classList.add("hidden"); }

  // Time series
  const tsCard = $("#ts-card");
  if (d.timeseries) {
    tsCard.classList.remove("hidden");
    Plotly.newPlot("ts-chart", d.timeseries.data, d.timeseries.layout, {responsive: true, displayModeBar: false});
  } else { tsCard.classList.add("hidden"); }

  // A/B test
  const abCard = $("#ab-card");
  if (d.ab_test) {
    abCard.classList.remove("hidden");
    const ab = d.ab_test;
    $("#ab-body").innerHTML = `
      <div class="ab-summary ${ab.significant ? 'sig' : 'notsig'}">
        <b>${ab.significant ? '✓ Statistically significant' : '⚠ Not statistically significant'}</b><br/>
        ${esc(ab.summary || '')}
      </div>
      ${ab.mean_a !== undefined ? `
      <div class="ab-metrics">
        <div class="ab-metric"><b>Group A (${esc(ab.group_a)})</b>n=${ab.n_a} · mean ${ab.mean_a}</div>
        <div class="ab-metric"><b>Group B (${esc(ab.group_b)})</b>n=${ab.n_b} · mean ${ab.mean_b}</div>
        <div class="ab-metric"><b>Lift</b>${ab.lift_pct}%</div>
        <div class="ab-metric"><b>p-value</b>${ab.p_value}</div>
      </div>` : ''}
    `;
  } else { abCard.classList.add("hidden"); }

  // Outliers
  const olCard = $("#outlier-card");
  if (d.outliers && d.outliers.count > 0) {
    olCard.classList.remove("hidden");
    const rows = d.outliers.rows;
    const cols = Object.keys(rows[0] || {});
    const body = $("#outlier-body");
    body.innerHTML = `<p class="muted small">${d.outliers.count} row(s) flagged as outliers (IQR method). Showing up to 50.</p>
    <div class="table-wrap"><table><thead><tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${esc(r[c]||'')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  } else { olCard.classList.add("hidden"); }

  // SQL panel
  renderSQL(d.sql_queries || []);

  // Data table
  state.tableRows = d.preview || [];
  state.tableCols = d.profile?.columns || Object.keys(state.tableRows[0] || {});
  renderTable();
}

function renderClean(c) {
  if (!c) return;
  const box = $("#clean-body");
  const rows = [
    ["Original shape", `${c.original_shape?.[0]} × ${c.original_shape?.[1]}`],
    ["Cleaned shape", `${c.cleaned_shape?.[0]} × ${c.cleaned_shape?.[1]}`],
    ["Duplicates removed", c.duplicates_removed || 0],
    ["Whitespace trimmed in", pillList(c.whitespace_columns_fixed)],
    ["Casing normalized", pillList(c.casing_normalized)],
    ["Types inferred", pillMap(c.types_inferred)],
    ["Nulls filled", pillMap(c.nulls_filled)],
    ["Columns dropped (>50% null)", pillMap(c.nulls_dropped)],
  ];
  box.innerHTML = rows.map(([l, v]) =>
    `<div class="clean-row"><div class="label">${l}</div><div class="value">${v || '—'}</div></div>`
  ).join("");
}
function pillList(arr) {
  if (!arr || !arr.length) return "";
  return arr.map(x => `<span class="pill">${esc(x)}</span>`).join("");
}
function pillMap(obj) {
  if (!obj || !Object.keys(obj).length) return "";
  return Object.entries(obj).map(([k, v]) =>
    `<span class="pill">${esc(k)}: ${esc(String(v))}</span>`).join("");
}

function renderCharts(charts, benchmarks) {
  const grid = $("#charts-grid");
  grid.innerHTML = "";
  charts.forEach((c, idx) => {
    const id = `chart-${idx}`;
    const card = document.createElement("div");
    card.className = "chart-card";
    card.innerHTML = `
      <h3>${esc(c.title || 'Chart')}</h3>
      <div id="${id}" class="chart-box"></div>
      <div class="insight">${esc(c.insight || '')}</div>
    `;
    grid.appendChild(card);

    // Benchmark overlay — simple horizontal line if benchmark metric matches chart title
    const fig = JSON.parse(JSON.stringify(c.figure));
    (benchmarks || []).forEach(b => {
      if (c.title && c.title.toLowerCase().includes(b.metric.toLowerCase())) {
        fig.layout = fig.layout || {};
        fig.layout.shapes = fig.layout.shapes || [];
        fig.layout.shapes.push({
          type: "line", xref: "paper", x0: 0, x1: 1,
          yref: "y", y0: b.value, y1: b.value,
          line: { color: "#EF4444", width: 2, dash: "dash" },
        });
        fig.layout.annotations = fig.layout.annotations || [];
        fig.layout.annotations.push({
          xref: "paper", x: 1, y: b.value, xanchor: "right",
          text: `Benchmark: ${b.value}`,
          showarrow: false, font: { color: "#EF4444", size: 11 },
        });
      }
    });
    Plotly.newPlot(id, fig.data, fig.layout, { responsive: true, displayModeBar: false });
  });
}

function renderSQL(queries) {
  const list = $("#sql-list");
  list.innerHTML = "";
  queries.forEach((q, i) => {
    const div = document.createElement("div");
    div.className = "sql-block";
    div.innerHTML = `
      <div class="sql-block-header">
        <span>${esc(q.title || `Query ${i+1}`)}</span>
        <button class="btn btn-ghost copy-btn" data-idx="${i}">Copy</button>
      </div>
      <pre><code class="language-sql">${esc(q.sql || '')}</code></pre>
    `;
    list.appendChild(div);
  });
  if (window.hljs) {
    list.querySelectorAll("code").forEach(c => {
      try { hljs.highlightElement(c); } catch(e) {}
    });
  }
  list.querySelectorAll(".copy-btn").forEach(btn => {
    btn.onclick = () => {
      const idx = parseInt(btn.dataset.idx);
      navigator.clipboard.writeText(queries[idx].sql || "");
      btn.textContent = "Copied!";
      setTimeout(() => btn.textContent = "Copy", 1500);
    };
  });
}

// ---------------------------------------------------------------------------
// Data table — sort, filter, column popup
// ---------------------------------------------------------------------------
function renderTable() {
  const filter = ($("#table-filter").value || "").toLowerCase();
  let rows = state.tableRows;
  if (filter) {
    rows = rows.filter(r => Object.values(r).some(v =>
      String(v).toLowerCase().includes(filter)));
  }
  if (state.tableSort.col) {
    const { col, dir } = state.tableSort;
    rows = [...rows].sort((a, b) => {
      const av = a[col], bv = b[col];
      const an = parseFloat(av), bn = parseFloat(bv);
      if (!isNaN(an) && !isNaN(bn)) return (an - bn) * dir;
      return String(av).localeCompare(String(bv)) * dir;
    });
  }
  const cols = state.tableCols;
  const t = $("#data-table");
  t.innerHTML = `<thead><tr>${cols.map(c => {
    const sort = state.tableSort.col === c ? (state.tableSort.dir === 1 ? ' ↑' : ' ↓') : '';
    return `<th data-col="${esc(c)}">${esc(c)}${sort}</th>`;
  }).join('')}</tr></thead>
  <tbody>${rows.map(r => `<tr>${cols.map(c => `<td>${esc(r[c] ?? '')}</td>`).join('')}</tr>`).join('')}</tbody>`;
  t.querySelectorAll("th").forEach(th => {
    th.onclick = () => {
      const col = th.dataset.col;
      if (state.tableSort.col === col) state.tableSort.dir *= -1;
      else state.tableSort = { col, dir: 1 };
      renderTable();
    };
    th.ondblclick = (e) => { e.stopPropagation(); showColumnPopup(th.dataset.col); };
  });
}

$("#table-filter").addEventListener("input", () => renderTable());

async function showColumnPopup(name) {
  const popup = $("#col-popup"); const overlay = $("#overlay");
  popup.classList.remove("hidden"); overlay.classList.remove("hidden");
  $("#col-popup-title").textContent = name;
  $("#col-popup-meta").textContent = "Loading...";
  $("#col-popup-chart").innerHTML = "";
  try {
    const r = await fetch(`/api/column/${encodeURIComponent(name)}`);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Failed");
    const parts = [
      `type: ${j.dtype}`,
      `unique: ${j.unique}`,
      `nulls: ${j.null_pct}%`,
    ];
    if (j.min !== undefined) parts.push(`min: ${j.min}`, `max: ${j.max}`, `mean: ${j.mean}`, `median: ${j.median}`);
    $("#col-popup-meta").textContent = parts.join(" · ");
    if (j.figure) Plotly.newPlot("col-popup-chart", j.figure.data, j.figure.layout, {responsive: true, displayModeBar: false});
  } catch (e) {
    $("#col-popup-meta").textContent = e.message;
  }
}

// ---------------------------------------------------------------------------
// Panels (SQL, Chat, Column popup)
// ---------------------------------------------------------------------------
$("#btn-sql").addEventListener("click", () => togglePanel("sql-panel"));
$("#btn-chat").addEventListener("click", () => togglePanel("chat-panel"));
$$(".btn-close").forEach(b => b.addEventListener("click", () => {
  const target = b.dataset.close;
  $("#" + target).classList.add("hidden");
  if (target === "col-popup") $("#overlay").classList.add("hidden");
}));
$("#overlay").addEventListener("click", () => {
  $("#col-popup").classList.add("hidden");
  $("#overlay").classList.add("hidden");
});

function togglePanel(id) {
  $("#" + id).classList.toggle("hidden");
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------
$("#chat-send").addEventListener("click", sendChat);
$("#chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(); });

async function sendChat() {
  const inp = $("#chat-input");
  const q = inp.value.trim();
  if (!q) return;
  inp.value = "";
  addChatMsg("user", q);
  const thinking = addChatMsg("ai", "Thinking...");
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ question: q }),
    });
    const j = await r.json();
    thinking.textContent = j.ok ? j.answer : (j.error || "Error");
  } catch (e) {
    thinking.textContent = e.message;
  }
}

function addChatMsg(who, text) {
  const log = $("#chat-log");
  const el = document.createElement("div");
  el.className = "chat-msg " + who;
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------
$("#btn-excel").addEventListener("click", () => { window.location.href = "/api/export/excel"; });
$("#btn-pdf").addEventListener("click", () => { window.location.href = "/api/export/pdf"; });
$("#btn-new").addEventListener("click", () => {
  $("#dashboard").classList.add("hidden");
  $("#setup-screen").classList.remove("hidden");
});

// ---------------------------------------------------------------------------
// Utils
// ---------------------------------------------------------------------------
function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
