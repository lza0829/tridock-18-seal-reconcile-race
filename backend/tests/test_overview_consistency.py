"""总览一致性（架构 A：overview_stats 物化表为准）核对清单。

覆盖：
1. sealed 后总览结案数 +1（物化表本身也 +1）
2. 人为改脏物化 → 读时互证标 stale（不假绿）→ 强制重算恢复
3. 新装载后可写日 / 缺腿提示同步变化
4. 并发 sealed 与总览刷新：不得半截显示旧可写日，不得假 stale
5. 每个写路径（装载/对齐/命运/结案/切日）后物化与底表互证一致
6. 直接改底表绕过写路径 → 总览仍展示物化快照且分叉可见
"""
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import DB_PATH
from app.db import connect
from app.main import app
from app.routers.dashboard import dashboard as dashboard_fn
from app.routers.reconcile import RunIn, SealIn
from app.routers.reconcile import run_reconcile as run_route
from app.routers.reconcile import seal as seal_route
from app.routers.settings import DayIn
from app.routers.settings import set_dock_day as set_day_route
from app.seed import seed_if_empty
from app.services.overview import compute_metrics

client = TestClient(app)
DAY = "2026-11-18"


def _reset_db():
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    seed_if_empty()


@pytest.fixture(autouse=True)
def fresh_db():
    _reset_db()


def _dash():
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    return r.json()


def _materialized_payload():
    conn = connect()
    try:
        row = conn.execute("SELECT payload FROM overview_stats WHERE id=1").fetchone()
        assert row, "overview_stats 物化行不存在"
        return json.loads(row["payload"])
    finally:
        conn.close()


def _live_metrics():
    conn = connect()
    try:
        return compute_metrics(conn)
    finally:
        conn.close()


def _assert_matches_base_tables(d):
    """底表互证：总览每个指标都必须等于按底表实时聚合的值。"""
    live = _live_metrics()
    for key, val in live.items():
        assert d[key] == val, f"字段 {key}: 总览={d.get(key)!r} 底表={val!r}"


def _dispose_all_pending(day):
    for v in client.get(f"/api/reconcile/variances?dock_day={day}").json():
        if v["fate"] == "pending":
            r = client.post(f"/api/reconcile/variances/{v['id']}/fate", json={"fate": "accept_short"})
            assert r.status_code == 200


# 1. sealed 后总览结案数 +1
def test_seal_increments_overview_sealed_count():
    d0 = _dash()
    assert d0["stale"] is False
    assert d0["sealed_count"] == 0 and d0["writable_today"] is True

    assert client.post("/api/reconcile/run", json={"dock_day": DAY}).status_code == 200
    _dispose_all_pending(DAY)
    assert client.post("/api/reconcile/seal", json={"dock_day": DAY}).status_code == 200

    d1 = _dash()
    assert d1["sealed_count"] == d0["sealed_count"] + 1
    assert d1["sealed_today"] is True and d1["writable_today"] is False
    assert d1["pending_variance"] == 0
    assert d1["stale"] is False
    # 物化表本身也 +1（不是只靠实时聚合撑门面）
    assert _materialized_payload()["sealed_count"] == d1["sealed_count"]
    _assert_matches_base_tables(d1)


