# Sandbox OpenCode 代理服务 — 接口文档

Base URL 默认：`http://127.0.0.1:5000`（以 `.env` 中 `FLASK_HOST` / `FLASK_PORT` 为准）

## 通用说明

### Session 语义

| 概念 | 说明 |
|------|------|
| `session_id` | 客户端传入的业务会话 ID。同一值复用同一沙箱实例，并续接同一 OpenCode 多轮对话 |
| OpenCode `sessionID` | 服务端从 `opencode run --format json` 事件中解析并缓存，客户端无需感知 |

- 任务状态与 session→沙箱映射均在**进程内存**中，服务重启后丢失
- gunicorn 请使用 `-w 1`，避免多 worker 导致任务/session 不一致

### 公共错误

请求体校验失败时返回 `400`：

```json
{"error": "query is required and must be a non-empty string"}
```

或：

```json
{"error": "session_id is required and must be a non-empty string"}
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

## 2. 流式对话（推荐）

### `POST /api/chat`

同步建立 SSE 连接，按 `session_id` 多轮对话并流式返回助手文本。

#### Headers

| Header | 值 |
|--------|-----|
| `Content-Type` | `application/json` |
| `Accept`（建议） | `text/event-stream` |

#### 请求体

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | 是 | 业务会话 ID，非空 |
| `query` | string | 是 | 本轮用户问题，非空 |

```json
{
  "session_id": "user-1",
  "query": "用一句话介绍 Python"
}
```

#### 响应

- HTTP `200`
- `Content-Type: text/event-stream`
- 每条 SSE 事件格式：`data: <JSON>\n\n`

#### SSE 事件类型

| `type` | 字段 | 说明 |
|--------|------|------|
| `delta` | `text` | 助手增量文本 |
| `done` | `answer` | 本轮结束，完整回复文本 |
| `error` | `message` | 执行失败 |

示例流：

```
data: {"type":"delta","text":"Python"}

data: {"type":"delta","text":" 是一种解释型语言。"}

data: {"type":"done","answer":"Python 是一种解释型语言。"}
```

失败示例：

```
data: {"type":"error","message":"SANDBOX_TEMPLATE_ID is required"}
```

#### 多轮示例

```bash
# 第一轮
curl -N -X POST http://127.0.0.1:5000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"user-1","query":"用一句话介绍 Python"}'

# 第二轮（同一 session_id，续聊）
curl -N -X POST http://127.0.0.1:5000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"user-1","query":"再给一个代码示例"}'
```

---

## 3. 异步提交查询（非流式）

### `POST /api/query`

提交任务后立即返回 `task_id`，后台执行；客户端轮询任务结果。同一 `session_id` 同样支持多轮。

#### Headers

| Header | 值 |
|--------|-----|
| `Content-Type` | `application/json` |

#### 请求体

与 `/api/chat` 相同：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | 是 | 业务会话 ID |
| `query` | string | 是 | 用户问题 |

```json
{
  "session_id": "user-1",
  "query": "用一句话介绍 Python"
}
```

#### 成功响应 `202`

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "session_id": "user-1",
  "status": "pending"
}
```

#### 示例

```bash
curl -X POST http://127.0.0.1:5000/api/query \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"user-1","query":"用一句话介绍 Python"}'
```

---

## 4. 查询任务状态

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
  "session_id": "user-1",
  "status": "succeeded",
  "answer": "Python 是一种解释型、通用的高级编程语言。",
  "error": null,
  "created_at": "2026-10-05T02:00:00+00:00",
  "finished_at": "2026-10-05T02:00:12+00:00"
}
```

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
3. 直到 `status` 为 `succeeded` 或 `failed`

---

## 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/chat` | SSE 流式多轮对话（推荐） |
| `POST` | `/api/query` | 异步提交（非流式） |
| `GET` | `/api/tasks/<task_id>` | 查询异步任务结果 |

## 调用建议

1. 需要打字机效果 / 低延迟反馈：用 **`/api/chat`**
2. 只需最终完整答案、可接受轮询：用 **`/api/query` + `/api/tasks/<id>`**
3. 多轮时客户端稳定复用同一个 `session_id`
4. 不要依赖进程重启后的 session / 任务状态
