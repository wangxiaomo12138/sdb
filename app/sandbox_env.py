from __future__ import annotations

import logging
import shlex
import time
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


def _status(stage: str, message: str, *, session_id: str = "") -> dict[str, Any]:
    logger.info(
        "沙箱环境初始化阶段 session_id=%s stage=%s message=%s",
        session_id or "(未分配)",
        stage,
        message,
    )
    return status_event(stage, message)


def validate_skill_file_urls(skill_file: Optional[str]) -> Optional[list[str]]:
    """校验并规范化 skill_file；支持英文逗号分隔多个 URL。

    空则返回 None，非法则抛 ValueError。
    """
    if skill_file is None:
        return None
    if not isinstance(skill_file, str):
        raise ValueError("skill_file must be a string of http(s) URL(s)")
    raw = skill_file.strip()
    if not raw:
        return None
    urls: list[str] = []
    for part in raw.split(","):
        url = part.strip()
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                f"skill_file entries must be http(s) URLs, got {url!r}"
            )
        urls.append(url)
    return urls or None


# 兼容旧名称
def validate_skill_file_url(skill_file: Optional[str]) -> Optional[list[str]]:
    return validate_skill_file_urls(skill_file)


def validate_create_env_params(body: dict[str, Any]) -> str:
    """从请求体取出用户数据 NAS 的 sub_path（兼容 camelCase），必填。"""
    sub_path = body.get("sub_path", body.get("subPath"))
    if not isinstance(sub_path, str) or not sub_path.strip():
        raise ValueError("sub_path is required and must be a non-empty string")
    return sub_path.strip()


def _copy_opencode_offline(sandbox_id: str) -> None:
    extract = Config.OPENCODE_OFFLINE_EXTRACT_PATH
    store = Config.OPENCODE_OFFLINE_STORE_PATH
    if not extract or not store:
        logger.info(
            "跳过OpenCode离线包拷贝 sandbox_id=%s extract=%s store=%s",
            sandbox_id,
            extract or "(空)",
            store or "(空)",
        )
        return

    logger.info(
        "开始拷贝OpenCode离线包 sandbox_id=%s extract=%s store=%s 超时秒=%s",
        sandbox_id,
        extract,
        store,
        _ENV_CMD_TIMEOUT,
    )
    started = time.monotonic()
    extract_q = shlex.quote(extract)
    store_q = shlex.quote(store)
    command = (
        f"mkdir -p {store_q} && "
        f"if [ ! -e {extract_q} ]; then "
        f"echo 'opencode extract path missing: {extract}' >&2; exit 1; "
        f"fi && "
        f"cp -a {extract_q} {store_q}/"
    )
    run_sandbox_command(command, sandbox_id=sandbox_id, timeout=_ENV_CMD_TIMEOUT)
    logger.info(
        "OpenCode离线包拷贝完成 sandbox_id=%s 耗时毫秒=%.1f",
        sandbox_id,
        (time.monotonic() - started) * 1000,
    )


def _skill_base_name(filename: str) -> str:
    lower = filename.lower()
    for suffix in (".tar.gz", ".tgz", ".tar", ".zip"):
        if lower.endswith(suffix):
            return filename[: -len(suffix)]
    if "." in filename:
        return filename.rsplit(".", 1)[0] or filename
    return filename


