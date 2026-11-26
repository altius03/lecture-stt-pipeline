from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from lecture_stt.stt.controller_topology import (
    ControllerTopologyError,
    run_topology_preflight,
)


PLAN_SCHEMA_VERSION = "lecture-stt/controller-shadow-bundle-plan@1"
MANIFEST_SCHEMA_VERSION = "lecture-stt/controller-shadow-bundle-manifest@1"
RESULT_SCHEMA_VERSION = "lecture-stt/controller-shadow-bundle-result@1"

BUNDLE_DIRECTORY_NAME = "controller-shadow-review-bundle"
BINARY_FILENAME = "lecture-stt-shadow"
PLIST_FILENAME = "com.geonha.lecture-stt-controller-shadow.plist.review"
MANIFEST_FILENAME = "bundle-manifest.json"
RUNTIME_DIRECTORY_NAME = "python-runtime"

MAX_BINARY_BYTES = 128 * 1024 * 1024
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_TEMPLATE_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SOURCE_MODULE_BYTES = 512 * 1024
RUNTIME_DIRECTORY_MODE = 0o500
RUNTIME_FILE_MODE = 0o400

_CANDIDATE_TEMPLATE = Path(
    "controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template"
)
_PACKAGE_INIT = Path("src/lecture_stt/__init__.py")
_STT_PACKAGE_INIT = Path("src/lecture_stt/stt/__init__.py")
_EVIDENCE_MODULE = Path("src/lecture_stt/stt/controller_evidence.py")
_READINESS_MODULE = Path("src/lecture_stt/stt/controller_readiness.py")
_RUNTIME_SOURCE_LAYOUT = (
    (_PACKAGE_INIT, Path("lecture_stt/__init__.py")),
    (_STT_PACKAGE_INIT, Path("lecture_stt/stt/__init__.py")),
    (_EVIDENCE_MODULE, Path("lecture_stt/stt/controller_evidence.py")),
    (_READINESS_MODULE, Path("lecture_stt/stt/controller_readiness.py")),
)
RUNTIME_BUNDLE_PATHS = tuple(
    bundle_relative_path.as_posix()
    for _, bundle_relative_path in _RUNTIME_SOURCE_LAYOUT
)
_PLACEHOLDER_BINDINGS = {
    "__BUNDLED_RUNTIME_ROOT__": "bundle_runtime_root",
    "__CONFIG_PATH__": "config",
    "__CONTROLLER_SHADOW_BIN__": "bundle_binary",
    "__EVIDENCE_ROOT__": "evidence_root",
    "__HOME__": "home",
    "__KILL_SWITCH_PATH__": "kill_switch",
    "__PYTHON_BIN__": "python_binary",
    "__REPO_ROOT__": "repo_root",
}
_ARTIFACT_NAMES = frozenset(
    {BINARY_FILENAME, PLIST_FILENAME, MANIFEST_FILENAME, RUNTIME_DIRECTORY_NAME}
)


class ControllerBundleError(RuntimeError):
    """Closed failure for the non-installable controller review bundle."""


class ControllerBundleWriteDisabledError(ControllerBundleError):
    """Raised when the explicit prepare guards are incomplete."""


class ControllerBundleRecoveryRequiredError(ControllerBundleError):
    """Raised when a partial or changed bundle must be reviewed manually."""


@dataclass(frozen=True)
class _StableFile:
    path: Path
    payload: bytes
    sha256: str
    identity: dict[str, int]


@dataclass(frozen=True)
class _RuntimeSource:
    source_relative_path: Path
    bundle_relative_path: Path
    source_path: Path
    payload: bytes
    sha256: str
    identity: dict[str, int]
    source_path_sha256: str


@dataclass(frozen=True)
class _BundleMaterial:
    plan: dict[str, Any]
    binary_payload: bytes
    rendered_plist: bytes
    manifest_payload: bytes
    runtime_sources: tuple[_RuntimeSource, ...]
    runtime_tree_sha256: str


@dataclass(frozen=True)
class PreparedBundle:
    bundle_root: Path
    binary_payload: bytes
    rendered_plist: bytes
    manifest_payload: bytes
    manifest: Mapping[str, Any]
    manifest_sha256: str
    runtime_files: Mapping[str, bytes]
    runtime_tree_sha256: str


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


def _identity(value: os.stat_result) -> dict[str, int]:
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "mode": int(value.st_mode),
        "nlink": int(value.st_nlink),
        "size": int(value.st_size),
        "mtime_ns": int(value.st_mtime_ns),
        "ctime_ns": int(value.st_ctime_ns),
    }


def _directory_identity(value: os.stat_result) -> dict[str, int]:
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "mode": int(value.st_mode),
    }


def _relative_posix(path: Path) -> str:
    return path.as_posix()


def _runtime_tree_contract(
    runtime_sources: Sequence[_RuntimeSource],
) -> list[dict[str, Any]]:
    return [
        {
            "bundle_relative_path": _relative_posix(item.bundle_relative_path),
            "source_path": _relative_posix(item.source_relative_path),
            "source_path_sha256": item.source_path_sha256,
            "source_identity": dict(item.identity),
            "source_sha256": item.sha256,
        }
        for item in runtime_sources
    ]


def _runtime_tree_sha256(runtime_sources: Sequence[_RuntimeSource]) -> str:
    return _sha256_json(
        {
            "directory_name": RUNTIME_DIRECTORY_NAME,
            "source_files": [
                {
                    "bundle_relative_path": _relative_posix(item.bundle_relative_path),
                    "sha256": item.sha256,
                }
                for item in runtime_sources
            ],
        }
    )


