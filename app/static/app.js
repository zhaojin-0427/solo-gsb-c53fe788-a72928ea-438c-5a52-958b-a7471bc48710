/* 建筑无障碍通路核验台 —— 原生 JS 前端 */
"use strict";

const state = {
  network: null,        // 实时路网 {id,name,floors,nodes,edges}
  closures: [],
  result: null,         // 最近一次查询结果（或被查看记录的结果）
  recordView: null,     // 正在查看的固化记录上下文
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ----------------------------------------------------------- 通用

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try { body = await resp.json(); } catch (_) { /* ignore */ }
  if (!resp.ok || (body && body.ok === false)) {
    const err = new Error((body && body.detail) || `HTTP ${resp.status}`);
    err.errors = body && body.errors;
    throw err;
  }
  return body ? body.data : null;
}

function showMsg(el, text, kind = "ok", errors = null) {
  el.innerHTML = "";
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  div.textContent = text;
  if (errors && errors.length) {
    const ul = document.createElement("ul");
    errors.slice(0, 50).forEach((e) => {
      const li = document.createElement("li");
      li.textContent = e.message || JSON.stringify(e);
      ul.appendChild(li);
    });
    div.appendChild(ul);
    if (errors.length > 50) {
      const more = document.createElement("div");
      more.textContent = `……另有 ${errors.length - 50} 处问题未列出`;
      ul.appendChild(more);
    }
  }
  el.appendChild(div);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ----------------------------------------------------------- 时区选项

const TZ_OFFSETS = (() => {
  const list = [];
  for (let m = -12 * 60; m <= 14 * 60; m += 30) {
    const sign = m >= 0 ? "+" : "-";
    const hh = String(Math.floor(Math.abs(m) / 60)).padStart(2, "0");
    const mm = String(Math.abs(m) % 60).padStart(2, "0");
    list.push({ value: `${sign}${hh}:${mm}`, minutes: m });
  }
  return list;
})();

function fillTzSelects() {
  for (const id of ["cl-tz", "rt-tz"]) {
    const sel = $(`#${id}`);
    sel.innerHTML = "";
    TZ_OFFSETS.forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t.value;
      opt.textContent = `UTC${t.value}`;
      sel.appendChild(opt);
    });
    sel.value = "+08:00";
  }
}

function localToIso(localValue, offset) {
  // localValue: "YYYY-MM-DDTHH:mm:ss"
  if (!localValue) return "";
  return localValue.length === 16 ? `${localValue}:00${offset}`
                                  : `${localValue}${offset}`;
}

function fmtUtc(isoStr) { return isoStr || ""; }

function fmtDuration(sec) {
  sec = Math.round(sec || 0);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h) return `${h}小时${m}分${s ? s + "秒" : ""}`;
  if (m) return `${m}分${s}秒`;
  return `${s}秒`;
}

// ----------------------------------------------------------- Tab 切换

$$(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.remove("active"));
    $$(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "records") loadRecords();
    if (btn.dataset.tab === "closures") refreshClosureTab();
  });
});

// ----------------------------------------------------------- 路网导入

$("#btn-load-sample").addEventListener("click", async () => {
  const resp = await fetch("/api/sample");
  const data = await resp.json();
  $("#network-json").value = JSON.stringify(data, null, 2);
});

$("#file-import").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => { $("#network-json").value = reader.result; };
  reader.readAsText(file);
});

$("#btn-import").addEventListener("click", async () => {
  const msg = $("#network-msg");
  let payload;
  try {
    payload = JSON.parse($("#network-json").value);
  } catch (err) {
    showMsg(msg, "JSON 解析失败：" + err.message, "err");
    return;
  }
  try {
    const data = await api("/api/networks/import", {
      method: "POST", body: JSON.stringify(payload),
    });
    await loadActive();
    showMsg(msg,
      `导入成功：版本 #${data.id}「${data.name}」，`
      + `${data.floors.length} 个楼层、${data.nodes.length} 个节点、${data.edges.length} 条有向边。`,
      "ok");
  } catch (err) {
    showMsg(msg, "导入被拒绝：" + err.message, "err", err.errors);
  }
});

