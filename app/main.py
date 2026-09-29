"""建筑无障碍通路核验台 —— FastAPI 入口。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import database as db
from .models import ClosureIn, RouteQuery
from .planning import (PlanningError, build_graph, iso, merge_intervals,
                       parse_aware, plan_route, validate_network)

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_PATH = BASE_DIR.parent / "sample_network.json"

app = FastAPI(title="建筑无障碍通路核验台", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ---------------------------------------------------------------- 辅助

def _active_or_404() -> Dict[str, Any]:
    net = db.get_active_network()
    if net is None:
        raise PlanningError("尚未导入任何路网，请先在“路网导入”页导入 JSON")
    return net


def _build_indexes(payload: Dict[str, Any]):
    g = build_graph(payload)
    node_by_id = {n["id"]: n for n in payload["nodes"]}
    edge_by_id = {e["id"]: e for e in payload["edges"]}
    return g, node_by_id, edge_by_id


def _closure_map(network_id: int
                 ) -> Tuple[Dict[str, List], List[Dict[str, Any]]]:
    rows = db.list_closures(network_id)
    grouped: Dict[str, List[Tuple]] = {}
    for r in rows:
        grouped.setdefault(r["edge_id"], []).append(
            (parse_aware(r["start_utc"], "封闭开始"),
             parse_aware(r["end_utc"], "封闭结束")))
    merged = {eid: merge_intervals(ivs) for eid, ivs in grouped.items()}
    # 双向边（one_way=false）导入时生成了 __rev 反向边：封闭对两个方向都生效
    net = db.get_network(network_id)
    if net:
        edge_ids = {e["id"] for e in net["payload"]["edges"]}
        for base in list(merged.keys()):
            if f"{base}__rev" in edge_ids:
                merged[f"{base}__rev"] = merged[base]
    return merged, rows


def _error(exc: PlanningError, status: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"ok": False, "detail": str(exc),
                 "errors": getattr(exc, "errors", None)})


# ---------------------------------------------------------------- 路网

@app.get("/api/networks")
def api_list_networks():
    return {"ok": True, "data": db.list_networks()}


@app.get("/api/networks/active")
def api_active_network():
    net = db.get_active_network()
    if net is None:
        return {"ok": True, "data": None}
    p = net["payload"]
    return {"ok": True, "data": {
        "id": net["id"], "name": net["name"],
        "imported_at": net["imported_at"],
        "floors": p["floors"], "nodes": p["nodes"], "edges": p["edges"]}}


@app.post("/api/networks/import")
async def api_import(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return _error(PlanningError("请求体不是合法 JSON"))
    try:
        normalized = validate_network(payload)
        network_id = db.create_network_version(normalized)
    except PlanningError as exc:
        return _error(exc)
    return {"ok": True, "data": {"id": network_id, **normalized}}


@app.post("/api/networks/{network_id}/activate")
def api_activate(network_id: int):
    if not db.set_active_network(network_id):
        return _error(PlanningError(f"路网版本 {network_id} 不存在"), 404)
    return {"ok": True}


@app.get("/api/sample")
def api_sample():
    return FileResponse(SAMPLE_PATH, media_type="application/json")


# ---------------------------------------------------------------- 封闭时段

@app.get("/api/closures")
def api_list_closures():
    net = _active_or_404()
    return {"ok": True, "data": db.list_closures(net["id"])}


@app.post("/api/closures")
def api_add_closure(body: ClosureIn):
    try:
        net = _active_or_404()
        start = parse_aware(body.start, "封闭开始时刻")
        end = parse_aware(body.end, "封闭结束时刻")
        if end <= start:
            raise PlanningError("封闭结束时刻必须晚于开始时刻")
        edge_ids = {e["id"] for e in net["payload"]["edges"]}
        if body.edge_id not in edge_ids:
            raise PlanningError(f"边 {body.edge_id} 不在当前路网中")
        cid = db.add_closure(net["id"], body.edge_id,
                             iso(start), iso(end), body.start, body.end)
    except PlanningError as exc:
        return _error(exc)
    return {"ok": True, "data": {"id": cid, "network_id": net["id"],
                                 "edge_id": body.edge_id,
                                 "start": body.start, "end": body.end,
                                 "start_utc": iso(start), "end_utc": iso(end)}}


@app.delete("/api/closures/{closure_id}")
def api_delete_closure(closure_id: int):
    try:
        net = _active_or_404()
    except PlanningError as exc:
        return _error(exc)
    if not db.delete_closure(net["id"], closure_id):
        return _error(PlanningError("封闭时段不存在或不属于当前路网版本"), 404)
    return {"ok": True}


# ---------------------------------------------------------------- 路线核验

@app.post("/api/route/query")
def api_route_query(q: RouteQuery):
    try:
        net = _active_or_404()
        payload = net["payload"]
        departure = parse_aware(q.departure, "出发时刻")
        g, node_by_id, edge_by_id = _build_indexes(payload)
        closures, closure_rows = _closure_map(net["id"])
        result = plan_route(
            g, node_by_id, edge_by_id, q.start, q.goal, departure,
            q.min_clear_width, q.max_slope, closures)
        params = {"start": q.start, "goal": q.goal, "departure": q.departure,
                  "departure_utc": iso(departure),
                  "min_clear_width": q.min_clear_width,
                  "max_slope": q.max_slope}
        record_id = db.save_record(
            net["id"], net["name"], payload,
            [{"edge_id": r["edge_id"], "start": r["raw_start"],
              "end": r["raw_end"], "start_utc": r["start_utc"],
              "end_utc": r["end_utc"]} for r in closure_rows],
            params, result)
    except PlanningError as exc:
        return _error(exc)
    return {"ok": True, "data": {"record_id": record_id, "result": result}}


@app.get("/api/records")
def api_list_records():
    return {"ok": True, "data": db.list_records()}


@app.get("/api/records/{record_id}")
def api_get_record(record_id: int):
    rec = db.get_record(record_id)
    if rec is None:
        return _error(PlanningError("核验记录不存在"), 404)
    return {"ok": True, "data": rec}


# ---------------------------------------------------------------- 前端

@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")