def _absolute_path(value: str | Path, *, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ControllerBundleError(f"{label} must be an absolute path")
    return Path(os.path.abspath(os.fspath(candidate)))


def _assert_real_components(path: Path, *, label: str, include_leaf: bool = True) -> None:
    if not path.is_absolute():
        raise ControllerBundleError(f"{label} must be an absolute path")
    current = Path(path.anchor)
    parts = path.parts[1:] if path.anchor else path.parts
    limit = len(parts) if include_leaf else max(0, len(parts) - 1)
    for component in parts[:limit]:
        current = current / component
        try:
            observed = current.lstat()
        except OSError as exc:
            raise ControllerBundleError(f"{label} is not safely inspectable") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise ControllerBundleError(f"{label} must not contain symlink components")
        if current != path and not stat.S_ISDIR(observed.st_mode):
            raise ControllerBundleError(f"{label} parent must be a directory")


def _stable_directory(value: str | Path, *, label: str) -> tuple[Path, dict[str, int]]:
    requested = _absolute_path(value, label=label)
    try:
        requested_observed = requested.lstat()
    except OSError as exc:
        raise ControllerBundleError(f"{label} is not safely inspectable") from exc
    if stat.S_ISLNK(requested_observed.st_mode):
        raise ControllerBundleError(f"{label} must not be a symlink")
    path = Path(os.path.realpath(os.fspath(requested)))
    _assert_real_components(path, label=label)
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ControllerBundleError(f"{label} is not safely inspectable") from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise ControllerBundleError(f"{label} must be a directory")
    return path, _directory_identity(observed)


def _stable_file(
    value: str | Path,
    *,
    label: str,
    maximum_bytes: int,
    executable: bool = False,
    allow_leaf_symlink: bool = False,
) -> _StableFile:
    requested = _absolute_path(value, label=label)
    try:
        requested_observed = requested.lstat()
    except OSError as exc:
        raise ControllerBundleError(f"{label} is not safely inspectable") from exc
    if stat.S_ISLNK(requested_observed.st_mode) and not allow_leaf_symlink:
        raise ControllerBundleError(f"{label} must not be a symlink")
    path = Path(os.path.realpath(os.fspath(requested)))
    _assert_real_components(path, label=label)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ControllerBundleError(f"{label} is not safely openable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ControllerBundleError(f"{label} must be a regular file")
        if before.st_nlink != 1:
            raise ControllerBundleError(f"{label} must be a regular single-link file")
        if before.st_size <= 0 or before.st_size > maximum_bytes:
            raise ControllerBundleError(f"{label} size is out of bounds")
        if executable and before.st_mode & 0o111 == 0:
            raise ControllerBundleError(f"{label} must be executable")

        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) != before.st_size:
            raise ControllerBundleError(f"{label} changed while it was read")

        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(current):
            raise ControllerBundleError(f"{label} changed while it was read")
        return _StableFile(
            path=path,
            payload=payload,
            sha256=_sha256_bytes(payload),
            identity=_identity(after),
        )
    finally:
        os.close(descriptor)


def _absent_kill_switch(value: str | Path) -> tuple[Path, dict[str, int]]:
    requested = _absolute_path(value, label="kill switch")
    if requested.name in {"", ".", ".."}:
        raise ControllerBundleError("kill switch must name a direct path")
    path = Path(os.path.realpath(os.fspath(requested.parent))) / requested.name
    _assert_real_components(path, label="kill switch", include_leaf=False)
    parent, parent_identity = _stable_directory(path.parent, label="kill switch parent")
    path = parent / path.name
    try:
        path.lstat()
    except FileNotFoundError:
        return path, parent_identity
    except OSError as exc:
        raise ControllerBundleError("kill switch is not safely inspectable") from exc
    raise ControllerBundleError("kill switch must be absent while the bundle is prepared")


def _temporary_output_root(value: str | Path) -> tuple[Path, dict[str, int]]:
    path, identity = _stable_directory(value, label="output root")
    _require_temporary_path(path, label="output root", allow_exact_base=False)
    return path, identity


def _require_temporary_path(
    path: Path,
    *,
    label: str,
    allow_exact_base: bool,
) -> None:
    temporary_base = Path(tempfile.gettempdir()).resolve(strict=True)
    try:
        common = Path(os.path.commonpath((os.fspath(path), os.fspath(temporary_base))))
    except ValueError as exc:
        raise ControllerBundleError(
            f"{label} must be inside the system temporary directory"
        ) from exc
    if common != temporary_base or (not allow_exact_base and path == temporary_base):
        raise ControllerBundleError(
            f"{label} must be inside the system temporary directory"
        )


def _replace_placeholders(value: Any, bindings: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_placeholders(item, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_placeholders(item, bindings) for item in value]
    if isinstance(value, str):
        rendered = value
        for placeholder, replacement in bindings.items():
            rendered = rendered.replace(placeholder, replacement)
        if "__" in rendered and any(
            placeholder in rendered for placeholder in _PLACEHOLDER_BINDINGS
        ):
            raise ControllerBundleError("candidate template contains unresolved placeholders")
        return rendered
    return value


def _render_candidate_plist(
    template: bytes,
    *,
    bindings: Mapping[str, str],
) -> bytes:
    try:
        parsed = plistlib.loads(template, fmt=plistlib.FMT_XML)
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise ControllerBundleError("candidate template plist is malformed") from exc
    rendered = _replace_placeholders(parsed, bindings)
    payload = plistlib.dumps(rendered, fmt=plistlib.FMT_XML, sort_keys=False)
    for placeholder in _PLACEHOLDER_BINDINGS:
        if placeholder.encode("utf-8") in payload:
            raise ControllerBundleError("candidate template contains unresolved placeholders")
    return payload


def _manifest_for_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    artifacts = plan["artifacts"]
    policy = plan["policy"]
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "expected_count": 1,
        "plan_sha256": plan["plan_sha256"],
        "topology_sha256": plan["topology"]["topology_sha256"],
        "candidate_template_sha256": plan["topology"]["candidate_template_sha256"],
        "artifacts": {
            "binary": {
                "filename": BINARY_FILENAME,
                "sha256": artifacts["binary_sha256"],
            },
            "bundle_directory_name": BUNDLE_DIRECTORY_NAME,
            "plist_review": {
                "filename": PLIST_FILENAME,
                "sha256": artifacts["rendered_plist_sha256"],
            },
        },
        "runtime": {
            "directory_name": RUNTIME_DIRECTORY_NAME,
            "tree_sha256": plan["runtime"]["runtime_tree_sha256"],
            "source_files": list(plan["runtime"]["source_files"]),
        },
        "policy": dict(policy),
    }


def _load_runtime_sources(repo_root: Path) -> tuple[_RuntimeSource, ...]:
    runtime_sources: list[_RuntimeSource] = []
    for source_relative_path, bundle_relative_path in _RUNTIME_SOURCE_LAYOUT:
        stable = _stable_file(
            repo_root / source_relative_path,
            label=f"runtime source {source_relative_path.as_posix()}",
            maximum_bytes=MAX_SOURCE_MODULE_BYTES,
        )
        runtime_sources.append(
            _RuntimeSource(
                source_relative_path=source_relative_path,
                bundle_relative_path=bundle_relative_path,
                source_path=stable.path,
                payload=stable.payload,
                sha256=stable.sha256,
                identity=stable.identity,
                source_path_sha256=_path_sha256(stable.path),
            )
        )
    return tuple(runtime_sources)


def _build_material(
    *,
    repo_root: str | Path,
    output_root: str | Path,
    source_binary: str | Path,
    python_binary: str | Path,
    config: str | Path,
    kill_switch: str | Path,
    home: str | Path,
    evidence_root: str | Path,
) -> _BundleMaterial:
    normalized_repo, repo_identity = _stable_directory(repo_root, label="repo root")
    normalized_output, output_identity = _temporary_output_root(output_root)
    normalized_home, home_identity = _stable_directory(home, label="home")
    _require_temporary_path(
        normalized_home,
        label="home",
        allow_exact_base=False,
    )
    binary = _stable_file(
        source_binary,
        label="source controller binary",
        maximum_bytes=MAX_BINARY_BYTES,
        executable=True,
    )
    _require_temporary_path(
        binary.path,
        label="source controller binary",
        allow_exact_base=False,
    )
    python = _stable_file(
        python_binary,
        label="Python binary",
        maximum_bytes=MAX_BINARY_BYTES,
        executable=True,
        allow_leaf_symlink=True,
    )
    config_file = _stable_file(
        config,
        label="controller config",
        maximum_bytes=MAX_CONFIG_BYTES,
    )
    _require_temporary_path(
        config_file.path,
        label="controller config",
        allow_exact_base=False,
    )
    normalized_kill_switch, kill_switch_parent_identity = _absent_kill_switch(kill_switch)
    _require_temporary_path(
        normalized_kill_switch,
        label="kill switch",
        allow_exact_base=False,
    )
    normalized_evidence_root, evidence_root_identity = _stable_directory(
        evidence_root,
        label="evidence root",
    )
    _require_temporary_path(
        normalized_evidence_root,
        label="evidence root",
        allow_exact_base=False,
    )
    evidence_root_stat = normalized_evidence_root.stat(follow_symlinks=False)
    if (
        stat.S_IMODE(evidence_root_stat.st_mode) != 0o700
        or evidence_root_stat.st_uid != os.geteuid()
    ):
        raise ControllerBundleError(
            "evidence root must be an owned stable 0700 directory"
        )
    evidence_root_identity = {
        **evidence_root_identity,
        "uid": int(evidence_root_stat.st_uid),
    }

    try:
        topology = run_topology_preflight(normalized_repo)
    except ControllerTopologyError as exc:
        raise ControllerBundleError("controller topology preflight failed") from exc
    template = _stable_file(
        normalized_repo / _CANDIDATE_TEMPLATE,
        label="candidate template",
        maximum_bytes=MAX_TEMPLATE_BYTES,
    )
    if template.sha256 != topology["candidate_template_sha256"]:
        raise ControllerBundleError("candidate template changed after topology preflight")
    runtime_sources = _load_runtime_sources(normalized_repo)

    bundle_directory = normalized_output / BUNDLE_DIRECTORY_NAME
    rendered_plist = _render_candidate_plist(
        template.payload,
        bindings={
            "__BUNDLED_RUNTIME_ROOT__": os.fspath(bundle_directory / RUNTIME_DIRECTORY_NAME),
            "__CONFIG_PATH__": os.fspath(config_file.path),
            "__CONTROLLER_SHADOW_BIN__": os.fspath(bundle_directory / BINARY_FILENAME),
            "__EVIDENCE_ROOT__": os.fspath(normalized_evidence_root),
            "__HOME__": os.fspath(normalized_home),
            "__KILL_SWITCH_PATH__": os.fspath(normalized_kill_switch),
            "__PYTHON_BIN__": os.fspath(python.path),
            "__REPO_ROOT__": os.fspath(normalized_repo),
        },
    )

    unsigned_plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "expected_count": 1,
        "bundle_name": BUNDLE_DIRECTORY_NAME,
        "bindings": {
            "repo_root_path_sha256": _path_sha256(normalized_repo),
            "repo_root_identity": repo_identity,
            "output_root_path_sha256": _path_sha256(normalized_output),
            "output_root_identity": output_identity,
            "home_path_sha256": _path_sha256(normalized_home),
            "home_identity": home_identity,
            "source_binary_path_sha256": _path_sha256(binary.path),
            "source_binary_identity": binary.identity,
            "python_binary_path_sha256": _path_sha256(python.path),
            "python_binary_identity": python.identity,
            "config_path_sha256": _path_sha256(config_file.path),
            "config_identity": config_file.identity,
            "kill_switch_path_sha256": _path_sha256(normalized_kill_switch),
            "kill_switch_parent_identity": kill_switch_parent_identity,
            "evidence_root_path_sha256": _path_sha256(normalized_evidence_root),
            "evidence_root_identity": evidence_root_identity,
        },
        "topology": {
            "topology_sha256": topology["topology_sha256"],
            "candidate_template_sha256": topology["candidate_template_sha256"],
        },
        "runtime": {
            "runtime_directory_name": RUNTIME_DIRECTORY_NAME,
            "runtime_tree_sha256": _runtime_tree_sha256(runtime_sources),
            "source_files": _runtime_tree_contract(runtime_sources),
        },
        "artifacts": {
            "binary_filename": BINARY_FILENAME,
            "binary_sha256": binary.sha256,
            "bundle_directory_name": BUNDLE_DIRECTORY_NAME,
            "plist_filename": PLIST_FILENAME,
            "rendered_plist_sha256": _sha256_bytes(rendered_plist),
            "manifest_filename": MANIFEST_FILENAME,
        },
        "policy": {
            "mode": "prepare_only",
            "execution_supported": False,
            "install_supported": False,
            "launchd_install_supported": False,
            "operational_install_supported": False,
            "uninstall_supported": False,
            "rollback_supported": False,
            "python_polling_authority": True,
            "stdout_is_readiness_ledger": False,
            "evidence_wrapper_connected": True,
        },
    }
    plan = dict(unsigned_plan)
    plan["plan_sha256"] = _sha256_json(unsigned_plan)
    manifest_payload = _json_bytes(_manifest_for_plan(plan))
    if len(manifest_payload) > MAX_MANIFEST_BYTES:
        raise ControllerBundleError("bundle manifest exceeds the size limit")
    return _BundleMaterial(
        plan=plan,
        binary_payload=binary.payload,
        rendered_plist=rendered_plist,
        manifest_payload=manifest_payload,
        runtime_sources=runtime_sources,
        runtime_tree_sha256=plan["runtime"]["runtime_tree_sha256"],
    )


