# Sandbox OpenCode 代理服务 — 接口文档

Base URL 默认：`http://127.0.0.1:5000`（以 `.env` 中 `FLASK_HOST` / `FLASK_PORT` 为准）

## 通用说明

### ID 语义

| 概念 | 说明 |
|------|------|
| `sandbox_id` | APIG 返回的沙箱实例 ID（Docker 模式为 `docker:{uuid}`）；由建环境接口 `ready` 下发，后续提问带此 ID |
| `opencode_session_id` | OpenCode 对话 ID（如 `ses_…`）；chat/query 的 `done` / 任务结果会回传；可选传入以续聊；也可用列表/详情接口查询 |

- 任务状态与 sandbox 绑定映射均在**进程内存**中，服务重启后丢失
- gunicorn 请使用 `-w 1`，避免多 worker 导致任务/沙箱映射不一致
- **推荐顺序**：先 `POST /api/sandbox/session` 取得 `sandbox_id` → chat/query → 从 `done` 取 `opencode_session_id` 续聊；可用 `/api/sessions` 列历史、`/api/sessions/detail` 看详情

### 公共错误

请求体校验失败时返回 `400`：

```json
{"error": "query is required and must be a non-empty string"}
```

或：

```json
{"error": "sandbox_id is required and must be a non-empty string"}
```

---

## 1. 健康检查

### `GET /health`

检查服务是否存活；可选探测沙箱连通性。

#### Query 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `probe_sandbox` | string | 否 | 传 `1` / `true` / `yes` 时额外探测沙箱 |

#### 成功响应 `200`

```json
{"status": "ok"}
```

带探测且沙箱正常：

```json
{
  "status": "ok",
  "sandbox": {
    "ok": true,
    "detail": "reachable home_dir=/home/gem"
  }
}
```

#### 沙箱不可达 `503`

```json
{
  "status": "ok",
  "sandbox": {
    "ok": false,
    "detail": "..."
  }
}
```

#### 示例

```bash
curl http://127.0.0.1:5000/health
curl 'http://127.0.0.1:5000/health?probe_sandbox=1'
```

---

## 2. 创建沙箱环境（SSE）

### `POST /api/sandbox/session`

创建沙箱：APIG 创建（含双 NAS `x-mounts`）、拷贝 OpenCode 离线包、可选下载 skill。过程以 SSE `status` 推送；**`ready` 返回 APIG 的 `sandboxId`**（Docker 模式为 `docker:{uuid}`），客户端无需也不应自行传入。

#### Headers

| Header | 值 |
|--------|-----|
| `Content-Type` | `application/json` |
| `Accept`（建议） | `text/event-stream` |

#### 请求体

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sub_path` | string | 是 | 用户数据 NAS 的 `subPath`，用于按用户隔离（也可用 camelCase：`subPath`） |
| `skill_file` | string | 否 | 可直接 HTTP 下载的 skill 文件或压缩包 URL；多个用英文逗号分隔（如预签名 S3 链接） |

`sub_path` 写入用户数据 NAS 挂载的 `subPath`。沙箱 `templateId`、用户数据 `workspaceId`/`mountPath`、以及 OpenCode 离线包 NAS 均使用服务端配置。

```json
{
  "sub_path": "users/u-1001",
  "skill_file": "https://example.com/skills/a.zip,https://example.com/skills/b.zip"
}
```

#### 响应

- HTTP `200`
- `Content-Type: text/event-stream`
- 每条 SSE 事件格式：`data: <JSON>\n\n`

#### SSE 事件类型

| `type` | 字段 | 说明 |
|--------|------|------|
| `status` | `stage`, `message` | 环境准备进度节点 |
| `ready` | `sandbox_id` | 环境就绪，客户端应保存该 `sandbox_id` |
| `error` | `message` | 失败 |

#### `status.stage` 约定

| stage | message（示例） |
|-------|-----------------|
| `request_received` | 接收到用户请求 |
| `checking_sandbox` | 检查沙箱状态 |
| `creating_sandbox` | 拉起沙箱 |
| `init_opencode` | 初始化 opencode 配置 |
| `init_skill` | 初始化 skill 配置（或跳过） |
| `sandbox_ready` | 沙箱初始化完成 |

示例流：

```
data: {"type":"status","stage":"request_received","message":"接收到用户请求"}

data: {"type":"status","stage":"checking_sandbox","message":"检查沙箱状态"}

data: {"type":"status","stage":"creating_sandbox","message":"拉起沙箱"}

data: {"type":"status","stage":"init_opencode","message":"初始化 opencode 配置"}

data: {"type":"status","stage":"init_skill","message":"跳过初始化 skill 配置（未提供 skill_file）"}

data: {"type":"status","stage":"sandbox_ready","message":"沙箱初始化完成"}

