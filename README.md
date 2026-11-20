# Tridock（到货三联台）

骨架：到货预报装载 → 实收入账 → 供应商回执装载 → 三方键对齐与差额处置 → 到货日结案封印。

- 前端 http://127.0.0.1:5900
- API http://127.0.0.1:10900

## 总览一致性架构（全局唯一：物化表为准）

总览（sealed 批次数 / 未决差额 / 可写日 / 缺腿提示 / 写闸）**只读 `overview_stats` 物化表**，不在请求时散装拼数，更不允许只改前端假数。契约：

- **写路径联动**：装载三腿、跑三联对齐、结案、改差额命运、切到货日，每个写路径都在**同一事务**内调用 `services/overview.recompute_overview` 再一起提交 —— 底表与物化原子生效，并发 sealed 与总览刷新只见全旧或全新，不会半截显示旧可写日。
- **读时互证**：`GET /api/dashboard` 展示值只取物化表，同时在**同一读快照**内按底表实时聚合做互证。三段链任一断裂即 `stale: true` 并附 `divergence` 明细（物化 payload ↔ 物化指纹 ↔ 底表实时指纹），前端以红色横幅展示，绝不假绿。
- **强制重算入口**：`POST /api/dashboard/recompute`（总览页「强制重算」按钮）按底表重建物化；人为改脏物化后重算即恢复。
- 数据库启 WAL + busy_timeout，读写不互堵。

核对测试：`backend/tests/test_overview_consistency.py`（sealed+1 / 改脏重算恢复 / 新装载同步 / 并发不半截 / 写路径互证 / 底表漂移暴露）。

## 启动

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 10900 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5900
```
