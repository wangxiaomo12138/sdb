from __future__ import annotations

import atexit
import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from app.config import Config
from app.sandbox_lifecycle import (
    SandboxLifecycleError,
    create_and_wait,
    refresh_sandbox,
)

logger = logging.getLogger(__name__)


@dataclass
class SessionSandboxBinding:
    sandbox_id: str
    opencode_session_id: Optional[str] = None
    # monotonic 时钟上的预计过期时刻
    expires_at: float = 0.0
    # 当前正在该沙箱上执行的任务数（含环境创建 SSE、对话与异步 query）
    active_count: int = 0
    # OpenCode 离线包是否已从提取路径拷贝到存放路径
    opencode_initialized: bool = False


class SessionSandboxManager:
    """进程内线程安全映射：session_id → 沙箱实例 + OpenCode 会话。"""

    def __init__(self) -> None:
        self._bindings: Dict[str, SessionSandboxBinding] = {}
        self._lock = threading.RLock()
        self._keeper_stop = threading.Event()
        self._keeper_thread: Optional[threading.Thread] = None
        logger.info("会话沙箱管理器已初始化")

    def get_active(self, session_id: str) -> Optional[SessionSandboxBinding]:
        """返回未过期绑定；已过期则清除并返回 None。"""
        if not session_id or not session_id.strip():
            return None
        session_id = session_id.strip()
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                return None
            if time.monotonic() >= binding.expires_at:
                logger.info(
                    "沙箱已过期，清除映射 session_id=%s sandbox_id=%s",
                    session_id,
                    binding.sandbox_id,
                )
                self._bindings.pop(session_id, None)
                return None
            return binding

    def create_session_env(
        self,
        session_id: str,
        *,
        user_data_subpath: Optional[str] = None,
    ) -> Tuple[SessionSandboxBinding, bool]:
        """确保 session 绑定可用沙箱。

        Returns:
            (binding, created)：created=True 表示本次新建了沙箱实例。
        """
        if not session_id or not session_id.strip():
            raise SandboxLifecycleError("session_id is required")

        session_id = session_id.strip()
        logger.info(
            "开始创建或复用沙箱环境 session_id=%s user_data_subpath=%s",
            session_id,
            (user_data_subpath or "").strip() or "(配置默认)",
        )

        existing = self.get_active(session_id)
        if existing is not None:
            remaining = existing.expires_at - time.monotonic()
            logger.info(
                "复用未过期沙箱 session_id=%s sandbox_id=%s "
                "opencode_initialized=%s 进行中任务数=%s 剩余TTL秒=%.1f",
                session_id,
                existing.sandbox_id,
                existing.opencode_initialized,
                existing.active_count,
                remaining,
            )
            return existing, False

        if Config.SANDBOX_DOCKER_CONTAINER:
            with self._lock:
                # get_active 已清过期；此处仍可能并发写入
                binding = self._bindings.get(session_id)
                if binding is not None and time.monotonic() < binding.expires_at:
                    return binding, False
                binding = SessionSandboxBinding(
                    sandbox_id=f"docker:{session_id}",
                    expires_at=time.monotonic() + 24 * 3600,
                )
                self._bindings[session_id] = binding
            logger.info(
                "Docker模式新建会话映射 session_id=%s sandbox_id=%s 容器名=%s "
                "user_data_subpath=%s",
                session_id,
                binding.sandbox_id,
                Config.SANDBOX_DOCKER_CONTAINER,
                (user_data_subpath or "").strip() or "(忽略)",
            )
            return binding, True

        started = time.monotonic()
        sandbox_id, ttl = create_and_wait(user_data_subpath=user_data_subpath)
        binding = SessionSandboxBinding(
            sandbox_id=sandbox_id,
            expires_at=time.monotonic() + max(ttl, 1),
        )
        with self._lock:
            self._bindings[session_id] = binding
            size = len(self._bindings)
        logger.info(
            "沙箱创建并绑定完成 session_id=%s sandbox_id=%s TTL秒=%s "
            "user_data_subpath=%s 耗时毫秒=%.1f 当前映射数=%s",
            session_id,
            sandbox_id,
            ttl,
            (user_data_subpath or "").strip() or "(配置默认)",
            (time.monotonic() - started) * 1000,
            size,
        )
        return binding, True

    def mark_opencode_initialized(self, session_id: str) -> None:
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                logger.warning(
                    "标记OpenCode已初始化跳过：找不到会话映射 session_id=%s",
                    session_id,
                )
                return
            binding.opencode_initialized = True
        logger.info(
            "已标记OpenCode离线包初始化完成 session_id=%s sandbox_id=%s",
            session_id,
            binding.sandbox_id,
        )

    def set_opencode_session_id(
        self, session_id: str, opencode_session_id: str
    ) -> None:
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                logger.warning(
                    "绑定OpenCode会话跳过：找不到会话映射 session_id=%s",
                    session_id,
                )
                return
            old = binding.opencode_session_id
            binding.opencode_session_id = opencode_session_id
        logger.info(
            "已绑定OpenCode会话 session_id=%s sandbox_id=%s "
            "opencode_session_id=%s 原值=%s",
            session_id,
            binding.sandbox_id,
            opencode_session_id,
            old or "(无)",
        )

    def clear_opencode_session_id(self, session_id: str) -> None:
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                return
            old = binding.opencode_session_id
            binding.opencode_session_id = None
        if old:
            logger.warning(
                "已清除OpenCode会话 session_id=%s sandbox_id=%s "
                "原opencode_session_id=%s",
                session_id,
                binding.sandbox_id,
                old,
            )

    def begin_task(self, session_id: str) -> None:
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                logger.warning(
                    "任务开始计数跳过：找不到会话映射 session_id=%s", session_id
                )
                return
            binding.active_count += 1
            active = binding.active_count
            sandbox_id = binding.sandbox_id
        logger.info(
            "任务占用沙箱开始 session_id=%s sandbox_id=%s 进行中任务数=%s",
            session_id,
            sandbox_id,
            active,
        )

    def end_task(self, session_id: str) -> None:
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                logger.warning(
                    "任务结束计数跳过：找不到会话映射 session_id=%s", session_id
                )
                return
            binding.active_count = max(0, binding.active_count - 1)
            active = binding.active_count
            sandbox_id = binding.sandbox_id
            remaining = binding.expires_at - time.monotonic()
        logger.info(
            "任务占用沙箱结束 session_id=%s sandbox_id=%s 进行中任务数=%s "
            "剩余TTL秒=%.1f",
            session_id,
            sandbox_id,
            active,
            remaining,
        )

    def refresh_busy_near_expiry(self) -> None:
        """有正在执行的任务，且剩余 TTL 进入安全窗口时，才调用 refresh 续期。"""
        if Config.SANDBOX_DOCKER_CONTAINER:
            return

        margin = Config.SANDBOX_KEEPALIVE_MARGIN_SECONDS
        now = time.monotonic()
        candidates: list[tuple[str, str, float, int]] = []
        with self._lock:
            for sid, binding in self._bindings.items():
                remaining = binding.expires_at - now
                if binding.active_count > 0 and remaining <= margin:
                    candidates.append(
                        (sid, binding.sandbox_id, remaining, binding.active_count)
                    )

        if not candidates:
            logger.debug(
                "保活扫描：无需续期 当前映射数=%s 续期阈值秒=%s",
                len(self._bindings),
                margin,
            )
            return

        duration = min(int(Config.SANDBOX_REFRESH_DURATION), 1800)
        logger.info(
            "保活扫描：发现待续期沙箱 数量=%s 续期秒数=%s",
            len(candidates),
            duration,
        )
        for sid, sandbox_id, remaining, active in candidates:
            logger.info(
                "开始续期沙箱 session_id=%s sandbox_id=%s "
                "剩余TTL秒=%.1f 进行中任务数=%s 续期秒数=%s",
                sid,
                sandbox_id,
                remaining,
                active,
                duration,
            )
            try:
                refresh_sandbox(sandbox_id, duration=duration)
            except SandboxLifecycleError as exc:
                logger.warning(
                    "续期沙箱失败 session_id=%s sandbox_id=%s 错误=%s",
                    sid,
                    sandbox_id,
                    exc,
                )
                continue
            with self._lock:
                binding = self._bindings.get(sid)
                if binding is None or binding.sandbox_id != sandbox_id:
                    logger.warning(
                        "续期成功但映射已变化，忽略本地TTL更新 "
                        "session_id=%s sandbox_id=%s",
                        sid,
                        sandbox_id,
                    )
                    continue
                binding.expires_at = time.monotonic() + duration
            logger.info(
                "续期沙箱成功 session_id=%s sandbox_id=%s 续期秒数=%s",
                sid,
                sandbox_id,
                duration,
            )

    def start_keepalive(self) -> None:
        if Config.SANDBOX_DOCKER_CONTAINER:
            logger.info("保活线程未启动（Docker模式无需保活）")
            return
        if self._keeper_thread is not None and self._keeper_thread.is_alive():
            logger.info("保活线程已在运行")
            return
        self._keeper_stop.clear()
        self._keeper_thread = threading.Thread(
            target=self._keepalive_loop,
            name="sandbox-keepalive",
            daemon=True,
        )
        self._keeper_thread.start()
        atexit.register(self.stop_keepalive)
        logger.info(
            "保活线程已启动 扫描间隔秒=%s 续期阈值秒=%s 续期秒数=%s",
            Config.SANDBOX_KEEPALIVE_INTERVAL_SECONDS,
            Config.SANDBOX_KEEPALIVE_MARGIN_SECONDS,
            Config.SANDBOX_REFRESH_DURATION,
        )

    def stop_keepalive(self) -> None:
        logger.info("保活线程正在停止")
        self._keeper_stop.set()

    def _keepalive_loop(self) -> None:
        interval = max(1.0, Config.SANDBOX_KEEPALIVE_INTERVAL_SECONDS)
        logger.info("保活循环进入 扫描间隔秒=%s", interval)
        while not self._keeper_stop.wait(interval):
            try:
                self.refresh_busy_near_expiry()
            except Exception:
                logger.exception("保活循环发生异常")
        logger.info("保活循环已退出")


# 进程级单例，生命周期与 TaskStore 一致
session_sandbox_manager = SessionSandboxManager()
