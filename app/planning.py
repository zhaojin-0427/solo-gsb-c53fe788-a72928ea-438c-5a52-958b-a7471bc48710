"""路网导入校验与时间依赖轮椅路线规划。

核心规则：
- 有向路网；楼梯楼梯（stairs）轮椅不可走；跨层仅 elevator / ramp 合法且可走。
- 每条边登记若干 [start, end) 半开封闭时段；行人可在节点处任意等候。
- 求 departure 起 24 小时（86400 秒）内最早到达；平局按总路程、再按边 ID 序列字典序。

时间依赖 Dijkstra（FIFO：等候只会让到达更晚），每个节点保留关于
(到达时刻, 路程) 的非支配标签；同一 (时刻, 路程) 保留多个不同边序列以便
到达终点时按完整边 ID 序列字典序择优。
"""
from __future__ import annotations

import heapq
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import networkx as nx

# ---------------------------------------------------------------- 常量

VALID_NODE_TYPES = {"entrance", "exit", "room", "junction"}
VALID_EDGE_TYPES = {"corridor", "elevator", "ramp", "stairs", "door", "other"}
# 允许轮椅跨层的边类型
VERTICAL_WHEELCHAIR = {"elevator", "ramp"}
# 物理上允许跨层的边类型（楼梯合法存在，但轮椅核验时不可走）
VERTICAL_PHYSICAL = {"elevator", "ramp", "stairs"}

TYPE_LABELS_CN = {
    "corridor": "通道",
    "elevator": "电梯",
    "ramp": "坡道",
    "stairs": "楼梯",
    "door": "门",
    "other": "其他",
}
NODE_TYPE_LABELS_CN = {
    "entrance": "出入口",
    "exit": "出入口",
    "room": "房间",
    "junction": "节点",
}

WINDOW_SECONDS = 24 * 3600
# 每个节点保留的非支配标签上限（建筑路网足够；超限按
# (到达时刻, 路程, 边 ID 序列) 保留最优前缀，与最终平局规则一致）
LABEL_CAP = 256

ISO_FMT_OUT = "%Y-%m-%dT%H:%M:%S%z"  # 输出用，带 +00:00 偏移


class PlanningError(ValueError):
    """输入数据或参数不合法。"""


# ---------------------------------------------------------------- 时间工具

def parse_aware(value: str, field: str = "时间") -> datetime:
    """解析必须带时区的 ISO 8601 时刻。"""
    if not isinstance(value, str):
        raise PlanningError(f"{field}必须是 ISO 8601 字符串")
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanningError(f"{field}不是合法的 ISO 8601 时刻：{value}") from exc
    if dt.tzinfo is None:
        raise PlanningError(f"{field}必须携带时区（offset，例如 +08:00 或 Z）：{value}")
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    """aware datetime -> ISO 8601（UTC，带偏移）。"""
    return dt.astimezone(timezone.utc).strftime(ISO_FMT_OUT)


def merge_intervals(
    intervals: Iterable[Tuple[datetime, datetime]],
) -> List[Tuple[datetime, datetime]]:
    """合并重叠/相接的半开 [start, end) 区间，返回按起点排序的列表。"""
    ordered = sorted(intervals, key=lambda x: x[0])
    merged: List[Tuple[datetime, datetime]] = []
    for start, end in ordered:
        if start >= end:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            prev_s, prev_e = merged[-1]
            merged[-1] = (prev_s, max(prev_e, end))
    return merged


def earliest_entry(
    ready: datetime,
    finish_by: datetime,
    traverse_seconds: float,
    blocks: List[Tuple[datetime, datetime]],
) -> Optional[Tuple[datetime, datetime]]:
    """求最早的进入时刻 entry >= ready，使遍历区间 [entry, entry+traverse)
    不与任何封闭区间相交，且 entry+traverse <= finish_by。

    blocks 为已合并、按起点升序的半开区间。等候策略：被封闭区间挡住时，
    跳到该封闭区间的结束时刻再试。
    """
    entry = ready
    duration = timedelta(seconds=traverse_seconds)
    while entry + duration <= finish_by:
        hit = None
        for b_start, b_end in blocks:
            # 相交判定（半开）：entry < b_end and entry+duration > b_start
            if entry < b_end and entry + duration > b_start:
                hit = (b_start, b_end)
                break
            if b_start >= entry + duration:
                break
        if hit is None:
            return entry, entry + duration
        entry = hit[1]  # 封闭结束后放行
    return None