def plan_controller_bundle(
    *,
    repo_root: str | Path,
    output_root: str | Path,
    source_binary: str | Path,
    python_binary: str | Path,
    config: str | Path,
    kill_switch: str | Path,
    home: str | Path,
    evidence_root: str | Path,
) -> dict[str, Any]:
    return _build_material(
        repo_root=repo_root,
        output_root=output_root,
        source_binary=source_binary,
        python_binary=python_binary,
        config=config,
        kill_switch=kill_switch,
        home=home,
        evidence_root=evidence_root,
    ).plan


def _validate_prepare_guards(
    plan: Mapping[str, Any],
    *,
    enable_prepare: bool,
    allow_write: bool,
    expected_count: int | None,
    expected_plan_sha256: str | None,
) -> None:
    if not enable_prepare:
        raise ControllerBundleWriteDisabledError(
            "bundle preparation is disabled; pass --enable-prepare explicitly"
        )
    if not allow_write:
        raise ControllerBundleWriteDisabledError(
            "bundle preparation requires --allow-write"
        )
    if isinstance(expected_count, bool) or expected_count != 1:
        raise ControllerBundleError("bundle preparation requires --expected-count 1")
    if expected_plan_sha256 != plan["plan_sha256"]:
        raise ControllerBundleError(
            "bundle preparation requires the exact --expected-plan-sha256"
        )