async function loadActive() {
  state.network = await api("/api/networks/active");
  state.closures = await api("/api/closures").catch(() => []);
  state.result = null;
  renderHeader();
  renderNetworkTab();
  refreshClosureTab();
  renderRouteSelectors();
  renderFloors();
  renderRouteResult(null);
}

function renderHeader() {
  const el = $("#active-info");
  if (!state.network) {
    el.textContent = "尚未导入路网";
    return;
  }
  const n = state.network;
  el.textContent = `当前版本 #${n.id}「${n.name}」 · ${n.floors.join(" / ")}`
    + ` · ${n.nodes.length} 节点 / ${n.edges.length} 有向边`;
}

function renderNetworkTab() {
  const ov = $("#network-overview");
  ov.innerHTML = "";
  loadVersions();
  if (!state.network) return;
  const n = state.network;
  const byFloor = {};
  n.nodes.forEach((x) => { (byFloor[x.floor] ||= []).push(x); });
  ov.innerHTML = `<div class="summary">
      <div class="stat"><b>${n.floors.length}</b><span>楼层</span></div>
      <div class="stat"><b>${n.nodes.length}</b><span>节点</span></div>
      <div class="stat"><b>${n.edges.length}</b><span>有向边</span></div>
      <div class="stat"><b>${n.floors.map((f) => `${f}:${byFloor[f].length}`).join("　")}</b><span>各层节点</span></div>
    </div>`;
}

async function loadVersions() {
  const versions = await api("/api/networks");
  const tbody = $("#versions-table tbody");
  tbody.innerHTML = versions.map((v) => `
    <tr>
      <td>#${v.id}</td>
      <td>${escapeHtml(v.name)}</td>
      <td>${v.imported_at}</td>
      <td>${v.floors.join("/")} · ${v.node_count}节点 · ${v.edge_count}边</td>
      <td>${v.is_active ? '<span class="badge yes">当前</span>' : ""}</td>
      <td>${v.is_active ? "" : `<button class="btn small" data-activate="${v.id}">切换为当前</button>`}</td>
    </tr>`).join("");
  tbody.querySelectorAll("[data-activate]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/networks/${btn.dataset.activate}/activate`, { method: "POST" });
      await loadActive();
    });
  });
}

// ----------------------------------------------------------- 封闭时段

function refreshClosureTab() {
  if (!state.network) {
    $("#cl-edge").innerHTML = '<option value="">（请先导入路网）</option>';
    renderClosuresTable();
    return;
  }
  const sel = $("#cl-edge");
  sel.innerHTML = state.network.edges
    .filter((e) => !e.id.endsWith("__rev"))
    .map((e) => `<option value="${e.id}">${e.id}（${edgeText(e)}）</option>`)
    .join("");
  renderClosuresTable();
}

function edgeText(e) {
  const nodes = state.network.nodes;
  const nm = Object.fromEntries(nodes.map((n) => [n.id, n.name]));
  return `${nm[e.source] || e.source} → ${nm[e.target] || e.target}，${typeLabel(e.type)}`;
}

function typeLabel(t) {
  return { corridor: "通道", elevator: "电梯", ramp: "坡道",
           stairs: "楼梯", door: "门", other: "其他" }[t] || t;
}