# ---------------------------------------------------------------- 导入校验

def _num(value: Any, field: str, errors: List[Dict[str, str]], eid: str,
         minimum: float = 0.0, allow_int: bool = True) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append({"code": "edge_field_type", "edge_id": eid,
                       "message": f"边 {eid} 的 {field} 必须是非负数字"})
        return None
    if value < minimum:
        errors.append({"code": "edge_field_range", "edge_id": eid,
                       "message": f"边 {eid} 的 {field} 不能小于 {minimum}"})
        return None
    return float(value)


def validate_network(payload: Any) -> Dict[str, Any]:
    """校验并规范化用户导入的路网 JSON。

    返回 {name, nodes:[...], edges:[...], floors:[...]}；
    不合法时抛出 PlanningError，errors 属性为结构化错误清单。
    """
    errors: List[Dict[str, Any]] = []
    if not isinstance(payload, dict):
        raise PlanningError("导入内容必须是 JSON 对象")

    name = payload.get("name", "未命名建筑")
    if not isinstance(name, str):
        errors.append({"code": "bad_name", "message": "name 必须是字符串"})
        name = str(name)

    raw_nodes = payload.get("nodes")
    raw_edges = payload.get("edges")
    if not isinstance(raw_nodes, list):
        raise PlanningError("nodes 必须是数组")
    if not isinstance(raw_edges, list):
        raise PlanningError("edges 必须是数组")

    # ---- 节点
    nodes: List[Dict[str, Any]] = []
    node_ids: set = set()
    node_floor: Dict[str, Any] = {}
    for i, n in enumerate(raw_nodes):
        if not isinstance(n, dict):
            errors.append({"code": "node_not_object", "index": i,
                           "message": f"第 {i} 个节点不是对象"})
            continue
        nid = n.get("id")
        if not isinstance(nid, str) or not nid:
            errors.append({"code": "node_bad_id", "index": i,
                           "message": f"第 {i} 个节点缺少字符串 id"})
            continue
        if nid in node_ids:
            errors.append({"code": "node_duplicate_id", "node_id": nid,
                           "message": f"节点 id 重复：{nid}"})
            continue
        floor = n.get("floor")
        if not isinstance(floor, str) or not floor:
            errors.append({"code": "node_bad_floor", "node_id": nid,
                           "message": f"节点 {nid} 缺少楼层 floor（字符串）"})
            continue
        x = n.get("x")
        y = n.get("y")
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            errors.append({"code": "node_bad_xy", "node_id": nid,
                           "message": f"节点 {nid} 的坐标 x 必须是数字"})
            continue
        if isinstance(y, bool) or not isinstance(y, (int, float)):
            errors.append({"code": "node_bad_xy", "node_id": nid,
                           "message": f"节点 {nid} 的坐标 y 必须是数字"})
            continue
        ntype = n.get("type", "junction")
        if ntype not in VALID_NODE_TYPES:
            errors.append({"code": "node_bad_type", "node_id": nid,
                           "message": f"节点 {nid} 的类型 {ntype!r} 非法，"
                                      f"允许：{sorted(VALID_NODE_TYPES)}"})
            continue
        label = n.get("name", nid)
        if not isinstance(label, str):
            label = str(label)
        node_ids.add(nid)
        node_floor[nid] = floor
        nodes.append({"id": nid, "floor": floor, "x": float(x), "y": float(y),
                      "type": ntype, "name": label})

    node_by_id = {n["id"]: n for n in nodes}

    # ---- 边
    edges: List[Dict[str, Any]] = []
    edge_ids: set = set()
    incident: Dict[str, int] = {}
    for i, e in enumerate(raw_edges):
        if not isinstance(e, dict):
            errors.append({"code": "edge_not_object", "index": i,
                           "message": f"第 {i} 条边不是对象"})
            continue
        eid = e.get("id")
        if not isinstance(eid, str) or not eid:
            errors.append({"code": "edge_bad_id", "index": i,
                           "message": f"第 {i} 条边缺少字符串 id"})
            continue
        if eid in edge_ids:
            errors.append({"code": "edge_duplicate_id", "edge_id": eid,
                           "message": f"边 id 重复：{eid}"})
            continue
        src, dst = e.get("source"), e.get("target")
        if not isinstance(src, str) or not isinstance(dst, str):
            errors.append({"code": "edge_bad_endpoints", "edge_id": eid,
                           "message": f"边 {eid} 需要字符串 source / target"})
            continue
        if src not in node_ids:
            errors.append({"code": "dangling_node", "edge_id": eid, "node_id": src,
                           "message": f"边 {eid} 的起点 {src} 不存在（悬空引用）"})
        if dst not in node_ids:
            errors.append({"code": "dangling_node", "edge_id": eid, "node_id": dst,
                           "message": f"边 {eid} 的终点 {dst} 不存在（悬空引用）"})
        etype = e.get("type", "corridor")
        if etype not in VALID_EDGE_TYPES:
            errors.append({"code": "edge_bad_type", "edge_id": eid,
                           "message": f"边 {eid} 的类型 {etype!r} 非法，"
                                      f"允许：{sorted(VALID_EDGE_TYPES)}"})
        length = _num(e.get("length"), "length(长度)", errors, eid)
        seconds = _num(e.get("traverse_seconds", e.get("seconds")),
                       "traverse_seconds(通行秒数)", errors, eid)
        if seconds is not None and seconds == 0:
            errors.append({"code": "edge_bad_seconds", "edge_id": eid,
                           "message": f"边 {eid} 的通行秒数必须大于 0"})
            seconds = None
        width = _num(e.get("clear_width", e.get("width")),
                     "clear_width(净宽)", errors, eid)
        slope = _num(e.get("slope"), "slope(坡度)", errors, eid)
        # 坡度合理性（>=1 视为百分数；统一规范为数值，0~100）
        if slope is not None and slope > 100:
            errors.append({"code": "edge_bad_slope", "edge_id": eid,
                           "message": f"边 {eid} 的坡度 {slope} 超过 100(%)"})
            slope = None
        ok_endpoints = src in node_ids and dst in node_ids
        # 跨层合法性（导入时硬性拒绝非法跨层连接：
        # 物理跨层只允许电梯/坡道/楼梯；轮椅路线中再排除楼梯）
        if ok_endpoints and etype in VALID_EDGE_TYPES:
            if (node_floor[src] != node_floor[dst]
                    and etype not in VERTICAL_PHYSICAL):
                errors.append({
                    "code": "illegal_cross_floor", "edge_id": eid,
                    "message": f"边 {eid} 为{TYPE_LABELS_CN.get(etype, etype)}，"
                               f"却从 {node_floor[src]} 层跨到 {node_floor[dst]} 层；"
                               f"跨层通道只允许电梯(elevator)、坡道(ramp)或楼梯(stairs)"})
        if (not ok_endpoints or etype not in VALID_EDGE_TYPES
                or None in (length, seconds, width, slope)):
            continue
        edge_ids.add(eid)
        incident[src] = incident.get(src, 0) + 1
        incident[dst] = incident.get(dst, 0) + 1
        label = e.get("name", eid)
        if not isinstance(label, str):
            label = str(label)
        edges.append({
            "id": eid, "source": src, "target": dst, "type": etype,
            "length": length, "traverse_seconds": seconds,
            "clear_width": width, "slope": slope, "name": label,
            "one_way": bool(e.get("one_way", True)),
        })

    # 无向边补反向（one_way=false 时自动生成反向边，ID 加后缀）
    extra: List[Dict[str, Any]] = []
    for e in edges:
        if not e["one_way"]:
            rev = dict(e)
            rev["id"] = f"{e['id']}__rev"
            rev["source"], rev["target"] = e["target"], e["source"]
            rev["one_way"] = True
            extra.append(rev)
    edges.extend(extra)

    # 悬空节点：没有任何边关联
    for nid in sorted(node_ids):
        if incident.get(nid, 0) == 0:
            errors.append({"code": "isolated_node", "node_id": nid,
                           "message": f"节点 {nid} 没有任何通道连接（悬空节点）"})

    if errors:
        err = PlanningError(f"路网校验失败，共 {len(errors)} 处问题")
        err.errors = errors  # type: ignore[attr-defined]
        raise err

    floors = sorted({n["floor"] for n in nodes}, key=_floor_sort_key)
    return {"name": name, "nodes": nodes, "edges": edges, "floors": floors}


