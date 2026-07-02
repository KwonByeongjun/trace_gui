const state = {
  payload: null,
  runs: [],
  filteredRuns: [],
  compare: [],
  selectedRunId: null,
  detail: null,
};

const els = {
  sourcePill: document.getElementById("sourcePill"),
  searchInput: document.getElementById("searchInput"),
  familyFilter: document.getElementById("familyFilter"),
  modeFilter: document.getElementById("modeFilter"),
  sortSelect: document.getElementById("sortSelect"),
  refreshBtn: document.getElementById("refreshBtn"),
  kpiGrid: document.getElementById("kpiGrid"),
  runCountLabel: document.getElementById("runCountLabel"),
  runsTable: document.getElementById("runsTable"),
  familyChart: document.getElementById("familyChart"),
  scatterChart: document.getElementById("scatterChart"),
  pairCountLabel: document.getElementById("pairCountLabel"),
  compareChart: document.getElementById("compareChart"),
  policyChart: document.getElementById("policyChart"),
  compareTable: document.getElementById("compareTable"),
  detailTitle: document.getElementById("detailTitle"),
  detailSubtitle: document.getElementById("detailSubtitle"),
  openSelectedBtn: document.getElementById("openSelectedBtn"),
  detailKpis: document.getElementById("detailKpis"),
  toolChart: document.getElementById("toolChart"),
  timelineChart: document.getElementById("timelineChart"),
  timelineTable: document.getElementById("timelineTable"),
  timelineCount: document.getElementById("timelineCount"),
  cacheRunLabel: document.getElementById("cacheRunLabel"),
  vllmChart: document.getElementById("vllmChart"),
  rejectChart: document.getElementById("rejectChart"),
  prefixChart: document.getElementById("prefixChart"),
  rawSample: document.getElementById("rawSample"),
};

const fmt = new Intl.NumberFormat("en-US");
const fmt1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
const fmt2 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

function text(value, fallback = "-") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function num(value, fallback = "-") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return fallback;
  return fmt.format(Number(value));
}

