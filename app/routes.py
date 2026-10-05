from __future__ import annotations

import json
from typing import Tuple, Union

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from app.sandbox_client import check_sandbox_health, stream_opencode

api_bp = Blueprint("api", __name__)


def _parse_session_query(
    body: dict,
) -> Union[Tuple[str, str], Tuple[None, Tuple[Response, int]]]:
    query = body.get("query")
    session_id = body.get("session_id")
    if not isinstance(query, str) or not query.strip():
        return None, (
            jsonify({"error": "query is required and must be a non-empty string"}),
            400,
        )
    if not isinstance(session_id, str) or not session_id.strip():
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
    payload = {"status": "ok"}
    if probe:
        ok, detail = check_sandbox_health()
        payload["sandbox"] = {"ok": ok, "detail": detail}
        if not ok:
            return jsonify(payload), 503
    return jsonify(payload)


@api_bp.post("/api/chat")
def chat():
    """SSE 流式对话。同一 session_id 续接 OpenCode 多轮会话。"""
    body = request.get_json(silent=True) or {}
    parsed = _parse_session_query(body)
    if parsed[0] is None:
        err_resp, status = parsed[1]  # type: ignore[misc]
        return err_resp, status
    session_id, query = parsed  # type: ignore[misc]

    def generate():
        for event in stream_opencode(query, session_id=session_id):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

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
    worker.submit(task.task_id)
    return jsonify(payload), 202


@api_bp.get("/api/tasks/<task_id>")
def get_task(task_id: str):
    store = current_app.extensions["task_store"]
    task = store.get(task_id)
    if task is None:
        return jsonify({"error": "task not found"}), 404
    return jsonify(task.to_dict())
