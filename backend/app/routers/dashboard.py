from __future__ import annotations
import json
from fastapi import APIRouter
from app.db import connect
from app.services.overview import (
    compute_metrics,
    diff_metrics,
    fingerprint,
    recompute_overview,
)

router = APIRouter(tags=["dashboard"])


def _snapshot(conn):
    """在同一读快照里同时取物化行与底表实时聚合。

    物化读取与互证必须落在同一快照，否则并发写（如结案提交）会让
    互证看到与物化不同代的底表，造成假 stale / 半截状态。
    """
    conn.execute("BEGIN")
    try:
        row = conn.execute(
            "SELECT payload, fingerprint, computed_at FROM overview_stats WHERE id=1"
        ).fetchone()
        live = compute_metrics(conn)
    finally:
        conn.rollback()
    return row, live


def _payload(row, live: dict) -> dict:
    """展示值只取自物化表；stale/分叉明细来自与底表的互证。

    互证是三段链，任一环节断裂即 stale：
    物化 payload ↔ 物化指纹（payload 被直接改脏）↔ 底表实时指纹（底表被改而未重算）。
    """
    try:
        materialized = json.loads(row["payload"])
    except (TypeError, ValueError):
        materialized = None
    stale = (
        materialized is None
        or fingerprint(materialized) != row["fingerprint"]
        or row["fingerprint"] != fingerprint(live)
    )
    if not stale:
        divergence = []
    elif materialized is None:
        divergence = [{"field": "payload", "materialized": "<物化行不可解析>", "live": "<底表可算>"}]
    else:
        divergence = diff_metrics(materialized, live)
    # 物化不可解析时退回实时值兜底展示，但仍标 stale，绝不假绿
    body = dict(materialized) if materialized is not None else dict(live)
    body["stale"] = stale
    body["divergence"] = divergence
    body["computed_at"] = row["computed_at"]
    return body


@router.get("/dashboard")
def dashboard():
    conn = connect()
    try:
        if conn.execute("SELECT 1 FROM overview_stats WHERE id=1").fetchone() is None:
            recompute_overview(conn)
            conn.commit()
        row, live = _snapshot(conn)
        return _payload(row, live)
    finally:
        conn.close()


@router.post("/dashboard/recompute")
def recompute():
    """强制重算入口：按底表重算物化表。重算后若仍分叉会照旧标 stale。"""
    conn = connect()
    try:
        recompute_overview(conn)
        conn.commit()
        row, live = _snapshot(conn)
        return _payload(row, live)
    finally:
        conn.close()
