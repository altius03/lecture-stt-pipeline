from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Mapping, Sequence

from lecture_stt.stt.controller_bundle import (
    BINARY_FILENAME,
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    MAX_SOURCE_MODULE_BYTES,
    PLIST_FILENAME,
    PreparedBundle,
    ControllerBundleError,
    RUNTIME_BUNDLE_PATHS,
    RUNTIME_DIRECTORY_MODE,
    RUNTIME_DIRECTORY_NAME,
    RUNTIME_FILE_MODE,
    inspect_prepared_bundle,
)


PLAN_SCHEMA_VERSION = "lecture-stt/controller-shadow-offline-activation-plan@1"
RECORD_SCHEMA_VERSION = "lecture-stt/controller-shadow-offline-activation-record@1"
RESULT_SCHEMA_VERSION = "lecture-stt/controller-shadow-offline-activation-result@1"

LOCK_FILENAME = "activation.lock"
VERSIONS_DIRECTORY_NAME = "versions"
MAX_RECORD_BYTES = 64 * 1024
MAX_ACTIONS = 10_000
ZERO_SHA256 = "0" * 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECORD_RE = re.compile(r"^(?P<sequence>\d{8})\.(?P<action>install|uninstall|rollback)\.json$")
_ACTIONS = {"install", "uninstall", "rollback"}
_MANIFEST_KEYS = {
    "artifacts",
    "candidate_template_sha256",
    "expected_count",
    "plan_sha256",
    "policy",
    "runtime",
    "schema_version",
    "topology_sha256",
}
_ARTIFACT_KEYS = {"binary", "bundle_directory_name", "plist_review"}
_ARTIFACT_ENTRY_KEYS = {"filename", "sha256"}
_RUNTIME_KEYS = {"directory_name", "source_files", "tree_sha256"}
_RUNTIME_SOURCE_KEYS = {
    "bundle_relative_path",
    "source_identity",
    "source_path",
    "source_path_sha256",
    "source_sha256",
}
_POLICY_KEYS = {
    "evidence_wrapper_connected",
    "execution_supported",
    "install_supported",
    "launchd_install_supported",
    "mode",
    "operational_install_supported",
    "python_polling_authority",
    "rollback_supported",
    "stdout_is_readiness_ledger",
    "uninstall_supported",
}
_VERSION_KEYS = {
    "binary_sha256",
    "manifest_sha256",
    "plan_sha256",
    "plist_sha256",
    "runtime_tree_sha256",
}
_RECORD_KEYS = {
    "action",
    "previous_active_plan_sha256",
    "previous_record_sha256",
    "record_sha256",
    "schema_version",
    "sequence",
    "source_version",
    "target_active_plan_sha256",
}


class ControllerActivationError(RuntimeError):
    """Closed failure for the temporary offline activation ledger."""


class ControllerActivationWriteDisabledError(ControllerActivationError):
    """Raised when an offline activation write guard is absent."""


class ControllerActivationBusyError(ControllerActivationError):
    """Raised when another process owns the activation ledger."""


class ControllerActivationRecoveryRequiredError(ControllerActivationError):
    """Raised for partial or changed activation evidence."""


