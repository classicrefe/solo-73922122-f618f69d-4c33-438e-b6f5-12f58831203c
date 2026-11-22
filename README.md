# 积水格入仓（巡查包 → SQLite → 框查/单格查询）

一个 Flask + SQLite 的小服务：导入巡查包 JSON，把「覆盖范围」和「现行积水格」一起落库，
并提供框查与单格查询网页。覆盖范围按矩形存储，不逐格展开；框查只读现行状态，不回放历史积水点。

## 快速启动

```bash
docker compose up --build
```

打开 <http://localhost:8000> 。数据落在命名卷 `flood-data` 的 `/data/flood.sqlite3`。
Compose 里设置了 `FLOOD_ALLOW_RESET=1`，网页上的「清空演示库」按钮才会生效。

本地（不用 Docker）：

```bash
python3 -m pip install -r requirements-dev.txt
FLOOD_ALLOW_RESET=1 python3 -m gunicorn --bind 127.0.0.1:8000 --workers 2 --threads 4 wsgi:app
# 或 python3 wsgi.py
```

## 包格式

```json
{
  "version": 7,
  "coverage": {"row_start": 0, "row_end": 50, "col_start": 0, "col_end": 20},
  "wet_cells": [[12, 4], [12, 5], [31, 2]]
}
```

- `version` 必须是正整数；`coverage` 四个字段都是整数，区间**左闭右开**（`row_end`/`col_end` 不含）。
- `wet_cells` 是 `[row, col]` 整数对，必须落在覆盖矩形内；重复坐标会被合并。
- 覆盖矩形内**未列出**的格子 = 已巡查、无积水。矩形外 = 未巡查。
- 未知字段会被拒绝（防止把拼错的 `wet_cells` 误读成「整片无积水」）。
- 上限：坐标 / 版号绝对值 ≤ 2,000,000,000（R*Tree 的 32 位范围），列出的积水点 ≤ 500,000 个。
  覆盖面积**不设上限**，也不展开。

## 语义

| 规则 | 实现 |
| --- | --- |
| 交叠处看最高版号 | 新包覆盖矩形内，旧版号（≤ 本包版号）的现行积水格先被清除，再写入本包列出的格；若某些格已被**更高**版号覆盖，它们的判定保留，不被旧包覆盖。 |
| 旧包晚到也能补新版没查过的地方 | 旧包只影响自己矩形内、且没有更高版号覆盖的区域；矩形外/新版已覆盖处不动。 |
| 没报积水点 = 可能已退水 | 包内 `wet_cells` 为空时正常入库，覆盖范围记为「已巡查无积水」，已清除该范围内的旧积水。响应里带 `warning` 提示这不是错误。 |
| 同版同内容重传不改变结果 | 对规范化后的内容（坐标排序去重、固定键序）取 SHA-256。摘要相同返回 `200 unchanged`，不写库。 |
| 同版异内容整包退回 | 摘要不同返回 `409 version_conflict`，整个事务回滚。 |
| 入库失败继续提供原结果 | 每次导入是单个 `BEGIN IMMEDIATE` 事务；任何异常（校验、冲突、SQLite 错误）都回滚，覆盖矩形与现行积水保持原样，服务继续响应旧数据。 |

覆盖与现行积水的存储：

- `coverage_rects`：**每包一行矩形**。SQLite 支持 R*Tree 时建虚拟表（有 `rtree_i32` 索引），
  不支持的构建自动退化为带索引的普通表，列名一致、SQL 共用。
- `current_wet`：**只存现行**积水格 `(row, col) → 生效版号`，没有历史表。
  因此框查是 `current_wet` 上的一次范围扫描，历史包再多也不会变慢，也不会重扫旧积水点。
- `packages`：版号、摘要、覆盖矩形副本、原始列出点数、入库时间，用于列表展示与幂等判断。

单格状态：`wet`（有积水，带生效版号）/ `dry`（已查无积水，带作出该判定的最高覆盖版号）/ `unsurveyed`（未巡查）。
注意：`dry` 的版号取「覆盖该格的最高版号」，这是产生当前结论的那一代巡查。

## 网页

1. **导入巡查包**：粘贴 JSON 或选择 `.json` 文件；六个示例按钮覆盖首次巡查 / 覆盖旧结果 /
   旧包晚到 / 同内容重传 / 同版异内容 / 清空。
