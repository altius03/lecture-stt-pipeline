"""Identity of the decoder output, shared by the live worker and offline retention."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import BinaryIO

WORKSPACE_PREFIX = ".lecture-stt-wav-"
WAV_NAME = "decoded.wav"
_UNSUPPORTED = {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM, errno.ENOSYS}


def witness_name(source: Path, stem: str) -> str:
    binding = os.fsencode(source.absolute()) + b"\0" + os.fsencode(stem)
    return "owner-" + hashlib.sha256(binding).hexdigest()


def is_private_workspace(directory: Path) -> bool:
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700)


def _same_entry(path: Path, info: os.stat_result) -> bool:
    try:
        return os.path.samestat(path.lstat(), info)
    except FileNotFoundError:
        return False


@dataclass
class TemporaryWav:
    path: Path
    witness: Path
    handle: BinaryIO
    directory_identity: os.stat_result

    @classmethod
    def create(cls, tmp_dir: Path, source: Path, stem: str) -> TemporaryWav:
        directory = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX, dir=tmp_dir))
        path = directory / WAV_NAME
        witness = directory / witness_name(source, stem)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            handle = os.fdopen(fd, "wb", buffering=0)
        except BaseException:
            directory.rmdir()
            raise
        wav = cls(path, witness, handle, directory.lstat())
        try:
            # Both names identify the exact output even across process restarts.
            # If the filesystem cannot create a witness, only the live handle
            # authorizes cleanup; a later process must preserve the orphan.
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.link(path, witness)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED:
                wav.remove()
                raise
        return wav

    @classmethod
    def reopen(cls, directory: Path, witness: Path) -> TemporaryWav | None:
        if not is_private_workspace(directory):
            return None
        # Pin the witness inode until the last comparison and unlink. A new
        # file at the output pathname cannot reuse an inode held by this fd.
        try:
            info = directory.lstat()
            fd = os.open(witness, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        handle = os.fdopen(fd, "rb", buffering=0)
        owned = os.fstat(fd)
        wav = cls(directory / WAV_NAME, witness, handle, info)
        if (stat.S_ISREG(owned.st_mode) and owned.st_uid == os.getuid()
                and owned.st_nlink == 2 and wav.matches_output() and _same_entry(witness, owned)):
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return None
            except OSError as exc:
                handle.close()
                if exc.errno in _UNSUPPORTED:
                    return None
                raise
            return wav
        handle.close()
        return None

    def matches_output(self) -> bool:
        return (_same_entry(self.path.parent, self.directory_identity)
                and _same_entry(self.path, os.fstat(self.handle.fileno())))

    def remove(self) -> bool:
        try:
            if not _same_entry(self.path.parent, self.directory_identity):
                return False
            owned = os.fstat(self.handle.fileno())
            removed = _same_entry(self.path, owned)
            if removed:
                self.path.unlink()
            if _same_entry(self.witness, owned):
                self.witness.unlink()
            try:
                self.path.parent.rmdir()
            except OSError as exc:
                if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT}:
                    raise
            return removed
        finally:
            self.handle.close()