def _floor_sort_key(floor: str) -> Tuple[int, Any]:
    """楼层排序：B2 < B1 < F1 < F2 ...；无法识别的排到后面。"""
    import re
    m = re.match(r"^\s*([A-Za-z]*)\s*(-?\d+)\s*$", str(floor))
    if not m:
        return (2, str(floor))
    prefix, num = m.group(1).upper(), int(m.group(2))
    if prefix in ("B", "BM", "BF"):
        return (0, -num)  # 地下：B2(-2) < B1(-1) < F1
    if prefix in ("", "F", "L"):
        return (1, num)
    if prefix == "M":
        return (1, num + 0.5)  # 夹层排在同号层之后
    return (2, str(floor))


def build_graph(net: Dict[str, Any]) -> nx.MultiDiGraph:
    """由规范化路网构造 NetworkX 多重有向图（边键即 edge id）。"""
    g = nx.MultiDiGraph()
    for n in net["nodes"]:
        g.add_node(n["id"], **n)
    for e in net["edges"]:
        g.add_edge(e["source"], e["target"], key=e["id"], **e)
    return g


# ---------------------------------------------------------------- 规划

def plan_route(
    g: nx.MultiDiGraph,
    node_by_id: Dict[str, Dict[str, Any]],
    edge_by_id: Dict[str, Dict[str, Any]],
    start: str,
    goal: str,
    departure: datetime,
    min_width: float,
    max_slope: float,
    closures_by_edge: Dict[str, List[Tuple[datetime, datetime]]],
) -> Dict[str, Any]:
    """时间依赖多标签 Dijkstra。

    返回结构：
      reachable=True : start/goal/route(steps)/total_length/total_seconds/
                       departure_utc/arrival_utc/wait_seconds/edge_count
      reachable=False: reasons[...](阻断原因), examined_until_utc
    """
    deadline = departure + timedelta(seconds=WINDOW_SECONDS)

    # 筛选可走边（静态条件：非楼梯、净宽、坡度）
    blocked_static: Dict[str, str] = {}
    adj: Dict[str, List[str]] = {n: [] for n in g.nodes}
    for eid, e in edge_by_id.items():
        u, v = e["source"], e["target"]
        if e["type"] == "stairs":
            blocked_static[eid] = "stairs"
        elif e["clear_width"] < min_width:
            blocked_static[eid] = "narrow"
        elif e["slope"] > max_slope:
            blocked_static[eid] = "steep"
        else:
            adj[u].append(eid)

    # ---- 端点检查
    if start not in node_by_id:
        return _unreachable([{
            "code": "unknown_node", "node_id": start,
            "message": f"起点 {start} 不在当前路网中"}], departure)
    if goal not in node_by_id:
        return _unreachable([{
            "code": "unknown_node", "node_id": goal,
            "message": f"终点 {goal} 不在当前路网中"}], departure)
    if start == goal:
        return {
            "reachable": True, "start": start, "goal": goal,
            "departure_utc": iso(departure), "arrival_utc": iso(departure),
            "deadline_utc": iso(deadline),
            "total_length": 0.0, "total_seconds": 0.0,
            "wait_seconds": 0.0, "edge_count": 0, "edge_ids": [],
            "steps": [],
        }

    # ---- 静态连通性（先给出结构层面的判断）
    static_g = nx.DiGraph()
    static_g.add_nodes_from(g.nodes)
    for eid, e in edge_by_id.items():
        if eid not in blocked_static:
            static_g.add_edge(e["source"], e["target"], key=eid)
    if not nx.has_path(static_g, start, goal):
        reasons = _static_reasons(static_g, start, goal, node_by_id,
                                  edge_by_id, blocked_static,
                                  min_width, max_slope)
        return _unreachable(reasons, departure, deadline)

    # ---- 时间依赖 Dijkstra
    # labels[n] = [(arrival, length, tuple_edge_ids, prev_info), ...]
    labels: Dict[str, List[Tuple[datetime, float, Tuple[str, ...], Any]]] = {
        n: [] for n in g.nodes}
    labels[start] = [(departure, 0.0, (), None)]
    # 堆项：(arrival_ts, length, node, tuple_ids, counter)
    counter = 0
    heap: List[Tuple[float, float, int, str, Tuple[str, ...]]] = [
        (departure.timestamp(), 0.0, counter, start, ())]
    max_edges = len(edge_by_id)
    # 记录搜索中发现的“时间上走不通”的边：eid -> (ready_utc, next_open_utc or None)
    temporal_blocked: Dict[str, Tuple[datetime, Optional[datetime]]] = {}

    while heap:
        arr_ts, length, _, node, edge_tuple = heapq.heappop(heap)
        arrival = datetime.fromtimestamp(arr_ts, tz=timezone.utc)
        # 标签可能已被支配，跳过
        if not _label_alive(labels[node], arrival, length, edge_tuple):
            continue
        if node == goal:
            return _assemble_success(
                start, goal, departure, deadline, labels[node],
                arrival, length, edge_tuple, g, edge_by_id, closures_by_edge)

        if len(edge_tuple) >= max_edges:
            continue
        for eid in adj[node]:
            e = edge_by_id[eid]
            v = e["target"]
            blocks = closures_by_edge.get(eid, [])
            attempt = earliest_entry(
                arrival, deadline, e["traverse_seconds"], blocks)
            if attempt is None:
                # 记录时间阻断（仅保留最早一次尝试）
                if eid not in temporal_blocked:
                    nxt = _next_opening(arrival, deadline,
                                        e["traverse_seconds"], blocks)
                    temporal_blocked[eid] = (arrival, nxt)
                continue
            entry, leave = attempt
            new_len = length + e["length"]
            new_tuple = edge_tuple + (eid,)
            # 该边在窗口内确实走通过：不再作为时间阻断边
            temporal_blocked.pop(eid, None)
            if _dominated(labels[v], leave, new_len):
                continue
            # 同序列去重
            if any(t == new_tuple for _, _, t, _ in labels[v]):
                continue
            _insert_label(labels[v], leave, new_len, new_tuple,
                          (node, arrival, length, edge_tuple, entry))
            counter += 1
            heapq.heappush(heap, (leave.timestamp(), new_len, counter,
                                  v, new_tuple))

    # ---- 不可达：给出时间阻断原因
    reasons = _temporal_reasons(static_g, start, goal, node_by_id,
                                edge_by_id, blocked_static, temporal_blocked,
                                departure, deadline, min_width, max_slope)
    return _unreachable(reasons, departure, deadline)