def _download_one_skill_file(sandbox_id: str, skill_file: str, store: str) -> None:
    store_q = shlex.quote(store)
    url_q = shlex.quote(skill_file)
    # 文件名取 URL 最后一段；无则用固定名
    name = urlparse(skill_file).path.rstrip("/").split("/")[-1] or "skill_file"
    base = _skill_base_name(name)
    dest = f"{store.rstrip('/')}/{base}"
    name_q = shlex.quote(name)
    tmp_q = shlex.quote(f"/tmp/skill_download_{uuid.uuid4().hex}")

    logger.info(
        "开始下载skill sandbox_id=%s 文件名=%s 目标目录=%s url=%s",
        sandbox_id,
        name,
        dest,
        preview(skill_file),
    )
    started = time.monotonic()

    # 优先 curl（-k 跳过证书校验，适配隔离网自签/内网证书；-S 在 -s 下仍输出错误）
    # 失败再 wget（--no-check-certificate）；按扩展名解压到以文件名命名的子目录
    command = f"""
mkdir -p {store_q}
TMP={tmp_q}
NAME={name_q}
URL={url_q}
if command -v curl >/dev/null 2>&1; then
  echo "skill download using curl" >&2
  if ! curl -kfsSL -o "$TMP" "$URL"; then
    echo "skill_file download failed via curl: $URL" >&2
    exit 1
  fi
elif command -v wget >/dev/null 2>&1; then
  echo "skill download using wget" >&2
  if ! wget --no-check-certificate -O "$TMP" "$URL"; then
    echo "skill_file download failed via wget: $URL" >&2
    exit 1
  fi
else
  echo 'neither curl nor wget available' >&2
  exit 1
fi
if [ ! -s "$TMP" ]; then
  echo "skill_file download produced empty file: $URL" >&2
  exit 1
fi
echo "skill download size bytes=$(wc -c < "$TMP")" >&2
# 按 skill 文件名（去掉压缩后缀）在存放目录下建子目录，再解压/落盘到该目录
case "$NAME" in
  *.tar.gz) BASE="${{NAME%.tar.gz}}" ;;
  *.tgz)    BASE="${{NAME%.tgz}}" ;;
  *.tar)    BASE="${{NAME%.tar}}" ;;
  *.zip)    BASE="${{NAME%.zip}}" ;;
  *)        BASE="${{NAME%.*}}"
            if [ -z "$BASE" ] || [ "$BASE" = "$NAME" ]; then BASE="$NAME"; fi
            ;;
esac
if [ -z "$BASE" ]; then
  echo "skill_file base name empty: $NAME" >&2
  exit 1
fi
DEST={store_q}/"$BASE"
mkdir -p "$DEST"
echo "skill extract dest=$DEST name=$NAME" >&2
case "$NAME" in
  *.tar.gz|*.tgz)
    tar -xzf "$TMP" -C "$DEST"
    ;;
  *.tar)
    tar -xf "$TMP" -C "$DEST"
    ;;
  *.zip)
    if command -v unzip >/dev/null 2>&1; then
      unzip -o -q "$TMP" -d "$DEST"
    else
      echo 'unzip not available for zip skill_file' >&2
      exit 1
    fi
    ;;
  *)
    mv "$TMP" "$DEST"/"$NAME"
    TMP=""
    ;;
esac
if [ -n "$TMP" ] && [ -f "$TMP" ]; then
  rm -f "$TMP"
fi
echo "skill install done dest=$DEST" >&2
"""
    output = run_sandbox_command(
        command, sandbox_id=sandbox_id, timeout=_ENV_CMD_TIMEOUT
    )
    logger.info(
        "skill下载并安装完成 sandbox_id=%s 文件名=%s 目标目录=%s "
        "耗时毫秒=%.1f 命令输出=%s",
        sandbox_id,
        name,
        dest,
        (time.monotonic() - started) * 1000,
        preview(output, 500),
    )


def _download_skill_files(sandbox_id: str, skill_urls: list[str]) -> None:
    store = Config.SKILL_FILE_STORE_PATH
    if not store:
        raise OpenCodeError(
            "SKILL_FILE_STORE_PATH is required when skill_file is provided"
        )
    logger.info(
        "开始批量下载skill sandbox_id=%s 数量=%s 存放目录=%s",
        sandbox_id,
        len(skill_urls),
        store,
    )
    batch_started = time.monotonic()
    for index, url in enumerate(skill_urls, start=1):
        logger.info(
            "下载skill文件 序号=%s/%s sandbox_id=%s url=%s",
            index,
            len(skill_urls),
            sandbox_id,
            preview(url),
        )
        try:
            _download_one_skill_file(sandbox_id, url, store)
        except OpenCodeError:
            logger.error(
                "下载skill失败 序号=%s/%s sandbox_id=%s url=%s",
                index,
                len(skill_urls),
                sandbox_id,
                preview(url),
            )
            raise
    logger.info(
        "批量下载skill完成 sandbox_id=%s 数量=%s 耗时毫秒=%.1f",
        sandbox_id,
        len(skill_urls),
        (time.monotonic() - batch_started) * 1000,
    )