def _open_directory(path: Path, *, label: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ControllerBundleError(f"{label} is not safely openable") from exc
    observed = os.fstat(descriptor)
    if not stat.S_ISDIR(observed.st_mode):
        os.close(descriptor)
        raise ControllerBundleError(f"{label} must be a directory")
    return descriptor


def _open_child_directory(
    parent_fd: int,
    name: str,
    *,
    label: str,
    expected_mode: int,
) -> tuple[int, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ControllerBundleRecoveryRequiredError(
            f"{label} is not safely openable"
        ) from exc
    observed = os.fstat(descriptor)
    if not stat.S_ISDIR(observed.st_mode):
        os.close(descriptor)
        raise ControllerBundleRecoveryRequiredError(f"{label} must be a directory")
    if stat.S_IMODE(observed.st_mode) != expected_mode:
        os.close(descriptor)
        raise ControllerBundleRecoveryRequiredError(
            f"{label} permissions changed"
        )
    return descriptor, observed


def _fsync_directory(descriptor: int, *, label: str) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ControllerBundleRecoveryRequiredError(
            f"{label} could not be durably synchronized"
        ) from exc


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
        raise ControllerBundleRecoveryRequiredError(
            "bundle artifact could not be created exclusively"
        ) from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ControllerBundleRecoveryRequiredError(
                    "bundle artifact write did not make progress"
                )
            view = view[written:]
        os.fsync(descriptor)
        current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        after = os.fstat(descriptor)
        if _identity(after) != _identity(current):
            raise ControllerBundleRecoveryRequiredError(
                "bundle artifact pathname changed while it was written"
            )
    except OSError as exc:
        raise ControllerBundleRecoveryRequiredError(
            "bundle artifact could not be written durably"
        ) from exc
    finally:
        os.close(descriptor)


def _mkdir_child_directory(
    parent_fd: int,
    name: str,
    *,
    mode: int,
    label: str,
    open_mode: int | None = None,
) -> int:
    try:
        os.mkdir(name, mode=mode, dir_fd=parent_fd)
    except OSError as exc:
        raise ControllerBundleRecoveryRequiredError(
            f"{label} could not be created exclusively"
        ) from exc
    descriptor, _ = _open_child_directory(
        parent_fd,
        name,
        label=label,
        expected_mode=mode if open_mode is None else open_mode,
    )
    return descriptor


def _fchmod_directory(descriptor: int, mode: int, *, label: str) -> None:
    try:
        os.fchmod(descriptor, mode)
    except OSError as exc:
        raise ControllerBundleRecoveryRequiredError(
            f"{label} permissions could not be sealed"
        ) from exc
    if stat.S_IMODE(os.fstat(descriptor).st_mode) != mode:
        raise ControllerBundleRecoveryRequiredError(f"{label} permissions changed")


def _write_runtime_tree(directory_fd: int, runtime_sources: Sequence[_RuntimeSource]) -> None:
    runtime_fd = _mkdir_child_directory(
        directory_fd,
        RUNTIME_DIRECTORY_NAME,
        mode=0o700,
        label="bundle runtime root",
        open_mode=0o700,
    )
    try:
        directories: dict[Path, int] = {Path(): runtime_fd}
        ordered_directories = [Path()]
        try:
            for item in runtime_sources:
                parent = Path()
                for component in item.bundle_relative_path.parts[:-1]:
                    next_parent = parent / component
                    if next_parent not in directories:
                        child_fd = _mkdir_child_directory(
                            directories[parent],
                            component,
                            mode=0o700,
                            label=f"bundle runtime directory {next_parent.as_posix()}",
                            open_mode=0o700,
                        )
                        directories[next_parent] = child_fd
                        ordered_directories.append(next_parent)
                    parent = next_parent
                _write_exclusive(
                    directories[parent],
                    item.bundle_relative_path.name,
                    item.payload,
                    mode=RUNTIME_FILE_MODE,
                )
            for directory in reversed(ordered_directories):
                _fchmod_directory(
                    directories[directory],
                    RUNTIME_DIRECTORY_MODE,
                    label=(
                        "bundle runtime root"
                        if directory == Path()
                        else f"bundle runtime directory {directory.as_posix()}"
                    ),
                )
            for directory in reversed(ordered_directories):
                _fsync_directory(
                    directories[directory],
                    label=(
                        "bundle runtime root"
                        if directory == Path()
                        else f"bundle runtime directory {directory.as_posix()}"
                    ),
                )
        finally:
            for directory in reversed(ordered_directories[1:]):
                os.close(directories[directory])
    finally:
        os.close(runtime_fd)


def _read_runtime_tree(directory_fd: int) -> tuple[dict[str, bytes], str]:
    runtime_fd, _ = _open_child_directory(
        directory_fd,
        RUNTIME_DIRECTORY_NAME,
        label="bundle runtime root",
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
                raise ControllerBundleRecoveryRequiredError(
                    "bundle runtime entries are not safely inspectable"
                ) from exc
            if not entries:
                raise ControllerBundleRecoveryRequiredError(
                    "bundle runtime directories must not be empty"
                )
            for entry in entries:
                try:
                    observed = os.stat(entry, dir_fd=current_fd, follow_symlinks=False)
                except OSError as exc:
                    raise ControllerBundleRecoveryRequiredError(
                        "bundle runtime entry is not safely inspectable"
                    ) from exc
                relative_path = current_path / entry
                if stat.S_ISDIR(observed.st_mode):
                    if relative_path.as_posix() not in expected_directories:
                        raise ControllerBundleRecoveryRequiredError(
                            "bundle runtime tree contains an unexpected directory"
                        )
                    child_fd, _ = _open_child_directory(
                        current_fd,
                        entry,
                        label=f"bundle runtime directory {relative_path.as_posix()}",
                        expected_mode=RUNTIME_DIRECTORY_MODE,
                    )
                    open_child_fds.add(child_fd)
                    stack.append((child_fd, relative_path))
                    continue
                if relative_path.as_posix() not in expected_paths:
                    raise ControllerBundleRecoveryRequiredError(
                        "bundle runtime tree contains an unexpected file"
                    )
                payload = _read_child(
                    current_fd,
                    entry,
                    maximum_bytes=MAX_SOURCE_MODULE_BYTES,
                    expected_mode=RUNTIME_FILE_MODE,
                )
                files[relative_path.as_posix()] = payload
            if current_path != Path():
                os.close(current_fd)
                open_child_fds.discard(current_fd)
        if set(files) != expected_paths:
            raise ControllerBundleRecoveryRequiredError(
                "bundle runtime tree is partial or contains unexpected files"
            )
        tree_sha256 = _runtime_tree_sha256(
            tuple(
                _RuntimeSource(
                    source_relative_path=source_relative_path,
                    bundle_relative_path=bundle_relative_path,
                    source_path=bundle_relative_path,
                    payload=files[bundle_relative_path.as_posix()],
                    sha256=_sha256_bytes(files[bundle_relative_path.as_posix()]),
                    identity={},
                    source_path_sha256="",
                )
                for source_relative_path, bundle_relative_path in _RUNTIME_SOURCE_LAYOUT
            )
        )
        return files, tree_sha256
    finally:
        for child_fd in open_child_fds:
            os.close(child_fd)
        os.close(runtime_fd)


def _read_child(
    directory_fd: int,
    filename: str,
    *,
    maximum_bytes: int,
    executable: bool = False,
    expected_mode: int,
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
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle artifact is not safely openable"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle artifact must be a regular single-link file"
            )
        if before.st_size <= 0 or before.st_size > maximum_bytes:
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle artifact size is out of bounds"
            )
        if stat.S_IMODE(before.st_mode) != expected_mode:
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle artifact permissions changed"
            )
        if executable and before.st_mode & 0o111 == 0:
            raise ControllerBundleRecoveryRequiredError(
                "existing controller bundle binary is not executable"
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle artifact pathname changed while it was read"
            ) from exc
        if (
            _identity(before) != _identity(after)
            or _identity(after) != _identity(current)
            or len(payload) != after.st_size
        ):
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle artifact changed while it was read"
            )
        return payload
    finally:
        os.close(descriptor)


