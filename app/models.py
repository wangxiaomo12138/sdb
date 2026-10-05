from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional


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

    def to_dict(self) -> dict:
        return asdict(self)


class TaskStore:
    """进程内线程安全的任务存储，服务重启后丢失。"""

    def __init__(self) -> None:
        self._tasks: Dict[str, Task] = {}
        self._lock = threading.Lock()

    def create(self, query: str, session_id: str) -> Task:
        task = Task(
            task_id=str(uuid.uuid4()),
            query=query,
            session_id=session_id,
        )
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def mark_running(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = "running"

    def mark_succeeded(self, task_id: str, answer: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = "succeeded"
            task.answer = answer
            task.error = None
            task.finished_at = _utc_now_iso()

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = "failed"
            task.error = error
            task.finished_at = _utc_now_iso()