def _label_alive(labellist: List[Tuple[datetime, float, Tuple[str, ...], Any]],
                 arrival: datetime, length: float,
                 edge_tuple: Tuple[str, ...]) -> bool:
    for a, l, t, _ in labellist:
        if t == edge_tuple:
            return a == arrival and l == length
    return False


def _dominated(labellist: List[Tuple[datetime, float, Tuple[str, ...], Any]],
               arrival: datetime, length: float) -> bool:
    """若已有标签 (a,l) 满足 a<=arrival 且 l<=length（至少一个严格），则被支配。"""
    for a, l, _, _ in labellist:
        if a <= arrival and l <= length and (a < arrival or l < length):
            return True
    return False


def _insert_label(labellist, arrival, length, edge_tuple, prev) -> None:
    # 删掉被新标签支配的旧标签
    keep = []
    for a, l, t, p in labellist:
        if arrival <= a and length <= l and (arrival < a or length < l):
            continue
        keep.append((a, l, t, p))
    keep.append((arrival, length, edge_tuple, prev))
    if len(keep) > LABEL_CAP:
        # 到达时刻、路程相同时，字典序小的边序列更优，丢弃尾部安全
        keep.sort(key=lambda x: (x[0], x[1], x[2]))
        keep = keep[:LABEL_CAP]
    labellist[:] = keep


