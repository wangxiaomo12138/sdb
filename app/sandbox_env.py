from __future__ import annotations

import logging
import shlex
import uuid
from typing import Any, Generator, Optional
from urllib.parse import urlparse

from app.config import Config
from app.logging_utils import preview
from app.sandbox_client import OpenCodeError, run_sandbox_command, status_event
from app.sandbox_lifecycle import SandboxLifecycleError
from app.session_sandbox import session_sandbox_manager

logger = logging.getLogger(__name__)

# 环境初始化（拷贝/下载）默认超时
_ENV_CMD_TIMEOUT = 300.0


def _status(stage: str, message: str) -> dict[str, Any]:
    return status_event(stage, message)


def validate_skill_file_url(skill_file: Optional[str]) -> Optional[str]:
    """校验并规范化 skill_file；空则返回 None，非法则抛 ValueError。"""
    if skill_file is None:
        return None
    if not isinstance(skill_file, str):
        raise ValueError("skill_file must be a string URL")
    raw = skill_file.strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("skill_file must be an http(s) URL")
    return raw


def _copy_opencode_offline(sandbox_id: str) -> None:
    extract = Config.OPENCODE_OFFLINE_EXTRACT_PATH
    store = Config.OPENCODE_OFFLINE_STORE_PATH
    if not extract or not store:
        logger.info(
            "跳过OpenCode离线包拷贝：提取或存放路径未配置 extract=%s store=%s",
            extract or "(空)",
            store or "(空)",
        )
        return

    extract_q = shlex.quote(extract)
    store_q = shlex.quote(store)
    command = (
        f"mkdir -p {store_q} && "
        f"if [ ! -e {extract_q} ]; then "
        f"echo 'opencode extract path missing: {extract}' >&2; exit 1; "
        f"fi && "
        f"cp -a {extract_q}/. {store_q}/"
    )
    run_sandbox_command(command, sandbox_id=sandbox_id, timeout=_ENV_CMD_TIMEOUT)


def _download_skill_file(sandbox_id: str, skill_file: str) -> None:
    store = Config.SKILL_FILE_STORE_PATH
    if not store:
        raise OpenCodeError(
            "SKILL_FILE_STORE_PATH is required when skill_file is provided"
        )

    store_q = shlex.quote(store)
    url_q = shlex.quote(skill_file)
    # 文件名取 URL 最后一段；无则用固定名
    name = urlparse(skill_file).path.rstrip("/").split("/")[-1] or "skill_file"
    name_q = shlex.quote(name)
    tmp_q = shlex.quote(f"/tmp/skill_download_{uuid.uuid4().hex}")

    # 优先 curl，失败再 wget；按扩展名解压
    command = f"""
mkdir -p {store_q}
TMP={tmp_q}
NAME={name_q}
URL={url_q}
if command -v curl >/dev/null 2>&1; then
  curl -fsSL -o "$TMP" "$URL"
elif command -v wget >/dev/null 2>&1; then
  wget -q -O "$TMP" "$URL"
else
  echo 'neither curl nor wget available' >&2
  exit 1
fi
case "$NAME" in
  *.tar.gz|*.tgz)
    tar -xzf "$TMP" -C {store_q}
    ;;
  *.tar)
    tar -xf "$TMP" -C {store_q}
    ;;
  *.zip)
    if command -v unzip >/dev/null 2>&1; then
      unzip -o -q "$TMP" -d {store_q}
    else
      echo 'unzip not available for zip skill_file' >&2
      exit 1
    fi
    ;;
  *)
    mv "$TMP" {store_q}/"$NAME"
    TMP=""
    ;;
esac
if [ -n "$TMP" ] && [ -f "$TMP" ]; then
  rm -f "$TMP"
fi
"""
    run_sandbox_command(command, sandbox_id=sandbox_id, timeout=_ENV_CMD_TIMEOUT)


def stream_create_sandbox_env(
    *,
    skill_file: Optional[str] = None,
) -> Generator[dict[str, Any], None, None]:
    """创建沙箱环境：NAS 挂载随 APIG 创建生效，再拷贝 OpenCode、可选下载 skill。

    session_id 由服务端生成，在 ready 事件中返回。
    事件：status | ready | error
    """
    yield _status("request_received", "接收到用户请求")

    try:
        skill_url = validate_skill_file_url(skill_file)
    except ValueError as exc:
        yield {"type": "error", "message": str(exc)}
        return

    sid = uuid.uuid4().hex
    logger.info(
        "开始创建沙箱环境 session_id=%s skill_file=%s",
        sid,
        preview(skill_url or "(无)"),
    )

    yield _status("checking_sandbox", "检查沙箱状态")

    try:
        yield _status("creating_sandbox", "拉起沙箱")
        binding, _created = session_sandbox_manager.create_session_env(sid)
    except SandboxLifecycleError as exc:
        logger.error(
            "创建沙箱环境失败 session_id=%s 错误=%s", sid, preview(exc, 500)
        )
        yield {"type": "error", "message": str(exc)}
        return

    session_sandbox_manager.begin_task(sid)
    try:
        yield _status("init_opencode", "初始化 opencode 配置")
        try:
            _copy_opencode_offline(binding.sandbox_id)
            session_sandbox_manager.mark_opencode_initialized(sid)
        except OpenCodeError as exc:
            logger.error(
                "OpenCode离线包初始化失败 session_id=%s 错误=%s",
                sid,
                preview(exc, 500),
            )
            yield {"type": "error", "message": str(exc)}
            return

        if skill_url:
            yield _status("init_skill", "初始化 skill 配置")
            try:
                _download_skill_file(binding.sandbox_id, skill_url)
            except OpenCodeError as exc:
                logger.error(
                    "Skill初始化失败 session_id=%s 错误=%s",
                    sid,
                    preview(exc, 500),
                )
                yield {"type": "error", "message": str(exc)}
                return
        else:
            yield _status("init_skill", "跳过初始化 skill 配置（未提供 skill_file）")

        yield _status("sandbox_ready", "沙箱初始化完成")
        logger.info(
            "沙箱环境就绪 session_id=%s sandbox_id=%s",
            sid,
            binding.sandbox_id,
        )
        yield {"type": "ready", "session_id": sid}
    finally:
        session_sandbox_manager.end_task(sid)
