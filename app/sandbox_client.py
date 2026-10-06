from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import time
from typing import Any, Generator, Iterable, Optional

import httpx
from agent_sandbox import Sandbox

from app.config import Config
from app.logging_utils import preview
from app.sandbox_lifecycle import SandboxLifecycleError
from app.session_sandbox import SessionSandboxBinding, session_sandbox_manager

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROVIDER_ERROR_RE = re.compile(r"(?im)^(error:|Error from provider)")


class OpenCodeError(Exception):
    """沙箱或 OpenCode 执行失败。"""


class SandboxHttpError(OpenCodeError):
    """运行面 HTTP 错误；网关 502/503/504 可回退到 SDK 轮询。"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


# APIG/前置网关常见超时码：SSE 长连接拿不到首包时会出现，Postman 同步 JSON 往往仍能通
_GATEWAY_TIMEOUT_STATUSES = {502, 503, 504}
_sse_disabled_reason: Optional[str] = None


def _clean_output(text: str) -> str:
    text = _ANSI_RE.sub("", text or "")
    return "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 32).strip()


def _log_http_request_for_postman(
    *,
    action: str,
    method: str,
    url: str,
    headers: dict[str, str],
    body: Any,
) -> None:
    """把即将发出的 HTTP 请求原样打日志，便于复制到 Postman。"""
    dump = json.dumps(
        {"method": method, "url": url, "headers": headers, "body": body},
        ensure_ascii=False,
        indent=2,
    )
    logger.info("%s 请求原样(可复制到Postman)\n%s", action, dump)


def _build_runtime_headers(sandbox_id: Optional[str] = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if Config.SANDBOX_APIG_HW_ID:
        headers["X-HW-ID"] = Config.SANDBOX_APIG_HW_ID
    if Config.SANDBOX_APIG_HW_APPKEY:
        headers["X-HW-APPKEY"] = Config.SANDBOX_APIG_HW_APPKEY
    if sandbox_id and not sandbox_id.startswith("docker:"):
        headers["x-livefunction-sandbox-id"] = sandbox_id
    if Config.SANDBOX_API_KEY:
        headers["X-AIO-API-Key"] = Config.SANDBOX_API_KEY
        headers["Authorization"] = f"Bearer {Config.SANDBOX_API_KEY}"
    return headers


def _build_client(timeout: float, sandbox_id: Optional[str] = None) -> Sandbox:
    headers = _build_runtime_headers(sandbox_id) or None
    return Sandbox(
        base_url=Config.SANDBOX_BASE_URL,
        headers=headers,
        timeout=timeout,
    )


def _build_opencode_command(
    query: str, *, opencode_session_id: Optional[str] = None
) -> str:
    quoted = shlex.quote(query)
    model = shlex.quote(Config.OPENCODE_MODEL) if Config.OPENCODE_MODEL else ""
    model_flag = f"-m {model} " if model else ""
    session_flag = ""
    if opencode_session_id:
        session_flag = f"--session {shlex.quote(opencode_session_id)} "
    # 沙箱内 OpenCode 1.x 用 --dangerously-skip-permissions 自动批准，不是新版 --auto
    command = (
        "opencode run --dangerously-skip-permissions --format json "
        f"{session_flag}{model_flag}{quoted}"
    )
    logger.info(
        "已构建OpenCode命令 模型=%s opencode_session_id=%s 问题=%s "
        "命令预览=%s",
        Config.OPENCODE_MODEL or "(default)",
        opencode_session_id or "(new)",
        preview(query),
        preview(command, 300),
    )
    return command


def _iter_ndjson_lines(chunks: Iterable[str]) -> Generator[str, None, None]:
    buf = ""
    for chunk in chunks:
        if not chunk:
            continue
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if line:
                yield line
    tail = buf.strip()
    if tail:
        yield tail


def _parse_opencode_events(
    lines: Iterable[str],
) -> Generator[dict[str, Any], None, None]:
    """将 OpenCode --format json 的 NDJSON 行解析为对话事件：delta / session / done / error。"""
    answer_parts: list[str] = []
    last_text = ""
    saw_json = False
    raw_fallback: list[str] = []
    json_lines = 0
    delta_events = 0

    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raw_fallback.append(line)
            logger.debug("OpenCode非JSON行 内容=%s", preview(line, 300))
            continue

        if not isinstance(event, dict):
            continue
        saw_json = True
        json_lines += 1

        session_id = event.get("sessionID") or event.get("sessionId")
        if isinstance(session_id, str) and session_id:
            logger.info("OpenCode事件 类型=会话 opencode_session_id=%s", session_id)
            yield {"type": "session", "opencode_session_id": session_id}

        etype = event.get("type")
        if etype in {"session.error", "error"}:
            err = event.get("error") or event.get("message") or event
            logger.warning(
                "OpenCode事件 类型=错误 消息=%s 原始=%s",
                preview(err, 500),
                preview(event, 500),
            )
            yield {"type": "error", "message": _clean_output(str(err))}
            return

        if etype == "text":
            part = event.get("part") or {}
            text = part.get("text") if isinstance(part, dict) else None
            if not isinstance(text, str) or not text:
                continue
            if text.startswith(last_text):
                delta = text[len(last_text) :]
            else:
                delta = text
            last_text = text
            if delta:
                answer_parts.append(delta)
                delta_events += 1
                if delta_events == 1 or delta_events % 20 == 0:
                    logger.info(
                        "OpenCode增量进度 事件数=%s 回复字符数=%s 最近片段=%s",
                        delta_events,
                        sum(len(p) for p in answer_parts),
                        preview(delta, 80),
                    )
                yield {"type": "delta", "text": delta}

    if not saw_json:
        joined = _clean_output("\n".join(raw_fallback))
        logger.warning(
            "OpenCode未产出JSON事件 原始行数=%s 预览=%s",
            len(raw_fallback),
            preview(joined, 500),
        )
        if joined:
            if _PROVIDER_ERROR_RE.search(joined):
                yield {"type": "error", "message": f"opencode failed: {joined}"}
                return
            yield {"type": "delta", "text": joined}
            answer_parts.append(joined)

    answer = "".join(answer_parts)
    if not answer and saw_json:
        # 部分续聊会话可能写库成功但没有 text 事件
        logger.warning(
            "OpenCode无文本事件 JSON行数=%s", json_lines
        )
        yield {
            "type": "error",
            "message": "opencode returned no text events",
        }
        return

    logger.info(
        "OpenCode解析完成 JSON行数=%s 增量事件数=%s 回复字符数=%s 回复=%s",
        json_lines,
        delta_events,
        len(answer),
        preview(answer),
    )
    yield {"type": "done", "answer": answer}


def _stream_via_docker(
    command: str, timeout: float
) -> Generator[str, None, None]:
    container = Config.SANDBOX_DOCKER_CONTAINER
    cmd = [
        "docker",
        "exec",
        "-u",
        "gem",
        "-w",
        "/home/gem",
        container,
        "bash",
        "-lc",
        command,
    ]
    logger.info(
        "Docker执行开始 容器=%s 超时秒=%s 命令=%s",
        container,
        timeout,
        preview(command, 300),
    )
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise OpenCodeError("docker executable not found on host") from exc
    except Exception as exc:
        raise OpenCodeError(f"docker exec failed: {exc}") from exc

    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    line_count = 0
    try:
        while True:
            if time.monotonic() > deadline:
                proc.kill()
                logger.error(
                    "Docker执行超时 容器=%s 超时秒=%s 已输出行数=%s",
                    container,
                    timeout,
                    line_count,
                )
                raise OpenCodeError(f"opencode timed out after {timeout}s")
            line = proc.stdout.readline()
            if line:
                line_count += 1
                if line_count == 1 or line_count % 50 == 0:
                    logger.debug(
                        "Docker标准输出 行数=%s 预览=%s",
                        line_count,
                        preview(line, 200),
                    )
                yield line
                continue
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        remaining = proc.stdout.read()
        if remaining:
            yield remaining
        elapsed_ms = (time.monotonic() - started) * 1000
        if proc.returncode not in (0, None) and proc.returncode != 0:
            # 退出码非 0 时 stdout 仍可能含 JSON，交给解析器判断
            logger.warning(
                "Docker中OpenCode非零退出 退出码=%s 行数=%s 耗时毫秒=%.1f",
                proc.returncode,
                line_count,
                elapsed_ms,
            )
        else:
            logger.info(
                "Docker执行结束 退出码=%s 行数=%s 耗时毫秒=%.1f",
                proc.returncode,
                line_count,
                elapsed_ms,
            )
    finally:
        if proc.poll() is None:
            logger.warning("Docker进程仍在运行，强制结束 pid=%s", proc.pid)
            proc.kill()


def _stream_via_api_sse(
    command: str, timeout: float, sandbox_id: str
) -> Generator[str, None, None]:
    """尝试带 SSE Accept 执行 shell，产出类似 stdout 的文本块。"""
    url = f"{Config.SANDBOX_BASE_URL}/v1/shell/exec"
    headers = {
        **_build_runtime_headers(sandbox_id),
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body = {
        "command": command,
        "timeout": timeout,
        "hard_timeout": timeout,
        "async_mode": False,
        "truncate": False,
    }
    logger.info(
        "沙箱SSE执行开始 地址=%s sandbox_id=%s 超时秒=%s 命令=%s",
        url,
        sandbox_id,
        timeout,
        preview(command, 300),
    )
    _log_http_request_for_postman(
        action="沙箱SSE执行",
        method="POST",
        url=url,
        headers=headers,
        body=body,
    )
    started = time.monotonic()
    # 与 APIG 客户端一致：不读系统代理，避免 Postman 直连能通、httpx 走代理 504
    with httpx.Client(
        timeout=httpx.Timeout(timeout + 30.0, connect=30.0),
        trust_env=False,
    ) as client:
        with client.stream("POST", url, headers=headers, json=body) as resp:
            content_type = (resp.headers.get("content-type") or "").lower()
            logger.info(
                "沙箱SSE执行响应 HTTP状态码=%s Content-Type=%s",
                resp.status_code,
                content_type or "(缺失)",
            )
            if resp.status_code >= 400:
                detail = resp.read().decode("utf-8", errors="replace")
                logger.error(
                    "沙箱SSE执行HTTP错误 状态码=%s 详情=%s",
                    resp.status_code,
                    preview(detail, 500),
                )
                raise SandboxHttpError(
                    f"sandbox request failed: HTTP {resp.status_code} {detail}",
                    resp.status_code,
                )

            if "text/event-stream" in content_type:
                sse_lines = 0
                for raw in resp.iter_lines():
                    if not raw:
                        continue
                    if raw.startswith("data:"):
                        data = raw[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        sse_lines += 1
                        # data 可能是 JSON 包装，也可能是原始 NDJSON 行
                        try:
                            parsed = json.loads(data)
                        except json.JSONDecodeError:
                            yield data + "\n"
                            continue
                        if isinstance(parsed, dict):
                            # 常见形态：{output}、{data:{output}}，或 OpenCode 事件本身
                            if "type" in parsed and (
                                "sessionID" in parsed or "part" in parsed
                            ):
                                yield json.dumps(parsed, ensure_ascii=False) + "\n"
                                continue
                            output = parsed.get("output")
                            nested = parsed.get("data")
                            if output is None and isinstance(nested, dict):
                                output = nested.get("output")
                            if isinstance(output, str):
                                yield output
                                if not output.endswith("\n"):
                                    yield "\n"
                                continue
                        yield data + "\n"
                logger.info(
                    "沙箱SSE流结束 行数=%s 耗时毫秒=%.1f",
                    sse_lines,
                    (time.monotonic() - started) * 1000,
                )
                return

            # 非 SSE 的 JSON 响应：取出 output 再按行产出
            logger.info(
                "沙箱返回非SSE的JSON，改为一次性解析 sandbox_id=%s",
                sandbox_id,
            )
            payload = resp.read()
            try:
                result = json.loads(payload)
            except json.JSONDecodeError as exc:
                logger.error(
                    "沙箱响应不是JSON 预览=%s", preview(payload, 300)
                )
                raise OpenCodeError(
                    f"sandbox response not JSON: {payload[:200]!r}"
                ) from exc

            success = result.get("success", True)
            message = result.get("message") or ""
            data = result.get("data") or {}
            output = ""
            exit_code = None
            if isinstance(data, dict):
                output = data.get("output") or ""
                exit_code = data.get("exit_code")
            logger.info(
                "沙箱同步JSON结果 成功=%s 退出码=%s 输出字符数=%s "
                "消息=%s 耗时毫秒=%.1f",
                success,
                exit_code,
                len(output or ""),
                preview(message),
                (time.monotonic() - started) * 1000,
            )
            if success is False or (exit_code is not None and exit_code != 0):
                detail = _clean_output(output or message or f"exit_code={exit_code}")
                raise OpenCodeError(f"opencode failed: {detail}")
            if output:
                yield output if output.endswith("\n") else output + "\n"


def _stream_via_api_async_poll(
    command: str, timeout: float, sandbox_id: str
) -> Generator[str, None, None]:
    """兜底：异步执行 shell，再 view 轮询增量 stdout。"""
    async_body = {
        "command": command,
        "timeout": min(5.0, timeout),
        "hard_timeout": timeout,
        "async_mode": True,
        "truncate": False,
    }
    logger.info(
        "沙箱异步轮询开始 sandbox_id=%s 超时秒=%s 命令=%s",
        sandbox_id,
        timeout,
        preview(command, 300),
    )
    _log_http_request_for_postman(
        action="沙箱异步执行",
        method="POST",
        url=f"{Config.SANDBOX_BASE_URL}/v1/shell/exec",
        headers=_build_runtime_headers(sandbox_id),
        body=async_body,
    )
    started = time.monotonic()
    client = _build_client(timeout=timeout, sandbox_id=sandbox_id)
    try:
        result = client.shell.exec_command(**async_body)
    except Exception as exc:
        logger.error("沙箱异步执行失败 错误=%s", preview(exc, 500))
        raise OpenCodeError(f"sandbox request failed: {exc}") from exc

    data = getattr(result, "data", None) or result
    shell_id = getattr(data, "session_id", None) or getattr(data, "id", None)
    if not shell_id and isinstance(data, dict):
        shell_id = data.get("session_id") or data.get("id")
    # 部分 SDK 把 session id 放在响应顶层
    if not shell_id:
        shell_id = getattr(result, "session_id", None)

    # 异步执行未返回 session 时，退回一次同步 exec
    if not shell_id:
        logger.warning(
            "沙箱异步执行未返回shell会话，回退同步执行 "
            "sandbox_id=%s",
            sandbox_id,
        )
        try:
            sync_result = client.shell.exec_command(
                command=command,
                timeout=timeout,
                hard_timeout=timeout,
                truncate=False,
            )
        except Exception as exc:
            logger.error("沙箱同步回退失败 错误=%s", preview(exc, 500))
            raise OpenCodeError(f"sandbox request failed: {exc}") from exc
        sync_data = getattr(sync_result, "data", None)
        output = ""
        exit_code = None
        if sync_data is not None:
            output = getattr(sync_data, "output", None) or ""
            exit_code = getattr(sync_data, "exit_code", None)
        success = getattr(sync_result, "success", True)
        message = getattr(sync_result, "message", None) or ""
        logger.info(
            "沙箱同步回退结果 成功=%s 退出码=%s 输出字符数=%s "
            "耗时毫秒=%.1f",
            success,
            exit_code,
            len(output or ""),
            (time.monotonic() - started) * 1000,
        )
        if success is False or (exit_code is not None and exit_code != 0):
            detail = _clean_output(output or message or f"exit_code={exit_code}")
            raise OpenCodeError(f"opencode failed: {detail}")
        if output:
            yield output if output.endswith("\n") else output + "\n"
        return

    logger.info(
        "沙箱异步轮询已取得shell会话 shell_id=%s sandbox_id=%s", shell_id, sandbox_id
    )
    deadline = time.monotonic() + timeout
    seen = 0
    polls = 0
    while time.monotonic() < deadline:
        polls += 1
        try:
            view = client.shell.view(id=str(shell_id))
        except Exception as exc:
            logger.error(
                "沙箱查看输出失败 shell_id=%s 轮询次数=%s 错误=%s",
                shell_id,
                polls,
                preview(exc, 300),
            )
            raise OpenCodeError(f"sandbox view failed: {exc}") from exc
        view_data = getattr(view, "data", None) or view
        console = (
            getattr(view_data, "output", None)
            or getattr(view_data, "console", None)
            or ""
        )
        status = getattr(view_data, "status", None) or getattr(
            view_data, "session_status", None
        )
        if isinstance(console, str) and len(console) > seen:
            chunk = console[seen:]
            seen = len(console)
            if polls == 1 or polls % 10 == 0:
                logger.info(
                    "沙箱轮询进度 shell_id=%s 轮询次数=%s 状态=%s "
                    "控制台字符数=%s 片段=%s",
                    shell_id,
                    polls,
                    status,
                    seen,
                    preview(chunk, 120),
                )
            yield chunk
        status_l = str(status or "").lower()
        if status_l in {"completed", "exited", "stopped", "idle", "done"}:
            logger.info(
                "沙箱轮询完成 shell_id=%s 轮询次数=%s 状态=%s "
                "控制台字符数=%s 耗时毫秒=%.1f",
                shell_id,
                polls,
                status_l,
                seen,
                (time.monotonic() - started) * 1000,
            )
            return
        # 再短等进程结束
        try:
            wait_res = client.shell.wait_for_process(
                id=str(shell_id), seconds=1
            )
            wait_data = getattr(wait_res, "data", None) or wait_res
            wait_status = getattr(wait_data, "status", None)
            if str(wait_status or "").lower() in {
                "completed",
                "exited",
                "stopped",
                "done",
            }:
                # 结束后再拉一次完整输出
                view = client.shell.view(id=str(shell_id))
                view_data = getattr(view, "data", None) or view
                console = (
                    getattr(view_data, "output", None)
                    or getattr(view_data, "console", None)
                    or ""
                )
                if isinstance(console, str) and len(console) > seen:
                    yield console[seen:]
                logger.info(
                    "沙箱等待进程结束 shell_id=%s 等待状态=%s "
                    "轮询次数=%s 耗时毫秒=%.1f",
                    shell_id,
                    wait_status,
                    polls,
                    (time.monotonic() - started) * 1000,
                )
                return
        except Exception as wait_exc:
            logger.debug(
                "沙箱等待进程短暂异常 shell_id=%s 错误=%s",
                shell_id,
                preview(wait_exc, 200),
            )
            time.sleep(0.2)
            continue
        time.sleep(0.2)

    logger.error(
        "沙箱异步轮询超时 shell_id=%s 轮询次数=%s 控制台字符数=%s "
        "超时秒=%s",
        shell_id,
        polls,
        seen,
        timeout,
    )
    raise OpenCodeError(f"opencode timed out after {timeout}s")


def _should_fallback_from_sse(exc: Exception) -> bool:
    if isinstance(exc, SandboxHttpError):
        return exc.status_code in _GATEWAY_TIMEOUT_STATUSES
    return not isinstance(exc, OpenCodeError)


def _stream_command_lines(
    command: str, timeout: float, sandbox_id: str
) -> Generator[str, None, None]:
    global _sse_disabled_reason
    if Config.SANDBOX_DOCKER_CONTAINER:
        logger.info("执行路径=Docker sandbox_id=%s", sandbox_id)
        yield from _stream_via_docker(command, timeout)
        return

    if _sse_disabled_reason:
        logger.info(
            "执行路径=异步轮询（已跳过SSE） sandbox_id=%s 原因=%s",
            sandbox_id,
            _sse_disabled_reason,
        )
        yield from _stream_via_api_async_poll(command, timeout, sandbox_id)
        return

    logger.info("执行路径=优先SSE sandbox_id=%s", sandbox_id)
    try:
        yield from _stream_via_api_sse(command, timeout, sandbox_id)
    except Exception as exc:
        if not _should_fallback_from_sse(exc):
            raise
        reason = str(exc)
        if isinstance(exc, SandboxHttpError) and exc.status_code in _GATEWAY_TIMEOUT_STATUSES:
            _sse_disabled_reason = f"HTTP {exc.status_code}"
        logger.warning(
            "SSE执行失败，回退异步轮询 sandbox_id=%s 错误=%s",
            sandbox_id,
            preview(reason, 300),
        )
        yield from _stream_via_api_async_poll(command, timeout, sandbox_id)


def stream_opencode(
    query: str,
    *,
    session_id: str,
    timeout: Optional[float] = None,
) -> Generator[dict[str, Any], None, None]:
    """为 session 确保沙箱，执行 --format json 的 opencode，产出对话事件。

    事件类型：delta | session | done | error
    """
    timeout = Config.OPENCODE_TIMEOUT_SECONDS if timeout is None else timeout
    logger.info(
        "开始流式执行OpenCode session_id=%s 超时秒=%s 问题=%s",
        session_id,
        timeout,
        preview(query),
    )
    try:
        binding = session_sandbox_manager.ensure_sandbox(session_id)
    except SandboxLifecycleError as exc:
        logger.error(
            "确保沙箱失败 session_id=%s 错误=%s",
            session_id,
            preview(exc, 500),
        )
        yield {"type": "error", "message": str(exc)}
        return

    session_sandbox_manager.begin_task(session_id)
    try:
        yield from _stream_opencode_bound(query, session_id, binding, timeout)
    finally:
        session_sandbox_manager.end_task(session_id)


def _stream_opencode_bound(
    query: str,
    session_id: str,
    binding: SessionSandboxBinding,
    timeout: float,
) -> Generator[dict[str, Any], None, None]:
    opencode_session_id = binding.opencode_session_id
    command = _build_opencode_command(query, opencode_session_id=opencode_session_id)
    answer = ""
    errored = False
    started = time.monotonic()
    logger.info(
        "已绑定沙箱准备执行 session_id=%s sandbox_id=%s "
        "opencode_session_id=%s 超时秒=%s",
        session_id,
        binding.sandbox_id,
        opencode_session_id or "(新建)",
        timeout,
    )

    try:
        lines = _iter_ndjson_lines(
            _stream_command_lines(command, timeout, binding.sandbox_id)
        )
        for event in _parse_opencode_events(lines):
            if event["type"] == "session":
                session_sandbox_manager.set_opencode_session_id(
                    session_id, event["opencode_session_id"]
                )
                continue
            if event["type"] == "delta":
                answer += event.get("text") or ""
                yield event
                continue
            if event["type"] == "error":
                errored = True
                # 续聊失败则清掉 OpenCode session，下次当新会话
                if opencode_session_id:
                    session_sandbox_manager.clear_opencode_session_id(session_id)
                logger.warning(
                    "流式OpenCode返回错误 session_id=%s sandbox_id=%s "
                    "耗时毫秒=%.1f 消息=%s",
                    session_id,
                    binding.sandbox_id,
                    (time.monotonic() - started) * 1000,
                    preview(event.get("message"), 500),
                )
                yield event
                return
            if event["type"] == "done":
                answer = event.get("answer") or answer
                logger.info(
                    "流式OpenCode完成 session_id=%s sandbox_id=%s "
                    "耗时毫秒=%.1f 回复字符数=%s",
                    session_id,
                    binding.sandbox_id,
                    (time.monotonic() - started) * 1000,
                    len(answer or ""),
                )
                yield event
                return
    except OpenCodeError as exc:
        if opencode_session_id:
            session_sandbox_manager.clear_opencode_session_id(session_id)
        logger.warning(
            "流式OpenCode异常 session_id=%s sandbox_id=%s "
            "耗时毫秒=%.1f 错误=%s",
            session_id,
            binding.sandbox_id,
            (time.monotonic() - started) * 1000,
            preview(exc, 500),
        )
        yield {"type": "error", "message": str(exc)}
        return
    except Exception as exc:
        logger.exception(
            "流式OpenCode未预期异常 session_id=%s sandbox_id=%s",
            session_id,
            binding.sandbox_id,
        )
        yield {"type": "error", "message": f"unexpected error: {exc}"}
        return

    if not errored:
        logger.info(
            "流式OpenCode兜底完成 session_id=%s 回复字符数=%s "
            "耗时毫秒=%.1f",
            session_id,
            len(answer),
            (time.monotonic() - started) * 1000,
        )
        yield {"type": "done", "answer": answer}


def run_opencode(
    query: str,
    *,
    session_id: str,
    timeout: Optional[float] = None,
) -> str:
    """在指定 session 上跑完 OpenCode，返回本轮完整回复文本。"""
    logger.info(
        "开始同步执行OpenCode session_id=%s 问题=%s", session_id, preview(query)
    )
    answer = ""
    for event in stream_opencode(query, session_id=session_id, timeout=timeout):
        if event["type"] == "delta":
            answer += event.get("text") or ""
        elif event["type"] == "done":
            final = event.get("answer") or answer
            logger.info(
                "同步OpenCode成功 session_id=%s 回复字符数=%s",
                session_id,
                len(final or ""),
            )
            return final
        elif event["type"] == "error":
            logger.warning(
                "同步OpenCode失败 session_id=%s 错误=%s",
                session_id,
                preview(event.get("message"), 500),
            )
            raise OpenCodeError(event.get("message") or "opencode failed")
    if not answer:
        raise OpenCodeError("opencode returned empty output")
    return answer


def check_sandbox_health(timeout: float = 10.0) -> tuple[bool, str]:
    """返回 (ok, detail)。配置了 docker 容器时优先探测容器。"""
    if Config.SANDBOX_DOCKER_CONTAINER:
        logger.info(
            "健康探测Docker 容器=%s 超时秒=%s",
            Config.SANDBOX_DOCKER_CONTAINER,
            timeout,
        )
        try:
            completed = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-u",
                    "gem",
                    Config.SANDBOX_DOCKER_CONTAINER,
                    "bash",
                    "-lc",
                    "opencode --version",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if completed.returncode == 0:
                detail = f"docker ok opencode={completed.stdout.strip()}"
                logger.info("健康探测Docker成功 详情=%s", detail)
                return True, detail
            detail = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or "docker exec failed"
            )
            logger.warning(
                "健康探测Docker失败 退出码=%s 详情=%s",
                completed.returncode,
                preview(detail, 300),
            )
            return False, detail
        except Exception as exc:
            logger.exception("健康探测Docker发生异常")
            return False, str(exc)

    # 无实例 id 只探 base URL；网关强制要求 sandbox-id 时可能失败
    logger.info(
        "健康探测API 地址=%s 超时秒=%s",
        Config.SANDBOX_BASE_URL,
        timeout,
    )
    client = _build_client(timeout=timeout, sandbox_id=None)
    try:
        ctx = client.sandbox.get_context()
        home_dir = getattr(ctx, "home_dir", None) or getattr(
            getattr(ctx, "data", None), "home_dir", None
        )
        detail = f"reachable home_dir={home_dir}"
        logger.info("健康探测API成功 详情=%s", detail)
        return True, detail
    except Exception as exc:
        logger.warning("健康探测API失败 错误=%s", preview(exc, 300))
        return False, str(exc)