def _parse_manifest(payload: bytes) -> Mapping[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ControllerBundleRecoveryRequiredError(
                    "existing bundle manifest contains duplicate keys"
                )
            result[key] = value
        return result

    try:
        decoded = payload.decode("utf-8", errors="strict")
        parsed = json.loads(decoded, object_pairs_hook=reject_duplicates)
    except ControllerBundleRecoveryRequiredError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest is invalid"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest must be an object"
        )
    return parsed


def _verify_existing_bundle(
    *,
    bundle_root: Path | None,
    output_fd: int,
    material: _BundleMaterial,
) -> dict[str, Any]:
    try:
        observed = os.stat(
            BUNDLE_DIRECTORY_NAME,
            dir_fd=output_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle is not safely inspectable"
        ) from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle path must be a real directory"
        )
    if stat.S_IMODE(observed.st_mode) != 0o700:
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle directory permissions changed"
        )
    bundle_fd, opened = _open_child_directory(
        output_fd,
        BUNDLE_DIRECTORY_NAME,
        label="existing bundle",
        expected_mode=0o700,
    )
    try:
        if _directory_identity(observed) != _directory_identity(opened):
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle path changed while it was opened"
            )
        try:
            entries = set(os.listdir(bundle_fd))
        except OSError as exc:
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle entries are not safely inspectable"
            ) from exc
        if entries != _ARTIFACT_NAMES:
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle is partial or contains unexpected entries"
            )
        binary = _read_child(
            bundle_fd,
            BINARY_FILENAME,
            maximum_bytes=MAX_BINARY_BYTES,
            executable=True,
            expected_mode=0o500,
        )
        plist = _read_child(
            bundle_fd,
            PLIST_FILENAME,
            maximum_bytes=MAX_TEMPLATE_BYTES,
            expected_mode=0o400,
        )
        manifest = _read_child(
            bundle_fd,
            MANIFEST_FILENAME,
            maximum_bytes=MAX_MANIFEST_BYTES,
            expected_mode=0o400,
        )
        runtime_files, runtime_tree_sha256 = _read_runtime_tree(bundle_fd)
        if binary != material.binary_payload or plist != material.rendered_plist:
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle artifacts do not match the current plan"
            )
        expected_runtime_files = {
            item.bundle_relative_path.as_posix(): item.payload
            for item in material.runtime_sources
        }
        if (
            runtime_files != expected_runtime_files
            or runtime_tree_sha256 != material.runtime_tree_sha256
        ):
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle runtime does not match the current plan"
            )
        if manifest != material.manifest_payload:
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle manifest does not match the current plan"
            )
        if _parse_manifest(manifest) != _manifest_for_plan(material.plan):
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle manifest contract is invalid"
            )
        try:
            final_entries = set(os.listdir(bundle_fd))
        except OSError as exc:
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle entries changed while they were inspected"
            ) from exc
        if final_entries != _ARTIFACT_NAMES:
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle changed while it was inspected"
            )
    finally:
        os.close(bundle_fd)
    prepared = PreparedBundle(
        bundle_root=bundle_root if bundle_root is not None else Path(BUNDLE_DIRECTORY_NAME),
        binary_payload=binary,
        rendered_plist=plist,
        manifest_payload=manifest,
        manifest=_parse_manifest(manifest),
        manifest_sha256=_sha256_bytes(manifest),
        runtime_files=runtime_files,
        runtime_tree_sha256=runtime_tree_sha256,
    )
    _validate_prepared_bundle_contract(prepared)
    return _result(material, status="skipped")


