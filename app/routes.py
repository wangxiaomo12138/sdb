from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Optional, Tuple, Union

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from app.logging_utils import preview
from app.sandbox_client import (
    OpenCodeError,
    SandboxSessionNotFoundError,
    check_sandbox_health,
    export_opencode_session,
    list_opencode_models,
    list_opencode_sessions,
    stream_opencode,
)
from app.sandbox_env import (
    stream_create_sandbox_env,
    validate_create_env_params,
    validate_skill_file_urls,
)
from app.sandbox_lifecycle import (
    SandboxLifecycleError,
    SandboxNotFoundError,
    get_sandbox_status,
)

api_bp = Blueprint("api", __name__)
logger = logging.getLogger(__name__)


def _parse_optional_string_field(
    body: dict,
    field: str,
) -> Union[Optional[str], Tuple[None, Tuple[Response, int]]]:
    raw = body.get(field)
    if raw is None:
        return None
    if not isinstance(raw, str):
        logger.warning(
            "请求参数校验失败 原因=%s类型错误 路径=%s",
            field,
            request.path,
        )
        return None, (
            jsonify({"error": f"{field} must be a string when provided"}),
            400,
        )
    stripped = raw.strip()
    return stripped or None


def _parse_optional_opencode_session_id(
    body: dict,
) -> Union[Optional[str], Tuple[None, Tuple[Response, int]]]:
    return _parse_optional_string_field(body, "opencode_session_id")


def _parse_optional_model(
    body: dict,
) -> Union[Optional[str], Tuple[None, Tuple[Response, int]]]:
    return _parse_optional_string_field(body, "model")


def _parse_sandbox_query(
    body: dict,
) -> Union[
    Tuple[str, str, Optional[str], Optional[str]],
    Tuple[None, Tuple[Response, int]],
]:
    query = body.get("query")
    sandbox_id = body.get("sandbox_id")
    if not isinstance(query, str) or not query.strip():
        logger.warning(
            "请求参数校验失败 原因=缺少query 路径=%s",
            request.path,
        )
        return None, (
            jsonify({"error": "query is required and must be a non-empty string"}),
            400,
        )
    if not isinstance(sandbox_id, str) or not sandbox_id.strip():
        logger.warning(
            "请求参数校验失败 原因=缺少sandbox_id 路径=%s",
            request.path,
        )
        return None, (
            jsonify(
                {"error": "sandbox_id is required and must be a non-empty string"}
            ),
            400,
        )
    oc = _parse_optional_opencode_session_id(body)
    if isinstance(oc, tuple):
        return oc
    model = _parse_optional_model(body)
    if isinstance(model, tuple):
        return model
    return sandbox_id.strip(), query.strip(), oc, model


def _require_query_sandbox_id() -> Union[str, Tuple[Response, int]]:
    sandbox_id = request.args.get("sandbox_id")
    if not isinstance(sandbox_id, str) or not sandbox_id.strip():
        logger.warning(
            "请求参数校验失败 原因=缺少sandbox_id 路径=%s",
            request.path,
        )
        return (
            jsonify(
                {"error": "sandbox_id is required and must be a non-empty string"}
            ),
            400,
        )
    return sandbox_id.strip()


@api_bp.get("/health")
def health():
    probe = request.args.get("probe_sandbox", "0") in {"1", "true", "yes"}
    logger.info(
        "健康检查 探测沙箱=%s 来源IP=%s", probe, request.remote_addr
    )
    payload = {"status": "ok"}
    if probe:
        started = time.monotonic()
        ok, detail = check_sandbox_health()
        elapsed_ms = (time.monotonic() - started) * 1000
        payload["sandbox"] = {"ok": ok, "detail": detail}
        logger.info(
            "健康检查沙箱探测 成功=%s 耗时毫秒=%.1f 详情=%s",
            ok,
            elapsed_ms,
            preview(detail, 300),
        )
        if not ok:
            return jsonify(payload), 503
    return jsonify(payload)


