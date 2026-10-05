# Flask + AIO Sandbox OpenCode 异步代理

Python 3.10 Flask 服务：按 `session_id` 复用第三方沙箱与 OpenCode 会话，支持多轮对话；主路径以 SSE 流式输出，并保留异步任务轮询。

## 架构

```
Client -> POST /api/chat (SSE) 或 POST /api/query (异步轮询)
       -> SessionSandboxManager（session_id -> sandboxId + opencode_session_id）
       -> APIG create/refresh 沙箱实例
       -> agent-sandbox / docker exec
       -> opencode run --format json [--session ...]
```

任务与 session 映射保存在进程内存中，**服务重启后会丢失**。

## 环境要求

- Python 3.10+（推荐 3.10）
- 远程沙箱容器内 `opencode` 可用
- 沙箱需配置可用模型凭证（启动时注入 `OPENCODE_API_KEY` + `OPENCODE_MODEL`，或写入 `~/.config/opencode/config.json` / `OPENCODE_JSON`）。未配置时任务会以 `failed` / SSE `error` 返回 provider 错误。

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env：填写 APIG / 模板 ID，或设置 SANDBOX_DOCKER_CONTAINER 做本地调试
python run.py
```

生产可用：

```bash
gunicorn -w 1 -b 0.0.0.0:5000 "run:app"
```

> 注意：任务存储与 session 映射在进程内，gunicorn 请使用 `-w 1`。

## 配置

| 变量 | 默认 | 说明 |
|------|------|------|
| `SANDBOX_BASE_URL` | `http://39.98.58.9:28080` | agent-sandbox SDK 地址 |
| `SANDBOX_API_KEY` | 空 | 可选；附加 `X-AIO-API-Key` / `Authorization` |
| `SANDBOX_APIG_ENDPOINT` | 空 | APIG 根地址，如 `https://apig.example.com` |
| `SANDBOX_APIG_HW_ID` | 空 | header `X-HW-ID`（创建/刷新与 SDK 调用） |
| `SANDBOX_APIG_HW_APPKEY` | 空 | header `X-HW-APPKEY` |
| `SANDBOX_TEMPLATE_ID` | 空 | 创建沙箱必填模板 ID |
| `SANDBOX_INSTANCE_TIMEOUT` | `900` | 创建时生命周期（秒），平台默认过期销毁 900s |
| `SANDBOX_REFRESH_DURATION` | `900` | 有任务且快到期时续期时长（秒，硬上限 1800） |
| `SANDBOX_KEEPALIVE_MARGIN_SECONDS` | `120` | 剩余 TTL 低于该值才考虑 refresh |
| `SANDBOX_KEEPALIVE_INTERVAL_SECONDS` | `15` | 保活扫描间隔 |
| `SANDBOX_READY_TIMEOUT_SECONDS` | `15` | 创建返回 starting 时的短等待 |
| `SANDBOX_DOCKER_CONTAINER` | 空 | 本地 docker 模式；跳过 APIG |
| `OPENCODE_MODEL` | `local/...` | OpenCode `-m` |
| `OPENCODE_TIMEOUT_SECONDS` | `600` | 单轮超时 |
| `WORKER_MAX_WORKERS` | `2` | `/api/query` 后台线程数 |
| `FLASK_HOST` / `FLASK_PORT` | `0.0.0.0` / `5000` | 监听地址 |

SDK 运行时还会自动注入 `x-livefunction-sandbox-id: <sandboxId>`。

## API

### `POST /api/chat`（推荐，SSE 流式）

```bash
curl -N -X POST http://127.0.0.1:5000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"user-1","query":"用一句话介绍 Python"}'
```

同一 `session_id` 再次请求会续聊：

```bash
curl -N -X POST http://127.0.0.1:5000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"user-1","query":"再举一个代码示例"}'
```

SSE `data` 为 JSON：

| type | 字段 | 说明 |
|------|------|------|
| `delta` | `text` | 助手增量文本 |
| `done` | `answer` | 本轮完整回复 |
| `error` | `message` | 失败信息 |

示例：

```
data: {"type":"delta","text":"Python"}
data: {"type":"delta","text":" 是一种..."}
data: {"type":"done","answer":"Python 是一种..."}
```

### `POST /api/query`（异步轮询，非流式）

```bash
curl -X POST http://127.0.0.1:5000/api/query \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"user-1","query":"用一句话介绍 Python"}'
```

`202` 响应：

```json
{"task_id":"...","session_id":"user-1","status":"pending"}
```

### `GET /api/tasks/<task_id>`

```bash
curl http://127.0.0.1:5000/api/tasks/<task_id>
```

`status`：`pending` | `running` | `succeeded` | `failed`

### `GET /health`

```bash
curl http://127.0.0.1:5000/health
curl 'http://127.0.0.1:5000/health?probe_sandbox=1'
```

## Session 语义

- 业务 `session_id`：客户端传入，用于绑定沙箱实例与 OpenCode 多轮会话
- OpenCode `sessionID`：从 `opencode run --format json` 事件中解析并缓存；后续请求自动加 `--session`
- 同一 `session_id` → 同一沙箱 + 同一 OpenCode 对话
- 沙箱默认 900s 后过期销毁；**仅当该沙箱上有正在执行的任务，且剩余时间进入安全窗口（默认 120s）时** 才调用 refresh 续期
- 空闲到期后下次请求会重新创建沙箱（OpenCode 会话不保留）
