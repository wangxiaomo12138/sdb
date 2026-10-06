from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Tuple, Union

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from app.logging_utils import preview
from app.sandbox_client import check_sandbox_health, stream_opencode

api_bp = Blueprint("api", __name__)
logger = logging.getLogger(__name__)


def _parse_session_query(
    body: dict,
) -> Union[Tuple[str, str], Tuple[None, Tuple[Response, int]]]:
    query = body.get("query")
    session_id = body.get("session_id")
    if not isinstance(query, str) or not query.strip():
        logger.warning(
            "请求参数校验失败 原因=缺少query 路径=%s",
            request.path,
        )
        return None, (
            jsonify({"error": "query is required and must be a non-empty string"}),
            400,
        )
    if not isinstance(session_id, str) or not session_id.strip():
        logger.warning(
            "请求参数校验失败 原因=缺少session_id 路径=%s",
            request.path,
        )
        return None, (
            jsonify(
                {"error": "session_id is required and must be a non-empty string"}
            ),
            400,
        )
    return session_id.strip(), query.strip()


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


@api_bp.post("/api/chat")
def chat():
    """SSE 流式对话。同一 session_id 续接 OpenCode 多轮会话。"""
    req_id = uuid.uuid4().hex[:12]
    body = request.get_json(silent=True) or {}
    parsed = _parse_session_query(body)
    if parsed[0] is None:
        err_resp, status = parsed[1]  # type: ignore[misc]
        return err_resp, status
    session_id, query = parsed  # type: ignore[misc]
    logger.info(
        "收到流式对话请求 请求ID=%s session_id=%s 问题=%s 来源IP=%s",
        req_id,
        session_id,
        preview(query),
        request.remote_addr,
    )

    def generate():
        started = time.monotonic()
        event_counts = {"delta": 0, "done": 0, "error": 0, "other": 0}
        answer_chars = 0
        try:
            for event in stream_opencode(query, session_id=session_id):
                etype = event.get("type") or "other"
                if etype == "delta":
                    event_counts["delta"] += 1
                    answer_chars += len(event.get("text") or "")
                elif etype in event_counts:
                    event_counts[etype] += 1
                else:
                    event_counts["other"] += 1
                if etype in {"done", "error"}:
                    type_label = "完成" if etype == "done" else "错误"
                    logger.info(
                        "流式对话事件 请求ID=%s session_id=%s 类型=%s 内容=%s",
                        req_id,
                        session_id,
                        type_label,
                        preview(event.get("message") or event.get("answer") or ""),
                    )
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception(
                "流式对话异常 请求ID=%s session_id=%s",
                req_id,
                session_id,
            )
            err = {"type": "error", "message": "unexpected stream error"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000
            logger.info(
                "流式对话结束 请求ID=%s session_id=%s 耗时毫秒=%.1f "
                "增量事件数=%s 完成=%s 错误=%s 回复字符数=%s",
                req_id,
                session_id,
                elapsed_ms,
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
    parsed = _parse_session_query(body)
    if parsed[0] is None:
        err_resp, status = parsed[1]  # type: ignore[misc]
        return err_resp, status
    session_id, query = parsed  # type: ignore[misc]

    store = current_app.extensions["task_store"]
    worker = current_app.extensions["task_worker"]

    task = store.create(query, session_id=session_id)
    # 提交前先快照：worker 可能立刻改同一 Task 对象
    payload = {
        "task_id": task.task_id,
        "session_id": task.session_id,
        "status": "pending",
    }
    logger.info(
        "异步查询已受理 任务ID=%s session_id=%s 问题=%s 来源IP=%s",
        task.task_id,
        session_id,
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
        "查询任务成功 任务ID=%s session_id=%s 状态=%s",
        task.task_id,
        task.session_id,
        task.status,
    )
    return jsonify(task.to_dict())
