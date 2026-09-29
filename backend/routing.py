"""Network validation and time-dependent wheelchair routing.

The routing problem is a time-dependent shortest path in a directed graph:
  * Stairs are never passable.
  * Cross-floor edges are only allowed for elevators or ramps.
  * Corridors connect nodes on the same floor.
  * The wheelchair must satisfy min clear width and max slope.
  * Waiting at nodes is permitted.
  * Traversal of an edge must not intersect any registered closure period.
  * We seek the earliest arrival within 24h of departure; ties are broken
    by total edge length, then by lexicographic edge-ID sequence.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import networkx as nx

# Edge types
TYPE_CORRIDOR = "corridor"
TYPE_STAIRS = "stairs"
TYPE_ELEVATOR = "elevator"
TYPE_RAMP = "ramp"

CROSS_FLOOR_TYPES = {TYPE_ELEVATOR, TYPE_RAMP}
PASSABLE_TYPES = {TYPE_CORRIDOR, TYPE_ELEVATOR, TYPE_RAMP}

HOURS_24 = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class NetworkValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_network(data: dict[str, Any]) -> None:
    """Validate the imported network. Raises NetworkValidationError on issues."""
    errors: list[str] = []

    floors = data.get("floors", [])
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])

    floor_ids = {f["id"] for f in floors}
    node_ids = {n["id"] for n in nodes}

    # Duplicate ids
    if len(floor_ids) != len(floors):
        errors.append("楼层 ID 存在重复")
    if len(node_ids) != len(nodes):
        errors.append("节点 ID 存在重复")
    edge_ids = [e["id"] for e in edges]
    if len(set(edge_ids)) != len(edge_ids):
        errors.append("通道 ID 存在重复")

    # Node floor references
    for n in nodes:
        if n["floor_id"] not in floor_ids:
            errors.append(f"节点 {n['id']} 引用了不存在的楼层 {n['floor_id']}")

    # Edge references and type checks
    for e in edges:
        if e["from"] not in node_ids:
            errors.append(f"通道 {e['id']} 的起点 {e['from']} 不存在")
        if e["to"] not in node_ids:
            errors.append(f"通道 {e['id']} 的终点 {e['to']} 不存在")

    # Dangling nodes: a node that appears in no edge at all.
    connected: set[str] = set()
    for e in edges:
        connected.add(e["from"])
        connected.add(e["to"])
    for n in nodes:
        if n["id"] not in connected:
            errors.append(f"节点 {n['id']} 是悬空节点（未连接任何通道）")

    # Cross-floor legality
    node_floor = {n["id"]: n["floor_id"] for n in nodes}
    for e in edges:
        if e["from"] in node_ids and e["to"] in node_ids:
            f1 = node_floor[e["from"]]
            f2 = node_floor[e["to"]]
            etype = e.get("type", TYPE_CORRIDOR)
            if f1 != f2 and etype not in CROSS_FLOOR_TYPES:
                errors.append(
                    f"通道 {e['id']} 跨楼层但类型为 {etype}，"
                    f"跨层仅允许电梯或坡道"
                )
            if f1 == f2 and etype in CROSS_FLOOR_TYPES:
                errors.append(
                    f"通道 {e['id']} 同楼层但类型为 {etype}，类型与楼层不一致"
                )

    # Numeric sanity
    for e in edges:
        if e.get("length", 0) < 0:
            errors.append(f"通道 {e['id']} 长度为负")
        if e.get("seconds", 0) < 0:
            errors.append(f"通道 {e['id']} 通行秒数为负")
        if e.get("width", 0) < 0:
            errors.append(f"通道 {e['id']} 净宽为负")
        if not (0 <= e.get("slope", 0) <= 90):
            errors.append(f"通道 {e['id']} 坡度应在 0~90 之间")

    if errors:
        raise NetworkValidationError(errors)


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph(data: dict[str, Any]) -> nx.DiGraph:
    """Build a NetworkX DiGraph from validated network data."""
    G = nx.DiGraph()
    node_floor: dict[str, str] = {}
    for n in data["nodes"]:
        G.add_node(
            n["id"],
            floor_id=n["floor_id"],
            x=n["x"],
            y=n["y"],
            kind=n.get("kind", "junction"),
            name=n.get("name"),
        )
        node_floor[n["id"]] = n["floor_id"]
    for e in data["edges"]:
        G.add_edge(
            e["from"],
            e["to"],
            id=e["id"],
            type=e.get("type", TYPE_CORRIDOR),
            length=float(e.get("length", 0.0)),
            seconds=float(e.get("seconds", 0.0)),
            width=float(e.get("width", 0.0)),
            slope=float(e.get("slope", 0.0)),
            cross_floor=node_floor[e["from"]] != node_floor[e["to"]],
        )
    return G


# ---------------------------------------------------------------------------
# Timezone-aware datetime helpers
# ---------------------------------------------------------------------------

def parse_dt(s: str) -> datetime:
    """Parse an ISO 8601 string. If naive, assume UTC."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Closure-aware edge departure
