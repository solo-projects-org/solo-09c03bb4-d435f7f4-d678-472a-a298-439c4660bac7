# 晶圆探针地图合并服务（Wafer Probe Map Merger）

把多台探针机上传的**局部测判网格**（含全片原点坐标与绕原点顺时针旋转角）拼成
一张**全片地图**，并给出每个格子的来源（机台、批次、局部坐标）。用于定位待复查裸片。

技术栈：FastAPI + SQLite（WAL），后台线程池做异步重算，自带 Swagger UI。

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 同一全片位置取**最高复测号**的测判 | `service._build_snapshot` 的 `ROW_NUMBER() ... ORDER BY retest_no DESC, batch.seq DESC` |
| 未测位置留空（稀疏列表不出现，`dense=true` 时为 `null`） | `service.get_map` |
| 相同位置 + 相同复测号 + **不同测判** → 整批 409 拒收，保留原地图 | `service._find_conflicts`（在写事务内比对） |
| 上传受理后旧地图立刻标记 `pending_update`，但仍可查询 | `accept_batch` 只推进 `accepted_seq`，不动 `map_cells` |
| 重算期间可继续受理其它机台上传 | 计算在请求之外；受理只取 `BEGIN IMMEDIATE` 短事务 |
| 迟到的旧重算不能覆盖新测判 | `_publish` 在写事务内校验 `accepted_seq == snapshot.seq` |
| 重算失败/重启后仍能查询待更新状态 | `wafers.last_error_seq` 持久化；失败时 `status=pending_update` + `recompute_failed=true` |
| 恢复后的地图与来源来自同一批已受理数据 | `map_cells` 与 `committed_seq` 同一事务写入；读接口用同一读快照 |

## 坐标约定

- 全片坐标 `(row, col)` 从 0 开始，行向下、列向右。
- 局部网格的 `(0,0)` 放在全片 `(origin_row, origin_col)`。
- 局部偏移 `(dr, dc)` **绕原点顺时针**旋转后叠加：
  - 90°：`(dr, dc) -> (dc, -dr)`
  - 180°：`(-dr, -dc)`
  - 270°：`(-dc, dr)`

例：原点 `(1,1)`、旋转 90°、局部 `(0,1)` → 全片 `(2,1)`；局部 `(1,0)` → 全片 `(1,0)`。

旋转超出全片边界的上传整批 422 拒收（不会只裁剪部分格子）。

## 接口一览

| Method | Path | 说明 |
| --- | --- | --- |
| POST | `/wafers/{wafer_id}/batches` | 上传一台机的局部网格，202 受理 |
| GET | `/wafers/{wafer_id}/map?dense=false` | 全片地图 + 每格来源 + 状态 |
| GET | `/wafers/{wafer_id}/map/cells/{row}/{col}` | 单格与来源 |
| GET | `/wafers/{wafer_id}/status` | 受理/发布序号与待更新状态 |
| POST | `/wafers/{wafer_id}/recompute` | 手动触发重算（失败后重试用） |
| GET | `/wafers/{wafer_id}/batches` | 已受理批次列表 |
| GET | `/wafers` | 全部晶圆与待更新标记 |
| GET | `/healthz` | 健康检查（Compose healthcheck 使用） |

错误统一为：

```json
{"error": {"code": "VERDICT_CONFLICT", "message": "...", "details": {...}}}
```

`status` 取值：`empty` / `pending_update` / `ready`。失败不改变 `pending_update`
语义，而是额外置 `recompute_failed=true` 并在 `last_recompute_error` 给出序号、
消息与时间。

### 上传示例

```bash
curl -s -X POST http://localhost:8000/wafers/LOT7/batches \
  -H 'content-type: application/json' -d '{
  "wafer_rows": 6, "wafer_cols": 6,
  "machine_id": "PROBE-1",
  "origin_row": 1, "origin_col": 1,
  "rotation_deg": 90,
  "local_grid": [
    [{"verdict": "PASS", "retest_no": 0}, {"verdict": "FAIL", "retest_no": 0}],
    [{"verdict": "PASS", "retest_no": 1}, null]
  ]
}'
```

`local_grid` 中 `null` 表示该局部位置本次未测：不会清除已有结果，也不会挡住它。

### 查询示例

```bash
curl -s 'http://localhost:8000/wafers/LOT7/map?dense=true' | python3 -m json.tool
curl -s  http://localhost:8000/wafers/LOT7/map/cells/1/1 | python3 -m json.tool
curl -s  http://localhost:8000/wafers/LOT7/status | python3 -m json.tool
```

`map.cells` 只列出已测格子；每格含 `retest_no` 与 `source{...}`。
`map.map_seq` 是这张地图对应的已受理序号；`accepted_seq` 是当前最新受理序号。

## 运行

### Docker Compose

```bash
docker compose up --build -d
# Swagger:  http://localhost:8000/docs
# OpenAPI:  http://localhost:8000/openapi.json
# 数据卷:   wafer-data -> /data/wafer.db（重启后保留）
docker compose logs -f
docker compose down          # 加 -v 才会删数据卷
```

### 本地（无 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WAFER_DB_PATH` | `data/wafer.db` | SQLite 路径 |
| `SCHEDULER_WORKERS` | `2` | 重算线程数 |
| `RECOMPUTE_DELAY_SECONDS` | `0` | 人为延迟，仅用于演示/测试交错 |

> 单进程部署（uvicorn 默认 1 worker）。若用多进程，请勿同时起多个进程指向同一个
> SQLite 做“各自调度”；正确性仍由序号闸门保证，但会重复计算。

## 测试

```bash
python3 -m pytest tests -q
```

覆盖：四种旋转与旋转复合、最高复测号优先、空位、跨机台/跨旋转同位置冲突、
批内重复、越界拒收、尺寸不一致、受理与发布交错、迟到快照拒发、失败后恢复、
重启后待更新状态、四机台拼片、路径校验。

## 已知限制

- 单机 SQLite；多实例横向扩容未实现（需要换 Postgres 或引入分布式锁）。
- 没有鉴权 / 多租户。
- 地图查询一次性返回全部已测格子；`dense=true` 上限 25 万格，超过返回 413。
- 上传请求体是 JSON，未做流式解析；单批上限 25 万格。
