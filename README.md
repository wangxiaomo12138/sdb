# Flask + AIO Sandbox OpenCode 异步代理

Python 3.10 Flask 服务：按 `session_id` 复用第三方沙箱与 OpenCode 会话，支持多轮对话；主路径以 SSE 流式输出，并保留异步任务轮询。

## 架构

```
Client -> POST /api/sandbox/session (SSE 建环境)
       -> POST /api/chat (SSE) 或 POST /api/query (异步轮询)
       -> SessionSandboxManager（session_id -> sandboxId + opencode_session_id）
       -> APIG create/refresh 沙箱实例（仅环境创建路径）
       -> agent-sandbox / docker exec
       -> opencode run --format json [--session ...]
```

任务与 session 映射保存在进程内存中，**服务重启后会丢失**。提问前须先创建环境。

## 环境要求

- Python 3.10+（推荐 3.10）
- 远程沙箱容器内 `opencode` 可用
- 沙箱需配置可用模型凭证（通过 `SANDBOX_ENV_VARS` 在执行 `opencode` 时注入，例如 `OPENCODE_API_KEY`，或写入 `~/.config/opencode/config.json` / `OPENCODE_JSON`）。未配置时任务会以 `failed` / SSE `error` 返回 provider 错误。

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
| `SANDBOX_APIG_SSL_VERIFY` | `true` | APIG HTTPS 证书校验；隔离网自签可设 `false` |
| `SANDBOX_APIG_CA_BUNDLE` | 空 | 企业 CA 证书文件路径；非空时优先生效 |
| `SANDBOX_APIG_TLS_VERSION` | `auto` | `auto`/`1.2`/`1.3`；Postman 通而 Python 不通时用 `auto` |
| `SANDBOX_APIG_SSL_COMPAT` | `true` | 放宽密码套件（`SECLEVEL=0`），贴近 Postman |
| `SANDBOX_APIG_PREFER_IPV4` | `true` | 优先 IPv4，避免 IPv6 握手挂起 |
| `SANDBOX_APIG_TRUST_ENV` | `false` | 是否读取系统 `HTTP(S)_PROXY` |
| `SANDBOX_APIG_HTTP_BACKEND` | `requests` | `requests` / `stdlib` / `httpx`；失败自动互切 |
| `SANDBOX_APIG_CONNECT_TIMEOUT_SECONDS` | `30` | APIG TCP/TLS 握手超时 |
| `SANDBOX_APIG_TIMEOUT_SECONDS` | `120` | APIG 整次请求超时 |
| `SANDBOX_APIG_PROXY` | 空 | `requests`/`httpx` 可用；可选 HTTP 代理 |
| `SANDBOX_INSTANCE_TIMEOUT` | `900` | 创建时生命周期（秒），平台默认过期销毁 900s |
| `SANDBOX_ENV_VARS` | 空 | JSON 对象；非空时在执行 `opencode` 前 `export`（如 `{"PATH":"xxx/bin:$PATH"}`） |
| `SANDBOX_NAS_USER_DATA_WORKSPACE_ID` 等 | 空 | 用户数据 NAS 挂载（workspaceId/subPath/mountPath/readOnly） |
| `SANDBOX_NAS_OPENCODE_WORKSPACE_ID` 等 | 空 | OpenCode 离线包 NAS 挂载 |
| `OPENCODE_OFFLINE_EXTRACT_PATH` | 空 | 沙箱内离线包提取/源路径 |
| `OPENCODE_OFFLINE_STORE_PATH` | 空 | 沙箱内离线包存放路径 |
| `SKILL_FILE_STORE_PATH` | 空 | 沙箱内 skill 存放目录 |
| `SANDBOX_REFRESH_DURATION` | `900` | 有任务且快到期时续期时长（秒，硬上限 1800） |
| `SANDBOX_KEEPALIVE_MARGIN_SECONDS` | `120` | 剩余 TTL 低于该值才考虑 refresh |
| `SANDBOX_KEEPALIVE_INTERVAL_SECONDS` | `15` | 保活扫描间隔 |
| `SANDBOX_READY_TIMEOUT_SECONDS` | `15` | 创建返回 starting 时的短等待 |
| `SANDBOX_DOCKER_CONTAINER` | 空 | 本地 docker 模式；跳过 APIG |
| `OPENCODE_MODEL` | `local/...` | OpenCode `-m` |
| `OPENCODE_TIMEOUT_SECONDS` | `600` | 单轮超时 |
| `WORKER_MAX_WORKERS` | `2` | `/api/query` 后台线程数 |
| `FLASK_HOST` / `FLASK_PORT` | `0.0.0.0` / `5000` | 监听地址 |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FILE` | 空 | 可选日志文件路径；空则只打 stdout |

SDK 运行时还会自动注入 `x-livefunction-sandbox-id: <sandboxId>`。

### 远程排查日志

日志为中文说明 + 关键字段（`session_id` / `sandbox_id` / `任务ID` / 耗时），密钥已脱敏，长文本自动截断。隔离网环境建议：

```bash
# .env
LOG_LEVEL=INFO
LOG_FILE=/var/log/sandbox-proxy.log
```

按一次请求串联排查时，优先搜：`收到创建沙箱环境请求` → `收到流式对话请求` / `异步查询已受理` → `开始创建或复用沙箱环境` / `提问时沙箱不可用` → `沙箱SSE执行开始` → `OpenCode` → `完成` / `失败`。
需要更细粒度（含非 JSON 行、轮询中间态）时把 `LOG_LEVEL=DEBUG`。

## API

完整契约见 [API.md](API.md)。推荐顺序：先建环境，再提问。

### `POST /api/sandbox/session`（SSE 建环境）

```bash
curl -N -X POST http://127.0.0.1:5000/api/sandbox/session \
  -H 'Content-Type: application/json' \
  -d '{}'
```

结束事件：`{"type":"ready","session_id":"<服务端生成>"}`。可选 `skill_file`（HTTP(S) URL，多个用英文逗号分隔）。**不要传 `session_id`。**

### `POST /api/chat`（推荐，SSE 流式）

```bash
curl -N -X POST http://127.0.0.1:5000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"user-1","query":"用一句话介绍 Python"}'
```

SSE `data` 为 JSON：`status` / `delta` / `done` / `error`。session 未创建或已过期会直接 `error`。

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

`status`：`pending` | `running` | `succeeded` | `failed`；另含 `current_stage` / `stages` 提问进度。

### `GET /health`

```bash
curl http://127.0.0.1:5000/health
curl 'http://127.0.0.1:5000/health?probe_sandbox=1'
```

## Session 语义

- 业务 `session_id`：由环境创建接口在 `ready` 事件中下发，绑定沙箱实例与 OpenCode 多轮会话
- OpenCode `sessionID`：从 `opencode run --format json` 事件中解析并缓存；后续请求自动加 `--session`
- 同一 `session_id` → 同一沙箱 + 同一 OpenCode 对话
- 沙箱默认 900s 后过期销毁；**环境创建或提问执行中**，且剩余时间进入安全窗口（默认 120s）时才 refresh 续期
- 空闲到期后提问会失败，需重新调用 `/api/sandbox/session`（OpenCode 会话不保留）
