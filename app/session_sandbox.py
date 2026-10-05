from __future__ import annotations

import atexit
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from app.config import Config
from app.sandbox_lifecycle import (
    SandboxLifecycleError,
    create_and_wait,
    refresh_sandbox,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SessionSandboxBinding:
    sandbox_id: str
    opencode_session_id: Optional[str] = None
    created_at: str = field(default_factory=_utc_now_iso)
    # monotonic 时钟上的预计过期时刻
    expires_at: float = 0.0
    # 当前正在该沙箱上执行的任务数（含 SSE 与异步 query）
    active_count: int = 0


class SessionSandboxManager:
    """进程内线程安全映射：session_id → 沙箱实例 + OpenCode 会话。"""

    def __init__(self) -> None:
        self._bindings: Dict[str, SessionSandboxBinding] = {}
        self._lock = threading.RLock()
        self._keeper_stop = threading.Event()
        self._keeper_thread: Optional[threading.Thread] = None

    def get(self, session_id: str) -> Optional[SessionSandboxBinding]:
        with self._lock:
            return self._bindings.get(session_id)

    def set_opencode_session_id(
        self, session_id: str, opencode_session_id: str
    ) -> None:
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                return
            binding.opencode_session_id = opencode_session_id

    def clear_opencode_session_id(self, session_id: str) -> None:
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                return
            binding.opencode_session_id = None

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._bindings.pop(session_id, None)

    def begin_task(self, session_id: str) -> None:
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                return
            binding.active_count += 1

    def end_task(self, session_id: str) -> None:
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                return
            binding.active_count = max(0, binding.active_count - 1)

    def ensure_sandbox(self, session_id: str) -> SessionSandboxBinding:
        """确保 session_id 已绑定未过期沙箱。空闲超时后下次请求再创建。

        Docker 模式跳过 APIG，使用合成的 sandbox_id。
        """
        if not session_id or not session_id.strip():
            raise SandboxLifecycleError("session_id is required")

        session_id = session_id.strip()

        if Config.SANDBOX_DOCKER_CONTAINER:
            with self._lock:
                binding = self._bindings.get(session_id)
                if binding is None:
                    binding = SessionSandboxBinding(
                        sandbox_id=f"docker:{session_id}",
                        expires_at=time.monotonic() + 24 * 3600,
                    )
                    self._bindings[session_id] = binding
                return binding

        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is not None and time.monotonic() < binding.expires_at:
                return binding
            if binding is not None:
                logger.info(
                    "sandbox expired session=%s sandbox=%s, recreating",
                    session_id,
                    binding.sandbox_id,
                )
                self._bindings.pop(session_id, None)

        sandbox_id, ttl = create_and_wait()
        binding = SessionSandboxBinding(
            sandbox_id=sandbox_id,
            expires_at=time.monotonic() + max(ttl, 1),
        )
        with self._lock:
            self._bindings[session_id] = binding
        return binding

    def refresh_busy_near_expiry(self) -> None:
        """有正在执行的任务，且剩余 TTL 进入安全窗口时，才调用 refresh 续期。"""
        if Config.SANDBOX_DOCKER_CONTAINER:
            return

        margin = Config.SANDBOX_KEEPALIVE_MARGIN_SECONDS
        now = time.monotonic()
        candidates: list[tuple[str, str]] = []
        with self._lock:
            for sid, binding in self._bindings.items():
                remaining = binding.expires_at - now
                if binding.active_count > 0 and remaining <= margin:
                    candidates.append((sid, binding.sandbox_id))

        duration = min(int(Config.SANDBOX_REFRESH_DURATION), 1800)
        for sid, sandbox_id in candidates:
            try:
                refresh_sandbox(sandbox_id, duration=duration)
            except SandboxLifecycleError as exc:
                logger.warning(
                    "keepalive refresh failed session=%s sandbox=%s: %s",
                    sid,
                    sandbox_id,
                    exc,
                )
                continue
            with self._lock:
                binding = self._bindings.get(sid)
                if binding is None or binding.sandbox_id != sandbox_id:
                    continue
                binding.expires_at = time.monotonic() + duration
            logger.info(
                "refreshed sandbox session=%s sandbox=%s duration=%ss",
                sid,
                sandbox_id,
                duration,
            )

    def start_keepalive(self) -> None:
        if Config.SANDBOX_DOCKER_CONTAINER:
            return
        if self._keeper_thread is not None and self._keeper_thread.is_alive():
            return
        self._keeper_stop.clear()
        self._keeper_thread = threading.Thread(
            target=self._keepalive_loop,
            name="sandbox-keepalive",
            daemon=True,
        )
        self._keeper_thread.start()
        atexit.register(self.stop_keepalive)

    def stop_keepalive(self) -> None:
        self._keeper_stop.set()

    def _keepalive_loop(self) -> None:
        interval = max(1.0, Config.SANDBOX_KEEPALIVE_INTERVAL_SECONDS)
        while not self._keeper_stop.wait(interval):
            try:
                self.refresh_busy_near_expiry()
            except Exception:
                logger.exception("sandbox keepalive loop error")


# 进程级单例，生命周期与 TaskStore 一致
session_sandbox_manager = SessionSandboxManager()