2. **已入仓巡查包**：每个包的覆盖矩形、包内点数、现行点数、摘要、时间。
3. **框查现行积水格**：行列半开区间 + 返回上限；SVG 里覆盖矩形按矩形画（不逐格），
   红点为现行积水；表格列出结果，截断时提示。
4. **单格查询**：显示三种状态之一。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/packages` | 导入；支持原始 JSON 体或 `multipart/form-data` 的 `file` 字段。`201 imported` / `200 unchanged` / `400 invalid_package` / `409 version_conflict` |
| `GET` | `/api/packages` | 包列表、最高版号、现行积水总数 |
| `GET` | `/api/wet?row_start=&row_end=&col_start=&col_end=&limit=` | 框内现行积水格 + 相交的覆盖矩形；`limit` 默认 2000、最大 5000，超出置 `truncated: true` |
| `GET` | `/api/cell?row=&col=` | 单格 `wet` / `dry` / `unsurveyed` |
| `GET` | `/healthz` | 健康检查 |
| `POST` | `/api/reset` | 仅 `FLOOD_ALLOW_RESET=1` 时可用（演示用） |

错误响应统一 `{"error", "message", "details", "committed": false}`。

## 目录

```
app/
  __init__.py      应用工厂、路由、请求校验
  db.py            SQLite 连接 / schema / R*Tree 可选降级
  parsing.py       包解析与规范化、摘要
  repository.py    事务化入库、覆盖判定、框查、单格查询
  templates/index.html, static/{app.js,styles.css,samples/}
tests/             pytest：解析、入库语义、查询/API、规模与降级
scripts/demo_http.py 按演示顺序跑一遍（test client 或 FLOOD_DEMO_URL）
```

## 自测

```bash
python3 -m pytest -q          # 52 passed
python3 scripts/demo_http.py  # 演示流程逐步输出（用 Flask test client）
FLOOD_DEMO_URL=http://localhost:8000 python3 scripts/demo_http.py   # 打真实服务
```

本次实测（详见交付说明）：

| 检查 | 结果 |
| --- | --- |
| `pytest` | 52 passed |
| 演示流程（HTTP，容器等价布局 + gunicorn 2 worker） | 3 包 `201`、重传 `200`、异内容 `409`、三种单格状态正确 |
| 百万级覆盖（2000×2000 × 1000 万倍面积） | 导入 23.5 ms，`coverage_rects` 仍只有 1 行，库 24 KB |
| 框查延迟（101 代历史包之后） | 平均 16.8 ms / 次，只看 `current_wet` 主键范围扫描 |
| 并发导入 16 线程 × 40 包 | 全部 `201`，0 次 `database is locked`，最终归属版本 = 40 |
| 无 R*Tree 的 SQLite 构建 | 自动降级普通表，全部流程通过（`tests/test_scale.py`） |
| 前端（jsdom 挂真实服务） | 导入/冲突提示/退水入库/框查/单格查询均正确，无 JS 报错 |

## 已知未完成 / 取舍

- **并发写入是库级串行**：用 `BEGIN IMMEDIATE` + `busy_timeout` 保证一致性，导入会排队。
  已验证 16 并发 × 40 个包全部 `201`、无 `database is locked`，但吞吐不是目标。
- **`dry` 不存逐格状态**：靠「覆盖矩形 + 现行积水」推导。这是「覆盖范围不得逐格展开」的直接后果；
  代价是单格 `dry` 查询要查覆盖矩形索引（R*Tree/普通索引），而不是 O(1) 点查。
- **框查上限 5000**：大范围框查会截断（明确返回 `truncated`），未做分页/流式导出。
  覆盖矩形一次全返回（按矩形压缩，通常很少）。
- **没有鉴权、没有多用户/审计日志**；`/api/reset` 仅为演示。
- **没有前端自动化测试框架**：用 jsdom 做了冒烟（页面渲染、框查、单格查询、无 JS 报错），
  但未纳入 CI。
- **Docker 只在无 Docker 守护进程的环境验证过配置**（YAML/Dockerfile/依赖），
  实际 `docker compose up` 需在有 Docker 的机器上跑；本地用同一 gunicorn 命令等价验证。
- 版号不连续/跳跃是允许的（包是独立巡查批次），未做「版号必须 +1」的约束。
- 同一版号重传时只比较规范化内容，不比较 JSON 的原始字节（这是刻意的：键序/空白不同仍算同内容）。
