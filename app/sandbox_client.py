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
from app.sandbox_lifecycle import SandboxLifecycleError
from app.session_sandbox import SessionSandboxBinding, session_sandbox_manager

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROVIDER_ERROR_RE = re.compile(r"(?im)^(error:|Error from provider)")


class OpenCodeError(Exception):
    """沙箱或 OpenCode 执行失败。"""


def _clean_output(text: str) -> str:
    text = _ANSI_RE.sub("", text or "")
    return "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 32).strip()


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
    return (
        "opencode run --dangerously-skip-permissions --format json "
        f"{session_flag}{model_flag}{quoted}"
    )


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

    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raw_fallback.append(line)
            continue

        if not isinstance(event, dict):
            continue
        saw_json = True

        session_id = event.get("sessionID") or event.get("sessionId")
        if isinstance(session_id, str) and session_id:
            yield {"type": "session", "opencode_session_id": session_id}

        etype = event.get("type")
        if etype in {"session.error", "error"}:
            err = event.get("error") or event.get("message") or event
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
                yield {"type": "delta", "text": delta}

    if not saw_json:
        joined = _clean_output("\n".join(raw_fallback))
        if joined:
            if _PROVIDER_ERROR_RE.search(joined):
                yield {"type": "error", "message": f"opencode failed: {joined}"}
                return
            yield {"type": "delta", "text": joined}
            answer_parts.append(joined)

    answer = "".join(answer_parts)
    if not answer and saw_json:
        # 部分续聊会话可能写库成功但没有 text 事件
        yield {
            "type": "error",
            "message": "opencode returned no text events",
        }
        return

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
    try:
        while True:
            if time.monotonic() > deadline:
                proc.kill()
                raise OpenCodeError(f"opencode timed out after {timeout}s")
            line = proc.stdout.readline()
            if line:
                yield line
                continue
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        remaining = proc.stdout.read()
        if remaining:
            yield remaining
        if proc.returncode not in (0, None) and proc.returncode != 0:
            # 退出码非 0 时 stdout 仍可能含 JSON，交给解析器判断
            logger.warning("docker opencode exit_code=%s", proc.returncode)
    finally:
        if proc.poll() is None:
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
    with httpx.Client(timeout=httpx.Timeout(timeout + 30.0)) as client:
        with client.stream("POST", url, headers=headers, json=body) as resp:
            content_type = (resp.headers.get("content-type") or "").lower()
            if resp.status_code >= 400:
                detail = resp.read().decode("utf-8", errors="replace")
                raise OpenCodeError(
                    f"sandbox request failed: HTTP {resp.status_code} {detail}"
                )

            if "text/event-stream" in content_type:
                for raw in resp.iter_lines():
                    if not raw:
                        continue
                    if raw.startswith("data:"):
                        data = raw[5:].strip()
                        if not data or data == "[DONE]":
                            continue
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
                return

            # 非 SSE 的 JSON 响应：取出 output 再按行产出
            payload = resp.read()
            try:
                result = json.loads(payload)
            except json.JSONDecodeError as exc:
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
            if success is False or (exit_code is not None and exit_code != 0):
                detail = _clean_output(output or message or f"exit_code={exit_code}")
                raise OpenCodeError(f"opencode failed: {detail}")
            if output:
                yield output if output.endswith("\n") else output + "\n"


