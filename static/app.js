/* Project Observatory: data comes from explicitly configured project reports. */
(() => {
  "use strict";
  const icons = {
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"></path><path d="M6.1 8a6 6 0 0 1 9.9-2L20 12M4 12l4 6a6 6 0 0 0 9.9-2"></path>',
    download: '<path d="M12 3v12m-4-4 4 4 4-4M5 16v5h14v-5"></path>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"></circle><path d="m16 16 5 5"></path>',
    close: '<path d="m6 6 12 12M18 6 6 18"></path>',
    arrow: '<path d="m9 5 7 7-7 7"></path>',
    cpu: '<rect x="5" y="5" width="14" height="14" rx="2"></rect><path d="M9 9h6v6H9zM9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3"></path>',
    gpu: '<rect x="3" y="6" width="18" height="12" rx="1"></rect><circle cx="11" cy="12" r="3.5"></circle><path d="M17 9v6M7 18v3m3-3v3m3-3v3M1 6h2"></path>',
    check: '<path d="m5 12 4 4L19 6"></path>',
    clock: '<circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path>',
  };
  const svg = (name, extra = "") => `<svg class="ui-icon ${extra}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.cpu}</svg>`;
  document.querySelectorAll("[data-icon]").forEach(node => { node.innerHTML = svg(node.dataset.icon); });
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const formatTime = value => value ? new Intl.DateTimeFormat("zh-CN", { timeZone: data?.timezone || "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value)) : "无记录";
  const ageMinutes = value => value ? Math.max(0, (Date.now() - new Date(value).getTime()) / 60000) : Infinity;
  const age = value => { const minutes = ageMinutes(value); return !Number.isFinite(minutes) ? "无记录" : minutes < 1 ? "不足1分钟前" : minutes < 60 ? `${Math.floor(minutes)}分钟前` : minutes < 1440 ? `${Math.floor(minutes / 60)}小时前` : `${Math.floor(minutes / 1440)}天前`; };
  const metric = primary => primary.text !== undefined ? esc(primary.text) : Number(primary.value).toLocaleString("en-US", { minimumFractionDigits: primary.digits || 0, maximumFractionDigits: primary.digits || 0 });
  const labels = { RUNNING: "运行中", PENDING: "等待依赖 / 排队", COMPLETED: "已结束", FAILED: "失败", CANCELLED: "已取消", TIMEOUT: "超时", OUT_OF_MEMORY: "内存不足", NODE_FAIL: "节点故障", UNKNOWN: "未观测" };
  const stateClass = state => state === "RUNNING" ? "running" : state === "COMPLETED" ? "completed" : ["FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"].includes(state) ? "failed" : "";
  let data = null;
  let kind = "all";
  let query = "";
  let pending = false;
  let selectedVersion = null;
  let learningProject = null;
  let refreshTimer = null;
  const charts = new Map();
  const offline = Boolean(window.DASHBOARD_SNAPSHOT);
  let traceIndex = null;
  let traceWorkspace = "all";
  let traceSession = null;
  let traceCursor = 0;
  let traceLoading = false;
  let traceRequestToken = 0;
  let traceFlow = null;
  let flowWindow = "all";
  let flowPlayhead = null;
  const workspaceNames = {};
  const clockTime = ms => new Intl.DateTimeFormat("zh-CN", { timeZone: data?.timezone || "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(ms));
  const compactNumber = value => value === null || value === undefined ? "—" : new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value);
  const durationLabel = ms => ms < 1000 ? `${Math.round(ms)} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : ms < 3600000 ? `${(ms / 60000).toFixed(1)} min` : `${(ms / 3600000).toFixed(1)} h`;

  function visibleFlowRange() {
    const end = Math.max(traceFlow.end_ms, traceFlow.start_ms + 1000);
    const span = flowWindow === "all" ? end - traceFlow.start_ms : Number(flowWindow) * 60000;
    return { start: Math.max(traceFlow.start_ms, end - span), end };
  }

  function compactFlowSlices(slices, start, end) {
    const endpoints = [];
    slices.forEach((item, index) => {
      const from = Math.max(start, item.start_ms);
      const to = Math.min(end, item.end_ms);
      if (to > from) { endpoints.push({ at: from, type: 1, index, item }); endpoints.push({ at: to, type: -1, index, item }); }
    });
    endpoints.sort((a, b) => a.at - b.at || a.type - b.type);
    const active = new Map();
    const visible = [];
    let previous = start;
    for (let i = 0; i < endpoints.length;) {
      const at = endpoints[i].at;
      if (at > previous && active.size) {
        const ongoing = [...active.values()];
        const selected = ongoing.at(-1);
        visible.push({ ...selected, start_ms: previous, end_ms: at,
          kind: ongoing.every(item => item.kind === selected.kind) ? selected.kind : "mixed", count: ongoing.length });
      }
      while (i < endpoints.length && endpoints[i].at === at) {
        const event = endpoints[i++];
        if (event.type === 1) active.set(event.index, event.item);
        else active.delete(event.index);
      }
      previous = at;
    }
    if (visible.length <= 240) return visible;
    const bins = new Map();
    for (const item of visible) {
      const bucket = Math.min(239, Math.max(0, Math.floor((item.start_ms - start) / (end - start) * 240)));
      const found = bins.get(bucket);
      if (!found) bins.set(bucket, { ...item, count: 1 });
      else {
        found.end_ms = Math.max(found.end_ms, item.end_ms);
        found.start_ms = Math.min(found.start_ms, item.start_ms);
        found.kind = found.kind === item.kind ? found.kind : "mixed";
        found.count++;
      }
    }
    return [...bins.values()].sort((a, b) => a.start_ms - b.start_ms);
  }

  function renderTraceFlow() {
    if (!traceFlow) { $("trace-flow").hidden = true; return; }
    $("trace-flow").hidden = false;
    const { start, end } = visibleFlowRange();
    const span = end - start;
    const pos = ms => Math.max(0, Math.min(100, (ms - start) / span * 100));
    const toolCount = traceFlow.lanes.reduce((sum, lane) => sum + lane.slices.filter(item => item.end_ms >= start && item.start_ms <= end).length, 0);
    $("flow-command").textContent = `codex trace --workspace ${traceFlow.workspace}`;
    $("flow-title").textContent = `${workspaceNames[traceFlow.workspace] || traceFlow.workspace} · ${traceFlow.lanes.length} 条 Agent 泳道`;
    $("flow-range-label").textContent = `${formatTime(start)} — ${formatTime(end)} · ${data.timezone}`;
    document.querySelectorAll("[data-flow-window]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.flowWindow === flowWindow)));
    const ticks = [0, .25, .5, .75, 1].map(frac => {
      const ms = start + span * frac;
      const label = span >= 12 * 3600000 ? formatTime(ms) : clockTime(ms).slice(0, 5);
      return `<span style="left:${(frac * 100).toFixed(1)}%">${esc(label)}</span>`;
    }).join("");
    const lanes = traceFlow.lanes.map(lane => {
      const turnHTML = lane.turns.filter(item => item.end_ms >= start && item.start_ms <= end).map(item => `<span class="flow-turn" style="left:${pos(item.start_ms).toFixed(3)}%;width:${Math.max(.25, pos(item.end_ms) - pos(item.start_ms)).toFixed(3)}%"></span>`).join("");
      const sliceHTML = compactFlowSlices(lane.slices, start, end).map(item => {
        const left = Math.min(99.9, pos(item.start_ms));
        const width = Math.max(.08, pos(item.end_ms) - left);
        const label = item.count > 1 ? `${item.count} 个片段` : item.label;
        const tip = `${lane.agent} · ${label} · ${clockTime(item.start_ms)} · ${durationLabel(item.end_ms - item.start_ms)}`;
        return `<button type="button" class="flow-slice ${esc(item.kind)}" style="left:${left.toFixed(3)}%;width:${Math.min(width, 100 - left).toFixed(3)}%" data-flow-session="${esc(lane.id)}" data-flow-line="${item.line}" title="${esc(tip)}" aria-label="${esc(tip)}"><span>${width > 6 ? esc(item.count > 1 ? `${item.count}×` : item.kind) : ""}</span></button>`;
      }).join("");
      return `<div class="flow-row ${lane.id === traceSession ? "selected" : ""}"><button class="flow-agent" type="button" data-flow-agent="${esc(lane.id)}"><strong>${esc(lane.agent)}</strong><small>${lane.parent_id ? "子 Agent" : "主会话"} · ${esc(lane.id.slice(0, 8))}</small></button><div class="flow-track">${turnHTML}${sliceHTML}</div><div class="flow-tokens"><strong>${esc(compactNumber(lane.token_total))}</strong><small>tok</small></div></div>`;
    }).join("");
    const playhead = flowPlayhead === null ? end : Math.max(start, Math.min(end, flowPlayhead));
    $("flow-chart").innerHTML = `<div class="flow-axis"><span>AGENT / SESSION</span><div class="flow-axis-track">${ticks}</div><span>TOKENS</span></div><div class="flow-lanes">${lanes}<div class="flow-playhead" aria-hidden="true"><span>${esc(clockTime(playhead).slice(0, 5))}</span></div></div>`;
    const track = $("flow-chart").querySelector(".flow-track");
    if (track) $("flow-chart").querySelector(".flow-playhead").style.left = `${track.offsetLeft + track.clientWidth * pos(playhead) / 100}px`;
    $("flow-summary").textContent = `${toolCount} 次调用 · ${durationLabel(span)} · one clock`;
  }

  async function loadTraceFlow(id) {
    if (offline) return;
    try {
      const response = await fetch(`/api/traces/${encodeURIComponent(id)}/flow`, { cache: "no-store" });
      if (!response.ok) throw new Error("read failed");
      const flow = await response.json();
      if (traceSession !== id) return;
      traceFlow = flow;
      flowPlayhead = null;
      renderTraceFlow();
    } catch (_) {
      if (traceSession === id) { traceFlow = null; renderTraceFlow(); }
    }
  }

  function selectTraceSession(id) {
    traceFlow = null;
    renderTraceFlow();
    loadTraceSession(id);
    loadTraceFlow(id);
  }

  function renderTraceIndex() {
    if (!traceIndex) return;
    const all = traceIndex.sessions || [];
    $("trace-workspaces").innerHTML = [{ id: "all", name: "全部", count: all.length }, ...traceIndex.workspaces.map(id => ({ id, name: workspaceNames[id] || id, count: all.filter(item => item.workspace === id).length }))]
      .map(item => `<button class="trace-workspace" type="button" data-trace-workspace="${esc(item.id)}" aria-pressed="${item.id === traceWorkspace}">${esc(item.name)} <span>${item.count}</span></button>`).join("");
    const sessions = all.filter(item => traceWorkspace === "all" || item.workspace === traceWorkspace);
    $("trace-count").textContent = `${sessions.length} 条`;
    $("trace-sessions").innerHTML = sessions.length ? sessions.map(item => `<button class="trace-session" type="button" data-trace-session="${esc(item.id)}" aria-pressed="${item.id === traceSession}"><span class="trace-session-top"><strong>${esc(workspaceNames[item.workspace] || item.workspace)}</strong><time>${esc(formatTime(item.updated_at))}</time></span><span class="trace-session-meta">${esc(item.agent)}${item.parent_id ? " · 子 Agent" : ""} · ${esc(item.id.slice(0, 8))}</span></button>`).join("") : '<div class="empty-state">这个工作区还没有本机 Codex 会话。</div>';
  }

  function traceEventHTML(event) {
    const time = event.at ? new Intl.DateTimeFormat("zh-CN", { timeZone: data?.timezone || "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(event.at)) : "—";
    return `<article class="trace-event ${esc(event.kind)}"><div class="trace-event-head"><span class="trace-kind">${esc(event.label)}</span><time>${esc(time)}</time><span>#${esc(event.line)}</span></div><p>${esc(event.detail)}</p></article>`;
  }

  async function loadTraceSession(id, append = false, jumpLine = null) {
    if (offline || (append && traceLoading)) return;
    const requestToken = ++traceRequestToken;
    traceLoading = true;
    const cursor = append ? traceCursor : jumpLine ? Math.max(0, jumpLine - 1) : 0;
    if (!append) {
      traceSession = id;
      traceCursor = 0;
      renderTraceIndex();
      if (traceFlow) renderTraceFlow();
      $("trace-events").innerHTML = '<div class="empty-state">正在读取时间线…</div>';
      $("trace-more").hidden = true;
    }
    try {
      const response = await fetch(`/api/traces/${encodeURIComponent(id)}?cursor=${cursor}`, { cache: "no-store" });
      if (!response.ok) throw new Error("read failed");
      const result = await response.json();
      if (traceSession !== id || requestToken !== traceRequestToken) return;
      const session = result.session;
      const summary = result.summary || {};
      $("trace-heading").innerHTML = `<strong>${esc(workspaceNames[session.workspace] || session.workspace)}${jumpLine ? ` · 第 ${jumpLine} 行附近` : ""}</strong><small>${esc(session.agent)} · ${summary.turns ?? "—"} 轮 · ${summary.tool_calls ?? "—"} 次工具调用 · ${summary.total_tokens === null ? "Token 未记录" : `${Number(summary.total_tokens).toLocaleString("en-US")} 日志累计 Token`}<br>${esc(session.id)}${session.parent_id ? ` · 父会话 ${esc(session.parent_id.slice(0, 8))}` : ""}</small>`;
      const html = result.events.map(traceEventHTML).join("");
      if (append) $("trace-events").insertAdjacentHTML("beforeend", html);
      else $("trace-events").innerHTML = html || '<div class="empty-state">当前会话尚无可展示的事件。</div>';
      traceCursor = result.next_cursor;
      $("trace-more").hidden = traceCursor === null;
    } catch (_) {
      if (requestToken !== traceRequestToken) return;
      $("trace-events").innerHTML = '<div class="empty-state">暂时无法读取该会话。请稍后重试。</div>';
      $("trace-more").hidden = true;
    } finally { if (requestToken === traceRequestToken) traceLoading = false; }
  }

  async function loadTraces() {
    if (offline) {
      $("trace-workspaces").innerHTML = "";
      $("trace-sessions").innerHTML = '<div class="empty-state">离线快照不包含个人会话。请打开在线本机看板。</div>';
      $("trace-refresh").hidden = true;
      return;
    }
    try {
      const response = await fetch("/api/traces", { cache: "no-store" });
      if (response.status === 403) {
        $("trace-sessions").innerHTML = '<div class="empty-state">详细 Trace 仅限本机访问。远程查看请通过 SSH 隧道打开 127.0.0.1:8790。</div>';
        $("trace-workspaces").innerHTML = "";
        return;
      }
      if (!response.ok) throw new Error("read failed");
      traceIndex = await response.json();
      if (traceIndex.enabled === false) {
        $("trace-sessions").innerHTML = '<div class="empty-state">会话查看尚未启用。需要时按 README 配置项目目录，并使用 --enable-traces 启动。</div>';
        $("trace-workspaces").innerHTML = "";
        return;
      }
      renderTraceIndex();
      const visible = traceIndex.sessions.filter(item => traceWorkspace === "all" || item.workspace === traceWorkspace);
      const choice = visible.some(item => item.id === traceSession) ? traceSession : visible[0]?.id;
      if (choice) selectTraceSession(choice);
      else { traceSession = null; traceFlow = null; renderTraceFlow(); }
    } catch (_) { $("trace-sessions").innerHTML = '<div class="empty-state">会话索引暂时不可用。请稍后点击更新会话。</div>'; }
  }

  function filtered() {
    return data.projects.filter(project => (kind === "all" || kind === project.kind) &&
      `${project.name} ${project.description} ${project.cluster} ${project.stage}`.toLowerCase().includes(query));
  }

  function renderSummary() {
    const summary = data.summary;
    const resourceCounts = Object.entries(data.projects.reduce((counts, project) => { counts[project.kind] = (counts[project.kind] || 0) + 1; return counts; }, {})).map(([name, count]) => `${count} ${esc(name)}`).join(" / ");
    const latest = data.jobs.map(job => job.observed_at).sort().pop();
    const stale = ageMinutes(latest) > 5;
    $("summary-strip").innerHTML = [
      `<div class="summary-item"><span class="summary-number">${summary.projects}</span><div class="summary-text"><strong>项目总数</strong>${resourceCounts}</div></div>`,
      `<div class="summary-item">${svg("check", "resource-symbol")}<span class="summary-number">${summary.verified_baselines}</span><div class="summary-text"><strong>已有验收证据</strong>报告声明通过，证据 SHA256 匹配</div></div>`,
      `<div class="summary-item ${stale ? "warning" : ""}"><span class="summary-number">${summary.observed_running_jobs}</span><div class="summary-text"><strong>日志记录为运行中</strong>${stale ? "状态需结合记录时间" : "以最近一次观测为准"}</div></div>`,
      `<div class="summary-item">${svg("clock", "resource-symbol")}<div class="summary-text"><strong>${offline ? "离线快照" : `每${data.refresh_seconds}秒自动读取`}</strong>${offline ? "保存于 " : "读取于 "}${esc(formatTime(data.generated_at))}</div></div>`,
    ].join("");
    $("generated-at").textContent = `${offline ? "快照生成" : "本机读取"} ${formatTime(data.generated_at)} · ${data.timezone}`;
  }

  function renderProjects(projects) {
    $("project-list").innerHTML = projects.length ? projects.map(project => `
      <button class="project-row" data-project="${esc(project.id)}" aria-label="查看 ${esc(project.name)} 项目详情">
        <div class="project-name"><span class="project-emblem ${esc(project.kind.toLowerCase())}">${svg(project.kind.toLowerCase())}</span><div><strong>${esc(project.name)}</strong><small>${esc(project.kind)} · ${esc(project.cluster)}</small></div></div>
        <div class="stage-cell"><span class="badge ${esc(project.tone)}">${esc(project.stage)}</span><small>记录更新 ${esc(formatTime(project.updated_at))}</small></div>
        <div class="phase-cell"><div class="phase-track">${data.phase_labels.map((label, i) => `<span class="phase-step ${esc(project.phase_states[i])}"><i class="phase-dot ${esc(project.phase_states[i])}"></i>${esc(label)}</span>`).join("")}</div></div>
        <div class="metric-cell"><div class="metric-number">${metric(project.primary)}<span class="metric-unit">${esc(project.primary.unit)}</span></div><div class="metric-description">${esc(project.primary.label)}<br>${esc(project.secondary)}</div></div>
        ${svg("arrow", "row-chevron")}
      </button>`).join("") : '<div class="empty-state">没有匹配的项目。请清空搜索或切换资源类型。</div>';
  }

  function makeChart(id, option) {
    if (!window.echarts) return false;
    let chart = charts.get(id);
    if (!chart) { chart = window.echarts.init($(id), null, { renderer: "svg" }); charts.set(id, chart); }
    chart.setOption(option, true);
    chart.resize();
    return true;
  }

  function chartBase() {
    return {
      animation: false,
      textStyle: { fontFamily: '"Segoe UI", "Microsoft YaHei", sans-serif', color: "#627185", fontSize: 10 },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, textStyle: { fontSize: 11 }, confine: true },
      aria: { enabled: true },
      grid: { top: 24, bottom: 48, left: 80, right: 55, containLabel: false },
    };
  }

  function fallback(id, rows, max) {
    $(id).innerHTML = rows.map(row => `<div class="fallback-row"><span>${esc(row.label)}</span><span class="fallback-bar"><i style="width:${(row.value / max * 100).toFixed(2)}%"></i></span><span class="fallback-value">${esc(row.display)}</span></div>`).join("");
  }

  function renderCharts(projects) {
    charts.forEach(chart => chart.dispose());
    charts.clear();
    const available = projects.filter(project => project.chart);
    $("analysis-grid").innerHTML = available.map((project, index) => `
      <article class="panel performance-panel" data-chart-project="${esc(project.id)}">
        <div class="panel-heading"><div><span class="resource-tag ${esc(project.kind.toLowerCase())}">${esc(project.kind)}</span><h3>${esc(project.name)}</h3></div><span class="small-label">${esc(project.chart.unit)} · ${project.chart.direction === "higher" ? "越高越好" : "越低越好"}</span></div>
        <div class="chart-insight"><strong>${esc(project.chart.title)}</strong>${esc(project.chart.insight)}</div>
        <div id="chart-${index}" class="chart" role="img" aria-label="${esc(project.name)} ${esc(project.chart.title)}"></div>
        <div id="chart-data-${index}" class="chart-fallback"></div><div class="chart-note">${esc(project.chart.note)}</div>
      </article>`).join("") || '<div id="no-performance" class="panel empty-state">当前筛选下尚无已验证的性能记录。</div>';
    available.forEach((project, index) => {
      const chart = project.chart;
      const colors = ["#a2b2c4", "#237b70", "#4c71c6", "#d5a24e"];
      const option = chartBase();
      option.grid = { top: 12, bottom: 74, left: 88, right: 65 };
      option.tooltip = { trigger: "axis", axisPointer: { type: "shadow" }, renderMode: "richText", confine: true };
      option.legend = { type: "scroll", bottom: 4, itemHeight: 8, textStyle: { fontSize: 10 } };
      option.xAxis = { type: "value", min: 0, name: chart.unit, nameLocation: "middle", nameGap: 28,
        splitLine: { lineStyle: { color: "#e7ecf2", type: "dashed" } } };
      option.yAxis = { type: "category", inverse: true, data: chart.labels, axisTick: { show: false }, axisLine: { show: false }, axisLabel: { fontSize: 10, width: 78, overflow: "truncate" } };
      option.series = chart.series.map((item, seriesIndex) => ({ name: item.name, type: "bar", barMaxWidth: 23,
        data: item.values, itemStyle: { color: colors[seriesIndex % colors.length] },
        label: { show: true, position: "right", fontSize: 10, formatter: params => Number(params.value).toFixed(chart.digits) } }));
      const rendered = makeChart(`chart-${index}`, option);
      $(`chart-${index}`).hidden = !rendered;
      $(`chart-data-${index}`).classList.toggle("visible", !rendered);
      const rows = chart.labels.flatMap((label, rowIndex) => chart.series.map(item => ({
        label: `${label} · ${item.name}`, value: item.values[rowIndex], display: `${item.values[rowIndex].toFixed(chart.digits)} ${chart.unit}` })));
      fallback(`chart-data-${index}`, rows, Math.max(1, ...rows.map(row => row.value)) * 1.1);
    });
    $("analysis-grid").style.gridTemplateColumns = available.length === 1 ? "1fr" : "";
  }

  function boundsDetail(bounds) {
    const list = items => `<ul>${(items || []).map(item => `<li>${esc(item)}</li>`).join("")}</ul>`;
    return `<div class="bound-proof"><h4>推导</h4>${list(bounds.derivation)}<h4>适用条件与边界</h4>${list(bounds.assumptions)}<h4>下一步要测什么</h4>${list(bounds.next_measurements)}<h4>证据与原理来源</h4>${(bounds.sources || []).map(item => `<p class="bound-source"><code>${esc(item.path)}</code><small>SHA256 ${esc(item.sha256)}</small></p>`).join("")}${(bounds.references || []).map(item => `<p><a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)} ↗</a></p>`).join("")}${!(bounds.sources?.length || bounds.references?.length) ? '<p class="small-label">尚无足够输入可构造数值上界。</p>' : ""}</div>`;
  }

  function renderBounds(projects) {
    $("bounds-list").innerHTML = projects.map(project => {
      const bounds = project.bounds;
      if (!bounds) return "";
      const percent = bounds.model?.reference_percent;
      const bar = Number.isFinite(percent) ? `<div class="bound-bar" aria-label="${esc(bounds.kind)}参考占比 ${percent.toFixed(2)}%"><span style="width:${Math.max(0, Math.min(100, percent)).toFixed(2)}%"></span></div><p class="bound-scale">参考占比 ${percent.toFixed(2)}% · 适用条件见推导</p>` : "";
      return `<article class="panel bound-card ${esc(bounds.status)}" data-bound-project="${esc(project.id)}"><div class="bound-heading"><h3>${esc(project.name)}</h3><span>${esc(bounds.kind)}</span></div><p class="bound-current">当前：${esc(bounds.current)}</p><h4 class="bound-headline">${esc(bounds.headline)}</h4><p class="bound-ceiling">${esc(bounds.ceiling)}</p>${bar}<p class="bound-gap">${esc(bounds.gap)}</p><p class="bound-meaning">${esc(bounds.meaning)}</p><details class="bound-details"><summary>查看推导、假设与证据</summary>${boundsDetail(bounds)}</details></article>`;
    }).join("") || '<div class="panel empty-state">当前筛选下没有匹配项目。</div>';
  }

  function renderJobs(projects) {
    const jobs = projects.flatMap(project => project.jobs || []);
    const latest = jobs.map(job => job.observed_at).sort().pop();
    $("job-age").innerHTML = latest ? `${esc(formatTime(latest))}${ageMinutes(latest) > 5 ? '<span class="stale-label">记录陈旧</span>' : " · 最近记录"}` : "无已接入的监控日志";
    $("job-list").innerHTML = jobs.length ? jobs.map(job => `<tr><td class="mono">${esc(job.id)}</td><td>${esc(job.version)} · ${esc(job.label)}</td><td><span class="job-state ${stateClass(job.state)}"><i></i>${esc(labels[job.state] || job.state)}</span></td><td class="mono">${esc(job.elapsed || "—")}</td></tr>`).join("") : '<tr><td colspan="4" class="empty-state">当前项目没有已接入的作业观测记录。</td></tr>';
  }

  function renderFocus(projects) {
    $("focus-list").innerHTML = projects.map(project => `<div class="focus-row"><button class="focus-project" data-project="${esc(project.id)}">${esc(project.name)}</button><p>${esc(project.next_steps[0] || "先核对项目来源文件，确认当前进展。")}</p></div>`).join("") || '<div class="empty-state">没有匹配的项目。</div>';
  }

  function renderLearning(projects) {
    const candidates = projects.filter(project => project.learning?.versions?.length);
    const owner = candidates.find(project => project.id === learningProject) || candidates[0];
    const learning = owner?.learning;
    if (owner) learningProject = owner.id;
    $("learning-project").innerHTML = candidates.map(project => `<option value="${esc(project.id)}" ${project.id === owner?.id ? "selected" : ""}>${esc(project.name)}</option>`).join("");
    $("learning").hidden = !learning;
    if (!learning) return;
    const item = learning.versions.find(version => version.version === selectedVersion) || learning.versions[0];
    $("version-picker").innerHTML = learning.versions.map(version => `<button class="version-button" data-version="${esc(version.version)}" aria-pressed="${version.version === item.version}"><strong>${esc(version.version)}</strong><small>${esc(version.status)}</small></button>`).join("");
    $("version-detail").innerHTML = `
      <div class="learning-title"><div><span class="eyebrow">${esc(item.version)} / ${esc(item.status)}</span><h3>${esc(item.title)}</h3></div><p>${esc(item.result)}</p></div>
      <div class="mechanism-flow"><div><small>原路径</small><strong>${esc(item.before)}</strong></div><span aria-hidden="true">→</span><div><small>改进路径 / 方案</small><strong>${esc(item.after)}</strong></div></div>
      <div class="lesson-grid"><article><h4>先找到问题</h4><p>${esc(item.bottleneck)}</p></article><article><h4>核心改动</h4><p>${esc(item.change)}</p></article><article><h4>怎样证明有效</h4><p>${esc(item.validate)}</p></article></div>
      <div class="lesson-takeaway"><strong>我们能学到什么</strong><p>${esc(item.lesson)}</p></div>
      <p class="learning-boundary"><strong>结论边界：</strong>${esc(item.boundary)}</p><div class="learning-source"><span>报告来源</span><code>${esc(item.source)}</code></div>
      <details class="learning-references"><summary>原理参考与阅读材料</summary><ul>${learning.references.map(reference => `<li><a href="${esc(reference.url)}" target="_blank" rel="noopener noreferrer">${esc(reference.title)}</a></li>`).join("")}</ul></details>`;
  }

  function render() {
    document.title = data.title;
    $("page-title").textContent = data.title;
    $("page-subtitle").textContent = data.subtitle;
    $("brand-title").textContent = data.title;
    $("data-banner").hidden = !data.demo;
    $("footer-status").textContent = `只读项目看板 · ${offline ? "离线快照" : `每${data.refresh_seconds}秒读取报告`} · ${data.timezone}`;
    data.projects.forEach(project => { workspaceNames[project.id] = project.name; });
    $("resource-filters").innerHTML = ["all", ...new Set(data.projects.map(project => project.kind))].map(value => `<button data-kind="${esc(value)}" aria-pressed="${kind === value}">${esc(value === "all" ? "全部项目" : value)}</button>`).join("");
    if (traceIndex?.enabled !== false) renderTraceIndex();
    renderSummary();
    const projects = filtered();
    renderProjects(projects);
    renderCharts(projects);
    renderBounds(projects);
    renderJobs(projects);
    renderFocus(projects);
    renderLearning(projects);
    if (data.warnings.length) {
      $("error-banner").hidden = false;
      $("error-banner").textContent = data.warnings.map(item => `${item.project}：${item.message}`).join("；");
    }
  }

  function openProject(id) {
    const project = data.projects.find(item => item.id === id);
    if (!project) return;
    let inventoryHTML = "";
    if (project.chart) {
      const chart = project.chart;
      inventoryHTML = `<table class="detail-table"><thead><tr><th>比较项</th>${chart.series.map(item => `<th>${esc(item.name)} / ${esc(chart.unit)}</th>`).join("")}</tr></thead><tbody>${chart.labels.map((label, rowIndex) => `<tr><td>${esc(label)}</td>${chart.series.map(item => `<td class="mono">${item.values[rowIndex].toFixed(chart.digits)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    }
    $("dialog-body").innerHTML = `
      <div class="dialog-title-row"><h2 id="dialog-title">${esc(project.name)}</h2><span class="resource-tag ${esc(project.kind.toLowerCase())}">${esc(project.kind)} · ${esc(project.cluster)}</span></div>
      <p class="dialog-description">${esc(project.description)}<br>${esc(project.summary)}</p>
      <div class="dialog-metrics"><div><div class="metric-number">${metric(project.primary)}<span class="metric-unit">${esc(project.primary.unit)}</span></div><small>${esc(project.primary.label)}</small></div><div><span class="badge ${esc(project.tone)}">${esc(project.stage)}</span><small>${esc(project.correctness)}</small></div></div>
      ${inventoryHTML ? `<section class="dialog-section"><h3>记录明细</h3>${inventoryHTML}</section>` : ""}
      ${project.bounds ? `<section class="dialog-section"><h3>性能上界与剩余空间</h3><p><strong>${esc(project.bounds.kind)}</strong></p><p>${esc(project.bounds.ceiling)}</p><p>${esc(project.bounds.headline)}；${esc(project.bounds.gap)}</p><details class="bound-details"><summary>查看适用条件与证据</summary>${boundsDetail(project.bounds)}</details></section>` : ""}
      <section class="dialog-section"><h3>建议下一步</h3><ol>${project.next_steps.map(text => `<li>${esc(text)}</li>`).join("") || "<li>先核对项目来源记录。</li>"}</ol></section>
      ${project.limitations.length ? `<section class="dialog-section"><h3>怎样理解这些记录</h3>${project.limitations.map(text => `<p>${esc(text)}</p>`).join("")}</section>` : ""}
      ${project.git ? `<section class="dialog-section"><h3>代码版本</h3><div class="detail-git"><code>${esc(project.git.commit)}</code><span>${esc(project.git.branch)}</span><span>${project.git.modified_tracked_files} 个已跟踪文件有未提交修改</span></div><p>${esc(project.git.subject)}</p></section>` : ""}
      <section class="dialog-section"><h3>数据来源 · 相对配置的项目目录</h3>${project.sources.map(source => `<div class="source-row"><strong>${esc(source.label)}</strong><code>${esc(source.path)}</code><small>SHA256 ${esc(source.sha256)}</small><time>文件更新 ${esc(formatTime(source.modified_at))} · ${esc(age(source.modified_at))}</time></div>`).join("") || "<p>尚无可读取的来源文件。</p>"}</section>`;
    if (!$("project-dialog").open) $("project-dialog").showModal();
    $("project-dialog").scrollTop = 0;
  }

  async function load() {
    if (pending) return;
    pending = true;
    $("refresh-button").disabled = true;
    $("refresh-label").textContent = "读取中…";
    try {
      if (offline) data = window.DASHBOARD_SNAPSHOT;
      else {
        const response = await fetch("/api/dashboard", { cache: "no-store" });
        if (!response.ok) throw new Error("read failed");
        data = await response.json();
      }
      $("error-banner").hidden = true;
      render();
      if (!offline && !refreshTimer) refreshTimer = setInterval(() => { if (!document.hidden) load(); }, data.refresh_seconds * 1000);
    } catch (error) {
      $("error-banner").textContent = data ? "刷新失败，当前保留上一次读取结果。请检查看板服务后重试。" : "暂时无法读取项目记录。请检查看板服务后点击刷新。";
      $("error-banner").hidden = false;
    } finally {
      pending = false;
      $("refresh-button").disabled = offline;
      $("refresh-label").textContent = offline ? "离线快照" : "刷新记录";
    }
  }

  $("refresh-button").addEventListener("click", load);
  $("trace-refresh").addEventListener("click", loadTraces);
  $("trace-more").addEventListener("click", () => { if (traceSession && traceCursor !== null) loadTraceSession(traceSession, true); });
  $("search").addEventListener("input", event => { query = event.target.value.trim().toLowerCase(); if (data) render(); });
  $("learning-project").addEventListener("change", event => { learningProject = event.target.value; selectedVersion = null; if (data) renderLearning(filtered()); });
  document.addEventListener("click", event => {
    const kindButton = event.target.closest("[data-kind]");
    if (kindButton) { kind = kindButton.dataset.kind; if (data) render(); }
    const rangeButton = event.target.closest("[data-flow-window]");
    if (rangeButton) { flowWindow = rangeButton.dataset.flowWindow; flowPlayhead = null; renderTraceFlow(); }
    const flowSlice = event.target.closest("[data-flow-line]");
    if (flowSlice) {
      const laneId = flowSlice.dataset.flowSession;
      const line = Number(flowSlice.dataset.flowLine);
      const lane = traceFlow?.lanes.find(item => item.id === laneId);
      const slice = lane?.slices.find(item => item.line === line);
      flowPlayhead = slice?.start_ms ?? null;
      loadTraceSession(laneId, false, line);
      document.querySelector(".trace-detail")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    const flowAgent = event.target.closest("[data-flow-agent]");
    if (flowAgent) loadTraceSession(flowAgent.dataset.flowAgent);
    const workspaceButton = event.target.closest("[data-trace-workspace]");
    if (workspaceButton) {
      traceWorkspace = workspaceButton.dataset.traceWorkspace;
      renderTraceIndex();
      const visible = traceIndex.sessions.filter(item => traceWorkspace === "all" || item.workspace === traceWorkspace);
      if (visible.length) selectTraceSession(visible[0].id);
      else { traceSession = null; traceFlow = null; renderTraceFlow(); $("trace-events").innerHTML = '<div class="empty-state">这个工作区还没有本机 Codex 会话。</div>'; }
    }
    const sessionButton = event.target.closest("[data-trace-session]");
    if (sessionButton) selectTraceSession(sessionButton.dataset.traceSession);
    const version = event.target.closest("[data-version]");
    if (version && data) { selectedVersion = version.dataset.version; renderLearning(filtered()); }
    const button = event.target.closest("[data-project]");
    if (button && data) openProject(button.dataset.project);
  });
  $("close-dialog").addEventListener("click", () => $("project-dialog").close());
  $("project-dialog").addEventListener("click", event => { if (event.target === $("project-dialog")) { const rect = event.target.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right) event.target.close(); } });
  window.addEventListener("resize", () => { charts.forEach(chart => chart.resize()); if (traceFlow) renderTraceFlow(); });
  if (offline) {
    $("export-button").hidden = true;
    document.querySelector(".header-status").innerHTML = '<span class="status-dot"></span>离线快照 · 不自动更新';
  } else {
    document.addEventListener("visibilitychange", () => { if (!document.hidden && data && ageMinutes(data.generated_at) > 1) load(); });
  }
  load();
  loadTraces();
})();
