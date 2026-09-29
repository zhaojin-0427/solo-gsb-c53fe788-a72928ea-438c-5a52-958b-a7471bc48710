"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ClosureIn(BaseModel):
    edge_id: str = Field(..., min_length=1)
    start: str = Field(..., description="带时区的 ISO 8601 起始时刻，含秒，闭区间端")
    end: str = Field(..., description="带时区的 ISO 8601 结束时刻，含秒，开区间端")


class ClosureOut(ClosureIn):
    id: int
    network_id: int
    start_utc: str
    end_utc: str


class RouteQuery(BaseModel):
    start: str = Field(..., min_length=1)
    goal: str = Field(..., min_length=1)
    departure: str = Field(..., description="带时区的 ISO 8601 出发时刻")
    min_clear_width: float = Field(0.0, ge=0)
    max_slope: float = Field(90.0, ge=0)


class NetworkMeta(BaseModel):
    id: int
    name: str
    imported_at: str
    floors: List[str]
    node_count: int
    edge_count: int


class RecordSummary(BaseModel):
    id: int
    network_id: int
    network_name: str
    created_at: str
    start: str
    goal: str
    departure: str
    min_clear_width: float
    max_slope: float
    reachable: bool


class StandardResponse(BaseModel):
    ok: bool = True
    detail: Optional[str] = None
    data: Optional[Any] = None
    errors: Optional[List[Dict[str, Any]]] = None