function renderClosuresTable() {
  const tbody = $("#closures-table tbody");
  if (!state.closures.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="hint">暂无封闭时段</td></tr>';
    return;
  }
  tbody.innerHTML = state.closures.map((c) => `
    <tr>
      <td>#${c.id}</td>
      <td>${escapeHtml(c.edge_id)}</td>
      <td>${escapeHtml(c.raw_start)}</td>
      <td>${escapeHtml(c.raw_end)}</td>
      <td>${c.start_utc}<br>～ ${c.end_utc}</td>
      <td><button class="btn small danger" data-del="${c.id}">删除</button></td>
    </tr>`).join("");
  tbody.querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/closures/${btn.dataset.del}`, { method: "DELETE" });
      state.closures = await api("/api/closures");
      renderClosuresTable();
    });
  });
}

$("#btn-add-closure").addEventListener("click", async () => {
  const msg = $("#closure-msg");
  const body = {
    edge_id: $("#cl-edge").value,
    start: localToIso($("#cl-start").value, $("#cl-tz").value),
    end: localToIso($("#cl-end").value, $("#cl-tz").value),
  };
  if (!body.edge_id || !body.start || !body.end) {
    showMsg(msg, "请完整选择边、开始与结束时刻", "err");
    return;
  }
  try {
    await api("/api/closures", { method: "POST", body: JSON.stringify(body) });
    state.closures = await api("/api/closures");
    renderClosuresTable();
    showMsg(msg, `已登记：${body.edge_id} 封闭 ${body.start} ～ ${body.end}`, "ok");
  } catch (err) {
    showMsg(msg, "登记失败：" + err.message, "err", err.errors);
  }
});

// ----------------------------------------------------------- 路线查询

function renderRouteSelectors() {
  if (!state.network) {
    $("#rt-start").innerHTML = "";
    $("#rt-goal").innerHTML = "";
    return;
  }
  const opts = state.network.nodes
    .map((n) => `<option value="${n.id}">${n.name}（${n.floor}）</option>`)
    .join("");
  $("#rt-start").innerHTML = opts;
  $("#rt-goal").innerHTML = opts;
  const rooms = state.network.nodes.filter((n) => n.type === "room");
  if (state.network.nodes[0]) $("#rt-start").value = state.network.nodes[0].id;
  if (rooms.length) $("#rt-goal").value = rooms[rooms.length - 1].id;
}

$("#btn-query").addEventListener("click", async () => {
  const msg = $("#route-msg");
  const body = {
    start: $("#rt-start").value,
    goal: $("#rt-goal").value,
    departure: localToIso($("#rt-departure").value, $("#rt-tz").value),
    min_clear_width: parseFloat($("#rt-width").value || "0"),
    max_slope: parseFloat($("#rt-slope").value || "0"),
  };
  if (!body.departure) {
    showMsg(msg, "请选择出发时刻", "err");
    return;
  }
  try {
    const data = await api("/api/route/query", {
      method: "POST", body: JSON.stringify(body),
    });
    state.result = data.result;
    state.recordView = { record_id: data.record_id, frozen: false };
    showMsg(msg, `核验完成，记录已固化为 #${data.record_id}`, "ok");
    renderRouteResult(data.result);
    renderFloors();
  } catch (err) {
    showMsg(msg, "查询失败：" + err.message, "err", err.errors);
  }
});

// ----------------------------------------------------------- 结果渲染

function activeRouteEdgeSet(result) {
  return new Set((result && result.edge_ids) || []);
}
function activeRouteNodeSet(result) {
  const set = new Set();
  (result?.steps || []).forEach((s) => { set.add(s.source); set.add(s.target); });
  return set;
}