def _next_opening(ready: datetime, deadline: datetime,
                  traverse_seconds: float,
                  blocks: List[Tuple[datetime, datetime]]) -> Optional[datetime]:
    """边在窗口内何时能通行（最早进入时刻）；整个窗口都走不通返回 None。"""
    entry = ready
    duration = timedelta(seconds=traverse_seconds)
    for b_start, b_end in blocks:
        if entry < b_end and entry + duration > b_start:
            entry = b_end
    if entry + duration <= deadline:
        return entry
    return None


def _assemble_success(start, goal, departure, deadline, goal_labels,
                      arrival, length, edge_tuple, g, edge_by_id,
                      closures_by_edge):
    # 终点可能有多个相同 (时刻, 路程) 的标签，按完整边 ID 序列字典序择优
    best_tuple = None
    for a, l, t, _ in goal_labels:
        if a == arrival and l == length:
            if best_tuple is None or t < best_tuple:
                best_tuple = t
    edge_tuple = best_tuple or edge_tuple

    # 正向重建步骤（重新模拟时间，保证步骤时间一致）
    steps = []
    cur_time = departure
    total_wait = 0.0
    for eid in edge_tuple:
        e = edge_by_id[eid]
        blocks = closures_by_edge.get(eid, [])
        found = earliest_entry(cur_time, arrival, e["traverse_seconds"], blocks)
        if found is None:  # 理论上不会发生
            break
        entry, leave = found
        wait = (entry - cur_time).total_seconds()
        total_wait += wait
        steps.append({
            "edge_id": eid, "source": e["source"], "target": e["target"],
            "type": e["type"], "type_label": TYPE_LABELS_CN.get(e["type"], e["type"]),
            "name": e["name"], "length": e["length"],
            "traverse_seconds": e["traverse_seconds"],
            "clear_width": e["clear_width"], "slope": e["slope"],
            "enter_utc": iso(entry), "leave_utc": iso(leave),
            "wait_before_seconds": wait,
            "cross_floor": e["source"] and _is_cross(g, e),
        })
        cur_time = leave
    return {
        "reachable": True, "start": start, "goal": goal,
        "departure_utc": iso(departure), "arrival_utc": iso(arrival),
        "deadline_utc": iso(deadline),
        "total_length": round(length, 3),
        "total_seconds": (arrival - departure).total_seconds(),
        "wait_seconds": total_wait,
        "edge_count": len(edge_tuple), "edge_ids": list(edge_tuple),
        "steps": steps,
    }


