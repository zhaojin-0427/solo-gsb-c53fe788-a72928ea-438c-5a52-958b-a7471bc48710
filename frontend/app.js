// 建筑无障碍通路核验台 - 前端逻辑

const state = {
  network: null, // { version, data }
  closures: [],
  currentFloorId: null,
  routeResult: null,
};

const SVG_NS = "http://www.w3.org/2000/svg";

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function $(id) {
  return document.getElementById(id);
}

function setMsg(id, text, type = "") {
  const el = $(id);
  el.textContent = text;
  el.className = "msg " + type;
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("zh-CN", { hour12: false });
}

function fmtDuration(sec) {
  if (sec == null) return "";
  if (sec < 60) return sec.toFixed(0) + " 秒";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return s ? `${m} 分 ${s} 秒` : `${m} 分`;
}

// ---------------------------------------------------------------------------
// 路网导入
// ---------------------------------------------------------------------------

$("btn-import").addEventListener("click", async () => {
  const file = $("import-file").files[0];
  if (!file) {
    setMsg("import-msg", "请先选择 JSON 文件", "error");
    return;
  }
  try {
    const text = await file.text();
    const data = JSON.parse(text);
    const resp = await fetch("/api/network/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    const result = await resp.json();
    if (!resp.ok) {
      const details = result.detail?.errors || [result.detail || "导入失败"];
      setMsg("import-msg", "导入失败：" + details.join("；"), "error");
      return;
    }
    setMsg("import-msg", `导入成功，路网版本 v${result.version}`, "success");
    await loadNetwork();
  } catch (e) {
    setMsg("import-msg", "解析失败：" + e.message, "error");
  }
});

$("network-version-select").addEventListener("change", async (e) => {
  const version = parseInt(e.target.value, 10);
  if (!version) return;
  const resp = await fetch(`/api/network/${version}`);
  const result = await resp.json();
  state.network = { version, data: result.data };
  state.currentFloorId = result.data.floors[0]?.id || null;
  renderFloorTabs();
  renderMap();
  await loadClosures();
});

async function loadNetwork() {
  const resp = await fetch("/api/network");
  if (!resp.ok) {
    state.network = null;
    state.closures = [];
    updateNetworkStatus();
    renderFloorTabs();
    renderMap();
    renderClosures();
    return;
  }
  const result = await resp.json();
  state.network = { version: result.version, data: result.data };
  state.currentFloorId = result.data.floors[0]?.id || null;
  updateNetworkStatus();
  await loadNetworkVersions();
  renderFloorTabs();
  renderMap();
  await loadClosures();
}

async function loadNetworkVersions() {
  const resp = await fetch("/api/network/versions");
  const result = await resp.json();
  const sel = $("network-version-select");
  sel.innerHTML = "";
  result.versions.forEach((v) => {
    const opt = document.createElement("option");
    opt.value = v.version;
    opt.textContent = `v${v.version} (${new Date(v.created_at).toLocaleString("zh-CN")})`;
    if (state.network && v.version === state.network.version) opt.selected = true;
    sel.appendChild(opt);
  });
}

function updateNetworkStatus() {
  const el = $("network-status");
  if (state.network) {
    el.textContent = `路网版本 v${state.network.version}`;
  } else {
    el.textContent = "未导入路网";
  }
}

// ---------------------------------------------------------------------------
// 楼层图渲染
// ---------------------------------------------------------------------------

function renderFloorTabs() {
  const container = $("floor-tabs");
  container.innerHTML = "";
  if (!state.network) return;
  state.network.data.floors.forEach((f) => {
    const btn = document.createElement("button");
    btn.className = "floor-tab" + (f.id === state.currentFloorId ? " active" : "");
    btn.textContent = f.name;
    btn.addEventListener("click", () => {
      state.currentFloorId = f.id;
      renderFloorTabs();
      renderMap();
    });
    container.appendChild(btn);
  });
}

function renderMap() {
  const svg = $("floor-svg");
  svg.innerHTML = "";
  if (!state.network || !state.currentFloorId) return;

  const { floors, nodes, edges } = state.network.data;
  const floor = floors.find((f) => f.id === state.currentFloorId);
  if (!floor) return;

  const floorNodes = nodes.filter((n) => n.floor_id === state.currentFloorId);
  const nodeIds = new Set(floorNodes.map((n) => n.id));

  // 计算边界
  const xs = floorNodes.map((n) => n.x);
  const ys = floorNodes.map((n) => n.y);
  const minX = Math.min(...xs) - 40;
  const maxX = Math.max(...xs) + 40;
  const minY = Math.min(...ys) - 40;
  const maxY = Math.max(...ys) + 40;
  const width = maxX - minX;
  const height = maxY - minY;

  svg.setAttribute("viewBox", `${minX} ${minY} ${width} ${height}`);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);

  // 绘制边
  const edgeLayer = document.createElementNS(SVG_NS, "g");
  edgeLayer.setAttribute("class", "edges");
  edges.forEach((e) => {
    const from = nodes.find((n) => n.id === e.from);
    const to = nodes.find((n) => n.id === e.to);
    if (!from || !to) return;
    // 只绘制本楼层的边（两端都在本楼层）
    if (from.floor_id !== state.currentFloorId && to.floor_id !== state.currentFloorId) return;

    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", from.x);
    line.setAttribute("y1", from.y);
    line.setAttribute("x2", to.x);
    line.setAttribute("y2", to.y);
    line.setAttribute("class", "edge-line");
    line.setAttribute("stroke", edgeColor(e.type));
    line.setAttribute("stroke-width", edgeWidth(e.type));
    line.setAttribute("stroke-dasharray", e.type === "stairs" ? "6,4" : "none");
    line.dataset.edgeId = e.id;

    // 高亮路线
    if (state.routeResult && state.routeResult.reachable) {
      const routeEdgeIds = new Set(state.routeResult.steps.map((s) => s.edge_id));
      if (routeEdgeIds.has(e.id)) {
        line.setAttribute("stroke", "#ef4444");
        line.setAttribute("stroke-width", edgeWidth(e.type) + 3);
        line.style.opacity = "1";
      } else {
        line.style.opacity = "0.35";
      }
    }

    line.addEventListener("click", () => showEdgeInfo(e));
    edgeLayer.appendChild(line);

    // 边标签
    const mx = (from.x + to.x) / 2;
    const my = (from.y + to.y) / 2;
    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("x", mx);
    text.setAttribute("y", my - 4);
    text.setAttribute("class", "edge-label");
    text.textContent = e.id;
    edgeLayer.appendChild(text);
  });
  svg.appendChild(edgeLayer);

  // 绘制节点
  const nodeLayer = document.createElementNS(SVG_NS, "g");
  nodeLayer.setAttribute("class", "nodes");
  floorNodes.forEach((n) => {
    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("cx", n.x);
    circle.setAttribute("cy", n.y);
    circle.setAttribute("r", nodeRadius(n));
    circle.setAttribute("class", "node-circle");
    circle.setAttribute("fill", nodeColor(n.kind));
    circle.dataset.nodeId = n.id;

    // 高亮路线节点
    if (state.routeResult && state.routeResult.reachable) {
      const routeNodeIds = new Set();
      state.routeResult.steps.forEach((s) => {
        routeNodeIds.add(s.from_node);
        routeNodeIds.add(s.to_node);
      });
      if (routeNodeIds.has(n.id)) {
        circle.setAttribute("stroke", "#ef4444");
        circle.setAttribute("stroke-width", "3");
      } else {
        circle.style.opacity = "0.4";
      }
    }

    circle.addEventListener("click", () => showNodeInfo(n));
    nodeLayer.appendChild(circle);

    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", n.x);
    label.setAttribute("y", n.y - nodeRadius(n) - 4);
    label.setAttribute("class", "node-label");
    label.textContent = n.name || n.id;
    nodeLayer.appendChild(label);
  });
  svg.appendChild(nodeLayer);
}