@api_bp.get("/api/sandbox/status")
def sandbox_status():
    """查询 APIG 沙箱实例信息与状态（GET /livefunction/sandboxes/{sandboxId}）。"""
    parsed = _require_query_sandbox_id()
    if not isinstance(parsed, str):
        return parsed
    sandbox_id = parsed
    logger.info(
        "收到沙箱状态查询 sandbox_id=%s 来源IP=%s",
        sandbox_id,
        request.remote_addr,
    )
    try:
        data = get_sandbox_status(sandbox_id)
    except SandboxNotFoundError as exc:
        logger.warning(
            "沙箱状态查询：实例不存在 sandbox_id=%s 错误=%s",
            sandbox_id,
            exc,
        )
        return jsonify({"error": str(exc)}), 404
    except SandboxLifecycleError as exc:
        logger.warning(
            "沙箱状态查询失败 sandbox_id=%s 错误=%s",
            sandbox_id,
            preview(exc, 500),
        )
        return jsonify({"error": str(exc)}), 502
    return jsonify(data)


@api_bp.post("/api/sandbox/session")
def create_sandbox_session():
    """SSE：创建沙箱环境；ready 返回 APIG sandbox_id（Docker 为 docker:{uuid}）。"""
    req_id = uuid.uuid4().hex[:12]
    body = request.get_json(silent=True) or {}
    try:
        sub_path = validate_create_env_params(body)
        skill_urls = validate_skill_file_urls(body.get("skill_file"))
    except ValueError as exc:
        logger.warning(
            "创建环境参数校验失败 请求ID=%s 错误=%s", req_id, exc
        )
        return jsonify({"error": str(exc)}), 400

    skill_file_raw = (
        ",".join(skill_urls) if skill_urls else None
    )
    logger.info(
        "收到创建沙箱环境请求 请求ID=%s sub_path=%s "
        "skill_file数量=%s skill_file=%s 来源IP=%s",
        req_id,
        sub_path,
        len(skill_urls or []),
        preview(skill_file_raw or "(无)"),
        request.remote_addr,
    )

    def generate():
        started = time.monotonic()
        final_sandbox: Optional[str] = None
        status_count = 0
        try:
            for event in stream_create_sandbox_env(
                sub_path=sub_path,
                skill_file=skill_file_raw,
            ):
                etype = event.get("type")
                if etype == "ready":
                    final_sandbox = event.get("sandbox_id")
                    logger.info(
                        "创建沙箱环境SSE就绪 请求ID=%s sandbox_id=%s",
                        req_id,
                        final_sandbox,
                    )
                elif etype == "status":
                    status_count += 1
                    logger.info(
                        "创建沙箱环境SSE状态 请求ID=%s stage=%s message=%s",
                        req_id,
                        event.get("stage"),
                        event.get("message"),
                    )
                elif etype == "error":
                    logger.warning(
                        "创建沙箱环境SSE错误 请求ID=%s message=%s",
                        req_id,
                        preview(event.get("message"), 500),
                    )
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception("创建沙箱环境异常 请求ID=%s", req_id)
            err = {"type": "error", "message": "unexpected stream error"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        finally:
            logger.info(
                "创建沙箱环境结束 请求ID=%s sandbox_id=%s 状态事件数=%s "
                "耗时毫秒=%.1f",
                req_id,
                final_sandbox or "(无)",
                status_count,
                (time.monotonic() - started) * 1000,
            )

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@api_bp.post("/api/chat")
def chat():
    """SSE 流式对话。可选 opencode_session_id 续聊；不传则开新对话。"""
    req_id = uuid.uuid4().hex[:12]
    body = request.get_json(silent=True) or {}
    parsed = _parse_sandbox_query(body)
    if parsed[0] is None:
        err_resp, status = parsed[1]  # type: ignore[misc]
        return err_resp, status
    sandbox_id, query, opencode_session_id, model = parsed  # type: ignore[misc]
    logger.info(
        "收到流式对话请求 请求ID=%s sandbox_id=%s opencode_session_id=%s "
        "model=%s 问题=%s 来源IP=%s",
        req_id,
        sandbox_id,
        opencode_session_id or "(new)",
        model or "(default)",
        preview(query),
        request.remote_addr,
    )

    def generate():
        started = time.monotonic()
        event_counts = {
            "status": 0,
            "delta": 0,
            "done": 0,
            "error": 0,
            "other": 0,
        }
        answer_chars = 0
        try:
            for event in stream_opencode(
                query,
                sandbox_id=sandbox_id,
                opencode_session_id=opencode_session_id,
                model=model,
            ):
                etype = event.get("type") or "other"
                if etype == "delta":
                    event_counts["delta"] += 1
                    answer_chars += len(event.get("text") or "")
                elif etype in event_counts:
                    event_counts[etype] += 1
                else:
                    event_counts["other"] += 1
                if etype in {"done", "error", "status"}:
                    logger.info(
                        "流式对话事件 请求ID=%s sandbox_id=%s 类型=%s 内容=%s",
                        req_id,
                        sandbox_id,
                        etype,
                        preview(
                            event.get("message")
                            or event.get("answer")
                            or event.get("opencode_session_id")
                            or event.get("stage")
                            or ""
                        ),
                    )
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception(
                "流式对话异常 请求ID=%s sandbox_id=%s",
                req_id,
                sandbox_id,
            )
            err = {"type": "error", "message": "unexpected stream error"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000
            logger.info(
                "流式对话结束 请求ID=%s sandbox_id=%s 耗时毫秒=%.1f "
                "状态事件数=%s 增量事件数=%s 完成=%s 错误=%s 回复字符数=%s",
                req_id,
                sandbox_id,
                elapsed_ms,
                event_counts["status"],
                event_counts["delta"],
                event_counts["done"],
                event_counts["error"],
                answer_chars,
            )

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@api_bp.post("/api/query")
def create_query():
    body = request.get_json(silent=True) or {}
    parsed = _parse_sandbox_query(body)
    if parsed[0] is None:
        err_resp, status = parsed[1]  # type: ignore[misc]
        return err_resp, status
    sandbox_id, query, opencode_session_id, model = parsed  # type: ignore[misc]

    store = current_app.extensions["task_store"]
    worker = current_app.extensions["task_worker"]

    task = store.create(
        query,
        sandbox_id=sandbox_id,
        opencode_session_id=opencode_session_id,
        model=model,
    )
    # 提交前先快照：worker 可能立刻改同一 Task 对象
    payload = {
        "task_id": task.task_id,
        "sandbox_id": task.sandbox_id,
        "status": "pending",
    }
    if opencode_session_id:
        payload["opencode_session_id"] = opencode_session_id
    if model:
        payload["model"] = model
    logger.info(
        "异步查询已受理 任务ID=%s sandbox_id=%s opencode_session_id=%s "
        "model=%s 问题=%s 来源IP=%s",
        task.task_id,
        sandbox_id,
        opencode_session_id or "(new)",
        model or "(default)",
        preview(query),
        request.remote_addr,
    )
    worker.submit(task.task_id)
    return jsonify(payload), 202


@api_bp.get("/api/tasks/<task_id>")
def get_task(task_id: str):
    store = current_app.extensions["task_store"]
    task = store.get(task_id)
    if task is None:
        logger.warning("查询任务不存在 任务ID=%s", task_id)
        return jsonify({"error": "task not found"}), 404
    logger.info(
        "查询任务成功 任务ID=%s sandbox_id=%s opencode_session_id=%s "
        "状态=%s current_stage=%s",
        task.task_id,
        task.sandbox_id,
        task.opencode_session_id or "(无)",
        task.status,
        task.current_stage,
    )
    return jsonify(task.to_dict())


@api_bp.get("/api/models")
def list_models():
    """列出沙箱内指定 provider 的模型（opencode models <provider>）。"""
    parsed = _require_query_sandbox_id()
    if not isinstance(parsed, str):
        return parsed
    sandbox_id = parsed

    provider_raw = request.args.get("provider")
    if not isinstance(provider_raw, str) or not provider_raw.strip():
        logger.warning(
            "请求参数校验失败 原因=缺少provider 路径=%s",
            request.path,
        )
        return (
            jsonify(
                {"error": "provider is required and must be a non-empty string"}
            ),
            400,
        )
    provider = provider_raw.strip()

    logger.info(
        "收到模型列表请求 sandbox_id=%s provider=%s 来源IP=%s",
        sandbox_id,
        provider,
        request.remote_addr,
    )
    try:
        models = list_opencode_models(sandbox_id, provider)
    except SandboxSessionNotFoundError as exc:
        logger.warning(
            "模型列表失败：沙箱不存在 sandbox_id=%s 错误=%s",
            sandbox_id,
            exc,
        )
        return jsonify({"error": str(exc)}), 404
    except OpenCodeError as exc:
        logger.warning(
            "模型列表失败 sandbox_id=%s provider=%s 错误=%s",
            sandbox_id,
            provider,
            preview(exc, 500),
        )
        return jsonify({"error": str(exc)}), 502
    return jsonify(
        {"sandbox_id": sandbox_id, "provider": provider, "models": models}
    )


@api_bp.get("/api/sessions")
def list_sessions():
    """列出沙箱内 OpenCode 历史 session（opencode session list --format json）。"""
    parsed = _require_query_sandbox_id()
    if not isinstance(parsed, str):
        return parsed
    sandbox_id = parsed
    logger.info(
        "收到 session 列表请求 sandbox_id=%s 来源IP=%s",
        sandbox_id,
        request.remote_addr,
    )
    try:
        sessions = list_opencode_sessions(sandbox_id)
    except SandboxSessionNotFoundError as exc:
        logger.warning(
            "session 列表失败：沙箱不存在 sandbox_id=%s 错误=%s",
            sandbox_id,
            exc,
        )
        return jsonify({"error": str(exc)}), 404
    except OpenCodeError as exc:
        logger.warning(
            "session 列表失败 sandbox_id=%s 错误=%s",
            sandbox_id,
            preview(exc, 500),
        )
        return jsonify({"error": str(exc)}), 502
    return jsonify({"sandbox_id": sandbox_id, "sessions": sessions})


@api_bp.get("/api/sessions/detail")
def get_session_export():
    """导出指定 OpenCode session 聊天详情（opencode export）。

    Query：sandbox_id、opencode_session_id（均为必填）。
    """
    parsed = _require_query_sandbox_id()
    if not isinstance(parsed, str):
        return parsed
    sandbox_id = parsed

    oc_raw = request.args.get("opencode_session_id")
    if not isinstance(oc_raw, str) or not oc_raw.strip():
        logger.warning(
            "请求参数校验失败 原因=缺少opencode_session_id 路径=%s",
            request.path,
        )
        return (
            jsonify(
                {
                    "error": (
                        "opencode_session_id is required and must be "
                        "a non-empty string"
                    )
                }
            ),
            400,
        )
    oc_id = oc_raw.strip()

    logger.info(
        "收到 session 详情请求 sandbox_id=%s opencode_session_id=%s 来源IP=%s",
        sandbox_id,
        oc_id,
        request.remote_addr,
    )
    try:
        exported = export_opencode_session(sandbox_id, oc_id)
    except SandboxSessionNotFoundError as exc:
        logger.warning(
            "session 详情失败：沙箱不存在 sandbox_id=%s 错误=%s",
            sandbox_id,
            exc,
        )
        return jsonify({"error": str(exc)}), 404
    except OpenCodeError as exc:
        logger.warning(
            "session 详情失败 sandbox_id=%s opencode_session_id=%s 错误=%s",
            sandbox_id,
            oc_id,
            preview(exc, 500),
        )
        return jsonify({"error": str(exc)}), 502
    return jsonify(
        {
            "sandbox_id": sandbox_id,
            "opencode_session_id": oc_id,
            "export": exported,
        }
    )