# 2. 人为改脏物化 → stale 可见 → 重算恢复
def test_dirty_materialized_marked_stale_and_recompute_restores():
    d0 = _dash()
    assert d0["stale"] is False

    conn = connect()
    payload = json.loads(conn.execute("SELECT payload FROM overview_stats WHERE id=1").fetchone()["payload"])
    payload["sealed_count"] = 999
    payload["pending_variance"] = 777
    conn.execute("UPDATE overview_stats SET payload=? WHERE id=1",
                 (json.dumps(payload, ensure_ascii=False, sort_keys=True),))
    conn.commit()
    conn.close()

    d1 = _dash()
    assert d1["stale"] is True, "改脏物化后必须标 stale，不得显示完全正常"
    fields = {x["field"] for x in d1["divergence"]}
    assert {"sealed_count", "pending_variance"} <= fields
    # 读总览只读物化表：展示值是脏快照值，同时分叉明细给出底表真值
    assert d1["sealed_count"] == 999
    live_sealed = [x for x in d1["divergence"] if x["field"] == "sealed_count"][0]["live"]
    assert live_sealed == d0["sealed_count"]

    d2 = client.post("/api/dashboard/recompute").json()
    assert d2["stale"] is False
    assert d2["sealed_count"] == d0["sealed_count"]
    assert d2["pending_variance"] == d0["pending_variance"]
    assert _dash()["stale"] is False


# 3. 新装载后可写日 / 缺腿提示同步变化
def test_new_load_updates_writable_days_and_missing_legs():
    d0 = _dash()
    assert d0["missing_receive"] == 0 and d0["missing_confirm"] == 0

    # 只装预报、不装另两腿 → 缺实收/缺回执同步 +1
    r = client.post("/api/expects", json={
        "doc_no": "EX-NEW-1", "supplier_code": "SUP-A", "sku": "SKU-新品",
        "dock_day": DAY, "qty": 5,
    })
    assert r.status_code == 200
    d1 = _dash()
    assert d1["expect_count"] == d0["expect_count"] + 1
    assert d1["missing_receive"] == 1 and d1["missing_confirm"] == 1
    assert d1["missing_expect"] == 0
    assert d1["stale"] is False

    # 新到货日装载 → 可写日列表同步出现该日且可写
    new_day = "2026-11-19"
    r = client.post("/api/receives", json={
        "receive_no": "RC-NEW-1", "supplier_code": "SUP-B", "sku": "SKU-箱-大",
        "dock_day": new_day, "qty": 3,
    })
    assert r.status_code == 200
    d2 = _dash()
    entry = {x["dock_day"]: x for x in d2["days"]}[new_day]
    assert entry["writable"] is True and entry["sealed"] is False
    assert entry["receive_count"] == 1
    assert d2["stale"] is False
    _assert_matches_base_tables(d2)


# 4. 并发 sealed 与总览刷新：不得半截显示旧可写日
def test_concurrent_seal_and_dashboard_refresh_never_half_shown():
    days = [f"2026-11-2{i}" for i in range(5)]
    violations = []
    done = threading.Event()

    def check(d):
        # 每个快照都必须内部自洽：要么全旧、要么全新，不得半截
        if d["stale"] is not False:
            return f"并发下出现 stale: {d['divergence']}"
        if d["writable_today"] == d["sealed_today"]:
            return f"写闸自相矛盾: sealed={d['sealed_today']} writable={d['writable_today']}"
        today = {x["dock_day"]: x for x in d["days"]}.get(d["dock_day"])
        if today is None:
            return f"可写日列表缺当前日 {d['dock_day']}"
        if today["sealed"] != d["sealed_today"] or today["writable"] != d["writable_today"]:
            return f"半截状态: 顶栏写闸与可写日列表不一致 {d['dock_day']}"
        sealed_days = sum(1 for x in d["days"] if x["sealed"])
        if d["sealed_count"] != sealed_days:
            return f"结案数 {d['sealed_count']} 与已结案日数 {sealed_days} 分叉"
        for x in d["days"]:
            if x["writable"] == x["sealed"]:
                return f"日 {x['dock_day']} writable==sealed"
        if d["latest_batch"] and d["latest_batch"]["status"] == "sealed" and not d["sealed_today"]:
            return "批次已 sealed 但当日仍显示可写（旧可写日半截）"
        return None

    def reader():
        while not done.is_set():
            try:
                d = dashboard_fn()
            except Exception as e:  # 不允许 database locked 等任何错误
                violations.append(f"读取异常: {e}")
                return
            bad = check(d)
            if bad:
                violations.append(bad)
                return
            time.sleep(0.001)

    def writer():
        try:
            for day in days:
                set_day_route(DayIn(dock_day=day))
                run_route(RunIn(dock_day=day))
                seal_route(SealIn(dock_day=day))
        except Exception as e:
            violations.append(f"写路径异常: {e}")

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    writer()
    done.set()
    for t in threads:
        t.join()

    assert violations == [], f"并发下出现半截/假 stale: {violations[:3]}"
    final = _dash()
    assert final["sealed_count"] == 5
    assert final["dock_day"] == days[-1]
    assert final["writable_today"] is False
    _assert_matches_base_tables(final)