function edgeColor(type) {
  switch (type) {
    case "elevator":
      return "#8b5cf6";
    case "ramp":
      return "#ec4899";
    case "stairs":
      return "#9ca3af";
    default:
      return "#3b82f6";
  }
}

function edgeWidth(type) {
  return type === "corridor" ? 4 : 5;
}

function nodeColor(kind) {
  switch (kind) {
    case "entrance":
      return "#10b981";
    case "exit":
      return "#14b8a6";
    case "room":
      return "#f59e0b";
    case "elevator":
      return "#8b5cf6";
    default:
      return "#6b7280";
  }
}

function nodeRadius(n) {
  if (n.kind === "entrance" || n.kind === "exit") return 9;
  if (n.kind === "room") return 8;
  return 6;
}

function showNodeInfo(n) {
  const edges = state.network.data.edges.filter(
    (e) => e.from === n.id || e.to === n.id
  );
  const info = $("node-info");
  info.innerHTML = `<strong>${escapeHtml(n.name || n.id)}</strong> (${n.id}) · 类型: ${escapeHtml(n.kind)} · 坐标: (${n.x}, ${n.y}) · 连接通道: ${edges.map((e) => e.id).join(", ") || "无"}`;
}

function showEdgeInfo(e) {
  const info = $("node-info");
  info.innerHTML = `<strong>通道 ${escapeHtml(e.id)}</strong> · 类型: ${escapeHtml(e.type)} · 长度: ${e.length}m · 通行: ${e.seconds}s · 净宽: ${e.width}m · 坡度: ${e.slope}°`;
}