def stream_create_sandbox_env(
    *,
    sub_path: str,
    skill_file: Optional[str] = None,
) -> Generator[dict[str, Any], None, None]:
    """创建沙箱环境：NAS 挂载随 APIG 创建生效，再拷贝 OpenCode、可选下载 skill。

    sub_path 由调用方传入，写入用户数据 NAS 的 subPath 以实现用户隔离；
    沙箱 templateId 与 OpenCode NAS 仍使用服务端配置。
    session_id 由服务端生成，在 ready 事件中返回。
    事件：status | ready | error
    """
    overall_started = time.monotonic()
    yield _status("request_received", "接收到用户请求")

    try:
        skill_urls = validate_skill_file_urls(skill_file)
    except ValueError as exc:
        logger.warning("skill_file参数校验失败 错误=%s", exc)
        yield {"type": "error", "message": str(exc)}
        return

    user_subpath = (sub_path or "").strip()
    if not user_subpath:
        msg = "sub_path is required"
        logger.warning("创建环境参数校验失败 错误=%s", msg)
        yield {"type": "error", "message": msg}
        return

    sid = uuid.uuid4().hex
    x_mounts = Config.build_x_mounts(user_data_subpath=user_subpath)
    logger.info(
        "开始创建沙箱环境 session_id=%s template_id=%s user_data_subpath=%s "
        "skill_file数量=%s skill_file=%s X-mounts=%s "
        "OpenCode提取路径=%s OpenCode存放路径=%s Skill存放路径=%s",
        sid,
        Config.SANDBOX_TEMPLATE_ID or "(未配置)",
        user_subpath,
        len(skill_urls or []),
        preview(",".join(skill_urls) if skill_urls else "(无)"),
        x_mounts if x_mounts is not None else "(未配置)",
        Config.OPENCODE_OFFLINE_EXTRACT_PATH or "(未配置)",
        Config.OPENCODE_OFFLINE_STORE_PATH or "(未配置)",
        Config.SKILL_FILE_STORE_PATH or "(未配置)",
    )

    yield _status("checking_sandbox", "检查沙箱状态", session_id=sid)

    try:
        yield _status("creating_sandbox", "拉起沙箱", session_id=sid)
        create_started = time.monotonic()
        binding, created = session_sandbox_manager.create_session_env(
            sid,
            user_data_subpath=user_subpath,
        )
        logger.info(
            "沙箱实例就绪 session_id=%s sandbox_id=%s 新建=%s "
            "user_data_subpath=%s 耗时毫秒=%.1f",
            sid,
            binding.sandbox_id,
            created,
            user_subpath,
            (time.monotonic() - create_started) * 1000,
        )
    except SandboxLifecycleError as exc:
        logger.error(
            "创建沙箱环境失败 session_id=%s 错误=%s 总耗时毫秒=%.1f",
            sid,
            preview(exc, 500),
            (time.monotonic() - overall_started) * 1000,
        )
        yield {"type": "error", "message": str(exc)}
        return

    session_sandbox_manager.begin_task(sid)
    try:
        yield _status("init_opencode", "初始化 opencode 配置", session_id=sid)
        try:
            _copy_opencode_offline(binding.sandbox_id)
            session_sandbox_manager.mark_opencode_initialized(sid)
            logger.info(
                "OpenCode初始化完成 session_id=%s sandbox_id=%s",
                sid,
                binding.sandbox_id,
            )
        except OpenCodeError as exc:
            logger.error(
                "OpenCode离线包初始化失败 session_id=%s sandbox_id=%s 错误=%s",
                sid,
                binding.sandbox_id,
                preview(exc, 500),
            )
            yield {"type": "error", "message": str(exc)}
            return

        if skill_urls:
            yield _status(
                "init_skill",
                f"初始化 skill 配置（共 {len(skill_urls)} 个）",
                session_id=sid,
            )
            try:
                _download_skill_files(binding.sandbox_id, skill_urls)
                logger.info(
                    "Skill初始化完成 session_id=%s sandbox_id=%s 数量=%s",
                    sid,
                    binding.sandbox_id,
                    len(skill_urls),
                )
            except OpenCodeError as exc:
                logger.error(
                    "Skill初始化失败 session_id=%s sandbox_id=%s 错误=%s",
                    sid,
                    binding.sandbox_id,
                    preview(exc, 500),
                )
                yield {"type": "error", "message": str(exc)}
                return
        else:
            yield _status(
                "init_skill",
                "跳过初始化 skill 配置（未提供 skill_file）",
                session_id=sid,
            )
            logger.info(
                "跳过Skill初始化 session_id=%s sandbox_id=%s",
                sid,
                binding.sandbox_id,
            )

        yield _status("sandbox_ready", "沙箱初始化完成", session_id=sid)
        logger.info(
            "沙箱环境就绪 session_id=%s sandbox_id=%s 总耗时毫秒=%.1f",
            sid,
            binding.sandbox_id,
            (time.monotonic() - overall_started) * 1000,
        )
        yield {"type": "ready", "session_id": sid}
    finally:
        session_sandbox_manager.end_task(sid)
        logger.info(
            "沙箱环境初始化占用结束 session_id=%s sandbox_id=%s",
            sid,
            binding.sandbox_id,
        )