# ---------------------------------------------------------------------------

def earliest_departure(
    arrival: datetime,
    seconds: float,
    closures: list[tuple[datetime, datetime]],
) -> datetime:
    """Given arrival time at the from-node, return the earliest departure
    time >= arrival such that traversing the edge does not intersect any
    closure period.

    closures is a list of (start, end) UTC datetimes with start < end.
    """
    dep = arrival
    secs = timedelta(seconds=seconds)
    changed = True
    while changed:
        changed = False
        for cs, ce in closures:
            # Intersection of [dep, dep+secs) with [cs, ce)
            if dep < ce and dep + secs > cs:
                # Cannot finish before cs (would require dep <= cs - secs,
                # but dep >= arrival and arrival+secs > cs here). So depart
                # after the closure ends.
                if dep < ce:
                    dep = ce
                    changed = True
    return dep


# ---------------------------------------------------------------------------
# Label-setting shortest path
# ---------------------------------------------------------------------------

@dataclass
class Label:
    node: str
    arrival: datetime
    distance: float
    seq: tuple[str, ...]
    path: list[dict[str, Any]] = field(default_factory=list)

    def key(self) -> tuple:
        return (self.arrival, self.distance, self.seq)


def _lex_le(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Lexicographic comparison of edge-id sequences."""
    return a <= b


def _dominates(a: Label, b: Label) -> bool:
    """Return True if label a dominates label b (a is no worse in all
    dimensions and strictly better in at least one)."""
    if a.arrival > b.arrival:
        return False
    if a.distance > b.distance:
        return False
    if not _lex_le(a.seq, b.seq):
        return False
    # All <=; need at least one strict.
    if a.arrival < b.arrival or a.distance < b.distance or a.seq < b.seq:
        return True
    return False


def _add_label(labels: list[Label], new: Label) -> bool:
    """Add new label to labels, removing dominated ones. Return True if added."""
    for i, lab in enumerate(labels):
        if _dominates(lab, new):
            return False
    # Remove labels dominated by new.
    kept = [lab for lab in labels if not _dominates(new, lab)]
    kept.append(new)
    labels[:] = kept
    return True


def find_route(
    G: nx.DiGraph,
    start: str,
    end: str,
    departure: datetime,
    min_width: float,
    max_slope: float,
    closures_by_edge: dict[str, list[tuple[datetime, datetime]]],
) -> dict[str, Any]:
    """Find the best wheelchair route. Returns a result dict."""
    departure_utc = to_utc(departure)
    deadline = departure_utc + HOURS_24

    if start not in G:
        return {
            "reachable": False,
            "reasons": [f"起点 {start} 不在路网中"],
            "blocking_closures": [],
        }
    if end not in G:
        return {
            "reachable": False,
            "reasons": [f"终点 {end} 不在路网中"],
            "blocking_closures": [],
        }

    # Static feasibility: does any path exist under width/slope/type constraints?
    def static_ok(u: str, v: str, data: dict[str, Any]) -> bool:
        if data["type"] not in PASSABLE_TYPES:
            return False
        if data["type"] == TYPE_STAIRS:
            return False
        if data["width"] < min_width:
            return False
        if data["slope"] > max_slope:
            return False
        return True

    static_G = nx.subgraph_view(
        G,
        filter_edge=lambda u, v: static_ok(u, v, G[u][v]),
    )
    if not nx.has_path(static_G, start, end):
        reasons = ["不存在满足净宽/坡度要求的可达路径"]
        # Distinguish disconnection vs constraint blocking.
        if not nx.has_path(G, start, end):
            reasons.append("起终点在路网中不连通")
        else:
            reasons.append("所有路径均被净宽或坡度限制阻断")
        return {
            "reachable": False,
            "reasons": reasons,
            "blocking_closures": [],
        }

    # Label-setting search.
    labels: dict[str, list[Label]] = {n: [] for n in G.nodes}
    start_label = Label(node=start, arrival=departure_utc, distance=0.0, seq=())
    labels[start].append(start_label)

    heap: list[tuple] = []
    counter = 0
    heapq.heappush(heap, (start_label.arrival, 0.0, (), counter, start_label))

    best: Label | None = None

    while heap:
        arr, dist, seq, _, lab = heapq.heappop(heap)
        # Skip if this label was dominated after being pushed.
        if lab not in labels.get(lab.node, []):
            continue
        # Prune: if we already have a best and this label can't beat it.
        if best is not None and lab.key() >= best.key():
            continue

        if lab.node == end:
            if best is None or lab.key() < best.key():
                best = lab
            continue

        for v, edata in G[lab.node].items():
            if not static_ok(lab.node, v, edata):
                continue
            eid = edata["id"]
            closures = closures_by_edge.get(eid, [])
            dep = earliest_departure(lab.arrival, edata["seconds"], closures)
            arr_v = dep + timedelta(seconds=edata["seconds"])
            if arr_v > deadline:
                continue
            dist_v = lab.distance + edata["length"]
            seq_v = lab.seq + (eid,)

            step = {
                "edge_id": eid,
                "from_node": lab.node,
                "to_node": v,
                "type": edata["type"],
                "length": edata["length"],
                "seconds": edata["seconds"],
                "width": edata["width"],
                "slope": edata["slope"],
                "wait_seconds": (dep - lab.arrival).total_seconds(),
                "arrival": arr_v.isoformat(),
                "cross_floor": edata["cross_floor"],
            }
            new_label = Label(
                node=v,
                arrival=arr_v,
                distance=dist_v,
                seq=seq_v,
                path=lab.path + [step],
            )
            if _add_label(labels[v], new_label):
                counter += 1
                heapq.heappush(
                    heap,
                    (new_label.arrival, new_label.distance, new_label.seq, counter, new_label),
                )

    if best is None:
        # Static path exists but closures block within 24h.
        blocking = _find_blocking_closures(G, start, end, min_width, max_slope, closures_by_edge, deadline)
        return {
            "reachable": False,
            "reasons": ["路径被封闭时段阻断，24 小时内无法到达"],
            "blocking_closures": blocking,
        }

    # Build result.
    wait_total = sum(s["wait_seconds"] for s in best.path)
    travel_total = sum(s["seconds"] for s in best.path)
    return {
        "reachable": True,
        "arrival": best.arrival.isoformat(),
        "total_seconds": travel_total + wait_total,
        "total_length": best.distance,
        "wait_seconds": wait_total,
        "steps": best.path,
        "reasons": [],
        "blocking_closures": [],
    }


def _find_blocking_closures(
    G: nx.DiGraph,
    start: str,
    end: str,
    min_width: float,
    max_slope: float,
    closures_by_edge: dict[str, list[tuple[datetime, datetime]]],
    deadline: datetime,
) -> list[dict[str, Any]]:
    """Find closures that block the free-flow shortest path (by seconds)."""
    def static_ok(u: str, v: str, data: dict[str, Any]) -> bool:
        return (
            data["type"] in PASSABLE_TYPES
            and data["type"] != TYPE_STAIRS
            and data["width"] >= min_width
            and data["slope"] <= max_slope
        )

    static_G = nx.subgraph_view(G, filter_edge=lambda u, v: static_ok(u, v, G[u][v]))
    try:
        path = nx.shortest_path(static_G, start, end, weight="seconds")
    except nx.NetworkXNoPath:
        return []

    blocking: list[dict[str, Any]] = []
    for i in range(len(path) - 1):
        u = path[i]
        v = path[i + 1]
        edata = G[u][v]
        eid = edata["id"]
        for cs, ce in closures_by_edge.get(eid, []):
            blocking.append(
                {
                    "edge_id": eid,
                    "from_node": u,
                    "to_node": v,
                    "start": cs.isoformat(),
                    "end": ce.isoformat(),
                }
            )
    return blocking