// ---------------------------------------------------------------------------
// 封闭时段
// ---------------------------------------------------------------------------

async function loadClosures() {
  if (!state.network) {
    state.closures = [];
    renderClosures();
    return;
  }
  const resp = await fetch("/api/closures");
  const result = await resp.json();
  state.closures = result.closures || [];
  renderClosures();
}

function renderClosures() {
  const container = $("closure-list");
  if (state.closures.length === 0) {
    container.innerHTML = '<p class="hint">暂无封闭时段。</p>';
    return;
  }
  container.innerHTML = "";
  state.closures.forEach((c) => {
    const item = document.createElement("div");
    item.className = "closure-item";
    const start = new Date(c.start).toLocaleString("zh-CN", { hour12: false });
    const end = new Date(c.end).toLocaleString("zh-CN", { hour12: false });
    item.innerHTML = `<span><strong>${escapeHtml(c.edge_id)}</strong>: ${start} ~ ${end}</span>`;
    const del = document.createElement("button");
    del.className = "del-btn";
    del.textContent = "删除";
    del.addEventListener("click", () => deleteClosure(c.id));
    item.appendChild(del);
    container.appendChild(item);
  });
}

$("btn-add-closure").addEventListener("click", async () => {
  const edgeId = $("closure-edge").value.trim();
  const startVal = $("closure-start").value;
  const endVal = $("closure-end").value;
  const tz = $("closure-tz").value.trim() || "UTC";
  if (!edgeId || !startVal || !endVal) {
    setMsg("query-msg", "请填写完整的封闭时段信息", "error");
    return;
  }
  // datetime-local 转 ISO 带时区
  const start = localToIso(startVal, tz);
  const end = localToIso(endVal, tz);
  try {
    const resp = await fetch("/api/closures", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ edge_id: edgeId, start, end, tz }),
    });
    const result = await resp.json();
    if (!resp.ok) {
      setMsg("query-msg", "登记失败：" + (result.detail || "未知错误"), "error");
      return;
    }
    setMsg("query-msg", "封闭时段已登记", "success");
    $("closure-edge").value = "";
    await loadClosures();
  } catch (e) {
    setMsg("query-msg", "登记失败：" + e.message, "error");
  }
});