function renderRouteResult(result) {
  const el = $("#route-result");
  if (!result) { el.innerHTML = ""; return; }
  if (!result.reachable) {
    el.innerHTML = `
      <div class="card">
        <h3 style="color:#991b1b">⛔ 24 小时内不可达</h3>
        <div class="msg err"><ul>
          ${result.reasons.map((r) => `<li>${escapeHtml(r.message)}</li>`).join("")}
        </ul></div>
        <p class="hint">核验窗：${result.departure_utc} ～ ${result.deadline_utc || ""}（UTC，半开）</p>
      </div>`;
    return;
  }
  const netForGroup = state.recordView?.frozen
    ? state.recordView.network : state.network;
  const grouped = groupStepsByFloor(result.steps, netForGroup);
  el.innerHTML = `
    <div class="card">
      <h3>✅ 已找到最早可达路线${state.recordView?.record_id ? `（记录 #${state.recordView.record_id}）` : ""}</h3>
      <div class="summary">
        <div class="stat"><b>${fmtDuration(result.total_seconds)}</b><span>总耗时</span></div>
        <div class="stat"><b>${result.arrival_utc}</b><span>最早到达 (UTC)</span></div>
        <div class="stat"><b>${result.total_length} m</b><span>总路程</span></div>
        <div class="stat"><b>${fmtDuration(result.wait_seconds)}</b><span>途中等待合计</span></div>
        <div class="stat"><b>${result.edge_count}</b><span>经过边数</span></div>
      </div>
      <p class="hint">出发 ${result.departure_utc}；核验截止 ${result.deadline_utc}（UTC，24h，半开区间）</p>
      ${Object.entries(grouped).map(([floor, steps]) => renderFloorSteps(floor, steps)).join("")}
    </div>`;
}

function groupStepsByFloor(steps, network) {
  if (!network) return { "": steps };
  const nodeFloor = Object.fromEntries(network.nodes.map((n) => [n.id, n.floor]));
  const groups = {};
  let curFloor = steps.length ? nodeFloor[steps[0].source] : "";
  steps.forEach((s) => {
    const f = nodeFloor[s.source];
    if (f !== curFloor) curFloor = f;
    (groups[curFloor] ||= []).push(s);
  });
  return groups;
}

function renderFloorSteps(floor, steps) {
  return `<h4 style="margin:12px 0 4px;color:#14375c">楼层 ${escapeHtml(floor)}</h4>
    <div class="step-list">
      ${steps.map((s, i) => {
        const wait = s.wait_before_seconds > 0
          ? `<div class="step wait">⏸ 在节点等待 ${fmtDuration(s.wait_before_seconds)}</div>` : "";
        const cross = s.cross_floor ? " cross" : "";
        return `${wait}
          <div class="step${cross}">
            <b>${i + 1}. ${escapeHtml(s.edge_id)}</b>（${s.type_label}${s.cross_floor ? " · 跨层" : ""}）
            ${escapeHtml(s.source)} → ${escapeHtml(s.target)}
            <div class="meta">
              进入 ${s.enter_utc} → 离开 ${s.leave_utc}（UTC）；
              长 ${s.length}m · ${s.traverse_seconds}秒 · 净宽 ${s.clear_width}m · 坡度 ${s.slope}%
            </div>
          </div>`;
      }).join("")}
    </div>`;
}

// ----------------------------------------------------------- 楼层图 SVG

function renderFloors() {
  const host = $("#floors");
  const network = state.recordView?.frozen ? state.recordView.network : state.network;
  if (!network) { host.innerHTML = ""; return; }
  const result = state.result;
  const routeEdges = activeRouteEdgeSet(result);
  const routeNodes = activeRouteNodeSet(result);
  const waitAt = {}; // node -> 等待秒数（取最近一次）
  (result?.steps || []).forEach((s) => {
    if (s.wait_before_seconds > 0) waitAt[s.source] = s.wait_before_seconds;
  });

  host.innerHTML = `<h3>楼层图${state.recordView?.frozen ? "（固化快照）" : ""}</h3>
    <div class="legend">
      <span><i style="background:#94a3b8"></i>普通通道</span>
      <span><i style="background:#7c3aed"></i>电梯/坡道（跨层）</span>
      <span><i style="background:#b91c1c"></i>楼梯（轮椅禁行）</span>
      <span><i style="background:#eab308;height:7px"></i>选中路线</span>
      <span>🟠 节点等待　🟢 起点　🔴 终点</span>
    </div>
    <div class="floors"></div>`;
  const wrap = host.querySelector(".floors");

  const nodesById = Object.fromEntries(network.nodes.map((n) => [n.id, n]));
  window._allNodes = nodesById;
  network.floors.forEach((floor) => {
    const fNodes = network.nodes.filter((n) => n.floor === floor);
    const fEdges = network.edges.filter((e) => nodesById[e.source]?.floor === floor
                                         && nodesById[e.target]?.floor === floor);
    const crossOut = network.edges.filter((e) => nodesById[e.source]?.floor === floor
                                             && nodesById[e.target]?.floor !== floor);
    wrap.appendChild(renderFloorSvg(floor, fNodes, fEdges, crossOut,
                                    routeEdges, routeNodes, waitAt, result));
  });
}

function renderFloorSvg(floor, nodes, edges, crossOut,
                        routeEdges, routeNodes, waitAt, result) {
  const PAD = 60;
  const xs = nodes.map((n) => n.x), ys = nodes.map((n) => n.y);
  const minX = Math.min(...xs) - PAD, maxX = Math.max(...xs) + PAD;
  const minY = Math.min(...ys) - PAD, maxY = Math.max(...ys) + PAD;
  const W = maxX - minX, H = maxY - minY;

  const card = document.createElement("div");
  card.className = "floor-card";
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `${minX} ${minY} ${W} ${H}`);

  // 箭头标记（普通/路线）
  const defs = document.createElementNS(svgNS, "defs");
  [["arrow", "#94a3b8"], ["arrow-v", "#7c3aed"], ["arrow-s", "#b91c1c"],
   ["arrow-r", "#ca8a04"]].forEach(([id, color]) => {
    const m = document.createElementNS(svgNS, "marker");
    m.setAttribute("id", `${id}-${floor}`);
    m.setAttribute("markerWidth", "8");
    m.setAttribute("markerHeight", "8");
    m.setAttribute("refX", "7");
    m.setAttribute("refY", "3");
    m.setAttribute("orient", "auto");
    m.innerHTML = `<path d="M0,0 L7,3 L0,6 Z" fill="${color}"/>`;
    defs.appendChild(m);
  });
  svg.appendChild(defs);

  const stepOrder = {};
  (result?.edge_ids || []).forEach((id, i) => { stepOrder[id] = i + 1; });

  const drawEdge = (e, isCross) => {
    const a = nodes.find((n) => n.id === e.source);
    const b = isCross ? null : nodes.find((n) => n.id === e.target);
    // 路线存的是实际使用的精确边 id（双向反向边以 __rev 后缀区分）
    const selected = routeEdges.has(e.id);
    let color = "#94a3b8", marker = `arrow-${floor}`;
    if (e.type === "stairs") { color = "#b91c1c"; marker = `arrow-s-${floor}`; }
    if (isCross || e.type === "elevator" || e.type === "ramp") {
      color = "#7c3aed"; marker = `arrow-v-${floor}`;
    }
    if (selected) { color = "#ca8a04"; marker = `arrow-r-${floor}`; }

    const g = document.createElementNS(svgNS, "g");
    const title = document.createElementNS(svgNS, "title");
    title.textContent = `${e.id} ${e.name || ""} [${typeLabel(e.type)}] `
      + `${e.length}m/${e.traverse_seconds}s/净宽${e.clear_width}m/坡${e.slope}%`;
    g.appendChild(title);

    if (isCross) {
      // 跨层边：画带箭头的短斜线桩
      const len = 34, ang = -Math.PI / 4;
      const x2 = a.x + Math.cos(ang) * len, y2 = a.y + Math.sin(ang) * len;
      const line = document.createElementNS(svgNS, "line");
      line.setAttribute("x1", a.x); line.setAttribute("y1", a.y);
      line.setAttribute("x2", x2); line.setAttribute("y2", y2);
      line.setAttribute("stroke", selected ? "#ca8a04" : color);
      line.setAttribute("stroke-width", selected ? 5 : 2.5);
      line.setAttribute("marker-end", `url(#${selected ? `arrow-r-${floor}` : marker})`);
      g.appendChild(line);
      const label = document.createElementNS(svgNS, "text");
      label.setAttribute("x", x2 + 2); label.setAttribute("y", y2);
      label.setAttribute("font-size", "11");
      label.setAttribute("fill", selected ? "#a16207" : color);
      const targetFloor = (window._allNodes?.[e.target] || {}).floor || "?";
      label.textContent = `${typeLabel(e.type)}→${targetFloor}${selected ? " ★" : ""}`;
      g.appendChild(label);
    } else {
      const curved = e.id.endsWith("__rev");
      const line = document.createElementNS(svgNS, "path");
      let d;
      if (curved) {
        const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        const dx = b.x - a.x, dy = b.y - a.y;
        const k = 14 / Math.hypot(dx, dy) || 0;
        const cx = mx - dy * k, cy = my + dx * k;
        d = `M${a.x},${a.y} Q${cx},${cy} ${b.x},${b.y}`;
      } else {
        d = `M${a.x},${a.y} L${b.x},${b.y}`;
      }
      line.setAttribute("d", d);
      line.setAttribute("fill", "none");
      line.setAttribute("stroke", color);
      line.setAttribute("stroke-width", selected ? 5 : 2.2);
      line.setAttribute("marker-end", `url(#${marker})`);
      g.appendChild(line);

      if (selected && stepOrder[e.id]) {
        const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        const c = document.createElementNS(svgNS, "circle");
        c.setAttribute("cx", mx); c.setAttribute("cy", my); c.setAttribute("r", "9");
        c.setAttribute("fill", "#fde047"); c.setAttribute("stroke", "#a16207");
        g.appendChild(c);
        const t = document.createElementNS(svgNS, "text");
        t.setAttribute("x", mx); t.setAttribute("y", my + 3.5);
        t.setAttribute("text-anchor", "middle");
        t.setAttribute("font-size", "11");
        t.setAttribute("font-weight", "700");
        t.setAttribute("fill", "#713f12");
        t.textContent = stepOrder[e.id];
        g.appendChild(t);
      }
    }
    svg.appendChild(g);
  };

  edges.forEach((e) => drawEdge(e, false));
  crossOut.forEach((e) => drawEdge(e, true));

  // 节点
  nodes.forEach((n) => {
    const isStart = result && (n.id === result.start);
    const isGoal = result && (n.id === result.goal);
    const onRoute = routeNodes.has(n.id);
    const g = document.createElementNS(svgNS, "g");
    const c = document.createElementNS(svgNS, "circle");
    c.setAttribute("cx", n.x); c.setAttribute("cy", n.y);
    c.setAttribute("r", isStart || isGoal ? 10 : 7);
    let fill = "#cbd5e1";
    if (n.type === "entrance" || n.type === "exit") fill = "#38bdf8";
    if (n.type === "room") fill = "#a7f3d0";
    if (onRoute) fill = "#fef08a";
    if (isStart) fill = "#22c55e";
    if (isGoal) fill = "#ef4444";
    c.setAttribute("fill", fill);
    c.setAttribute("stroke", "#334155");
    c.setAttribute("stroke-width", "1.2");
    g.appendChild(c);

    const title = document.createElementNS(svgNS, "title");
    title.textContent = `${n.id} ${n.name}（${n.type}）`;
    g.appendChild(title);

    const t = document.createElementNS(svgNS, "text");
    t.setAttribute("x", n.x + 9); t.setAttribute("y", n.y + 4);
    t.setAttribute("font-size", "12");
    t.textContent = n.name;
    g.appendChild(t);

    if (waitAt[n.id]) {
      const w = document.createElementNS(svgNS, "text");
      w.setAttribute("x", n.x); w.setAttribute("y", n.y - 13);
      w.setAttribute("text-anchor", "middle");
      w.setAttribute("class", "wait-badge");
      w.textContent = `等${fmtDuration(waitAt[n.id])}`;
      g.appendChild(w);
    }
    svg.appendChild(g);
  });

  const h = document.createElement("h4");
  h.textContent = `${floor} 层（${nodes.length} 节点）`;
  card.appendChild(h);
  card.appendChild(svg);
  return card;
}