data: {"type":"ready","sandbox_id":"a1b2c3d4e5f6..."}
```

客户端应保存 `ready.sandbox_id`，供后续 `/api/chat`、`/api/query` 使用。

#### 示例

```bash
curl -N -X POST http://127.0.0.1:5000/api/sandbox/session \
  -H 'Content-Type: application/json' \
  -d '{"sub_path":"users/u-1001"}'
```

---

## 3. 查询沙箱状态

### `GET /api/sandbox/status`

根据 `sandbox_id` 调用 APIG `GET /livefunction/sandboxes/{sandboxId}`，返回沙箱实例信息与状态。

#### Query 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sandbox_id` | string | 是 | APIG 沙箱实例 ID（不支持 `docker:` 本地模式） |

#### 成功响应 `200`

```json
{
  "sandbox_id": "a1b2c3d4e5f6...",
  "template_id": "tpl-xxxx",
  "timeout": 900,
  "cpu": 2,
  "memory": 4096,
  "status": "running",
  "start_at": "2026-10-15T09:00:00Z",
  "ready_time": 1200,
  "create_client": "10.0.0.1",
  "x_mounts": []
}
```

`status` 取值：`starting` / `running` / `stopped` / `error`。

#### 错误

| HTTP | 说明 |
|------|------|
| `400` | 缺少或非法 `sandbox_id` |
| `404` | 沙箱实例不存在（APIG 业务码 404） |
| `502` | APIG 调用失败，或其他业务错误 |

```json
{"error": "沙箱实例不存在"}
```

#### 示例

```bash
curl 'http://127.0.0.1:5000/api/sandbox/status?sandbox_id=a1b2c3d4e5f6...'
```

---

## 4. 流式对话（推荐）

### `POST /api/chat`

同步建立 SSE 连接，在已创建环境的 `sandbox_id` 上对话并流式返回助手文本。

- **不传** `opencode_session_id`：开新 OpenCode 对话
- **传入** `opencode_session_id`：对该 OpenCode 会话续聊
- **不会自动创建沙箱**；若沙箱 不存在或已过期，返回 `error`，需先调用 `/api/sandbox/session`

#### Headers

| Header | 值 |
|--------|-----|
| `Content-Type` | `application/json` |
| `Accept`（建议） | `text/event-stream` |

#### 请求体

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sandbox_id` | string | 是 | 环境创建接口 `ready` 返回的沙箱 ID |
| `query` | string | 是 | 本轮用户问题，非空 |
| `opencode_session_id` | string | 否 | 指定 OpenCode 会话续聊；省略则新开 |
| `model` | string | 否 | `provider/model`；有则覆盖服务端 `OPENCODE_MODEL`；空串视为未传 |
| `file` | string[] | 否 | 附件 URL 数组（预签名 S3 等 http(s) 链接）；服务端在沙箱内下载后以 `opencode run -f` 传入；空数组视为未传 |

```json
{
  "sandbox_id": "a1b2c3d4e5f6...",
  "query": "用一句话介绍 Python"
}
```

指定模型：

```json
{
  "sandbox_id": "a1b2c3d4e5f6...",
  "query": "用一句话介绍 Python",
  "model": "bailian-token-plan/qwen3.6-plus"
}
```

带附件：

```json
{
  "sandbox_id": "a1b2c3d4e5f6...",
  "query": "总结这个文档",
  "file": [
    "https://bucket.s3.example.com/doc.pdf?X-Amz-Signature=...",
    "https://bucket.s3.example.com/img.png?X-Amz-Signature=..."
  ]
}
```

续聊示例：

```json
{
  "sandbox_id": "a1b2c3d4e5f6...",
  "opencode_session_id": "ses_xxx",
  "query": "再给一个代码示例"
}
```

#### 响应

- HTTP `200`
- `Content-Type: text/event-stream`
- 每条 SSE 事件格式：`data: <JSON>\n\n`

#### SSE 事件类型

| `type` | 字段 | 说明 |
|--------|------|------|
| `status` | `stage`, `message` | 提问路径进度（如检查沙箱、下载附件、正在处理） |
| `delta` | `text` | 助手增量文本 |
| `done` | `answer`, `opencode_session_id` | 本轮结束；`opencode_session_id` 供后续续聊 |
| `error` | `message` | 执行失败（含附件下载失败） |

提问路径常见 `stage`：`request_received`、`checking_sandbox`、`processing`；有 `file` 时在 `processing` 前出现 `downloading_attachments`。

示例流：

```
data: {"type":"status","stage":"request_received","message":"接收到用户请求"}

data: {"type":"status","stage":"checking_sandbox","message":"检查沙箱状态"}

data: {"type":"status","stage":"downloading_attachments","message":"正在下载附件（2 个）"}