async function deleteClosure(id) {
  const resp = await fetch(`/api/closures/${id}`, { method: "DELETE" });
  if (resp.ok) {
    await loadClosures();
  }
}

function localToIso(localStr, tz) {
  // localStr 格式: 2026-09-29T08:00:00
  // 构造带时区的 ISO 字符串
  const dt = new Date(localStr);
  // 使用 Intl 时区偏移
  const offset = getTimezoneOffset(tz, dt);
  const sign = offset <= 0 ? "+" : "-";
  const abs = Math.abs(offset);
  const oh = String(Math.floor(abs / 60)).padStart(2, "0");
  const om = String(abs % 60).padStart(2, "0");
  return localStr + sign + oh + om;
}

function getTimezoneOffset(tz, date) {
  // 返回分钟偏移（UTC - local）
  try {
    const dtf = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
    const parts = dtf.formatToParts(date);
    const map = {};
    parts.forEach((p) => (map[p.type] = p.value));
    const asLocal = new Date(
      `${map.year}-${map.month}-${map.day}T${map.hour}:${map.minute}:${map.second}`
    );
    return (asLocal - date) / 60000;
  } catch {
    return 0;
  }
}

// ---------------------------------------------------------------------------
// 路线查询
// ---------------------------------------------------------------------------

$("btn-query").addEventListener("click", async () => {
  if (!state.network) {
    setMsg("query-msg", "请先导入路网", "error");
    return;
  }
  const start = $("q-start").value.trim();
  const end = $("q-end").value.trim();
  const depVal = $("q-departure").value;
  const minWidth = parseFloat($("q-min-width").value) || 0;
  const maxSlope = parseFloat($("q-max-slope").value) || 90;
  if (!start || !end || !depVal) {
    setMsg("query-msg", "请填写起终点和出发时刻", "error");
    return;
  }
  const departure = localToIso(depVal, "Asia/Shanghai");
  try {
    const resp = await fetch("/api/route/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start,
        end,
        departure,
        min_width: minWidth,
        max_slope: maxSlope,
      }),
    });
    const result = await resp.json();
    if (!resp.ok) {
      setMsg("query-msg", "查询失败：" + (result.detail || "未知错误"), "error");
      return;
    }
    state.routeResult = result;
    renderResult();
    renderMap();
    setMsg("query-msg", result.reachable ? "查询成功" : "不可达", result.reachable ? "success" : "error");
    await loadRecords();
  } catch (e) {
    setMsg("query-msg", "查询失败：" + e.message, "error");
  }
});