function ms(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return fmt1.format(Number(value));
}

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${fmt1.format(Number(value) * 100)}%`;
}

function shortId(id, max = 82) {
  if (!id) return "-";
  return id.length > max ? `${id.slice(0, max - 1)}...` : id;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

function setLoading(message) {
  els.sourcePill.textContent = message;
}

async function loadRuns(force = false) {
  setLoading("Loading run index");
  const payload = await fetchJson(`/api/runs${force ? "?refresh=1" : ""}`);
  state.payload = payload;
  state.runs = payload.runs || [];
  state.compare = payload.compare || [];
  els.sourcePill.textContent = `${payload.stats.run_count} runs | read-only: ${payload.sources.runs_dir}`;
  populateFilters();
  applyFilters();
  renderCompare();
}

function populateFilters() {
  const families = Array.from(new Set(state.runs.map((run) => run.tags.family))).sort();
  const current = els.familyFilter.value;
  els.familyFilter.innerHTML = `<option value="">All</option>${families
    .map((family) => `<option value="${escapeHtml(family)}">${escapeHtml(family)}</option>`)
    .join("")}`;
  if (families.includes(current)) els.familyFilter.value = current;
}

function applyFilters() {
  const query = els.searchInput.value.trim().toLowerCase();
  const family = els.familyFilter.value;
  const mode = els.modeFilter.value;
  let runs = [...state.runs];

  if (query) {
    runs = runs.filter((run) => {
      const haystack = [
        run.id,
        run.model,
        run.tags.family,
        run.tags.workload,
        run.tags.policy_case,
        run.tags.repair_mode,
        ...(run.labels || []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
  }
  if (family) runs = runs.filter((run) => run.tags.family === family);
  if (mode) {
    runs = runs.filter((run) => (run.tags.repair_mode || "none") === mode);
  }

  const sort = els.sortSelect.value;
  runs.sort((a, b) => {
    if (sort === "requests_desc") return b.metrics.request_count - a.metrics.request_count;
    if (sort === "latency_asc") return metricLatency(a) - metricLatency(b);
    if (sort === "latency_desc") return metricLatency(b) - metricLatency(a);
    if (sort === "hit_desc") return metricHit(b) - metricHit(a);
    return startedSortKey(b).localeCompare(startedSortKey(a));
  });

  state.filteredRuns = runs;
  renderOverview();
}

function metricLatency(run) {
  const value = run.metrics.latency_ms.mean;
  return value === null || value === undefined ? Number.POSITIVE_INFINITY : Number(value);
}

function startedSortKey(run) {
  if (run.started_at) return run.started_at;
  return /^\d{8}T/.test(run.id) ? run.id : "0000";
}

function metricHit(run) {
  const value = run.metrics.prefix.hit_ratio;
  return value === null || value === undefined ? -1 : Number(value);
}

function renderKpis(stats) {
  const items = [
    ["Runs", num(stats.run_count), `${num(stats.runs_with_events)} with gateway events`],
    ["Requests", num(stats.total_requests), `${num(stats.runs_with_vllm_events)} with vLLM events`],
    ["Mean Latency", ms(stats.mean_latency_ms), "ms across summarized runs"],
    ["Prefix Hit", pct(stats.prefix_hit_ratio), "token-weighted aggregate"],
    ["Replay Errors", num(stats.replay_errors), "from replay.json"],
    ["Repair Pairs", num(state.compare.length), "repair_on vs reject_only"],
  ];
  els.kpiGrid.innerHTML = items
    .map(
      ([label, value, sub]) => `
        <div class="kpi">
          <div class="label">${escapeHtml(label)}</div>
          <div class="value numeric">${escapeHtml(value)}</div>
          <div class="sub">${escapeHtml(sub)}</div>
        </div>
      `,
    )
    .join("");
}

function renderOverview() {
  if (!state.payload) return;
  renderKpis(state.payload.stats);
  els.runCountLabel.textContent = `${state.filteredRuns.length} visible`;
  renderRunsTable();
  renderBarChart(
    els.familyChart,
    countBy(state.filteredRuns, (run) => run.tags.family),
    { color: "var(--accent)", maxRows: 10 },
  );
  renderScatter(els.scatterChart, state.filteredRuns);
}

function countBy(rows, fn) {
  const counts = {};
  for (const row of rows) {
    const key = fn(row) || "unknown";
    counts[key] = (counts[key] || 0) + 1;
  }
  return Object.entries(counts)
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value || a.label.localeCompare(b.label));
}

function renderRunsTable() {
  const rows = state.filteredRuns.slice(0, 300);
  els.runsTable.innerHTML = rows
    .map((run) => {
      const mode = run.tags.repair_mode || "none";
      const files = [
        run.files.summary.exists ? "summary" : null,
        run.files.events.exists ? "events" : null,
        run.files.vllm_events.exists ? "vllm" : null,
        run.files.replay.exists ? "replay" : null,
        run.files.candidate_index.exists ? "candidates" : null,
      ].filter(Boolean);
      return `
        <tr data-run-id="${escapeHtml(run.id)}" class="${run.id === state.selectedRunId ? "selected" : ""}">
          <td>
            <div class="run-name" title="${escapeHtml(run.id)}">${escapeHtml(shortId(run.id))}</div>
            <div class="tags">${(run.labels || []).slice(0, 5).map((x) => `<span class="tag">${escapeHtml(x)}</span>`).join("")}</div>
          </td>
          <td>${escapeHtml(run.tags.family)}</td>
          <td class="mode-${escapeHtml(mode)}">${escapeHtml(mode)}</td>
          <td>${escapeHtml(run.tags.policy_case)}</td>
          <td class="numeric">${num(run.metrics.request_count)}</td>
          <td class="numeric">${ms(run.metrics.latency_ms.mean)}</td>
          <td class="numeric">${ms(run.metrics.latency_ms.p95)}</td>
          <td class="numeric">${pct(run.metrics.prefix.hit_ratio)}</td>
          <td class="numeric">${num(run.metrics.replay.errors)}</td>
          <td>${escapeHtml(files.join(", ") || "-")}</td>
        </tr>
      `;
    })
    .join("");

  els.runsTable.querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", () => selectRun(tr.dataset.runId, true));
  });
}

function renderBarChart(container, data, options = {}) {
  if (!data || !data.length) {
    container.innerHTML = `<div class="empty">No data</div>`;
    return;
  }
  const rows = data.slice(0, options.maxRows || 12);
  const max = Math.max(...rows.map((x) => Number(x.value) || 0), 1);
  container.innerHTML = rows
    .map((row, idx) => {
      const width = Math.max(2, (Number(row.value) / max) * 100);
      const cls = idx % 3 === 1 ? "warn" : idx % 3 === 2 ? "alt" : "";
      return `
        <div class="bar-row">
          <div class="bar-label" title="${escapeHtml(row.label)}">${escapeHtml(row.label)}</div>
          <div class="bar-track"><div class="bar-fill ${cls}" style="width:${width}%"></div></div>
          <div class="numeric">${num(row.value)}</div>
        </div>
      `;
    })
    .join("");
}

function renderScatter(container, runs) {
  const points = runs
    .filter((run) => run.metrics.latency_ms.mean !== null && run.metrics.prefix.hit_ratio !== null)
    .slice(0, 260)
    .map((run) => ({
      id: run.id,
      x: Number(run.metrics.prefix.hit_ratio),
      y: Number(run.metrics.latency_ms.mean),
      family: run.tags.family,
    }));

  if (!points.length) {
    container.innerHTML = `<div class="empty">No latency/prefix overlap</div>`;
    return;
  }

  const w = 680;
  const h = 250;
  const pad = 38;
  const xMax = Math.max(...points.map((p) => p.x), 0.01);
  const yMax = Math.max(...points.map((p) => p.y), 1);
  const sx = (x) => pad + (x / xMax) * (w - pad * 1.5);
  const sy = (y) => h - pad - (y / yMax) * (h - pad * 1.5);
  const dots = points
    .map(
      (p) =>
        `<circle class="dot" cx="${sx(p.x).toFixed(1)}" cy="${sy(p.y).toFixed(1)}" r="4"><title>${escapeHtml(p.id)}\n${pct(p.x)} hit\n${ms(p.y)} ms</title></circle>`,
    )
    .join("");

  container.innerHTML = `
    <svg class="svg-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="Latency versus prefix hit ratio">
      <line class="axis" x1="${pad}" y1="${h - pad}" x2="${w - 10}" y2="${h - pad}"></line>
      <line class="axis" x1="${pad}" y1="12" x2="${pad}" y2="${h - pad}"></line>
      <line class="grid-line" x1="${pad}" y1="${sy(yMax / 2)}" x2="${w - 10}" y2="${sy(yMax / 2)}"></line>
      <text x="${pad}" y="${h - 10}" fill="#66717d" font-size="11">prefix hit ratio</text>
      <text x="2" y="20" fill="#66717d" font-size="11">ms</text>
      ${dots}
    </svg>
  `;
}

function renderCompare() {
  els.pairCountLabel.textContent = `${state.compare.length} matched pairs`;
  renderCompareTable();
  const chartRows = state.compare
    .filter((pair) => pair.delta.mean_latency_ms !== null)
    .slice(0, 24)
    .map((pair) => ({
      label: `${pair.policy_case || "case"} r${pair.repeat || "-"}`,
      value: Math.abs(pair.delta.mean_latency_ms),
      raw: pair.delta.mean_latency_ms,
    }));
  renderSignedBars(els.compareChart, chartRows);
  renderBarChart(
    els.policyChart,
    countBy(state.compare, (pair) => pair.policy_case || "unknown"),
    { maxRows: 10 },
  );
}

function renderSignedBars(container, rows) {
  if (!rows.length) {
    container.innerHTML = `<div class="empty">No paired latency data</div>`;
    return;
  }
  const max = Math.max(...rows.map((row) => Math.abs(row.raw)), 1);
  container.innerHTML = rows
    .map((row) => {
      const width = Math.max(2, (Math.abs(row.raw) / max) * 100);
      const cls = row.raw <= 0 ? "" : "warn";
      const deltaClass = row.raw <= 0 ? "delta-good" : "delta-bad";
      return `
        <div class="bar-row">
          <div class="bar-label" title="${escapeHtml(row.label)}">${escapeHtml(row.label)}</div>
          <div class="bar-track"><div class="bar-fill ${cls}" style="width:${width}%"></div></div>
          <div class="numeric ${deltaClass}">${ms(row.raw)}</div>
        </div>
      `;
    })
    .join("");
}

function renderCompareTable() {
  els.compareTable.innerHTML = state.compare
    .map((pair) => {
      const delta = pair.delta.mean_latency_ms;
      const deltaClass = delta === null ? "" : delta <= 0 ? "delta-good" : "delta-bad";
      return `
        <tr data-run-id="${escapeHtml(pair.repair_on.id)}">
          <td><div class="run-name" title="${escapeHtml(pair.pair_key)}">${escapeHtml(shortId(pair.pair_key, 72))}</div></td>
          <td>${escapeHtml(pair.policy_case || "-")}</td>
          <td class="numeric">${text(pair.repeat)}</td>
          <td class="numeric">${ms(pair.reject_only.mean_latency_ms)}</td>
          <td class="numeric">${ms(pair.repair_on.mean_latency_ms)}</td>
          <td class="numeric ${deltaClass}">${ms(delta)}</td>
          <td class="numeric ${deltaClass}">${pct(pair.delta.mean_latency_pct)}</td>
          <td class="numeric">${pct(pair.delta.hit_ratio)}</td>
        </tr>
      `;
    })
    .join("");
  els.compareTable.querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", () => selectRun(tr.dataset.runId, true));
  });
}

async function selectRun(runId, loadDetail = false) {
  state.selectedRunId = runId;
  renderRunsTable();
  const run = state.runs.find((item) => item.id === runId);
  if (run) {
    els.detailTitle.textContent = shortId(run.id, 120);
    els.detailSubtitle.textContent = `${run.tags.family} | ${run.tags.policy_case} | ${run.tags.repair_mode || "none"}`;
  }
  if (loadDetail) {
    await loadDetailForSelected();
    setActiveView("detail");
  }
}

async function loadDetailForSelected() {
  if (!state.selectedRunId) return;
  els.detailSubtitle.textContent = "Loading detail";
  const detail = await fetchJson(`/api/runs/${encodeURIComponent(state.selectedRunId)}`);
  state.detail = detail;
  renderDetail();
}

function renderDetail() {
  const detail = state.detail;
  if (!detail) return;
  const summary = detail.summary;
  els.detailTitle.textContent = shortId(summary.id, 120);
  els.detailSubtitle.textContent = `${summary.metrics.request_count} requests | ${summary.metrics.latency_ms.source || "no latency source"} | ${summary.files.events.exists ? "events.jsonl" : "no events.jsonl"}`;

  const kpis = [
    ["Requests", num(summary.metrics.request_count), `${num(detail.events.loaded)} gateway events loaded`],
    ["Mean Latency", ms(summary.metrics.latency_ms.mean), "ms"],
    ["P95 Latency", ms(summary.metrics.latency_ms.p95), "ms"],
    ["Prefix Hit", pct(summary.metrics.prefix.hit_ratio), `${num(summary.metrics.prefix.hit_tokens)} hit tokens`],
    ["Replay Errors", num(summary.metrics.replay.errors), text(summary.metrics.replay.policy, "no policy")],
  ];
  els.detailKpis.innerHTML = kpis
    .map(
      ([label, value, sub]) => `
        <div class="kpi">
          <div class="label">${escapeHtml(label)}</div>
          <div class="value numeric">${escapeHtml(value)}</div>
          <div class="sub">${escapeHtml(sub)}</div>
        </div>
      `,
    )
    .join("");

  renderBarChart(
    els.toolChart,
    Object.entries(detail.events.tool_counts).map(([label, value]) => ({ label, value })),
    { maxRows: 10 },
  );
  renderTimelineChart(detail.events.timeline);
  renderTimelineTable(detail.events.timeline);
  renderCacheView();
}

function renderTimelineChart(rows) {
  const data = rows
    .filter((row) => row.elapsed_ms !== null)
    .slice(0, 80)
    .map((row, idx) => ({ label: row.turn_id || `#${idx + 1}`, value: row.elapsed_ms }));
  renderBarChart(els.timelineChart, data, { maxRows: 18 });
}

