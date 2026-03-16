from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import re
import tempfile
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Union


logger = logging.getLogger(__name__)


_WHITESPACE_RE = re.compile(r"\s+")
_INVALID_CHARS_RE = re.compile(r"[^0-9A-Za-z가-힣_-]")
_MULTI_UNDERSCORE_RE = re.compile(r"_+")


# 현재 시각 기반 경로/이름에 쓰기 좋은 타임스탬프를 만든다.
def local_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# 파일명으로 안전한 스템을 만들기 위해 공백/특수문자를 정리한다.
def sanitize_stem(stem: str, max_length: int = 80) -> str:
    if not stem:
        return "audio"

    name = stem.strip()
    name = _WHITESPACE_RE.sub("_", name)
    # 경로 구분자와 파일명 특수문자를 제거해 안전한 파일명으로 변환한다.
    name = name.replace("/", "_").replace("\\", "_")
    name = name.replace(":", "_").replace("*", "_")
    name = name.replace("?", "_").replace("\"", "_")
    name = name.replace("<", "_").replace(">", "_").replace("|", "_")
    name = _INVALID_CHARS_RE.sub("_", name)
    name = _MULTI_UNDERSCORE_RE.sub("_", name)
    name = name.strip("._-")

    if not name:
        return "audio"

    name = name[:max_length]
    name = name.strip("._-")
    return name or "audio"


# 임시 식별자 생성에 쓰는 짧은 랜덤 문자열을 반환한다.
def short_id(length: int = 8) -> str:
    return uuid.uuid4().hex[:length]


# 경로가 없으면 생성하고 Path 객체를 반환한다.
def ensure_dir(path: Union[str, Path]) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# ISO8601 형식의 현재 시각 문자열을 반환한다.
def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


# 예외의 스택트레이스를 문자열로 변환한다.
def stacktrace(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


# 파일의 SHA-256 해시를 계산한다.
def compute_sha256(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# 파일 쓰기 실패를 줄이기 위해 임시파일-교체 방식으로 저장한다.
def atomic_write(path: Union[str, Path], data: Any, encoding: str = "utf-8") -> None:
    target = Path(path)
    ensure_dir(target.parent)

    if isinstance(data, (dict, list)):
        content = json.dumps(data, ensure_ascii=False, indent=2)
        mode = "w"
        kwargs = {"encoding": encoding}
    elif isinstance(data, str):
        content = data
        mode = "w"
        kwargs = {"encoding": encoding}
    elif isinstance(data, (bytes, bytearray)):
        content = data
        mode = "wb"
        kwargs = {}
    else:
        content = str(data)
        mode = "w"
        kwargs = {"encoding": encoding}

    with tempfile.NamedTemporaryFile(
        mode=mode,
        delete=False,
        dir=str(target.parent),
        prefix=f".{target.name}",
        suffix=".tmp",
        **kwargs,
    ) as f:
        f.write(content)
        temp_path = Path(f.name)

    os.replace(temp_path, target)


# os.replace 실패(크로스 디바이스) 시 복사-동기화-교체 방식으로 보완한다.
def safe_move_file(src: Union[str, Path], dst: Union[str, Path]) -> None:
    src_path = Path(src)
    dst_path = Path(dst)
    if not src_path.exists():
        raise FileNotFoundError(f"Source file does not exist: {src_path}")
    if dst_path.exists():
        raise FileExistsError(f"Destination file already exists: {dst_path}")

    ensure_dir(dst_path.parent)

    try:
        os.replace(src_path, dst_path)
        return
    except OSError:
        # 다른 장치 이동인 경우를 대비해 copy/fync/replace로 대체 이동한다.
        tmp_path = dst_path.with_name(f".{dst_path.name}.{uuid.uuid4().hex}.tmp")
        copied = False
        try:
            shutil.copy2(src_path, tmp_path)
            copied = True
            try:
                with open(tmp_path, "rb") as handle:
                    os.fsync(handle.fileno())
            except OSError:
                logger.debug("Unable to fsync temporary copy %s", tmp_path, exc_info=True)

            os.replace(tmp_path, dst_path)
            try:
                src_path.unlink()
            except OSError:
                logger.warning("Failed to remove source file after copy-move: %s", src_path, exc_info=True)
            return
        finally:
            if copied and tmp_path.exists():
                # os.replace가 이미 소비한 경우는 삭제 시도해도 대부분 no-op이다.
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            if not copied and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


def is_temporary_file(path: Path) -> bool:
    # 전송 중 임시 파일은 폴더에서 제외한다.
    name = path.name
    if name.startswith("."):
        return True
    if name.startswith("~"):
        return True
    lower = name.lower()
    if lower.endswith(".tmp") or lower.endswith(".part"):
        return True
    return False


def read_text_file(path: Union[str, Path], default: str = "") -> str:
    # 파일이 없으면 기본값을 돌려 호출부에서 별도 예외 분기를 줄인다.
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


# 현재 epoch 시각을 반환한다.
def now() -> float:
    return time.time()


# 설정이 있으면 그 db 위치 기준으로 pause 플래그 경로를 산출한다.
def get_pause_flag_path(config: dict | None = None) -> Path:
    if isinstance(config, dict):
        db_path = (config.get("paths") or {}).get("db_path")
        if db_path:
            try:
                return Path(db_path).expanduser().parent / "paused"
            except Exception:
                pass

    return Path("/Users/geonha/lecture_stt/state/paused")


def is_paused(config: dict | None = None) -> bool:
    # pause 플래그 존재로 파이프라인 일시정지 상태를 판단한다.
    return get_pause_flag_path(config).exists()


# pause 플래그를 생성/삭제해 즉시 처리 중지를 제어한다.
def set_paused(paused: bool, config: dict | None = None) -> None:
    pause_path = get_pause_flag_path(config)
    pause_path.parent.mkdir(parents=True, exist_ok=True)
    if paused:
        pause_path.touch(exist_ok=True)
    else:
        pause_path.unlink(missing_ok=True)
