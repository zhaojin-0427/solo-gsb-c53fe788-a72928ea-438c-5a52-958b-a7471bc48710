"""核心规则冒烟测试（不依赖服务进程，直接跑 planning）。"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/workspace")
from app.planning import (build_graph, earliest_entry, iso, merge_intervals,
                          parse_aware, plan_route, validate_network,
                          PlanningError)

fails = []

def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL"), name, extra)
    if not cond:
        fails.append(name)


def base_net():
    return {
        "name": "t",
        "nodes": [
            {"id": "A", "name": "A", "floor": "F1", "x": 0, "y": 0, "type": "entrance"},
            {"id": "B", "name": "B", "floor": "F1", "x": 10, "y": 0, "type": "junction"},
            {"id": "C", "name": "C", "floor": "F1", "x": 20, "y": 0, "type": "room"},
            {"id": "D", "name": "D", "floor": "F2", "x": 0, "y": 10, "type": "junction"},
            {"id": "E", "name": "E", "floor": "F2", "x": 10, "y": 10, "type": "room"},
        ],
        "edges": [
            {"id": "eAB", "source": "A", "target": "B", "type": "corridor",
             "length": 5, "traverse_seconds": 10, "clear_width": 1.2, "slope": 0},
            {"id": "eBC", "source": "B", "target": "C", "type": "corridor",
             "length": 5, "traverse_seconds": 10, "clear_width": 1.2, "slope": 0},
            {"id": "eBD", "source": "B", "target": "D", "type": "elevator",
             "length": 3, "traverse_seconds": 30, "clear_width": 1.1, "slope": 0},
            {"id": "eDE", "source": "D", "target": "E", "type": "corridor",
             "length": 5, "traverse_seconds": 10, "clear_width": 1.2, "slope": 0},
            {"id": "stairCE", "source": "C", "target": "E", "type": "stairs",
             "length": 6, "traverse_seconds": 10, "clear_width": 1.2, "slope": 30},
        ],
        "floors": ["F1", "F2"],
    }

def indexes(net):
    g = build_graph(net)
    nb = {n["id"]: n for n in net["nodes"]}
    eb = {e["id"]: e for e in net["edges"]}
    return g, nb, eb

dep = parse_aware("2026-10-01T08:00:00+08:00")

# 1) 正常导入
net = validate_network(base_net())
check("import ok", len(net["edges"]) == 5)

# 2) 悬空引用
bad = base_net()
bad["edges"][0]["target"] = "GHOST"
try:
    validate_network(bad)
    check("dangling reference rejected", False)
except PlanningError as e:
    check("dangling reference rejected",
          any(x["code"] == "dangling_node" for x in e.errors))

# 3) 悬空（孤立）节点
bad = base_net()
bad["nodes"].append({"id": "X", "name": "X", "floor": "F1", "x": 9, "y": 9, "type": "junction"})
try:
    validate_network(bad)
    check("isolated node rejected", False)
except PlanningError as e:
    check("isolated node rejected",
          any(x["code"] == "isolated_node" for x in e.errors))

# 4) 非法跨层（走廊跨层）
bad = base_net()
bad["edges"][0] = {"id": "badCross", "source": "A", "target": "D", "type": "corridor",
                   "length": 5, "traverse_seconds": 10, "clear_width": 1.2, "slope": 0}
try:
    validate_network(bad)
    check("illegal cross-floor rejected", False)
except PlanningError as e:
    check("illegal cross-floor rejected",
          any(x["code"] == "illegal_cross_floor" for x in e.errors))

# 5) 同层基本路线
g, nb, eb = indexes(net)
r = plan_route(g, nb, eb, "A", "C", dep, 0.9, 100, {})
check("A->C reachable", r["reachable"] and r["edge_ids"] == ["eAB", "eBC"],
      str(r.get("edge_ids")))
check("A->C timing", r["total_seconds"] == 20 and r["total_length"] == 10.0)

# 6) 楼梯不可走：C->E 只能 B->D 电梯方向不通（C 无去 B 的反向边）
r = plan_route(g, nb, eb, "C", "E", dep, 0.9, 100, {})
check("stairs blocked C->E unreachable", not r["reachable"]
      and any(x.get("edge_id") == "stairCE" for x in r["reasons"]),
      str(r.get("reasons")))

# 7) 跨层电梯可走 A->E
r = plan_route(g, nb, eb, "A", "E", dep, 0.9, 100, {})
check("elevator cross-floor A->E",
      r["reachable"] and r["edge_ids"] == ["eAB", "eBD", "eDE"])

# 8) 净宽过滤：要求 1.15 时电梯 1.1 不可用
r = plan_route(g, nb, eb, "A", "E", dep, 1.15, 100, {})
check("width filter blocks elevator", not r["reachable"])

# 9) 封闭时段 + 节点等候：封 eAB [08:00:00+08, 08:05:00+08)
#    到达 B 应在 08:05:10（等 300 秒）
s = parse_aware("2026-10-01T08:05:00+08:00")
blocks = {"eAB": merge_intervals([(dep.astimezone(timezone.utc),
                                   s.astimezone(timezone.utc))])}
r = plan_route(g, nb, eb, "A", "C", dep, 0.9, 100, blocks)
check("wait for closure",
      r["reachable"] and r["wait_seconds"] == 300
      and r["steps"][0]["enter_utc"] == iso(s)
      and r["arrival_utc"] == iso(parse_aware("2026-10-01T08:05:20+08:00")),
      str((r.get("wait_seconds"), r.get("arrival_utc"))))

# 10) 封闭超过 24h 窗口 -> 不可达，原因里包含 eAB
long_start = dep
long_end = parse_aware("2026-10-02T08:30:00+08:00")
blocks = {"eAB": [(long_start, long_end)]}
r = plan_route(g, nb, eb, "A", "C", dep, 0.9, 100, blocks)
check("closure beyond 24h unreachable",
      not r["reachable"] and any(x.get("edge_id") == "eAB" for x in r["reasons"]))

# 11) earliest_entry 半开区间：[8:05 封结束) 时 8:05 可进入
from datetime import timedelta
ent = earliest_entry(dep, dep + timedelta(hours=24), 10,
                     [(dep, s.astimezone(timezone.utc))])
check("half-open release", ent is not None and ent[0] == s.astimezone(timezone.utc))

# 12) 平局：长度短优先；构造等到达两条路
net2p = {
    "name": "t2",
    "nodes": [
        {"id": "S", "name": "S", "floor": "F1", "x": 0, "y": 0, "type": "entrance"},
        {"id": "M", "name": "M", "floor": "F1", "x": 5, "y": 5, "type": "junction"},
        {"id": "T", "name": "T", "floor": "F1", "x": 10, "y": 0, "type": "room"},
    ],
    "edges": [
        {"id": "zLong", "source": "S", "target": "T", "type": "corridor",
         "length": 20, "traverse_seconds": 10, "clear_width": 2, "slope": 0},
        {"id": "aSM", "source": "S", "target": "M", "type": "corridor",
         "length": 4, "traverse_seconds": 5, "clear_width": 2, "slope": 0},
        {"id": "bMT", "source": "M", "target": "T", "type": "corridor",
         "length": 4, "traverse_seconds": 5, "clear_width": 2, "slope": 0},
    ],
    "floors": ["F1"],
}
g2, nb2, eb2 = indexes(validate_network(net2p))
r = plan_route(g2, nb2, eb2, "S", "T", dep, 0, 100, {})
check("tie: shorter length wins", r["reachable"]
      and r["edge_ids"] == ["aSM", "bMT"] and r["total_length"] == 8)

# 13) 边 ID 序列字典序：等长等同时间两条直达
net3 = {
    "name": "t3",
    "nodes": [
        {"id": "S", "name": "S", "floor": "F1", "x": 0, "y": 0, "type": "entrance"},
        {"id": "T", "name": "T", "floor": "F1", "x": 10, "y": 0, "type": "room"},
    ],
    "edges": [
        {"id": "zEdge", "source": "S", "target": "T", "type": "corridor",
         "length": 5, "traverse_seconds": 5, "clear_width": 2, "slope": 0},
        {"id": "aEdge", "source": "S", "target": "T", "type": "corridor",
         "length": 5, "traverse_seconds": 5, "clear_width": 2, "slope": 0},
    ],
    "floors": ["F1"],
}
g3, nb3, eb3 = indexes(validate_network(net3))
r = plan_route(g3, nb3, eb3, "S", "T", dep, 0, 100, {})
check("tie: edge-id lexicographic", r["reachable"] and r["edge_ids"] == ["aEdge"],
      str(r.get("edge_ids")))

# 14) 坡度过滤：ramp 12% 超过 8.33% 被拒（用示例路网逻辑复核）
sample = validate_network(__import__("json").load(
    open("/workspace/sample_network.json", encoding="utf-8")))
gs, nbs, ebs = indexes(sample)
# F1_HALL -> F2_HALL：电梯可行（经 F1_ELEV），坡道 12% 在 8.33 限制下排除
r = plan_route(gs, nbs, ebs, "F1_ENT", "F2_R201", dep, 0.9, 8.33, {})
check("sample: steep ramp excluded, elevator used",
      r["reachable"] and "el_f1_f2" in r["edge_ids"]
      and "rp_f1_f2" not in r["edge_ids"], str(r.get("edge_ids")))
# 放宽坡度后坡道更短？电梯线长 7+4+9+11=31，坡道线 6+25+10+11=52 -> 仍电梯优先（长度）
r2 = plan_route(gs, nbs, ebs, "F1_ENT", "F2_R201", dep, 0.9, 100, {})
check("sample: elevator shorter anyway",
      r2["reachable"] and "el_f1_f2" in r2["edge_ids"])
# 封闭电梯后改走坡道
blocks = {"el_f1_f2": merge_intervals([
    (dep, parse_aware("2026-10-01T08:20:00+08:00"))])}
r3 = plan_route(gs, nbs, ebs, "F1_ENT", "F2_R201", dep, 0.9, 100, blocks)
check("sample: elevator closed -> ramp",
      r3["reachable"] and "rp_f1_f2" in r3["edge_ids"]
      and "el_f1_f2" not in r3["edge_ids"], str(r3.get("edge_ids")))

# 15) start==goal
r = plan_route(g, nb, eb, "A", "A", dep, 0.9, 100, {})
check("same node trivial", r["reachable"] and r["edge_count"] == 0)

print()
if fails:
    print(f"{len(fails)} FAILURES:", fails)
    sys.exit(1)
print("ALL TESTS PASSED")