function renderTimelineTable(rows) {
  els.timelineCount.textContent = `${rows.length} requests loaded`;
  els.timelineTable.innerHTML = rows
    .slice(0, 300)
    .map(
      (row) => `
        <tr>
          <td>${escapeHtml(row.turn_id || "-")}</td>
          <td>${escapeHtml(row.tool_name || "-")}</td>
          <td>${escapeHtml(row.agent_role || "-")}</td>
          <td class="numeric">${escapeHtml(row.status_code ?? "-")}</td>
          <td class="numeric">${ms(row.elapsed_ms)}</td>
          <td class="numeric">${num(row.cache_hit_tokens)}</td>
          <td class="numeric">${num(row.cache_miss_tokens)}</td>
          <td>${escapeHtml((row.reject_reasons || []).join(", ") || "-")}</td>
          <td><div class="run-name" title="${escapeHtml(row.program_id || "")}">${escapeHtml(shortId(row.program_id || "-", 74))}</div></td>
        </tr>
      `,
    )
    .join("");
}

function renderCacheView() {
  const detail = state.detail;
  if (!detail) {
    els.cacheRunLabel.textContent = "select a run";
    return;
  }
  els.cacheRunLabel.textContent = shortId(detail.summary.id, 64);
  renderBarChart(
    els.vllmChart,
    Object.entries(detail.vllm.event_counts).map(([label, value]) => ({ label, value })),
    { maxRows: 12 },
  );
  renderBarChart(
    els.rejectChart,
    Object.entries(detail.vllm.reject_reasons).map(([label, value]) => ({ label, value })),
    { maxRows: 12 },
  );
  renderPrefixChart(detail.vllm.prefix_points);
  els.rawSample.textContent = JSON.stringify(
    {
      gateway_events: detail.events.sample.slice(0, 20),
      vllm_events: detail.vllm.sample.slice(0, 20),
      replay: detail.raw.replay,
    },
    null,
    2,
  );
}

