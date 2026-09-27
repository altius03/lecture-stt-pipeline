from __future__ import annotations

import errno
import ctypes
import hashlib
import json
import logging
import os
import re
import stat
import sys
import tempfile
import time
import traceback
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Union

from lecture_stt.shared.paths import pause_flag_path


logger = logging.getLogger(__name__)


_WHITESPACE_RE = re.compile(r"\s+")
_MULTI_UNDERSCORE_RE = re.compile(r"_+")


# 현재 시각 기반 경로/이름에 쓰기 좋은 타임스탬프를 만든다.
def local_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# 파일명으로 안전한 스템을 만들기 위해 공백/특수문자를 정리한다.
def sanitize_stem(stem: str, max_length: int = 80) -> str:
    if not stem:
        return "audio"

    name = unicodedata.normalize("NFC", str(stem)).strip()
    name = _WHITESPACE_RE.sub("_", name)
    # 경로 구분자와 파일명 특수문자를 제거해 안전한 파일명으로 변환한다.
    name = name.replace("/", "_").replace("\\", "_")
    name = name.replace(":", "_").replace("*", "_")
    name = name.replace("?", "_").replace("\"", "_")
    name = name.replace("<", "_").replace(">", "_").replace("|", "_")
    normalized_chars: list[str] = []
    for char in name:
        category = unicodedata.category(char)
        if char in {"_", "-"}:
            normalized_chars.append(char)
        elif category[:1] in {"L", "N", "M"}:
            normalized_chars.append(char)
        else:
            normalized_chars.append("_")
    name = "".join(normalized_chars)
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
# iCloud Drive 동기화 중 발생하는 OSError(errno=11, EDEADLK)에 대해
# 지수 백오프로 재시도한다.
def compute_sha256(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> str:
    max_retries = 6
    delay = 1.0
    for attempt in range(max_retries):
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError as exc:
            if exc.errno == 11 and attempt < max_retries - 1:
                logger.warning(
                    "compute_sha256: OSError errno=11 (iCloud 잠금 추정), "
                    "%d/%d 재시도, %.1f초 후: %s",
                    attempt + 1, max_retries, delay, path,
                )
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
            else:
                raise


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


def _rename_no_replace(src_path: Path, dst_path: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(src_path)
    target = os.fsencode(dst_path)
    if sys.platform == "darwin":
        rename = getattr(libc, "renamex_np", None)
        if rename is None:
            raise OSError(errno.ENOTSUP, "Atomic no-clobber rename is unavailable")
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        result = rename(source, target, 0x4)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOTSUP, "Atomic no-clobber rename is unavailable")
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        result = rename(-100, source, -100, target, 0x1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "Atomic no-clobber rename is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fspath(src_path), None, os.fspath(dst_path))


# 같은 장치에서 원자적으로 이동하고 대상 파일이 생겼다면 원본을 보존한다.
def safe_move_file(src: Union[str, Path], dst: Union[str, Path]) -> None:
    src_path = Path(src)
    dst_path = Path(dst)
    if os.path.lexists(dst_path):
        raise FileExistsError(f"Destination file already exists: {dst_path}")
    ensure_dir(dst_path.parent)
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError(errno.ENOTSUP, "No-follow file open is unavailable")
    with os.fdopen(os.open(src_path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
        expected = os.fstat(source.fileno())
        if not stat.S_ISREG(expected.st_mode):
            raise ValueError(f"Source must be a regular file: {src_path}")
        _rename_no_replace(src_path, dst_path)
        actual = dst_path.lstat()
        if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            try:
                _rename_no_replace(dst_path, src_path)
            except OSError:
                raise RuntimeError(f"Source changed during move; preserved at {dst_path}")
            raise RuntimeError(f"Source changed during move; restored at {src_path}")
        try:
            os.fsync(source.fileno())
        except OSError:
            # 이름 이동은 이미 끝났다. 호출자에게 실패를 돌리면 새 위치를 추적하지 못한다.
            logger.warning("Moved file, but file sync failed: %s", dst_path, exc_info=True)
    for parent in {src_path.parent, dst_path.parent}:
        try:
            dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            logger.warning("Moved file, but directory sync failed: %s", parent, exc_info=True)


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

    return pause_flag_path()


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