data: {"type":"status","stage":"processing","message":"正在处理请求"}

data: {"type":"delta","text":"Python"}

data: {"type":"delta","text":" 是一种解释型语言。"}

data: {"type":"done","answer":"Python 是一种解释型语言。","opencode_session_id":"ses_xxx"}
```

沙箱未创建示例：

```
data: {"type":"error","message":"sandbox not found or expired; create via POST /api/sandbox/session first"}
```

#### 多轮示例

```bash
# 先创建环境，从 SSE ready 事件取出 sandbox_id（APIG sandboxId）
curl -N -X POST http://127.0.0.1:5000/api/sandbox/session \
  -H 'Content-Type: application/json' \
  -d '{"sub_path":"users/u-1001"}'
# 假设得到 sandbox_id=sbx-xxxx...

# 第一轮（新开），从 done 取出 opencode_session_id
curl -N -X POST http://127.0.0.1:5000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"sandbox_id":"sbx-xxxx...","query":"用一句话介绍 Python"}'

# 指定模型
curl -N -X POST http://127.0.0.1:5000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"sandbox_id":"sbx-xxxx...","query":"用一句话介绍 Python","model":"bailian-token-plan/qwen3.6-plus"}'

# 带附件（S3 预签名 URL）
curl -N -X POST http://127.0.0.1:5000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"sandbox_id":"sbx-xxxx...","query":"总结这个文档","file":["https://bucket.s3.example.com/doc.pdf"]}'

# 第二轮（显式传入 opencode_session_id 续聊）
curl -N -X POST http://127.0.0.1:5000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"sandbox_id":"sbx-xxxx...","opencode_session_id":"ses_xxx","query":"再给一个代码示例"}'
```

---

## 5. 异步提交查询（非流式）

### `POST /api/query`

提交任务后立即返回 `task_id`，后台执行；客户端轮询任务结果。同样要求 session 环境已创建。续聊语义与 `/api/chat` 相同。

#### Headers

| Header | 值 |
|--------|-----|
| `Content-Type` | `application/json` |

#### 请求体

与 `/api/chat` 相同：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sandbox_id` | string | 是 | 环境创建接口 `ready` 返回的沙箱 ID |
| `query` | string | 是 | 用户问题 |
| `opencode_session_id` | string | 否 | 指定 OpenCode 会话续聊；省略则新开 |
| `model` | string | 否 | `provider/model`；有则覆盖服务端 `OPENCODE_MODEL`；空串视为未传 |
| `file` | string[] | 否 | 附件 URL 数组（预签名 S3 等）；下载后以 `-f` 传给 opencode |

```json
{
  "sandbox_id": "a1b2c3d4e5f6...",
  "query": "用一句话介绍 Python"
}
```

#### 成功响应 `202`

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "sandbox_id": "a1b2c3d4e5f6...",
  "status": "pending"
}
```

若请求带了 `opencode_session_id`、`model` 或 `file`，`202` 响应会原样带回；最终以任务结果中的值为准。

#### 示例

```bash
curl -X POST http://127.0.0.1:5000/api/query \
  -H 'Content-Type: application/json' \
  -d '{"sandbox_id":"a1b2c3d4e5f6...","query":"用一句话介绍 Python"}'
```

---

## 6. 查询任务状态

### `GET /api/tasks/<task_id>`

#### 路径参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `task_id` | string | `POST /api/query` 返回的任务 ID |

#### 成功响应 `200`

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "query": "用一句话介绍 Python",
  "sandbox_id": "user-1",
  "opencode_session_id": "ses_xxx",
  "status": "succeeded",
  "answer": "Python 是一种解释型、通用的高级编程语言。",
  "error": null,
  "created_at": "2026-10-05T02:00:00+00:00",
  "finished_at": "2026-10-05T02:00:12+00:00",
  "current_stage": "processing",
  "stages": [
    {
      "stage": "request_received",
      "message": "接收到用户请求",
      "at": "2026-10-05T02:00:01+00:00"
    },
    {
      "stage": "checking_sandbox",
      "message": "检查沙箱状态",
      "at": "2026-10-05T02:00:01+00:00"
    },
    {
      "stage": "processing",
      "message": "正在处理请求",
      "at": "2026-10-05T02:00:02+00:00"
    }
  ]
}
```

成功时 `opencode_session_id` 为可续聊的 OpenCode 会话 ID。

#### `status` 枚举

| 值 | 说明 |
|----|------|
| `pending` | 已入队，未开始 |
| `running` | 执行中 |
| `succeeded` | 成功，`answer` 有值 |
| `failed` | 失败，`error` 有值 |

#### 未找到 `404`

```json
{"error": "task not found"}
```

#### 示例

```bash
curl http://127.0.0.1:5000/api/tasks/550e8400-e29b-41d4-a716-446655440000
```