function renderPrefixChart(points) {
  const rows = (points || []).slice(0, 160);
  if (!rows.length) {
    els.prefixChart.innerHTML = `<div class="empty">No prefix hit/miss events loaded</div>`;
    return;
  }
  const w = 760;
  const h = 320;
  const pad = 36;
  const maxTokens = Math.max(...rows.map((p) => Math.max(p.hit_tokens, p.miss_tokens, p.num_tokens || 0)), 1);
  const barW = Math.max(2, (w - pad * 2) / rows.length - 1);
  const bars = rows
    .map((p, idx) => {
      const x = pad + idx * ((w - pad * 2) / rows.length);
      const hitH = (p.hit_tokens / maxTokens) * (h - pad * 1.8);
      const missH = (p.miss_tokens / maxTokens) * (h - pad * 1.8);
      const yHit = h - pad - hitH;
      const yMiss = yHit - missH;
      return `
        <rect x="${x.toFixed(1)}" y="${yMiss.toFixed(1)}" width="${barW.toFixed(1)}" height="${missH.toFixed(1)}" fill="#d97706" opacity="0.72">
          <title>${escapeHtml(p.event_type)}\nmiss ${num(p.miss_tokens)}\nhit ${num(p.hit_tokens)}</title>
        </rect>
        <rect x="${x.toFixed(1)}" y="${yHit.toFixed(1)}" width="${barW.toFixed(1)}" height="${hitH.toFixed(1)}" fill="#1f8a83" opacity="0.86"></rect>
      `;
    })
    .join("");
  els.prefixChart.innerHTML = `
    <svg class="svg-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="Prefix token hit and miss sequence">
      <line class="axis" x1="${pad}" y1="${h - pad}" x2="${w - 12}" y2="${h - pad}"></line>
      <line class="axis" x1="${pad}" y1="12" x2="${pad}" y2="${h - pad}"></line>
      ${bars}
      <text x="${pad}" y="${h - 10}" fill="#66717d" font-size="11">request order</text>
      <text x="2" y="20" fill="#66717d" font-size="11">tokens</text>
      <rect x="${w - 150}" y="18" width="10" height="10" fill="#1f8a83"></rect>
      <text x="${w - 134}" y="27" fill="#66717d" font-size="11">hit</text>
      <rect x="${w - 96}" y="18" width="10" height="10" fill="#d97706"></rect>
      <text x="${w - 80}" y="27" fill="#66717d" font-size="11">miss</text>
    </svg>
  `;
}

function setActiveView(id) {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === id);
  });
  document.querySelectorAll(".view").forEach((view) => {
    view.classList.toggle("active", view.id === id);
  });
  if (id === "cache") renderCacheView();
}

function bindEvents() {
  [els.searchInput, els.familyFilter, els.modeFilter, els.sortSelect].forEach((el) => {
    el.addEventListener("input", applyFilters);
    el.addEventListener("change", applyFilters);
  });
  els.refreshBtn.addEventListener("click", () => loadRuns(true).catch(showError));
  els.openSelectedBtn.addEventListener("click", () => loadDetailForSelected().catch(showError));
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => setActiveView(tab.dataset.view));
  });
}

function showError(error) {
  console.error(error);
  els.sourcePill.textContent = `Error: ${error.message || error}`;
}

bindEvents();
loadRuns().catch(showError);
