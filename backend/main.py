"""FastAPI application for the accessible path verification platform."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

import database as db
import routing
from schemas import (
    ClosureIn,
    NetworkIn,
    RouteQuery,
)

app = FastAPI(title="建筑无障碍通路核验台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

@app.post("/api/network/import")
def import_network(net: NetworkIn) -> dict[str, Any]:
    data = net.model_dump(by_alias=True)
    # Normalize edge keys: model_dump with by_alias gives "from"/"to".
    try:
        routing.validate_network(data)
    except routing.NetworkValidationError as exc:
        raise HTTPException(status_code=400, detail={"errors": exc.errors})
    version = db.insert_network(data)
    return {"version": version, "message": "路网导入成功"}


@app.get("/api/network")
def get_network() -> dict[str, Any]:
    res = db.get_current_network()
    if res is None:
        raise HTTPException(status_code=404, detail="尚未导入路网")
    version, data = res
    return {"version": version, "data": data}


@app.get("/api/network/versions")
def network_versions() -> dict[str, Any]:
    return {"versions": db.list_network_versions()}


@app.get("/api/network/{version}")
def get_network_version(version: int) -> dict[str, Any]:
    data = db.get_network(version)
    if data is None:
        raise HTTPException(status_code=404, detail="路网版本不存在")
    return {"version": version, "data": data}


# ---------------------------------------------------------------------------
# Closures
# ---------------------------------------------------------------------------

def _current_version() -> int:
    res = db.get_current_network()
    if res is None:
        raise HTTPException(status_code=404, detail="尚未导入路网")
    return res[0]


@app.post("/api/closures")
def add_closure(closure: ClosureIn) -> dict[str, Any]:
    version = _current_version()
    # Validate edge exists.
    net = db.get_network(version)
    edge_ids = {e["id"] for e in net["edges"]}
    if closure.edge_id not in edge_ids:
        raise HTTPException(status_code=400, detail=f"通道 {closure.edge_id} 不存在")
    # Validate datetimes.
    try:
        start = routing.parse_dt(closure.start)
        end = routing.parse_dt(closure.end)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"时间格式错误: {exc}")
    if end <= start:
        raise HTTPException(status_code=400, detail="封闭时段结束必须晚于开始")
    cid = db.add_closure(version, closure.edge_id, closure.start, closure.end, closure.tz)
    db.bump_closure_version(version)
    return {"id": cid, "message": "封闭时段已登记"}


@app.get("/api/closures")
def list_closures() -> dict[str, Any]:
    version = _current_version()
    return {"network_version": version, "closures": db.list_closures(version)}


@app.delete("/api/closures/{closure_id}")
def delete_closure(closure_id: int) -> dict[str, Any]:
    version = _current_version()
    ok = db.delete_closure(closure_id)
    if not ok:
        raise HTTPException(status_code=404, detail="封闭时段不存在")
    db.bump_closure_version(version)
    return {"message": "已删除"}


# ---------------------------------------------------------------------------
# Route query
# ---------------------------------------------------------------------------

def _build_closures_by_edge(
    closures: list[dict[str, Any]],
) -> dict[str, list[tuple[datetime, datetime]]]:
    result: dict[str, list[tuple[datetime, datetime]]] = {}
    for c in closures:
        start = routing.to_utc(routing.parse_dt(c["start"]))
        end = routing.to_utc(routing.parse_dt(c["end"]))
        result.setdefault(c["edge_id"], []).append((start, end))
    for k in result:
        result[k].sort()
    return result


@app.post("/api/route/query")
def query_route(q: RouteQuery) -> dict[str, Any]:
    res = db.get_current_network()
    if res is None:
        raise HTTPException(status_code=404, detail="尚未导入路网")
    version, data = res
    G = routing.build_graph(data)

    closures = db.list_closures(version)
    closures_by_edge = _build_closures_by_edge(closures)

    try:
        departure = routing.parse_dt(q.departure)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"出发时间格式错误: {exc}")

    result = routing.find_route(
        G,
        q.start,
        q.end,
        departure,
        q.min_width,
        q.max_slope,
        closures_by_edge,
    )

    # Solidify the record.
    closure_version, closure_snap = db.closure_snapshot(version)
    record_id = db.insert_record(
        network_version=version,
        closure_version=closure_version,
        closure_snap=closure_snap,
        query=q.model_dump(),
        result=result,
        reachable=result["reachable"],
    )
    result["record_id"] = record_id
    result["network_version"] = version
    result["closure_version"] = closure_version
    return result


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@app.get("/api/records")
def list_records() -> dict[str, Any]:
    return {"records": db.list_records()}


@app.get("/api/records/{record_id}")
def get_record(record_id: int) -> dict[str, Any]:
    rec = db.get_record(record_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="核验记录不存在")
    return rec


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(FRONTEND_DIR / "index.html"))


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