@dataclass(frozen=True)
class _ActivationState:
    records: tuple[dict[str, Any], ...]
    head_sha256: str
    journal_sha256: str
    active_plan_sha256: str
    installed_versions: Mapping[str, Mapping[str, str]]
    stored_versions: Mapping[str, Mapping[str, str]]


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (_canonical_json(value) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _path_sha256(path: Path) -> str:
    return _sha256_bytes(os.fsencode(path))


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ControllerActivationRecoveryRequiredError(
            f"{label} has an invalid closed key set"
        )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ControllerActivationRecoveryRequiredError(f"{label} must be an object")
    return value


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ControllerActivationRecoveryRequiredError(f"{label} must be a boolean")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ControllerActivationRecoveryRequiredError(
            f"{label} must be a non-empty string"
        )
    return value


def _require_sha256(value: Any, label: str, *, allow_zero: bool = True) -> str:
    digest = _require_string(value, label)
    if _SHA256_RE.fullmatch(digest) is None or (not allow_zero and digest == ZERO_SHA256):
        raise ControllerActivationRecoveryRequiredError(
            f"{label} must be a lowercase SHA-256"
        )
    return digest


def _absolute_path(value: str | Path, *, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ControllerActivationError(f"{label} must be absolute")
    return Path(os.path.abspath(os.fspath(candidate)))


def _require_temporary_path(path: Path, *, label: str) -> None:
    temporary_base = Path(tempfile.gettempdir()).resolve(strict=True)
    try:
        common = Path(os.path.commonpath((os.fspath(path), os.fspath(temporary_base))))
    except ValueError as exc:
        raise ControllerActivationError(
            f"{label} must be inside the system temporary directory"
        ) from exc
    if common != temporary_base or path == temporary_base:
        raise ControllerActivationError(
            f"{label} must be inside the system temporary directory"
        )


def _assert_real_components(path: Path, *, label: str) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            observed = current.lstat()
        except OSError as exc:
            raise ControllerActivationError(f"{label} is not safely inspectable") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise ControllerActivationError(f"{label} must not contain symlink components")
        if current != path and not stat.S_ISDIR(observed.st_mode):
            raise ControllerActivationError(f"{label} parent must be a directory")


def _open_activation_root(value: str | Path) -> tuple[Path, int, dict[str, int]]:
    requested = _absolute_path(value, label="activation root")
    try:
        requested_stat = requested.lstat()
    except OSError as exc:
        raise ControllerActivationError(
            "activation root is not safely inspectable"
        ) from exc
    if stat.S_ISLNK(requested_stat.st_mode):
        raise ControllerActivationError("activation root must not be a symlink")
    path = Path(os.path.realpath(os.fspath(requested)))
    _require_temporary_path(path, label="activation root")
    _assert_real_components(path, label="activation root")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ControllerActivationError("activation root is not safely openable") from exc
    try:
        observed = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_mode", "st_uid")
        if (
            not stat.S_ISDIR(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o700
            or observed.st_uid != os.geteuid()
            or any(getattr(observed, field) != getattr(current, field) for field in fields)
        ):
            raise ControllerActivationError(
                "activation root must be an owned stable 0700 directory"
            )
        identity = {
            "device": int(observed.st_dev),
            "inode": int(observed.st_ino),
            "mode": int(observed.st_mode),
            "uid": int(observed.st_uid),
        }
        return path, descriptor, identity
    except Exception:
        os.close(descriptor)
        raise


def _open_lock(root_fd: int, *, create: bool, exclusive: bool) -> int | None:
    flags = (
        os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(LOCK_FILENAME, flags, 0o600, dir_fd=root_fd)
    except FileNotFoundError:
        return None
    except FileExistsError:
        if create:
            return _open_lock(root_fd, create=False, exclusive=exclusive)
        raise
    except OSError as exc:
        raise ControllerActivationRecoveryRequiredError(
            "activation lock is not safely openable"
        ) from exc
    try:
        observed = os.fstat(descriptor)
        current = os.stat(LOCK_FILENAME, dir_fd=root_fd, follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid")
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o600
            or observed.st_nlink != 1
            or observed.st_uid != os.geteuid()
            or any(getattr(observed, field) != getattr(current, field) for field in fields)
        ):
            raise ControllerActivationRecoveryRequiredError(
                "activation lock contract changed"
            )
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControllerActivationBusyError("activation ledger is busy") from exc
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _fsync_directory(descriptor: int, *, label: str) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ControllerActivationRecoveryRequiredError(
            f"{label} could not be durably synchronized"
        ) from exc


def _read_child(
    directory_fd: int,
    filename: str,
    *,
    maximum_bytes: int,
    expected_mode: int,
    executable: bool = False,
) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(filename, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ControllerActivationRecoveryRequiredError(
            "activation artifact is not safely openable"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != expected_mode
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_size <= 0
            or before.st_size > maximum_bytes
            or (executable and before.st_mode & 0o111 == 0)
        ):
            raise ControllerActivationRecoveryRequiredError(
                "activation artifact contract changed"
            )
        chunks: list[bytes] = []
        bytes_read = 0
        while bytes_read <= maximum_bytes:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, maximum_bytes + 1 - bytes_read),
            )
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_uid",
        )
        if (
            len(payload) != after.st_size
            or any(getattr(before, field) != getattr(after, field) for field in fields)
            or any(getattr(after, field) != getattr(current, field) for field in fields)
        ):
            raise ControllerActivationRecoveryRequiredError(
                "activation artifact changed while it was read"
            )
        return payload
    finally:
        os.close(descriptor)


def _write_exclusive(
    directory_fd: int,
    filename: str,
    payload: bytes,
    *,
    mode: int,
) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(filename, flags, mode, dir_fd=directory_fd)
    except OSError as exc:
        raise ControllerActivationRecoveryRequiredError(
            "activation artifact could not be created exclusively"
        ) from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ControllerActivationRecoveryRequiredError(
                    "activation artifact write did not make progress"
                )
            view = view[written:]
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_uid")
        if (
            not stat.S_ISREG(after.st_mode)
            or stat.S_IMODE(after.st_mode) != mode
            or after.st_nlink != 1
            or after.st_uid != os.geteuid()
            or after.st_size != len(payload)
            or any(getattr(after, field) != getattr(current, field) for field in fields)
        ):
            raise ControllerActivationRecoveryRequiredError(
                "activation artifact pathname changed while it was written"
            )
    except OSError as exc:
        raise ControllerActivationRecoveryRequiredError(
            "activation artifact could not be written durably"
        ) from exc
    finally:
        os.close(descriptor)


def _fchmod_directory(descriptor: int, mode: int, *, label: str) -> None:
    try:
        os.fchmod(descriptor, mode)
    except OSError as exc:
        raise ControllerActivationRecoveryRequiredError(
            f"{label} permissions could not be sealed"
        ) from exc
    if stat.S_IMODE(os.fstat(descriptor).st_mode) != mode:
        raise ControllerActivationRecoveryRequiredError(f"{label} contract changed")


def _write_runtime_tree(directory_fd: int, prepared: PreparedBundle) -> None:
    try:
        os.mkdir(RUNTIME_DIRECTORY_NAME, mode=0o700, dir_fd=directory_fd)
    except OSError as exc:
        raise ControllerActivationRecoveryRequiredError(
            "activation runtime root could not be created exclusively"
        ) from exc
    runtime_fd = _open_child_directory(
        directory_fd,
        RUNTIME_DIRECTORY_NAME,
        label="activation runtime root",
        expected_mode=0o700,
    )
    try:
        directories: dict[Path, int] = {Path(): runtime_fd}
        ordered_directories = [Path()]
        try:
            for relative_name, payload in sorted(prepared.runtime_files.items()):
                relative_path = Path(relative_name)
                parent = Path()
                for component in relative_path.parts[:-1]:
                    next_parent = parent / component
                    if next_parent not in directories:
                        try:
                            os.mkdir(
                                component,
                                mode=0o700,
                                dir_fd=directories[parent],
                            )
                        except OSError as exc:
                            raise ControllerActivationRecoveryRequiredError(
                                "activation runtime directory could not be created exclusively"
                            ) from exc
                        directories[next_parent] = _open_child_directory(
                            directories[parent],
                            component,
                            label=f"activation runtime directory {next_parent.as_posix()}",
                            expected_mode=0o700,
                        )
                        ordered_directories.append(next_parent)
                    parent = next_parent
                _write_exclusive(
                    directories[parent],
                    relative_path.name,
                    payload,
                    mode=RUNTIME_FILE_MODE,
                )
            for directory in reversed(ordered_directories):
                _fchmod_directory(
                    directories[directory],
                    RUNTIME_DIRECTORY_MODE,
                    label=(
                        "activation runtime root"
                        if directory == Path()
                        else f"activation runtime directory {directory.as_posix()}"
                    ),
                )
            for directory in reversed(ordered_directories):
                _fsync_directory(
                    directories[directory],
                    label=(
                        "activation runtime root"
                        if directory == Path()
                        else f"activation runtime directory {directory.as_posix()}"
                    ),
                )
        finally:
            for directory in reversed(ordered_directories[1:]):
                os.close(directories[directory])
    finally:
        os.close(runtime_fd)


def _read_runtime_tree(directory_fd: int) -> tuple[dict[str, bytes], str]:
    runtime_fd = _open_child_directory(
        directory_fd,
        RUNTIME_DIRECTORY_NAME,
        label="activation runtime root",
        expected_mode=RUNTIME_DIRECTORY_MODE,
    )
    files: dict[str, bytes] = {}
    expected_paths = frozenset(RUNTIME_BUNDLE_PATHS)
    expected_directories = frozenset(
        parent.as_posix()
        for relative_name in RUNTIME_BUNDLE_PATHS
        for parent in Path(relative_name).parents
        if parent != Path()
    )
    open_child_fds: set[int] = set()
    try:
        stack: list[tuple[int, Path]] = [(runtime_fd, Path())]
        while stack:
            current_fd, current_path = stack.pop()
            try:
                entries = sorted(os.listdir(current_fd))
            except OSError as exc:
                raise ControllerActivationRecoveryRequiredError(
                    "activation runtime entries are not inspectable"
                ) from exc
            if not entries:
                raise ControllerActivationRecoveryRequiredError(
                    "activation runtime directories must not be empty"
                )
            for entry in entries:
                try:
                    observed = os.stat(entry, dir_fd=current_fd, follow_symlinks=False)
                except OSError as exc:
                    raise ControllerActivationRecoveryRequiredError(
                        "activation runtime entry is not inspectable"
                    ) from exc
                relative_path = current_path / entry
                if stat.S_ISDIR(observed.st_mode):
                    if relative_path.as_posix() not in expected_directories:
                        raise ControllerActivationRecoveryRequiredError(
                            "activation runtime contains an unexpected directory"
                        )
                    child_fd = _open_child_directory(
                        current_fd,
                        entry,
                        label=f"activation runtime directory {relative_path.as_posix()}",
                        expected_mode=RUNTIME_DIRECTORY_MODE,
                    )
                    open_child_fds.add(child_fd)
                    stack.append((child_fd, relative_path))
                    continue
                if relative_path.as_posix() not in expected_paths:
                    raise ControllerActivationRecoveryRequiredError(
                        "activation runtime contains an unexpected file"
                    )
                files[relative_path.as_posix()] = _read_child(
                    current_fd,
                    entry,
                    maximum_bytes=MAX_SOURCE_MODULE_BYTES,
                    expected_mode=RUNTIME_FILE_MODE,
                )
            if current_path != Path():
                os.close(current_fd)
                open_child_fds.discard(current_fd)
        if set(files) != expected_paths:
            raise ControllerActivationRecoveryRequiredError(
                "activation runtime is partial or contains unexpected files"
            )
        tree_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "directory_name": RUNTIME_DIRECTORY_NAME,
                    "source_files": [
                        {
                            "bundle_relative_path": relative_name,
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                        for relative_name, payload in sorted(files.items())
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return files, tree_sha256
    finally:
        for child_fd in open_child_fds:
            os.close(child_fd)
        os.close(runtime_fd)


def _parse_json(payload: bytes, *, label: str) -> Mapping[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ControllerActivationRecoveryRequiredError(
                    f"{label} contains duplicate keys"
                )
            result[key] = value
        return result

    try:
        decoded = payload.decode("utf-8", errors="strict")
        if not decoded.endswith("\n") or decoded.count("\n") != 1:
            raise ControllerActivationRecoveryRequiredError(
                f"{label} must be one canonical JSON line"
            )
        parsed = json.loads(decoded, object_pairs_hook=reject_duplicates)
    except ControllerActivationRecoveryRequiredError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerActivationRecoveryRequiredError(f"{label} is invalid") from exc
    mapping = _require_mapping(parsed, label)
    if _json_bytes(mapping) != payload:
        raise ControllerActivationRecoveryRequiredError(f"{label} is not canonical")
    return mapping


def _version_evidence(prepared: PreparedBundle) -> dict[str, str]:
    manifest = prepared.manifest
    parsed_manifest = _parse_json(
        prepared.manifest_payload,
        label="bundle manifest",
    )
    if parsed_manifest != manifest:
        raise ControllerActivationRecoveryRequiredError(
            "bundle manifest payload changed while it was inspected"
        )
    _require_exact_keys(manifest, _MANIFEST_KEYS, "bundle manifest")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ControllerActivationRecoveryRequiredError(
            "bundle manifest schema version is invalid"
        )
    if manifest.get("expected_count") != 1:
        raise ControllerActivationRecoveryRequiredError(
            "bundle manifest expected count is invalid"
        )
    plan_sha256 = _require_sha256(
        manifest.get("plan_sha256"),
        "bundle manifest plan_sha256",
        allow_zero=False,
    )
    _require_sha256(
        manifest.get("topology_sha256"),
        "bundle manifest topology_sha256",
        allow_zero=False,
    )
    _require_sha256(
        manifest.get("candidate_template_sha256"),
        "bundle manifest candidate_template_sha256",
        allow_zero=False,
    )
    artifacts = _require_mapping(manifest.get("artifacts"), "bundle manifest artifacts")
    _require_exact_keys(artifacts, _ARTIFACT_KEYS, "bundle manifest artifacts")
    binary = _require_mapping(artifacts.get("binary"), "bundle manifest binary")
    plist = _require_mapping(artifacts.get("plist_review"), "bundle manifest plist")
    _require_exact_keys(binary, _ARTIFACT_ENTRY_KEYS, "bundle manifest binary")
    _require_exact_keys(plist, _ARTIFACT_ENTRY_KEYS, "bundle manifest plist")
    if binary.get("filename") != BINARY_FILENAME or plist.get("filename") != PLIST_FILENAME:
        raise ControllerActivationRecoveryRequiredError(
            "bundle manifest filenames are invalid"
        )
    if artifacts.get("bundle_directory_name") != prepared.bundle_root.name:
        raise ControllerActivationRecoveryRequiredError(
            "bundle manifest directory name is invalid"
        )
    binary_sha256 = _require_sha256(
        binary.get("sha256"),
        "bundle manifest binary sha256",
        allow_zero=False,
    )
    plist_sha256 = _require_sha256(
        plist.get("sha256"),
        "bundle manifest plist sha256",
        allow_zero=False,
    )
    if binary_sha256 != _sha256_bytes(prepared.binary_payload):
        raise ControllerActivationRecoveryRequiredError(
            "bundle binary does not match its manifest"
        )
    if plist_sha256 != _sha256_bytes(prepared.rendered_plist):
        raise ControllerActivationRecoveryRequiredError(
            "bundle plist does not match its manifest"
        )
    runtime = _require_mapping(manifest.get("runtime"), "bundle manifest runtime")
    _require_exact_keys(runtime, _RUNTIME_KEYS, "bundle manifest runtime")
    if runtime.get("directory_name") != RUNTIME_DIRECTORY_NAME:
        raise ControllerActivationRecoveryRequiredError(
            "bundle runtime directory name is invalid"
        )
    runtime_tree_sha256 = _require_sha256(
        runtime.get("tree_sha256"),
        "bundle runtime tree sha256",
        allow_zero=False,
    )
    if runtime_tree_sha256 != prepared.runtime_tree_sha256:
        raise ControllerActivationRecoveryRequiredError(
            "bundle runtime tree does not match its manifest"
        )
    runtime_source_files = runtime.get("source_files")
    if not isinstance(runtime_source_files, list) or not runtime_source_files:
        raise ControllerActivationRecoveryRequiredError(
            "bundle runtime source list is invalid"
        )
    actual_runtime_files = set(prepared.runtime_files)
    if len(runtime_source_files) != len(actual_runtime_files):
        raise ControllerActivationRecoveryRequiredError(
            "bundle runtime source count is invalid"
        )
    for entry in runtime_source_files:
        mapping = _require_mapping(entry, "bundle runtime source")
        _require_exact_keys(mapping, _RUNTIME_SOURCE_KEYS, "bundle runtime source")
        bundle_relative_path = _require_string(
            mapping.get("bundle_relative_path"),
            "bundle runtime source bundle_relative_path",
        )
        if bundle_relative_path not in prepared.runtime_files:
            raise ControllerActivationRecoveryRequiredError(
                "bundle runtime source path is invalid"
            )
        if _require_sha256(
            mapping.get("source_sha256"),
            "bundle runtime source sha256",
            allow_zero=False,
        ) != _sha256_bytes(prepared.runtime_files[bundle_relative_path]):
            raise ControllerActivationRecoveryRequiredError(
                "bundle runtime source digest is invalid"
            )
        _require_sha256(
            mapping.get("source_path_sha256"),
            "bundle runtime source path sha256",
            allow_zero=False,
        )
        source_identity = _require_mapping(
            mapping.get("source_identity"),
            "bundle runtime source identity",
        )
        for key, value in source_identity.items():
            if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int):
                raise ControllerActivationRecoveryRequiredError(
                    "bundle runtime source identity is invalid"
                )
        _require_string(
            mapping.get("source_path"),
            "bundle runtime source path",
        )
    if actual_runtime_files != {
        _require_string(
            _require_mapping(entry, "bundle runtime source").get("bundle_relative_path"),
            "bundle runtime source bundle_relative_path",
        )
        for entry in runtime_source_files
    }:
        raise ControllerActivationRecoveryRequiredError(
            "bundle runtime source set is invalid"
        )
    policy = _require_mapping(manifest.get("policy"), "bundle manifest policy")
    _require_exact_keys(policy, _POLICY_KEYS, "bundle manifest policy")
    if (
        policy.get("mode") != "prepare_only"
        or _require_bool(
            policy.get("evidence_wrapper_connected"),
            "bundle policy evidence_wrapper_connected",
        )
        is not True
        or _require_bool(
            policy.get("python_polling_authority"),
            "bundle policy python_polling_authority",
        )
        is not True
        or _require_bool(
            policy.get("stdout_is_readiness_ledger"),
            "bundle policy stdout_is_readiness_ledger",
        )
        is not False
    ):
        raise ControllerActivationRecoveryRequiredError(
            "bundle manifest evidence policy is invalid"
        )
    for key in (
        "execution_supported",
        "install_supported",
        "launchd_install_supported",
        "operational_install_supported",
        "rollback_supported",
        "uninstall_supported",
    ):
        if _require_bool(policy.get(key), f"bundle policy {key}") is not False:
            raise ControllerActivationRecoveryRequiredError(
                "bundle manifest write policy is invalid"
            )
    return {
        "plan_sha256": plan_sha256,
        "manifest_sha256": prepared.manifest_sha256,
        "binary_sha256": binary_sha256,
        "plist_sha256": plist_sha256,
        "runtime_tree_sha256": runtime_tree_sha256,
    }


def _open_child_directory(
    parent_fd: int,
    name: str,
    *,
    label: str,
    expected_mode: int,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ControllerActivationRecoveryRequiredError(
            f"{label} is not safely openable"
        ) from exc
    observed = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stat.S_IMODE(observed.st_mode) != expected_mode
        or observed.st_uid != os.geteuid()
    ):
        os.close(descriptor)
        raise ControllerActivationRecoveryRequiredError(f"{label} contract changed")
    return descriptor


def _read_version(versions_fd: int, plan_sha256: str) -> dict[str, str]:
    version_fd = _open_child_directory(
        versions_fd,
        plan_sha256,
        label="activation version",
        expected_mode=0o700,
    )
    try:
        try:
            entries = set(os.listdir(version_fd))
        except OSError as exc:
            raise ControllerActivationRecoveryRequiredError(
                "activation version entries are not inspectable"
            ) from exc
        if entries != {
            BINARY_FILENAME,
            PLIST_FILENAME,
            MANIFEST_FILENAME,
            RUNTIME_DIRECTORY_NAME,
        }:
            raise ControllerActivationRecoveryRequiredError(
                "activation version is partial or contains unexpected entries"
            )
        binary = _read_child(
            version_fd,
            BINARY_FILENAME,
            maximum_bytes=128 * 1024 * 1024,
            expected_mode=0o500,
            executable=True,
        )
        plist = _read_child(
            version_fd,
            PLIST_FILENAME,
            maximum_bytes=256 * 1024,
            expected_mode=0o400,
        )
        manifest_payload = _read_child(
            version_fd,
            MANIFEST_FILENAME,
            maximum_bytes=64 * 1024,
            expected_mode=0o400,
        )
        runtime_files, runtime_tree_sha256 = _read_runtime_tree(version_fd)
    finally:
        os.close(version_fd)
    manifest = _parse_json(manifest_payload, label="activation version manifest")
    prepared = PreparedBundle(
        bundle_root=Path("controller-shadow-review-bundle"),
        binary_payload=binary,
        rendered_plist=plist,
        manifest_payload=manifest_payload,
        manifest=manifest,
        manifest_sha256=_sha256_bytes(manifest_payload),
        runtime_files=runtime_files,
        runtime_tree_sha256=runtime_tree_sha256,
    )
    evidence = _version_evidence(prepared)
    if evidence["plan_sha256"] != plan_sha256:
        raise ControllerActivationRecoveryRequiredError(
            "activation version directory does not match its plan"
        )
    return evidence


def _read_versions(root_fd: int, entries: set[str]) -> dict[str, Mapping[str, str]]:
    if VERSIONS_DIRECTORY_NAME not in entries:
        return {}
    versions_fd = _open_child_directory(
        root_fd,
        VERSIONS_DIRECTORY_NAME,
        label="activation versions",
        expected_mode=0o700,
    )
    try:
        try:
            names = sorted(os.listdir(versions_fd))
        except OSError as exc:
            raise ControllerActivationRecoveryRequiredError(
                "activation versions are not inspectable"
            ) from exc
        result: dict[str, Mapping[str, str]] = {}
        for name in names:
            if _SHA256_RE.fullmatch(name) is None or name == ZERO_SHA256:
                raise ControllerActivationRecoveryRequiredError(
                    "activation version name is invalid"
                )
            result[name] = _read_version(versions_fd, name)
        return result
    finally:
        os.close(versions_fd)


def _validate_record(
    record: Mapping[str, Any],
    *,
    sequence: int,
    expected_action: str,
    previous_record_sha256: str,
    previous_active_plan_sha256: str,
    known_versions: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, Any], str]:
    _require_exact_keys(record, _RECORD_KEYS, "activation record")
    if record.get("schema_version") != RECORD_SCHEMA_VERSION:
        raise ControllerActivationRecoveryRequiredError(
            "activation record schema version is invalid"
        )
    if record.get("sequence") != sequence or record.get("action") != expected_action:
        raise ControllerActivationRecoveryRequiredError(
            "activation record sequence or action is invalid"
        )
    if record.get("previous_record_sha256") != previous_record_sha256:
        raise ControllerActivationRecoveryRequiredError(
            "activation record hash chain is broken"
        )
    if record.get("previous_active_plan_sha256") != previous_active_plan_sha256:
        raise ControllerActivationRecoveryRequiredError(
            "activation record previous state is invalid"
        )
    source = _require_mapping(record.get("source_version"), "activation source version")
    _require_exact_keys(source, _VERSION_KEYS, "activation source version")
    normalized_source = {
        key: _require_sha256(
            source.get(key),
            f"activation source version {key}",
            allow_zero=False,
        )
        for key in sorted(_VERSION_KEYS)
    }
    source_plan = normalized_source["plan_sha256"]
    stored = known_versions.get(source_plan)
    if stored is None or dict(stored) != normalized_source:
        raise ControllerActivationRecoveryRequiredError(
            "activation record references an unknown or changed version"
        )
    target = _require_sha256(
        record.get("target_active_plan_sha256"),
        "activation target state",
    )
    if expected_action == "install":
        if previous_active_plan_sha256 != ZERO_SHA256 or target != source_plan:
            raise ControllerActivationRecoveryRequiredError(
                "activation install transition is invalid"
            )
    elif expected_action == "uninstall":
        if previous_active_plan_sha256 != source_plan or target != ZERO_SHA256:
            raise ControllerActivationRecoveryRequiredError(
                "activation uninstall transition is invalid"
            )
    elif expected_action == "rollback":
        if previous_active_plan_sha256 != ZERO_SHA256 or target != source_plan:
            raise ControllerActivationRecoveryRequiredError(
                "activation rollback transition is invalid"
            )
    unsigned = dict(record)
    observed_record_sha256 = _require_sha256(
        unsigned.pop("record_sha256"),
        "activation record sha256",
        allow_zero=False,
    )
    expected_record_sha256 = _sha256_json(unsigned)
    if observed_record_sha256 != expected_record_sha256:
        raise ControllerActivationRecoveryRequiredError(
            "activation record sha256 is invalid"
        )
    return dict(record), target


def _load_state(root_fd: int) -> _ActivationState:
    try:
        entries = set(os.listdir(root_fd))
    except OSError as exc:
        raise ControllerActivationRecoveryRequiredError(
            "activation root entries are not inspectable"
        ) from exc
    allowed_static = {LOCK_FILENAME, VERSIONS_DIRECTORY_NAME}
    record_names: list[tuple[int, str, str]] = []
    for entry in entries - allowed_static:
        match = _RECORD_RE.fullmatch(entry)
        if match is None:
            raise ControllerActivationRecoveryRequiredError(
                "activation root contains an unexpected entry"
            )
        record_names.append(
            (int(match.group("sequence")), match.group("action"), entry)
        )
    record_names.sort()
    if len(record_names) > MAX_ACTIONS:
        raise ControllerActivationRecoveryRequiredError("activation journal is full")
    for expected_sequence, (sequence, _, _) in enumerate(record_names, start=1):
        if sequence != expected_sequence:
            raise ControllerActivationRecoveryRequiredError(
                "activation journal sequence is not contiguous"
            )
    stored_versions = _read_versions(root_fd, entries)
    installed_versions: dict[str, Mapping[str, str]] = {}
    records: list[dict[str, Any]] = []
    previous_record_sha256 = ZERO_SHA256
    active_plan_sha256 = ZERO_SHA256
    journal_digest = hashlib.sha256()
    for sequence, action, filename in record_names:
        payload = _read_child(
            root_fd,
            filename,
            maximum_bytes=MAX_RECORD_BYTES,
            expected_mode=0o400,
        )
        record = _parse_json(payload, label="activation record")
        validated, active_plan_sha256 = _validate_record(
            record,
            sequence=sequence,
            expected_action=action,
            previous_record_sha256=previous_record_sha256,
            previous_active_plan_sha256=active_plan_sha256,
            known_versions=stored_versions,
        )
        source = _require_mapping(
            validated["source_version"],
            "activation source version",
        )
        if action == "install":
            plan_sha256 = str(source["plan_sha256"])
            existing = installed_versions.get(plan_sha256)
            if existing is not None and dict(existing) != dict(source):
                raise ControllerActivationRecoveryRequiredError(
                    "activation installed version evidence changed"
                )
            installed_versions[plan_sha256] = dict(source)
        previous_record_sha256 = str(validated["record_sha256"])
        journal_digest.update(payload)
        records.append(validated)
    if set(installed_versions) - set(stored_versions):
        raise ControllerActivationRecoveryRequiredError(
            "activation journal references a missing version"
        )
    return _ActivationState(
        records=tuple(records),
        head_sha256=previous_record_sha256,
        journal_sha256=journal_digest.hexdigest(),
        active_plan_sha256=active_plan_sha256,
        installed_versions=installed_versions,
        stored_versions=stored_versions,
    )


def _state_material(
    *,
    action: str,
    root_identity: Mapping[str, int],
    state: _ActivationState,
    source_version: Mapping[str, str],
    version_status: str,
) -> dict[str, Any]:
    target = (
        ZERO_SHA256
        if action == "uninstall"
        else str(source_version["plan_sha256"])
    )
    unsigned: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "action": action,
        "expected_count": 1,
        "scope": "temporary_offline",
        "activation_root_identity": dict(root_identity),
        "activation_root_path_sha256": "",
        "sequence": len(state.records) + 1,
        "previous_record_sha256": state.head_sha256,
        "journal_sha256": state.journal_sha256,
        "previous_active_plan_sha256": state.active_plan_sha256,
        "target_active_plan_sha256": target,
        "source_version": dict(source_version),
        "version_status": version_status,
        "policy": {
            "launchctl_invoked": False,
            "launchd_install_supported": False,
            "operational_install_supported": False,
            "python_polling_authority": True,
        },
    }
    return unsigned


def _materialize_plan(
    *,
    action: str,
    normalized_root: Path,
    root_identity: Mapping[str, int],
    state: _ActivationState,
    bundle_root: str | Path | None = None,
    target_plan_sha256: str | None = None,
) -> tuple[dict[str, Any], _ActivationState, PreparedBundle | None]:
    if action not in _ACTIONS:
        raise ControllerActivationError("activation action is invalid")
    if len(state.records) >= MAX_ACTIONS:
        raise ControllerActivationRecoveryRequiredError(
            "activation journal is full"
        )
    prepared: PreparedBundle | None = None
    if action == "install":
        if bundle_root is None:
            raise ControllerActivationError("install requires a prepared bundle")
        try:
            prepared = inspect_prepared_bundle(bundle_root)
        except ControllerBundleError as exc:
            raise ControllerActivationRecoveryRequiredError(
                "prepared bundle is invalid"
            ) from exc
        source_version = _version_evidence(prepared)
        if state.active_plan_sha256 == source_version["plan_sha256"]:
            version_status = "active"
        elif state.active_plan_sha256 != ZERO_SHA256:
            raise ControllerActivationError(
                "another offline version is active; uninstall it before install"
            )
        else:
            stored = state.stored_versions.get(source_version["plan_sha256"])
            if stored is None:
                version_status = "missing"
            elif dict(stored) == source_version:
                version_status = (
                    "stored"
                    if source_version["plan_sha256"] in state.installed_versions
                    else "orphan"
                )
            else:
                raise ControllerActivationRecoveryRequiredError(
                    "stored activation version changed"
                )
    elif action == "uninstall":
        if state.active_plan_sha256 == ZERO_SHA256:
            source_version = {
                "plan_sha256": ZERO_SHA256,
                "manifest_sha256": ZERO_SHA256,
                "binary_sha256": ZERO_SHA256,
                "plist_sha256": ZERO_SHA256,
                "runtime_tree_sha256": ZERO_SHA256,
            }
            version_status = "inactive"
        else:
            stored = state.stored_versions.get(state.active_plan_sha256)
            if stored is None:
                raise ControllerActivationRecoveryRequiredError(
                    "active activation version is missing"
                )
            source_version = dict(stored)
            version_status = "stored"
    else:
        if target_plan_sha256 is None:
            raise ControllerActivationError("rollback requires a target plan SHA-256")
        target = _require_sha256(
            target_plan_sha256,
            "rollback target plan_sha256",
            allow_zero=False,
        )
        if state.active_plan_sha256 == target:
            stored = state.stored_versions.get(target)
            if stored is None:
                raise ControllerActivationRecoveryRequiredError(
                    "active rollback target is missing"
                )
            source_version = dict(stored)
            version_status = "active"
        elif state.active_plan_sha256 != ZERO_SHA256:
            raise ControllerActivationError(
                "offline activation must be inactive before rollback"
            )
        else:
            installed = state.installed_versions.get(target)
            stored = state.stored_versions.get(target)
            if installed is None or stored is None or dict(installed) != dict(stored):
                raise ControllerActivationError(
                    "rollback target is not an installed immutable version"
                )
            source_version = dict(stored)
            version_status = "stored"
    unsigned = _state_material(
        action=action,
        root_identity=root_identity,
        state=state,
        source_version=source_version,
        version_status=version_status,
    )
    unsigned["activation_root_path_sha256"] = _path_sha256(normalized_root)
    plan = dict(unsigned)
    plan["plan_sha256"] = _sha256_json(unsigned)
    return plan, state, prepared


def _plan(
    *,
    action: str,
    activation_root: str | Path,
    bundle_root: str | Path | None = None,
    target_plan_sha256: str | None = None,
    exclusive: bool,
    create_lock: bool,
) -> tuple[dict[str, Any], _ActivationState, PreparedBundle | None]:
    normalized_root, root_fd, root_identity = _open_activation_root(activation_root)
    lock_fd: int | None = None
    try:
        lock_fd = _open_lock(
            root_fd,
            create=create_lock,
            exclusive=exclusive,
        )
        if lock_fd is None and exclusive:
            raise ControllerActivationRecoveryRequiredError(
                "activation lock could not be created"
            )
        state = _load_state(root_fd)
        return _materialize_plan(
            action=action,
            normalized_root=normalized_root,
            root_identity=root_identity,
            state=state,
            bundle_root=bundle_root,
            target_plan_sha256=target_plan_sha256,
        )
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(root_fd)


def plan_install(
    *,
    activation_root: str | Path,
    bundle_root: str | Path,
) -> dict[str, Any]:
    plan, _, _ = _plan(
        action="install",
        activation_root=activation_root,
        bundle_root=bundle_root,
        exclusive=False,
        create_lock=False,
    )
    return plan


def plan_uninstall(*, activation_root: str | Path) -> dict[str, Any]:
    plan, _, _ = _plan(
        action="uninstall",
        activation_root=activation_root,
        exclusive=False,
        create_lock=False,
    )
    return plan


def plan_rollback(
    *,
    activation_root: str | Path,
    target_plan_sha256: str,
) -> dict[str, Any]:
    plan, _, _ = _plan(
        action="rollback",
        activation_root=activation_root,
        target_plan_sha256=target_plan_sha256,
        exclusive=False,
        create_lock=False,
    )
    return plan


def _validate_apply_guards(
    plan: Mapping[str, Any],
    *,
    enable_activation: bool,
    allow_write: bool,
    expected_count: int | None,
    expected_plan_sha256: str | None,
) -> None:
    if not enable_activation:
        raise ControllerActivationWriteDisabledError(
            "offline activation is disabled; pass --enable-activation explicitly"
        )
    if not allow_write:
        raise ControllerActivationWriteDisabledError(
            "offline activation requires --allow-write"
        )
    if isinstance(expected_count, bool) or expected_count != 1:
        raise ControllerActivationError(
            "offline activation requires --expected-count 1"
        )
    if expected_plan_sha256 != plan.get("plan_sha256"):
        raise ControllerActivationError(
            "offline activation requires the exact --expected-plan-sha256"
        )


def _ensure_versions_directory(root_fd: int) -> int:
    try:
        os.mkdir(VERSIONS_DIRECTORY_NAME, mode=0o700, dir_fd=root_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ControllerActivationRecoveryRequiredError(
            "activation versions directory could not be created"
        ) from exc
    _fsync_directory(root_fd, label="activation root")
    return _open_child_directory(
        root_fd,
        VERSIONS_DIRECTORY_NAME,
        label="activation versions",
        expected_mode=0o700,
    )


def _write_version(root_fd: int, prepared: PreparedBundle, evidence: Mapping[str, str]) -> None:
    versions_fd = _ensure_versions_directory(root_fd)
    try:
        plan_sha256 = str(evidence["plan_sha256"])
        try:
            os.mkdir(plan_sha256, mode=0o700, dir_fd=versions_fd)
        except OSError as exc:
            raise ControllerActivationRecoveryRequiredError(
                "activation version could not be created exclusively"
            ) from exc
        _fsync_directory(versions_fd, label="activation versions")
        version_fd = _open_child_directory(
            versions_fd,
            plan_sha256,
            label="new activation version",
            expected_mode=0o700,
        )
        try:
            _write_exclusive(
                version_fd,
                BINARY_FILENAME,
                prepared.binary_payload,
                mode=0o500,
            )
            _write_exclusive(
                version_fd,
                PLIST_FILENAME,
                prepared.rendered_plist,
                mode=0o400,
            )
            _write_runtime_tree(version_fd, prepared)
            _write_exclusive(
                version_fd,
                MANIFEST_FILENAME,
                prepared.manifest_payload,
                mode=0o400,
            )
            _fsync_directory(version_fd, label="activation version")
        finally:
            os.close(version_fd)
        observed = _read_version(versions_fd, plan_sha256)
        if observed != dict(evidence):
            raise ControllerActivationRecoveryRequiredError(
                "new activation version does not match its source"
            )
    finally:
        os.close(versions_fd)


def _append_record(root_fd: int, plan: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "sequence": plan["sequence"],
        "action": plan["action"],
        "previous_record_sha256": plan["previous_record_sha256"],
        "previous_active_plan_sha256": plan["previous_active_plan_sha256"],
        "target_active_plan_sha256": plan["target_active_plan_sha256"],
        "source_version": dict(plan["source_version"]),
    }
    record = dict(unsigned)
    record["record_sha256"] = _sha256_json(unsigned)
    filename = f"{int(plan['sequence']):08d}.{plan['action']}.json"
    _write_exclusive(root_fd, filename, _json_bytes(record), mode=0o400)
    _fsync_directory(root_fd, label="activation root")
    return record


def _result(
    plan: Mapping[str, Any],
    *,
    status: str,
    record_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": status,
        "action": plan["action"],
        "expected_count": 1,
        "plan_sha256": plan["plan_sha256"],
        "record_sha256": record_sha256,
        "active": plan["target_active_plan_sha256"] != ZERO_SHA256,
        "active_plan_sha256": plan["target_active_plan_sha256"],
        "scope": "temporary_offline",
        "launchctl_invoked": False,
        "operational_install_supported": False,
        "python_polling_authority": True,
    }


def _apply(
    *,
    action: str,
    activation_root: str | Path,
    bundle_root: str | Path | None,
    target_plan_sha256: str | None,
    enable_activation: bool,
    allow_write: bool,
    expected_count: int | None,
    expected_plan_sha256: str | None,
) -> dict[str, Any]:
    initial, _, _ = _plan(
        action=action,
        activation_root=activation_root,
        bundle_root=bundle_root,
        target_plan_sha256=target_plan_sha256,
        exclusive=False,
        create_lock=False,
    )
    _validate_apply_guards(
        initial,
        enable_activation=enable_activation,
        allow_write=allow_write,
        expected_count=expected_count,
        expected_plan_sha256=expected_plan_sha256,
    )
    normalized_root, root_fd, root_identity = _open_activation_root(activation_root)
    lock_fd: int | None = None
    try:
        lock_fd = _open_lock(root_fd, create=True, exclusive=True)
        if lock_fd is None:
            raise ControllerActivationRecoveryRequiredError(
                "activation lock could not be created"
            )
        _fsync_directory(root_fd, label="activation root")
        state = _load_state(root_fd)
        current_plan, current_state, current_prepared = _materialize_plan(
            action=action,
            normalized_root=normalized_root,
            root_identity=root_identity,
            state=state,
            bundle_root=bundle_root,
            target_plan_sha256=target_plan_sha256,
        )
        if current_plan["plan_sha256"] != initial["plan_sha256"]:
            raise ControllerActivationError(
                "offline activation evidence changed before write"
            )
        if (
            (
                action in {"install", "rollback"}
                and current_plan["version_status"] == "active"
            )
            or (
                action == "uninstall"
                and current_plan["version_status"] == "inactive"
            )
        ):
            return _result(
                current_plan,
                status="skipped",
                record_sha256=current_state.head_sha256,
            )
        if action == "install" and current_plan["version_status"] == "missing":
            if current_prepared is None:
                raise ControllerActivationRecoveryRequiredError(
                    "prepared bundle disappeared before version staging"
                )
            _write_version(
                root_fd,
                current_prepared,
                current_plan["source_version"],
            )
        record = _append_record(root_fd, current_plan)
        final_state = _load_state(root_fd)
        if (
            final_state.head_sha256 != record["record_sha256"]
            or final_state.active_plan_sha256
            != current_plan["target_active_plan_sha256"]
        ):
            raise ControllerActivationRecoveryRequiredError(
                "offline activation record did not become the verified head"
            )
        return _result(
            current_plan,
            status="applied",
            record_sha256=str(record["record_sha256"]),
        )
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(root_fd)


def apply_install(
    *,
    activation_root: str | Path,
    bundle_root: str | Path,
    enable_activation: bool,
    allow_write: bool,
    expected_count: int | None,
    expected_plan_sha256: str | None,
) -> dict[str, Any]:
    return _apply(
        action="install",
        activation_root=activation_root,
        bundle_root=bundle_root,
        target_plan_sha256=None,
        enable_activation=enable_activation,
        allow_write=allow_write,
        expected_count=expected_count,
        expected_plan_sha256=expected_plan_sha256,
    )


def apply_uninstall(
    *,
    activation_root: str | Path,
    enable_activation: bool,
    allow_write: bool,
    expected_count: int | None,
    expected_plan_sha256: str | None,
) -> dict[str, Any]:
    return _apply(
        action="uninstall",
        activation_root=activation_root,
        bundle_root=None,
        target_plan_sha256=None,
        enable_activation=enable_activation,
        allow_write=allow_write,
        expected_count=expected_count,
        expected_plan_sha256=expected_plan_sha256,
    )


def apply_rollback(
    *,
    activation_root: str | Path,
    target_plan_sha256: str,
    enable_activation: bool,
    allow_write: bool,
    expected_count: int | None,
    expected_plan_sha256: str | None,
) -> dict[str, Any]:
    return _apply(
        action="rollback",
        activation_root=activation_root,
        bundle_root=None,
        target_plan_sha256=target_plan_sha256,
        enable_activation=enable_activation,
        allow_write=allow_write,
        expected_count=expected_count,
        expected_plan_sha256=expected_plan_sha256,
    )


def _add_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--activation-root", required=True)


def _add_apply_guards(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--enable-activation", action="store_true")
    parser.add_argument("--allow-write", action="store_true")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--expected-plan-sha256")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage a temporary offline controller activation ledger"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("plan-install", "apply-install"):
        child = commands.add_parser(command)
        _add_root(child)
        child.add_argument("--bundle-root", required=True)
        if command.startswith("apply-"):
            _add_apply_guards(child)
    for command in ("plan-uninstall", "apply-uninstall"):
        child = commands.add_parser(command)
        _add_root(child)
        if command.startswith("apply-"):
            _add_apply_guards(child)
    for command in ("plan-rollback", "apply-rollback"):
        child = commands.add_parser(command)
        _add_root(child)
        child.add_argument("--target-plan-sha256", required=True)
        if command.startswith("apply-"):
            _add_apply_guards(child)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "plan-install":
            result = plan_install(
                activation_root=args.activation_root,
                bundle_root=args.bundle_root,
            )
        elif args.command == "apply-install":
            result = apply_install(
                activation_root=args.activation_root,
                bundle_root=args.bundle_root,
                enable_activation=args.enable_activation,
                allow_write=args.allow_write,
                expected_count=args.expected_count,
                expected_plan_sha256=args.expected_plan_sha256,
            )
        elif args.command == "plan-uninstall":
            result = plan_uninstall(activation_root=args.activation_root)
        elif args.command == "apply-uninstall":
            result = apply_uninstall(
                activation_root=args.activation_root,
                enable_activation=args.enable_activation,
                allow_write=args.allow_write,
                expected_count=args.expected_count,
                expected_plan_sha256=args.expected_plan_sha256,
            )
        elif args.command == "plan-rollback":
            result = plan_rollback(
                activation_root=args.activation_root,
                target_plan_sha256=args.target_plan_sha256,
            )
        else:
            result = apply_rollback(
                activation_root=args.activation_root,
                target_plan_sha256=args.target_plan_sha256,
                enable_activation=args.enable_activation,
                allow_write=args.allow_write,
                expected_count=args.expected_count,
                expected_plan_sha256=args.expected_plan_sha256,
            )
    except ControllerActivationError as exc:
        print(f"controller offline activation rejected: {exc}", file=sys.stderr)
        return 2
    print(_canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
