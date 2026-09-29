"""Pydantic schemas for request/response validation."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Network import
# ---------------------------------------------------------------------------

class NodeIn(BaseModel):
    id: str
    floor_id: str
    x: float
    y: float
    kind: str = "junction"  # entrance | exit | room | junction | elevator
    name: str | None = None


class EdgeIn(BaseModel):
    id: str
    from_node: str = Field(alias="from")
    to_node: str = Field(alias="to")
    type: str  # corridor | stairs | elevator | ramp
    length: float = 0.0
    seconds: float = 0.0
    width: float = 0.0
    slope: float = 0.0

    model_config = {"populate_by_name": True}


class FloorIn(BaseModel):
    id: str
    name: str
    level: int = 0


class NetworkIn(BaseModel):
    floors: list[FloorIn]
    nodes: list[NodeIn]
    edges: list[EdgeIn]


# ---------------------------------------------------------------------------
# Closures
# ---------------------------------------------------------------------------

class ClosureIn(BaseModel):
    edge_id: str
    start: str  # ISO 8601 with timezone offset, e.g. 2026-09-29T08:00:00+08:00
    end: str
    tz: str = "UTC"


# ---------------------------------------------------------------------------
# Route query
# ---------------------------------------------------------------------------

class RouteQuery(BaseModel):
    start: str
    end: str
    departure: str  # ISO 8601 with timezone
    min_width: float = 0.0
    max_slope: float = 90.0


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

class StepOut(BaseModel):
    edge_id: str
    from_node: str
    to_node: str
    from_name: str | None = None
    to_name: str | None = None
    floor_id: str
    floor_name: str
    type: str
    length: float
    seconds: float
    width: float
    slope: float
    wait_seconds: float
    arrival: str  # ISO
    cross_floor: bool


class RouteResult(BaseModel):
    reachable: bool
    arrival: str | None = None
    total_seconds: float | None = None
    total_length: float | None = None
    wait_seconds: float | None = None
    steps: list[StepOut] = []
    reasons: list[str] = []
    blocking_closures: list[dict[str, Any]] = []