def _is_cross(g, e):
    return g.nodes[e["source"]].get("floor") != g.nodes[e["target"]].get("floor")


# ---------------------------------------------------------------- 阻断原因

def _edge_desc(eid: str, e: Dict[str, Any],
               node_by_id: Dict[str, Dict[str, Any]]) -> str:
    u = node_by_id.get(e["source"], {})
    v = node_by_id.get(e["target"], {})
    return (f"{eid}（{u.get('name', e['source'])} → {v.get('name', e['target'])}，"
            f"{TYPE_LABELS_CN.get(e['type'], e['type'])}）")


def _static_reasons(static_g, start, goal, node_by_id, edge_by_id,
                    blocked_static, min_width, max_slope):
    """S = 从起点（沿可走边）可达集合；T = 可到达终点的集合。
    真正的阻断是被静态条件挡掉、且一端在 S、另一端在 T 的割边。"""
    reachable_from = nx.descendants(static_g, start) | {start}
    can_reach_goal = nx.ancestors(static_g, goal) | {goal}
    cut: List[str] = []
    for eid, e in edge_by_id.items():
        why = blocked_static.get(eid)
        if why is None:
            continue
        if e["source"] in reachable_from and e["target"] in can_reach_goal:
            cut.append(eid)
    if not cut:
        return [{"code": "no_static_path",
                 "message": "即便不考虑封闭时段，当前可走边也无法连通起点与终点"
                            "（可能是单向路网方向不通）"}]
    reasons = []
    for eid in cut[:12]:
        e = edge_by_id[eid]
        why = blocked_static[eid]
        desc = _edge_desc(eid, e, node_by_id)
        if why == "stairs":
            msg = f"楼梯不可通行：{desc} 是楼梯，轮椅无法经过"
        elif why == "narrow":
            msg = (f"净宽不足：{desc} 净宽 {e['clear_width']}m，"
                   f"小于要求的 {min_width}m")
        else:
            msg = (f"坡度过大：{desc} 坡度 {e['slope']}%，"
                   f"大于允许的 {max_slope}%")
        reasons.append({"code": f"blocked_{why}", "edge_id": eid,
                        "source": e["source"], "target": e["target"],
                        "message": msg})
    return reasons


