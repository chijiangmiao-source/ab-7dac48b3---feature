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

## 审计叶与 Merkle 山脉（MMR）

每份**首次完成**的帧裁决都在与窗口/回执**同一事务**内追加一片规范审计叶，叶内容（规范 JSON 后 SHA-256）绑定：

`链路 ID · receipt_id · counter · 载荷 SHA-256 · 裁决 · 裁决后最高扩展序号 · 全局叶序号`

- 命中原回执的重传**只返回原叶**（同一全局序号、同一叶哈希）；标识冲突（409）**不追加叶**
- 叶按全局提交顺序追加进 **Merkle 山脉**：仅追加、不改动历史；山脉的峰集合、峰装根（peak bagging）与全部内部节点都持久化在 SQLite，重启后从叶重放校验
- 全局叶序号在并发下**稠密且无重复**：序号、前序峰、根随同一次提交唯一确定（单一写锁 + `BEGIN IMMEDIATE`）
- 页面/接口可读取某回执的**全局序号、叶哈希、当前根与最小包含路径**（叶→所属峰的兄弟节点 + 其余各峰）；路径始终按**最新峰集合**给出，因此新裁决加入后仍可用最新根独立验证旧叶
- 启动时重放整座山脉并核对：每个已保存裁决恰好有一叶、每片叶都指向存在的裁决、同一回执不会有两叶、节点/峰/根与叶一致——发现任何不一致即拒绝启动
- 哈希域分离：叶 `SHA256("DSRX1\x00LEAF\x00" ‖ canonical)`，内部节点 `SHA256("DSRX1\x00NODE\x00" ‖ left32 ‖ right32)`，空根为常量；页面用浏览器 WebCrypto 独立重算，冒烟脚本用独立的 Python 实现重算


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

`verify` 服务等待 `web` 健康后执行：构建检查（compileall）→ 全部代码测试（回绕/过期/重复/并列/回执/重启/并发/MMR 审计/故障回滚）→ 对运行中的服务做 HTTP 冒烟（含独立重算包含路径），然后退出：

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
| `GET` | `/` | 单页操作界面（含浏览器内独立审计验证） |
| `POST` | `/api/links` | `{"name":"..."}` 创建链路 |
| `GET` | `/api/links` | 列出链路及窗口状态 |
| `GET` | `/api/links/{id}` | 当前最高扩展序号、位图、最近 64 个已接受位置 |
| `POST` | `/api/links/{id}/frames` | 提交到达帧 |
| `GET` | `/api/links/{id}/receipts/{receipt_id}` | 该回执的审计叶：全局序号、叶哈希、当前根、最小包含路径与峰集合 |
| `GET` | `/api/audit/root` | 当前全局叶总数与峰装根 |

提交帧请求体：

```json
{ "counter": 4294967295, "receipt_id": "tx-0001", "payload": {"sensor": "temp"} }
```

裁决响应：

```json
{
  "status": "accepted",          // accepted | duplicate | expired | rejected
  "retransmit": false,           // true = 命中回执记录、回放首次裁决（不追加叶）
  "counter": 4294967295,
  "extended": 4294967295,        // 解析出的 64 位扩展序号
  "highest": 4294967295,         // 当前最高扩展序号
  "recent": [4294967295],        // 最近 64 个已接受位置（新→旧）
  "leaf_seq": 12,                // 该回执审计叶的全局序号（重传时为原叶序号）
  "leaf_count": 12,              // 当前全局叶总数
  "root": "9f86…"                // 提交后的当前峰装根
}
```

审计叶响应（`GET /api/links/{id}/receipts/{receipt_id}`）：

```json
{
  "seq": 3, "leaf_count": 11,
  "link_id": "…", "receipt_id": "tx-0001",
  "counter": 4294967295, "payload_hash": "…",
  "status": "accepted", "highest": 4294967295,
  "leaf_hash": "…",
  "root": "…",                    // 始终为读取时的最新根
  "steps": [["right", 0, "…"]],  // 叶→峰的最小路径：[left|right, 兄弟高度, 兄弟哈希]
  "peaks": [{"height": 3, "hash": "…"}]  // 当前峰集合（高→低）
}
```

值班员可用任意语言按「叶哈希 → 沿 steps 折叠 → 在 peaks 中定位所属峰 → 峰装根」独立重算并与 `root` 比对。

## 本地开发（无需 Docker，纯标准库）

```bash
python3 -m unittest discover -s tests -v          # 单元 + 持久化 + HTTP 测试
DB_PATH=/tmp/rx.db PORT=8080 python3 -m app.server
python3 scripts/smoke.py http://127.0.0.1:8080    # 端到端冒烟
```

## 目录结构

```
app/window.py        # 纯函数：纪元候选、距离并列、滑动窗口位图裁决
app/merkle.py        # 纯函数：规范叶哈希、MMR 追加/峰集合/峰装根、最小包含路径与独立验证
app/db.py            # SQLite：窗口状态 + 回执记录 + 审计叶/MMR 节点/峰/根，单事务一致推导
app/server.py        # 标准库 HTTP 服务与 JSON API
app/static/index.html# 单页界面 + 浏览器 WebCrypto 独立验证面板
tests/               # 算法 / 持久化 / 回执 / 审计 / 故障回滚 / 重启 / 并发 / HTTP 测试
scripts/smoke.py     # 针对运行服务的端到端冒烟（含独立哈希实现的 MMR 验证）
scripts/verify.sh    # Compose verify 入口（退出码即验收结论）
```
