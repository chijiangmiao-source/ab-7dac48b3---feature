# 深空地面站 · 32 位帧计数滑动窗口接收器

在重启、乱序投递与 32 位计数器回绕之后，保证同一链路上的旧帧绝不会被再次接受。
提供浏览器页面、HTTP 接口、健康检查，以及以退出码报告结果的 Compose `verify` 验收服务。

## 核心算法

线路上的计数是 32 位无符号整数（`0 .. 2³²−1`），到达时无法直接知道它经历了第几次回绕（纪元 / epoch）。
内部以 **64 位扩展序号** 维护状态，收到帧时在当前最高序号所在纪元的相邻三个候选中选择**距当前最高值最近**者：

```
candidate(k) = counter + k·2³²,   k ∈ (epoch−1, epoch, epoch+1)
```

- **距离并列**：两个候选恰好相距 `2³¹`，无法定位纪元 → `rejected`（拒绝，状态不变）
- **首次帧**：直接以 counter 初始化窗口（epoch 0），位图位置 0
- **更新的序号**：位图按差值左移，位置 0 置位（大跨度跳跃会使旧记录自然滑出）
- **窗口内更旧的序号**：仅置位，最高值不变
- **位图已置位** → `duplicate`（重复）
- **偏移 ≥ 64**（落在 `highest−63 .. highest` 窗口之外）→ `expired`（过期）

位图与最高扩展序号持久化在 SQLite 中；重启后从同一行状态继续推导，回绕前后的乱序帧每帧只接受一次。

## 稳定回执标识

- 相同 `receipt_id` + 相同 `counter` + 完全相同 `payload` 的重传：**返回首次裁决**（accepted/duplicate/expired/rejected 原样回放）
- `receipt_id` 全局唯一；复用标识但改变**链路、计数或载荷** → HTTP `409` 拒绝，且不改动窗口
- 每次到达都在**同一个 SQLite `BEGIN IMMEDIATE` 事务**内读取并写入窗口状态与回执记录，配合写锁使并发到达串行化，窗口与回执不可能出现不一致

## 运行（Docker Compose）

```bash
# 默认宿主端口 8080
docker compose up --build

# 可配置宿主端口
HOST_PORT=9090 docker compose up --build
```

- 页面：http://localhost:8080/
- 健康检查：http://localhost:8080/healthz
- 数据持久化在命名卷 `receiver-data`（容器内 `/data/receiver.db`）

## 验收（verify 服务）

`verify` 服务等待 `web` 健康后执行：构建检查（compileall）→ 全部代码测试（回绕/过期/重复/并列/回执/重启/并发）→ 对运行中的服务做 HTTP 冒烟，然后退出：

```bash
docker compose up --build --abort-on-container-exit verify
# 或
docker compose run --rm verify
echo $?   # 0 = 验收通过，非 0 = 失败
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | `200 {"status":"ok"}` |
| `GET` | `/` | 单页操作界面 |
| `POST` | `/api/links` | `{"name":"..."}` 创建链路 |
| `GET` | `/api/links` | 列出链路及窗口状态 |
| `GET` | `/api/links/{id}` | 当前最高扩展序号、位图、最近 64 个已接受位置 |
| `POST` | `/api/links/{id}/frames` | 提交到达帧 |

提交帧请求体：

```json
{ "counter": 4294967295, "receipt_id": "tx-0001", "payload": {"sensor": "temp"} }
```

裁决响应：

```json
{
  "status": "accepted",          // accepted | duplicate | expired | rejected
  "retransmit": false,           // true = 命中回执记录、回放首次裁决
  "counter": 4294967295,
  "extended": 4294967295,        // 解析出的 64 位扩展序号
  "highest": 4294967295,         // 当前最高扩展序号
  "recent": [4294967295]         // 最近 64 个已接受位置（新→旧）
}
```

## 本地开发（无需 Docker，纯标准库）

```bash
python3 -m unittest discover -s tests -v          # 单元 + 持久化 + HTTP 测试
DB_PATH=/tmp/rx.db PORT=8080 python3 -m app.server
python3 scripts/smoke.py http://127.0.0.1:8080    # 端到端冒烟
```

## 目录结构

```
app/window.py        # 纯函数：纪元候选、距离并列、滑动窗口位图裁决
app/db.py            # SQLite：窗口状态 + 回执记录，单事务一致推导
app/server.py        # 标准库 HTTP 服务与 JSON API
app/static/index.html# 单页界面
tests/               # 算法 / 持久化 / 回执 / 重启 / 并发 / HTTP 测试
scripts/smoke.py     # 针对运行服务的端到端冒烟
scripts/verify.sh    # Compose verify 入口（退出码即验收结论）
```