// ----------------------------------------------------------- 核验记录

async function loadRecords() {
  const records = await api("/api/records");
  const tbody = $("#records-table tbody");
  tbody.innerHTML = records.length ? records.map((r) => `
    <tr>
      <td>#${r.id}</td>
      <td>#${r.network_id} ${escapeHtml(r.network_name)}</td>
      <td>${r.created_at}</td>
      <td>${escapeHtml(r.start)} → ${escapeHtml(r.goal)}</td>
      <td>${escapeHtml(r.departure)}</td>
      <td>≥${r.min_clear_width}m / ≤${r.max_slope}%</td>
      <td>${r.reachable ? '<span class="badge yes">可达</span>'
                        : '<span class="badge no">不可达</span>'}</td>
      <td><button class="btn small" data-rec="${r.id}">查看固化结果</button></td>
    </tr>`).join("")
    : '<tr><td colspan="8" class="hint">暂无核验记录</td></tr>';
  tbody.querySelectorAll("[data-rec]").forEach((btn) => {
    btn.addEventListener("click", () => viewRecord(btn.dataset.rec));
  });
}

async function viewRecord(id) {
  const rec = await api(`/api/records/${id}`);
  state.recordView = { record_id: Number(id), frozen: true,
                       network: rec.network_snapshot };
  state.result = rec.result;
  // 用快照渲染结果与楼层图（不改变实时路网 state.networkLive）
  renderRecordDetail(rec);
  renderRouteResult(rec.result);
  renderFloors();
  $(".tab[data-tab='route']").click();
}