def _stream_via_api_async_poll(
    command: str, timeout: float, sandbox_id: str
) -> Generator[str, None, None]:
    """兜底：异步执行 shell，再 view 轮询增量 stdout。"""
    client = _build_client(timeout=timeout, sandbox_id=sandbox_id)
    try:
        result = client.shell.exec_command(
            command=command,
            timeout=min(5.0, timeout),
            hard_timeout=timeout,
            async_mode=True,
            truncate=False,
        )
    except Exception as exc:
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
        try:
            sync_result = client.shell.exec_command(
                command=command,
                timeout=timeout,
                hard_timeout=timeout,
                truncate=False,
            )
        except Exception as exc:
            raise OpenCodeError(f"sandbox request failed: {exc}") from exc
        sync_data = getattr(sync_result, "data", None)
        output = ""
        exit_code = None
        if sync_data is not None:
            output = getattr(sync_data, "output", None) or ""
            exit_code = getattr(sync_data, "exit_code", None)
        success = getattr(sync_result, "success", True)
        message = getattr(sync_result, "message", None) or ""
        if success is False or (exit_code is not None and exit_code != 0):
            detail = _clean_output(output or message or f"exit_code={exit_code}")
            raise OpenCodeError(f"opencode failed: {detail}")
        if output:
            yield output if output.endswith("\n") else output + "\n"
        return

    deadline = time.monotonic() + timeout
    seen = 0
    while time.monotonic() < deadline:
        try:
            view = client.shell.view(id=str(shell_id))
        except Exception as exc:
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
            yield chunk
        status_l = str(status or "").lower()
        if status_l in {"completed", "exited", "stopped", "idle", "done"}:
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
                return
        except Exception:
            time.sleep(0.2)
            continue
        time.sleep(0.2)

    raise OpenCodeError(f"opencode timed out after {timeout}s")


def _stream_command_lines(
    command: str, timeout: float, sandbox_id: str
) -> Generator[str, None, None]:
    if Config.SANDBOX_DOCKER_CONTAINER:
        yield from _stream_via_docker(command, timeout)
        return

    try:
        yield from _stream_via_api_sse(command, timeout, sandbox_id)
    except OpenCodeError:
        raise
    except Exception as exc:
        logger.warning("SSE shell exec failed, falling back to poll: %s", exc)
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
    try:
        binding = session_sandbox_manager.ensure_sandbox(session_id)
    except SandboxLifecycleError as exc:
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
                yield event
                return
            if event["type"] == "done":
                answer = event.get("answer") or answer
                yield event
                return
    except OpenCodeError as exc:
        if opencode_session_id:
            session_sandbox_manager.clear_opencode_session_id(session_id)
        yield {"type": "error", "message": str(exc)}
        return
    except Exception as exc:
        logger.exception("stream_opencode unexpected error")
        yield {"type": "error", "message": f"unexpected error: {exc}"}
        return

    if not errored:
        yield {"type": "done", "answer": answer}


def run_opencode(
    query: str,
    *,
    session_id: str,
    timeout: Optional[float] = None,
) -> str:
    """在指定 session 上跑完 OpenCode，返回本轮完整回复文本。"""
    answer = ""
    for event in stream_opencode(query, session_id=session_id, timeout=timeout):
        if event["type"] == "delta":
            answer += event.get("text") or ""
        elif event["type"] == "done":
            return event.get("answer") or answer
        elif event["type"] == "error":
            raise OpenCodeError(event.get("message") or "opencode failed")
    if not answer:
        raise OpenCodeError("opencode returned empty output")
    return answer


def check_sandbox_health(timeout: float = 10.0) -> tuple[bool, str]:
    """返回 (ok, detail)。配置了 docker 容器时优先探测容器。"""
    if Config.SANDBOX_DOCKER_CONTAINER:
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
                return True, f"docker ok opencode={completed.stdout.strip()}"
            return (
                False,
                completed.stderr.strip()
                or completed.stdout.strip()
                or "docker exec failed",
            )
        except Exception as exc:
            return False, str(exc)

    # 无实例 id 只探 base URL；网关强制要求 sandbox-id 时可能失败
    client = _build_client(timeout=timeout, sandbox_id=None)
    try:
        ctx = client.sandbox.get_context()
        home_dir = getattr(ctx, "home_dir", None) or getattr(
            getattr(ctx, "data", None), "home_dir", None
        )
        return True, f"reachable home_dir={home_dir}"
    except Exception as exc:
        return False, str(exc)