#### 推荐轮询方式

1. `POST /api/query` 拿到 `task_id`
2. 每隔 1–2s 调用 `GET /api/tasks/<task_id>`
3. 可通过 `current_stage` / `stages` 观察提问进度
4. 直到 `status` 为 `succeeded` 或 `failed`

---

## 7. OpenCode 模型列表

### `GET /api/models`

在已绑定沙箱内执行 `opencode models <provider>`（环境变量依赖建环境时写入的 profile）。

#### Query 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sandbox_id` | string | 是 | 建环境返回的沙箱 ID |
| `provider` | string | 是 | Provider ID，用于过滤模型 |

#### 成功响应 `200`

```json
{
  "sandbox_id": "a1b2c3d4e5f6...",
  "provider": "bailian-token-plan",
  "models": [
    "bailian-token-plan/qwen3.6-plus",
    "bailian-token-plan/qwen3.6-flash"
  ]
}
```

`models` 为 CLI 按行输出的模型 ID 列表（空输出则为 `[]`）。

#### 错误

| HTTP | 说明 |
|------|------|
| `400` | 缺少 `sandbox_id` 或 `provider` |
| `404` | 沙箱不存在或已过期 |
| `502` | CLI 执行失败 |

#### 示例

```bash
curl 'http://127.0.0.1:5000/api/models?sandbox_id=a1b2c3d4e5f6...&provider=bailian-token-plan'
```

---

## 8. OpenCode Session 列表

### `GET /api/sessions`

在已绑定沙箱内执行 `opencode session list --format json`（环境变量依赖建环境时写入的 profile）。

#### Query 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sandbox_id` | string | 是 | 建环境返回的沙箱 ID |

#### 成功响应 `200`

```json
{
  "sandbox_id": "a1b2c3d4e5f6...",
  "sessions": []
}
```

`sessions` 为 CLI JSON 解析结果（结构随 OpenCode 版本而定）。

#### 错误

| HTTP | 说明 |
|------|------|
| `400` | 缺少 `sandbox_id` |
| `404` | 沙箱不存在或已过期 |
| `502` | CLI 执行失败或非 JSON |

#### 示例

```bash
curl 'http://127.0.0.1:5000/api/sessions?sandbox_id=a1b2c3d4e5f6...'
```

---

## 9. OpenCode Session 详情

### `GET /api/sessions/detail`

在已绑定沙箱内执行 `opencode export <opencode_session_id>`（环境变量依赖建环境时写入的 profile）。

#### Query 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sandbox_id` | string | 是 | 建环境返回的沙箱 ID |
| `opencode_session_id` | string | 是 | OpenCode 会话 ID |

#### 成功响应 `200`

```json
{
  "sandbox_id": "a1b2c3d4e5f6...",
  "opencode_session_id": "ses_xxx",
  "export": {}
}
```

`export`：CLI 输出能解析为 JSON 则为对象，否则为字符串。

#### 错误

与列表接口相同（`400` / `404` / `502`）；缺少 `opencode_session_id` 时亦为 `400`。

#### 示例

```bash
curl 'http://127.0.0.1:5000/api/sessions/detail?sandbox_id=a1b2c3d4e5f6...&opencode_session_id=ses_xxx'
```

---

## 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/sandbox/session` | SSE 创建/复用沙箱环境 |
| `GET` | `/api/sandbox/status` | 查询 APIG 沙箱实例状态 |
| `POST` | `/api/chat` | SSE 流式对话（可选 `opencode_session_id` / `model` / `file`） |
| `POST` | `/api/query` | 异步提交（非流式；可选 `model` / `file`） |
| `GET` | `/api/tasks/<task_id>` | 查询异步任务结果与进度 |
| `GET` | `/api/models` | OpenCode 指定 provider 的模型列表 |
| `GET` | `/api/sessions` | OpenCode 历史 session 列表 |
| `GET` | `/api/sessions/detail` | OpenCode session 聊天详情（export） |

## 调用建议

1. 先调 **`/api/sandbox/session`**，从 `ready` 事件拿到`sandbox_id`（APIG sandboxId）
2. 可用 **`/api/models?sandbox_id=&provider=`** 查模型，再在 chat/query 里传 `model`
3. 需要打字机效果 / 低延迟反馈：用 **`/api/chat`**；从 `done.opencode_session_id` 保存以便续聊
4. 只需最终完整答案、可接受轮询：用 **`/api/query` + `/api/tasks/<id>`**
5. 续聊时显式传 `opencode_session_id`；不传则开新对话
6. 用 **`/api/sessions`** 列历史，用 **`/api/sessions/detail`** 看详情后再选 ID 续聊
7. 沙箱过期后需重新创建环境；不要依赖进程重启后的 sandbox / 任务状态