def _temporal_reasons(static_g, start, goal, node_by_id, edge_by_id,
                      blocked_static, temporal_blocked,
                      departure, deadline, min_width, max_slope):
    """只列“时间割边”：删掉全程封闭的边后，一端在起点可达集、另一端在
    终点可达集的关键阻断边。"""
    cutoff = set(temporal_blocked.keys())
    g2 = nx.DiGraph()
    g2.add_nodes_from(static_g.nodes)
    for eid, e in edge_by_id.items():
        if eid in blocked_static or eid in cutoff:
            continue
        g2.add_edge(e["source"], e["target"])
    reachable_from = nx.descendants(g2, start) | {start}
    can_reach_goal = nx.ancestors(g2, goal) | {goal}
    reasons: List[Dict[str, Any]] = []
    for eid in sorted(cutoff):
        e = edge_by_id[eid]
        if not (e["source"] in reachable_from and e["target"] in can_reach_goal):
            continue
        ready, nxt = temporal_blocked[eid]
        desc = _edge_desc(eid, e, node_by_id)
        if nxt is None:
            msg = (f"封闭时段覆盖：{desc} 在 24 小时核验窗内"
                   f"（至 {iso(deadline)}）没有足够长的开放窗口可通过")
        else:
            msg = (f"封闭时段阻挡：{desc}，最早约 {iso(nxt)} 才能进入，"
                   f"但沿此路径无法在窗口内到达终点")
        reasons.append({"code": "blocked_closure", "edge_id": eid,
                        "source": e["source"], "target": e["target"],
                        "ready_utc": iso(ready),
                        "next_open_utc": iso(nxt) if nxt else None,
                        "message": msg})
        if len(reasons) >= 12:
            break
    if not reasons:
        reasons.append({
            "code": "no_temporal_path",
            "message": f"在出发后 24 小时内（{iso(departure)} 至 "
                       f"{iso(deadline)}）无法到达终点；封闭时段使所有可行"
                       f"路径的等待时间超出窗口"})
    return reasons


def _unreachable(reasons, departure, deadline=None):
    out = {"reachable": False, "reasons": reasons,
           "departure_utc": iso(departure)}
    if deadline:
        out["deadline_utc"] = iso(deadline)
    return out
