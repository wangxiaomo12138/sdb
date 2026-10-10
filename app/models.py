from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.logging_utils import preview

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Task:
    task_id: str
    query: str
    session_id: str
    status: str = "pending"  # pending | running | succeeded | failed
    answer: Optional[str] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=_utc_now_iso)
    finished_at: Optional[str] = None
    current_stage: Optional[str] = None
    stages: List[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class TaskStore:
    """进程内线程安全的任务存储，服务重启后丢失。"""

    def __init__(self) -> None:
        self._tasks: Dict[str, Task] = {}
        self._lock = threading.Lock()
        logger.info("任务存储已初始化（进程内存）")

    def create(self, query: str, session_id: str) -> Task:
        task = Task(
            task_id=str(uuid.uuid4()),
            query=query,
            session_id=session_id,
        )
        with self._lock:
            self._tasks[task.task_id] = task
            total = len(self._tasks)
        logger.info(
            "任务已创建 任务ID=%s session_id=%s 状态=pending 当前任务数=%s 问题=%s",
            task.task_id,
            session_id,
            total,
            preview(query),
        )
        return task

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def append_stage(self, task_id: str, stage: str, message: str) -> None:
        entry = {
            "stage": stage,
            "message": message,
            "at": _utc_now_iso(),
        }
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                logger.warning("追加阶段失败：任务不存在 任务ID=%s", task_id)
                return
            task.stages.append(entry)
            task.current_stage = stage
        logger.info(
            "任务阶段更新 任务ID=%s stage=%s message=%s",
            task_id,
            stage,
            preview(message, 200),
        )

    def mark_running(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                logger.warning("标记运行失败：任务不存在 任务ID=%s", task_id)
                return
            task.status = "running"
        logger.info("任务状态变更 状态=running 任务ID=%s", task_id)

    def mark_succeeded(self, task_id: str, answer: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                logger.warning("标记成功失败：任务不存在 任务ID=%s", task_id)
                return
            task.status = "succeeded"
            task.answer = answer
            task.error = None
            task.finished_at = _utc_now_iso()
        logger.info(
            "任务状态变更 状态=succeeded 任务ID=%s 回复字符数=%s",
            task_id,
            len(answer or ""),
        )

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                logger.warning("标记失败失败：任务不存在 任务ID=%s", task_id)
                return
            task.status = "failed"
            task.error = error
            task.finished_at = _utc_now_iso()
        logger.warning(
            "任务状态变更 状态=failed 任务ID=%s 错误=%s",
            task_id,
            preview(error, 500),
        )