def _validate_prepared_bundle_contract(prepared: PreparedBundle) -> None:
    manifest = prepared.manifest
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest artifacts contract is invalid"
        )
    binary_artifact = artifacts.get("binary")
    plist_artifact = artifacts.get("plist_review")
    if not isinstance(binary_artifact, Mapping) or not isinstance(
        plist_artifact,
        Mapping,
    ):
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest artifact entries are invalid"
        )
    policy = manifest.get("policy")
    if not isinstance(policy, Mapping):
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest policy contract is invalid"
        )
    runtime = manifest.get("runtime")
    if not isinstance(runtime, Mapping):
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest runtime contract is invalid"
        )
    runtime_directory = runtime.get("directory_name")
    runtime_tree_sha256 = runtime.get("tree_sha256")
    runtime_source_files = runtime.get("source_files")
    if (
        runtime_directory != RUNTIME_DIRECTORY_NAME
        or not isinstance(runtime_tree_sha256, str)
        or not isinstance(runtime_source_files, list)
    ):
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest runtime tree contract is invalid"
        )
    if artifacts.get("bundle_directory_name") != BUNDLE_DIRECTORY_NAME:
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest bundle directory contract is invalid"
        )
    if binary_artifact.get("sha256") != _sha256_bytes(prepared.binary_payload):
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle binary digest does not match its manifest"
        )
    if plist_artifact.get("sha256") != _sha256_bytes(prepared.rendered_plist):
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle plist digest does not match its manifest"
        )
    if policy.get("evidence_wrapper_connected") is not True:
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest must require the evidence wrapper"
        )
    if policy.get("launchd_install_supported") is not False:
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest launchd install policy drifted"
        )
    if policy.get("operational_install_supported") is not False:
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest operational install policy drifted"
        )
    if policy.get("python_polling_authority") is not True:
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest authority policy drifted"
        )
    actual_runtime_files = set(prepared.runtime_files)
    expected_runtime_files = {
        bundle_relative_path.as_posix()
        for _, bundle_relative_path in _RUNTIME_SOURCE_LAYOUT
    }
    if actual_runtime_files != expected_runtime_files:
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle runtime file set is invalid"
        )
    if runtime_tree_sha256 != prepared.runtime_tree_sha256:
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle runtime digest does not match its manifest"
        )
    if len(runtime_source_files) != len(_RUNTIME_SOURCE_LAYOUT):
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest runtime source contract drifted"
        )
    expected_sources: list[dict[str, Any]] = []
    for entry, (source_relative_path, bundle_relative_path) in zip(
        runtime_source_files,
        _RUNTIME_SOURCE_LAYOUT,
        strict=True,
    ):
        if not isinstance(entry, Mapping):
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle manifest runtime source entry is invalid"
            )
        source_identity = entry.get("source_identity")
        if not isinstance(source_identity, Mapping):
            raise ControllerBundleRecoveryRequiredError(
                "existing bundle manifest runtime source identity is invalid"
            )
        expected_sources.append(
            {
                "bundle_relative_path": bundle_relative_path.as_posix(),
                "source_path": source_relative_path.as_posix(),
                "source_path_sha256": str(entry.get("source_path_sha256", "")),
                "source_identity": dict(source_identity),
                "source_sha256": _sha256_bytes(
                    prepared.runtime_files[bundle_relative_path.as_posix()]
                ),
            }
        )
    if runtime_source_files != expected_sources:
        raise ControllerBundleRecoveryRequiredError(
            "existing bundle manifest runtime source contract drifted"
        )


