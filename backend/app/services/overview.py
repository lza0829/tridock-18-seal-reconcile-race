"""总览物化（架构 A：以 overview_stats 物化表为总览唯一读源）。

契约：
- 每个改动底表的写路径（装载三腿 / 跑对齐 / 结案 / 改差额命运 / 切到货日）
  必须在**同一事务**内调用 recompute_overview，再一起 commit —— 物化与底表原子生效。
- 读总览只读物化表；同时按底表实时聚合做互证，指纹不一致即 stale，
  由调用方（dashboard 路由）把分叉暴露出来，不得显示完全正常。
- 强制重算入口 = 再跑一次 recompute_overview 并提交。
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from app.services.gate import is_sealed


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _count(conn: sqlite3.Connection, table: str, dock_day: str) -> int:
    return conn.execute(
        f"SELECT COUNT(*) c FROM {table} WHERE dock_day=?", (dock_day,)
    ).fetchone()["c"]


def _leg_keys(conn: sqlite3.Connection, table: str, dock_day: str) -> set[tuple[str, str]]:
    rows = conn.execute(
        f"SELECT DISTINCT supplier_code, sku FROM {table} WHERE dock_day=?", (dock_day,)
    ).fetchall()
    return {(r["supplier_code"], r["sku"]) for r in rows}


def _day_entry(conn: sqlite3.Connection, dock_day: str) -> dict:
    sealed = is_sealed(conn, dock_day)
    return {
        "dock_day": dock_day,
        "expect_count": _count(conn, "expect_lines", dock_day),
        "receive_count": _count(conn, "receive_lines", dock_day),
        "confirm_count": _count(conn, "confirm_lines", dock_day),
        "sealed": sealed,
        "writable": not sealed,
    }


def compute_metrics(conn: sqlite3.Connection) -> dict:
    """按底表（三腿正式表 / reconcile_batches / 写闸）实时聚合总览指标。纯读。"""
    row = conn.execute("SELECT value FROM settings WHERE key='dock_day'").fetchone()
    day = row["value"] if row else ""

    expect_count = _count(conn, "expect_lines", day) if day else 0
    receive_count = _count(conn, "receive_lines", day) if day else 0
    confirm_count = _count(conn, "confirm_lines", day) if day else 0

    sealed_today = is_sealed(conn, day) if day else False
    sealed_count = conn.execute(
        "SELECT COUNT(*) c FROM reconcile_batches WHERE status='sealed'"
    ).fetchone()["c"]
    open_count = conn.execute(
        "SELECT COUNT(*) c FROM reconcile_batches WHERE status='open'"
    ).fetchone()["c"]
    pending = conn.execute(
        "SELECT COUNT(*) c FROM variance_rows WHERE fate='pending'"
    ).fetchone()["c"]

    # 缺腿提示（当前到货日）：三腿键集合并集里，各自缺哪一腿
    if day:
        ek = _leg_keys(conn, "expect_lines", day)
        rk = _leg_keys(conn, "receive_lines", day)
        ck = _leg_keys(conn, "confirm_lines", day)
        allk = ek | rk | ck
        missing_expect = len(allk - ek)
        missing_receive = len(allk - rk)
        missing_confirm = len(allk - ck)
    else:
        missing_expect = missing_receive = missing_confirm = 0

    # 可写日：三腿表与批表出现过的所有到货日 + 当前设置日
    day_rows = conn.execute(
        """
        SELECT d FROM (
          SELECT DISTINCT dock_day AS d FROM expect_lines
          UNION SELECT DISTINCT dock_day FROM receive_lines
          UNION SELECT DISTINCT dock_day FROM confirm_lines
          UNION SELECT DISTINCT dock_day FROM reconcile_batches
        )
        """
    ).fetchall()
    days = [_day_entry(conn, r["d"]) for r in day_rows]
    if day and all(x["dock_day"] != day for x in days):
        days.append(_day_entry(conn, day))
    days.sort(key=lambda x: x["dock_day"], reverse=True)

    batch = None
    if day:
        b = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? ORDER BY id DESC LIMIT 1",
            (day,),
        ).fetchone()
        if b:
            batch = {
                "id": b["id"],
                "batch_no": b["batch_no"],
                "status": b["status"],
                "matched_count": b["matched_count"],
                "variance_count": b["variance_count"],
                "sealed_at": b["sealed_at"],
            }

    return {
        "dock_day": day,
        "expect_count": expect_count,
        "receive_count": receive_count,
        "confirm_count": confirm_count,
        "sealed_today": sealed_today,
        "writable_today": not sealed_today,
        "sealed_count": sealed_count,
        "open_count": open_count,
        "pending_variance": pending,
        "missing_expect": missing_expect,
        "missing_receive": missing_receive,
        "missing_confirm": missing_confirm,
        "days": days,
        "latest_batch": batch,
    }


def fingerprint(metrics: dict) -> str:
    canon = json.dumps(metrics, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def recompute_overview(conn: sqlite3.Connection) -> dict:
    """按底表重算并写入 overview_stats。

    不 commit：调用方必须把本调用包进自己写路径的同一事务里一起提交，
    保证底表改动与总览物化原子生效（并发读者只见全旧或全新）。
    """
    metrics = compute_metrics(conn)
    conn.execute(
        """INSERT INTO overview_stats(id, payload, fingerprint, computed_at)
           VALUES (1, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             payload=excluded.payload,
             fingerprint=excluded.fingerprint,
             computed_at=excluded.computed_at""",
        (
            json.dumps(metrics, ensure_ascii=False, sort_keys=True),
            fingerprint(metrics),
            _now(),
        ),
    )
    return metrics


def diff_metrics(materialized: dict, live: dict) -> list[dict]:
    """逐字段对比物化值与底表实时值，给出分叉明细。"""
    out = []
    for key in sorted(set(materialized) | set(live)):
        m_val = materialized.get(key)
        l_val = live.get(key)
        if m_val != l_val:
            out.append({"field": key, "materialized": m_val, "live": l_val})
    return out