function renderResult() {
  const panel = $("result-panel");
  const r = state.routeResult;
  if (!r) {
    panel.innerHTML = '<p class="hint">请先查询路线。</p>';
    return;
  }
  if (!r.reachable) {
    let html = `<div class="result-summary unreachable">
      <div class="big">不可达</div>
    </div>`;
    html += '<ul class="reason-list">';
    r.reasons.forEach((reason) => {
      html += `<li>${escapeHtml(reason)}</li>`;
    });
    html += "</ul>";
    if (r.blocking_closures && r.blocking_closures.length > 0) {
      html += '<p class="hint" style="margin-top:10px">阻断封闭时段：</p>';
      r.blocking_closures.forEach((c) => {
        html += `<div class="blocking-closure">通道 ${escapeHtml(c.edge_id)} (${escapeHtml(c.from_node)} → ${escapeHtml(c.to_node)}): ${fmtTime(c.start)} ~ ${fmtTime(c.end)}</div>`;
      });
    }
    panel.innerHTML = html;
    return;
  }

  // 可达：逐层列出路线
  let html = `<div class="result-summary">
    <div class="big">到达时刻 ${fmtTime(r.arrival)}</div>
    <div class="result-meta">总行程 ${fmtDuration(r.total_seconds)} · 总等待 ${fmtDuration(r.wait_seconds)} · 总路程 ${r.total_length.toFixed(1)} m</div>
    <div class="result-meta">核验记录 #${r.record_id} · 路网 v${r.network_version} · 封闭版本 v${r.closure_version}</div>
  </div>`;

  // 按楼层分组步骤
  const stepsByFloor = {};
  r.steps.forEach((s) => {
    if (!stepsByFloor[s.floor_id]) stepsByFloor[s.floor_id] = [];
    stepsByFloor[s.floor_id].push(s);
  });

  const floorOrder = state.network.data.floors.map((f) => f.id);
  html += '<ul class="step-list">';
  floorOrder.forEach((fid) => {
    const steps = stepsByFloor[fid];
    if (!steps) return;
    const floor = state.network.data.floors.find((f) => f.id === fid);
    html += `<li class="step-item"><span class="step-floor">${escapeHtml(floor.name)}</span></li>`;
    steps.forEach((s) => {
      const typeLabel = edgeTypeLabel(s.type);
      const waitBadge = s.wait_seconds > 0 ? `<span class="wait-badge">等待 ${fmtDuration(s.wait_seconds)}</span>` : "";
      const crossBadge = s.cross_floor ? '<span class="wait-badge" style="background:#ede9fe;color:#5b21b6">跨层</span>' : "";
      html += `<li class="step-item">
        <div><span class="step-edge-type" style="background:${edgeColor(s.type)}22;color:${edgeColor(s.type)}">${typeLabel}</span> <strong>${escapeHtml(s.edge_id)}</strong>: ${escapeHtml(s.from_node)} → ${escapeHtml(s.to_node)}${waitBadge}${crossBadge}</div>
        <div class="step-detail">${s.length}m · 通行 ${fmtDuration(s.seconds)} · 净宽 ${s.width}m · 坡度 ${s.slope}° · 到达 ${fmtTime(s.arrival)}</div>
      </li>`;
    });
  });
  html += "</ul>";
  panel.innerHTML = html;
}

function edgeTypeLabel(type) {
  switch (type) {
    case "elevator":
      return "电梯";
    case "ramp":
      return "坡道";
    case "stairs":
      return "楼梯";
    default:
      return "通道";
  }
}

// ---------------------------------------------------------------------------
// 历史记录
// ---------------------------------------------------------------------------

async function loadRecords() {
  const resp = await fetch("/api/records");
  const result = await resp.json();
  renderRecords(result.records || []);
}

function renderRecords(records) {
  const container = $("records-list");
  if (records.length === 0) {
    container.innerHTML = '<p class="hint">暂无记录。</p>';
    return;
  }
  container.innerHTML = "";
  records.forEach((r) => {
    const item = document.createElement("div");
    item.className = "record-item";
    const badge = r.reachable
      ? '<span class="badge ok">可达</span>'
      : '<span class="badge bad">不可达</span>';
    item.innerHTML = `${badge} #${r.id} · 路网 v${r.network_version} · 封闭 v${r.closure_version} · ${new Date(r.created_at).toLocaleString("zh-CN")}`;
    item.addEventListener("click", () => viewRecord(r.id));
    container.appendChild(item);
  });
}

async function viewRecord(id) {
  const resp = await fetch(`/api/records/${id}`);
  const rec = await resp.json();
  state.routeResult = rec.result;
  // 切换到记录对应的路网版本
  if (state.network && rec.network_version !== state.network.version) {
    const netResp = await fetch(`/api/network/${rec.network_version}`);
    const netResult = await netResp.json();
    state.network = { version: rec.network_version, data: netResult.data };
    state.currentFloorId = netResult.data.floors[0]?.id || null;
    renderFloorTabs();
  }
  renderResult();
  renderMap();
  setMsg("query-msg", `已加载核验记录 #${id}`, "success");
}

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------

loadNetwork();
loadRecords();