def _result(material: _BundleMaterial, *, status: str) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": status,
        "expected_count": 1,
        "artifact_count": 4,
        "plan_sha256": material.plan["plan_sha256"],
        "manifest_sha256": _sha256_bytes(material.manifest_payload),
        "install_supported": False,
        "execution_supported": False,
        "launchd_install_supported": False,
    }


def inspect_prepared_bundle(bundle_root: str | Path) -> PreparedBundle:
    requested_bundle = _absolute_path(bundle_root, label="bundle root")
    try:
        requested_stat = requested_bundle.lstat()
    except OSError as exc:
        raise ControllerBundleRecoveryRequiredError(
            "bundle root is not safely inspectable"
        ) from exc
    if stat.S_ISLNK(requested_stat.st_mode):
        raise ControllerBundleRecoveryRequiredError(
            "bundle root must not be a symlink"
        )
    normalized_bundle = Path(os.path.realpath(os.fspath(requested_bundle)))
    _require_temporary_path(
        normalized_bundle,
        label="bundle root",
        allow_exact_base=False,
    )
    _assert_real_components(normalized_bundle, label="bundle root")
    if normalized_bundle.name != BUNDLE_DIRECTORY_NAME:
        raise ControllerBundleRecoveryRequiredError(
            "bundle root must point to the prepared bundle directory"
        )
    output_fd = _open_directory(normalized_bundle.parent, label="bundle parent")
    try:
        bundle_fd, _ = _open_child_directory(
            output_fd,
            normalized_bundle.name,
            label="prepared bundle",
            expected_mode=0o700,
        )
        try:
            entries = set(os.listdir(bundle_fd))
            if entries != _ARTIFACT_NAMES:
                raise ControllerBundleRecoveryRequiredError(
                    "prepared bundle is partial or contains unexpected entries"
                )
            binary = _read_child(
                bundle_fd,
                BINARY_FILENAME,
                maximum_bytes=MAX_BINARY_BYTES,
                executable=True,
                expected_mode=0o500,
            )
            plist = _read_child(
                bundle_fd,
                PLIST_FILENAME,
                maximum_bytes=MAX_TEMPLATE_BYTES,
                expected_mode=0o400,
            )
            manifest_payload = _read_child(
                bundle_fd,
                MANIFEST_FILENAME,
                maximum_bytes=MAX_MANIFEST_BYTES,
                expected_mode=0o400,
            )
            runtime_files, runtime_tree_sha256 = _read_runtime_tree(bundle_fd)
        finally:
            os.close(bundle_fd)
    finally:
        os.close(output_fd)
    manifest = _parse_manifest(manifest_payload)
    prepared = PreparedBundle(
        bundle_root=normalized_bundle,
        binary_payload=binary,
        rendered_plist=plist,
        manifest_payload=manifest_payload,
        manifest=manifest,
        manifest_sha256=_sha256_bytes(manifest_payload),
        runtime_files=runtime_files,
        runtime_tree_sha256=runtime_tree_sha256,
    )
    _validate_prepared_bundle_contract(prepared)
    return prepared