# 5. 每个写路径之后，物化都与底表互证一致
def test_every_write_path_keeps_overview_consistent():
    d = _dash()
    assert d["stale"] is False
    _assert_matches_base_tables(d)

    assert client.post("/api/expects", json={
        "doc_no": "EX-P1", "supplier_code": "SUP-A", "sku": "SKU-新1",
        "dock_day": DAY, "qty": 7}).status_code == 200
    _assert_matches_base_tables(_dash())

    assert client.post("/api/receives", json={
        "receive_no": "RC-P1", "supplier_code": "SUP-A", "sku": "SKU-新1",
        "dock_day": DAY, "qty": 7}).status_code == 200
    _assert_matches_base_tables(_dash())

    assert client.post("/api/confirms", json={
        "confirm_no": "CF-P1", "supplier_code": "SUP-A", "sku": "SKU-新1",
        "dock_day": DAY, "qty": 7}).status_code == 200
    d = _dash()
    _assert_matches_base_tables(d)
    assert d["missing_receive"] == 0 and d["missing_confirm"] == 0

    assert client.post("/api/reconcile/run", json={"dock_day": DAY}).status_code == 200
    d = _dash()
    _assert_matches_base_tables(d)
    assert d["open_count"] == 1 and d["pending_variance"] > 0

    pending = [v for v in client.get(f"/api/reconcile/variances?dock_day={DAY}").json()
               if v["fate"] == "pending"]
    assert client.post(f"/api/reconcile/variances/{pending[0]['id']}/fate",
                       json={"fate": "quarantine"}).status_code == 200
    d = _dash()
    _assert_matches_base_tables(d)
    assert d["pending_variance"] == len(pending) - 1

    _dispose_all_pending(DAY)
    assert client.post("/api/reconcile/seal", json={"dock_day": DAY}).status_code == 200
    d = _dash()
    _assert_matches_base_tables(d)
    assert d["sealed_count"] == 1 and d["writable_today"] is False

    assert client.post("/api/settings/dock-day", json={"dock_day": "2026-11-21"}).status_code == 200
    d = _dash()
    _assert_matches_base_tables(d)
    assert d["dock_day"] == "2026-11-21"
    assert d["expect_count"] == 0 and d["writable_today"] is True


# 6. 直接改底表绕过写路径 → 展示物化快照且分叉可见（读总览只读物化表）
def test_base_table_drift_is_exposed_as_stale_not_green():
    assert client.post("/api/reconcile/run", json={"dock_day": DAY}).status_code == 200
    d0 = _dash()
    assert d0["pending_variance"] > 0

    conn = connect()
    conn.execute("UPDATE variance_rows SET fate='quarantine' WHERE fate='pending'")
    conn.commit()
    conn.close()

    d1 = _dash()
    assert d1["stale"] is True, "底表被改而物化未重算时必须标 stale"
    assert d1["pending_variance"] == d0["pending_variance"], "展示值应仍是物化快照"
    live_pending = [x for x in d1["divergence"] if x["field"] == "pending_variance"][0]["live"]
    assert live_pending == 0

    d2 = client.post("/api/dashboard/recompute").json()
    assert d2["stale"] is False
    assert d2["pending_variance"] == 0