function renderRecordDetail(rec) {
  const el = $("#record-detail");
  const p = rec.params;
  const closed = rec.closures_snapshot;
  el.innerHTML = `
    <div class="frozen-banner">
      🔒 您正在查看固化记录 <b>#${rec.id}</b>（${rec.created_at} UTC）：
      路网版本 #${rec.network_id}「${escapeHtml(rec.network_name)}」与
      ${closed.length} 条封闭时段已随记录冻结；后续修改不会改变本结果。
      参数：${escapeHtml(p.start)} → ${escapeHtml(p.goal)}，出发 ${escapeHtml(p.departure)}
      （UTC ${p.departure_utc || ""}），净宽≥${p.min_clear_width}m，坡度≤${p.max_slope}%。
      <button id="btn-exit-frozen" class="btn small" style="margin-left:12px">返回实时视图</button>
    </div>`;
  $("#btn-exit-frozen").addEventListener("click", () => {
    state.recordView = null;
    state.result = null;
    $("#record-detail").innerHTML = "";
    renderRouteResult(null);
    renderFloors();
  });
}

// ----------------------------------------------------------- 启动

fillTzSelects();
// 默认出发时刻：当前整点
(function setDefaultDeparture() {
  const d = new Date(Date.now() + 60 * 1000);
  const pad = (x) => String(x).padStart(2, "0");
  const v = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T`
          + `${pad(d.getHours())}:${pad(d.getMinutes())}:00`;
  $("#rt-departure").value = v;
  $("#cl-start").value = v;
  d.setHours(d.getHours() + 1);
  $("#cl-end").value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T`
                     + `${pad(d.getHours())}:${pad(d.getMinutes())}:00`;
})();

// renderFloorSvg 需要跨层目标楼层：索引在 renderFloors 中挂到 window._allNodes
loadActive();
