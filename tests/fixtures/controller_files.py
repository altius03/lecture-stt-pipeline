import shutil
from pathlib import Path


def replace_file(path: Path, payload: bytes) -> None:
    """Replace the entry itself, including a directory or a dangling symlink."""
    if path.exists() or path.is_symlink():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    path.write_bytes(payload)


def write_file(path: Path, payload: bytes | str = b"", *, executable: bool = False) -> None:
    binary = payload.encode("utf-8") if isinstance(payload, str) else payload
    path.write_bytes(binary)
    path.chmod(0o700 if executable else 0o600)


def remove_tree(path: Path) -> None:
    """Remove a prepared tree with read-only contents, or unlink its replacement."""
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
        return
    for current in sorted(path.rglob("*"), reverse=True):
        try:
            current.chmod(0o700 if current.is_dir() else 0o600)
        except OSError:
            pass
    try:
        path.chmod(0o700)
    except OSError:
        pass
    shutil.rmtree(path)


def set_runtime_tree_mode(runtime_root: Path, mode: int) -> None:
    for directory in (
        runtime_root,
        runtime_root / "lecture_stt",
        runtime_root / "lecture_stt/stt",
    ):
        directory.chmod(mode)