def prepare_controller_bundle(
    *,
    repo_root: str | Path,
    output_root: str | Path,
    source_binary: str | Path,
    python_binary: str | Path,
    config: str | Path,
    kill_switch: str | Path,
    home: str | Path,
    evidence_root: str | Path,
    enable_prepare: bool,
    allow_write: bool,
    expected_count: int | None,
    expected_plan_sha256: str | None,
) -> dict[str, Any]:
    material = _build_material(
        repo_root=repo_root,
        output_root=output_root,
        source_binary=source_binary,
        python_binary=python_binary,
        config=config,
        kill_switch=kill_switch,
        home=home,
        evidence_root=evidence_root,
    )
    _validate_prepare_guards(
        material.plan,
        enable_prepare=enable_prepare,
        allow_write=allow_write,
        expected_count=expected_count,
        expected_plan_sha256=expected_plan_sha256,
    )
    normalized_output, expected_output_identity = _temporary_output_root(output_root)
    output_fd = _open_directory(normalized_output, label="output root")
    try:
        if _directory_identity(os.fstat(output_fd)) != expected_output_identity:
            raise ControllerBundleError("output root changed before bundle preparation")
        try:
            os.stat(BUNDLE_DIRECTORY_NAME, dir_fd=output_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ControllerBundleRecoveryRequiredError(
                "bundle destination is not safely inspectable"
            ) from exc
        else:
            return _verify_existing_bundle(
                bundle_root=normalized_output / BUNDLE_DIRECTORY_NAME,
                output_fd=output_fd,
                material=material,
            )

        try:
            os.mkdir(BUNDLE_DIRECTORY_NAME, mode=0o700, dir_fd=output_fd)
        except OSError as exc:
            raise ControllerBundleRecoveryRequiredError(
                "bundle directory could not be created exclusively"
            ) from exc
        _fsync_directory(output_fd, label="output root")

        bundle_fd, _ = _open_child_directory(
            output_fd,
            BUNDLE_DIRECTORY_NAME,
            label="new bundle",
            expected_mode=0o700,
        )
        try:
            _write_exclusive(
                bundle_fd,
                BINARY_FILENAME,
                material.binary_payload,
                mode=0o500,
            )
            _write_exclusive(
                bundle_fd,
                PLIST_FILENAME,
                material.rendered_plist,
                mode=0o400,
            )
            _write_runtime_tree(bundle_fd, material.runtime_sources)
            _fsync_directory(bundle_fd, label="bundle directory")
            _write_exclusive(
                bundle_fd,
                MANIFEST_FILENAME,
                material.manifest_payload,
                mode=0o400,
            )
            _fsync_directory(bundle_fd, label="bundle directory")
        finally:
            os.close(bundle_fd)
        _fsync_directory(output_fd, label="output root")
        _verify_existing_bundle(
            bundle_root=normalized_output / BUNDLE_DIRECTORY_NAME,
            output_fd=output_fd,
            material=material,
        )
    finally:
        os.close(output_fd)
    return _result(material, status="prepared")


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--source-binary", required=True)
    parser.add_argument("--python-bin", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--kill-switch", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--evidence-root", required=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a non-installable controller shadow review bundle"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan", help="compute the exact render-only plan")
    _add_common_arguments(plan_parser)
    prepare_parser = commands.add_parser(
        "prepare",
        help="prepare one hash-locked review bundle inside a temporary directory",
    )
    _add_common_arguments(prepare_parser)
    prepare_parser.add_argument("--enable-prepare", action="store_true")
    prepare_parser.add_argument("--allow-write", action="store_true")
    prepare_parser.add_argument("--expected-count", type=int)
    prepare_parser.add_argument("--expected-plan-sha256")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    common = {
        "repo_root": args.repo_root,
        "output_root": args.output_root,
        "source_binary": args.source_binary,
        "python_binary": args.python_bin,
        "config": args.config,
        "kill_switch": args.kill_switch,
        "home": args.home,
        "evidence_root": args.evidence_root,
    }
    try:
        if args.command == "plan":
            result = plan_controller_bundle(**common)
        else:
            result = prepare_controller_bundle(
                **common,
                enable_prepare=args.enable_prepare,
                allow_write=args.allow_write,
                expected_count=args.expected_count,
                expected_plan_sha256=args.expected_plan_sha256,
            )
    except ControllerBundleError as exc:
        print(f"controller shadow bundle rejected: {exc}", file=sys.stderr)
        return 2
    print(_canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
