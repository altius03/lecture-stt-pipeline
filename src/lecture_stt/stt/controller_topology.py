from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping, Sequence

SUMMARY_SCHEMA_VERSION = "lecture-stt/controller-topology-summary@1"
MAX_REPO_FILE_BYTES = 256 * 1024
_ALLOWED_PLACEHOLDERS = {
    "__BUNDLED_RUNTIME_ROOT__",
    "__CONFIG_PATH__",
    "__CONTROLLER_SHADOW_BIN__",
    "__EVIDENCE_ROOT__",
    "__HOME__",
    "__KILL_SWITCH_PATH__",
    "__PYTHON_BIN__",
    "__REPO_ROOT__",
}
_EXISTING_LABELS = (
    "com.geonha.lecture-stt",
    "com.geonha.lecture-stt-cleanup",
    "com.geonha.lecture-stt-distribute",
    "com.geonha.lecture-stt-webpanel",
)
_CANDIDATE_LABEL = "com.geonha.lecture-stt-controller-shadow"
_EXISTING_TEMPLATE_PATHS = tuple(
    Path("launchd") / f"{label}.plist" for label in _EXISTING_LABELS
)
_CANDIDATE_TEMPLATE_PATH = Path("controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template")
_SETUP_SCRIPT_PATH = Path("scripts/setup_launchd.sh")
_PLACEHOLDER_RE = re.compile(r"__[A-Z0-9_]+__")
_PLISTS_BLOCK_RE = re.compile(r"PLISTS=\(\s*(?P<body>.*?)\s*\)", re.DOTALL)
_QUOTED_LABEL_RE = re.compile(r'"([^"\n]+)"')
_CANDIDATE_KEYS = {
    "EnvironmentVariables",
    "Label",
    "LowPriorityIO",
    "Nice",
    "ProcessType",
    "ProgramArguments",
    "StandardErrorPath",
    "StandardOutPath",
    "StartInterval",
    "WorkingDirectory",
}
_CANDIDATE_PROGRAM_ARGUMENTS = [
    "__PYTHON_BIN__",
    "-m",
    "lecture_stt.stt.controller_evidence",
    "run",
    "--journal-root",
    "__EVIDENCE_ROOT__",
    "--controller-bin",
    "__CONTROLLER_SHADOW_BIN__",
    "--python-bin",
    "__PYTHON_BIN__",
    "--repo-root",
    "__REPO_ROOT__",
    "--config",
    "__CONFIG_PATH__",
    "--kill-switch",
    "__KILL_SWITCH_PATH__",
    "--timeout-sec",
    "1800",
    "--expected-count",
    "1",
    "--enable-observation",
    "--allow-write",
]
_CANDIDATE_TEMPLATE = {
    "EnvironmentVariables": {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "__BUNDLED_RUNTIME_ROOT__",
    },
    "Label": _CANDIDATE_LABEL,
    "LowPriorityIO": True,
    "Nice": 1,
    "ProcessType": "Background",
    "ProgramArguments": list(_CANDIDATE_PROGRAM_ARGUMENTS),
    "StandardErrorPath": "__HOME__/Library/Logs/lecture_stt/controller-shadow.err.log",
    "StandardOutPath": "__HOME__/Library/Logs/lecture_stt/controller-shadow.evidence-result.jsonl",
    "StartInterval": 300,
    "WorkingDirectory": "__BUNDLED_RUNTIME_ROOT__",
}


class ControllerTopologyError(RuntimeError):
    """Closed validation error for the static controller launchd topology."""


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_exact_keys(mapping: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(mapping)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ControllerTopologyError(
            f"{label} keys mismatch: missing={missing} unexpected={unexpected}"
        )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ControllerTopologyError(f"{label} must be an object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ControllerTopologyError(f"{label} must be a non-empty string")
    return value


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ControllerTopologyError(f"{label} must be a boolean")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ControllerTopologyError(f"{label} must be a positive integer")
    return value


def _normalize_repo_root(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    root = Path(os.path.abspath(os.fspath(candidate)))
    try:
        observed = root.lstat()
    except FileNotFoundError as exc:
        raise ControllerTopologyError("repo root does not exist") from exc
    except OSError as exc:
        raise ControllerTopologyError("repo root is not safely inspectable") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise ControllerTopologyError("repo root must be a directory")
    return root


def _repo_file_path(repo_root: Path, relative_path: Path) -> Path:
    current = repo_root
    for component in relative_path.parts[:-1]:
        current = current / component
        try:
            observed = current.lstat()
        except OSError as exc:
            raise ControllerTopologyError(
                f"{relative_path.as_posix()} parent is not safely inspectable"
            ) from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise ControllerTopologyError(
                f"{relative_path.as_posix()} parent must be a real directory"
            )
    return current / relative_path.name


def _read_repo_file_bytes(path: Path, *, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ControllerTopologyError(f"{label} is not safely openable") from exc

    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ControllerTopologyError(f"{label} must be a regular file")
        if before.st_nlink != 1:
            raise ControllerTopologyError(f"{label} must be a regular single-link file")
        if before.st_size <= 0 or before.st_size > MAX_REPO_FILE_BYTES:
            raise ControllerTopologyError(f"{label} size is out of bounds")

        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)

        after = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise ControllerTopologyError(f"{label} changed while it was read")
        if any(getattr(after, field) != getattr(current, field) for field in stable_fields):
            raise ControllerTopologyError(f"{label} changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _parse_plist_value(element: ET.Element, *, label: str) -> Any:
    tag = element.tag
    if tag == "dict":
        children = list(element)
        if len(children) % 2 != 0:
            raise ControllerTopologyError(f"{label} dict is malformed")
        result: dict[str, Any] = {}
        for index in range(0, len(children), 2):
            key_element = children[index]
            value_element = children[index + 1]
            if key_element.tag != "key":
                raise ControllerTopologyError(f"{label} dict is malformed")
            key_text = key_element.text or ""
            if not key_text:
                raise ControllerTopologyError(f"{label} dict key is invalid")
            if key_text in result:
                raise ControllerTopologyError(f"{label} contains duplicate plist keys")
            result[key_text] = _parse_plist_value(
                value_element,
                label=f"{label}.{key_text}",
            )
        return result
    if tag == "array":
        return [
            _parse_plist_value(child, label=f"{label}[{index}]")
            for index, child in enumerate(list(element))
        ]
    if tag == "string":
        return element.text or ""
    if tag == "integer":
        raw = (element.text or "").strip()
        try:
            return int(raw)
        except ValueError as exc:
            raise ControllerTopologyError(f"{label} integer is invalid") from exc
    if tag == "true":
        return True
    if tag == "false":
        return False
    raise ControllerTopologyError(f"{label} plist value is unsupported")


def _parse_plist_bytes(payload: bytes, *, label: str) -> Mapping[str, Any]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ControllerTopologyError(f"{label} plist is malformed") from exc
    if root.tag != "plist":
        raise ControllerTopologyError(f"{label} plist root is invalid")
    children = list(root)
    if len(children) != 1 or children[0].tag != "dict":
        raise ControllerTopologyError(f"{label} plist root is invalid")
    parsed = _parse_plist_value(children[0], label=label)
    return _require_mapping(parsed, label)


def _collect_placeholders(value: Any) -> set[str]:
    placeholders: set[str] = set()
    if isinstance(value, Mapping):
        for nested in value.values():
            placeholders.update(_collect_placeholders(nested))
    elif isinstance(value, list):
        for nested in value:
            placeholders.update(_collect_placeholders(nested))
    elif isinstance(value, str):
        placeholders.update(_PLACEHOLDER_RE.findall(value))
    return placeholders


def _read_plist(path: Path, *, label: str) -> tuple[Mapping[str, Any], str]:
    payload = _read_repo_file_bytes(path, label=label)
    return _parse_plist_bytes(payload, label=label), _sha256_bytes(payload)


def _validate_existing_templates(repo_root: Path) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    for relative_path, expected_label in zip(_EXISTING_TEMPLATE_PATHS, _EXISTING_LABELS, strict=True):
        parsed, digest = _read_plist(
            _repo_file_path(repo_root, relative_path),
            label=relative_path.as_posix(),
        )
        label = _require_string(parsed.get("Label"), f"{relative_path.as_posix()}.Label")
        if label != expected_label:
            raise ControllerTopologyError("existing launchd label does not match its filename")
        if label in seen_labels:
            raise ControllerTopologyError("existing launchd labels must be unique")
        seen_labels.add(label)
        details.append(
            {
                "filename": relative_path.name,
                "label": label,
                "stderr": _require_string(
                    parsed.get("StandardErrorPath"),
                    f"{relative_path.as_posix()}.StandardErrorPath",
                ),
                "stdout": _require_string(
                    parsed.get("StandardOutPath"),
                    f"{relative_path.as_posix()}.StandardOutPath",
                ),
                "sha256": digest,
            }
        )
    if tuple(item["label"] for item in details) != _EXISTING_LABELS:
        raise ControllerTopologyError("existing launchd labels drifted from the required set")
    return details


def _validate_candidate_template(repo_root: Path) -> tuple[dict[str, Any], str]:
    relative_path = _CANDIDATE_TEMPLATE_PATH
    parsed, digest = _read_plist(
        _repo_file_path(repo_root, relative_path),
        label=relative_path.as_posix(),
    )
    _require_exact_keys(parsed, _CANDIDATE_KEYS, relative_path.as_posix())
    environment = _require_mapping(
        parsed.get("EnvironmentVariables"),
        f"{relative_path.as_posix()}.EnvironmentVariables",
    )
    _require_exact_keys(
        environment,
        {"PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH"},
        f"{relative_path.as_posix()}.EnvironmentVariables",
    )
    if environment["PATH"] != _CANDIDATE_TEMPLATE["EnvironmentVariables"]["PATH"]:
        raise ControllerTopologyError("candidate PATH contract drifted")
    if (
        environment["PYTHONDONTWRITEBYTECODE"]
        != _CANDIDATE_TEMPLATE["EnvironmentVariables"]["PYTHONDONTWRITEBYTECODE"]
    ):
        raise ControllerTopologyError("candidate PYTHONDONTWRITEBYTECODE contract drifted")
    if environment["PYTHONPATH"] != _CANDIDATE_TEMPLATE["EnvironmentVariables"]["PYTHONPATH"]:
        raise ControllerTopologyError("candidate PYTHONPATH contract drifted")
    start_interval = _require_positive_int(
        parsed.get("StartInterval"),
        f"{relative_path.as_posix()}.StartInterval",
    )
    if start_interval > 3600:
        raise ControllerTopologyError("candidate StartInterval is not bounded")
    if start_interval != _CANDIDATE_TEMPLATE["StartInterval"]:
        raise ControllerTopologyError("candidate StartInterval contract drifted")
    if (
        _require_bool(parsed.get("LowPriorityIO"), f"{relative_path.as_posix()}.LowPriorityIO")
        is not True
    ):
        raise ControllerTopologyError("candidate LowPriorityIO contract drifted")
    nice = _require_positive_int(parsed.get("Nice"), f"{relative_path.as_posix()}.Nice")
    if nice != _CANDIDATE_TEMPLATE["Nice"]:
        raise ControllerTopologyError("candidate Nice contract drifted")
    process_type = _require_string(
        parsed.get("ProcessType"),
        f"{relative_path.as_posix()}.ProcessType",
    )
    if process_type != _CANDIDATE_TEMPLATE["ProcessType"]:
        raise ControllerTopologyError("candidate ProcessType contract drifted")
    label = _require_string(parsed.get("Label"), f"{relative_path.as_posix()}.Label")
    if label != _CANDIDATE_LABEL:
        raise ControllerTopologyError("candidate launchd label drifted")
    program_arguments = parsed.get("ProgramArguments")
    if not isinstance(program_arguments, list):
        raise ControllerTopologyError(
            f"{relative_path.as_posix()}.ProgramArguments must be an array"
        )
    if program_arguments != _CANDIDATE_PROGRAM_ARGUMENTS:
        raise ControllerTopologyError("candidate ProgramArguments drifted from the read-only shadow shape")
    placeholders = _collect_placeholders(parsed)
    if placeholders != _ALLOWED_PLACEHOLDERS:
        raise ControllerTopologyError("candidate placeholder set drifted")
    if "KeepAlive" in parsed or "RunAtLoad" in parsed:
        raise ControllerTopologyError("candidate must not auto-restart or run at load")
    stdout = _require_string(
        parsed.get("StandardOutPath"),
        f"{relative_path.as_posix()}.StandardOutPath",
    )
    stderr = _require_string(
        parsed.get("StandardErrorPath"),
        f"{relative_path.as_posix()}.StandardErrorPath",
    )
    working_directory = _require_string(
        parsed.get("WorkingDirectory"),
        f"{relative_path.as_posix()}.WorkingDirectory",
    )
    if working_directory != _CANDIDATE_TEMPLATE["WorkingDirectory"]:
        raise ControllerTopologyError("candidate working directory contract drifted")
    if stdout == stderr:
        raise ControllerTopologyError("candidate stdout and stderr paths must differ")
    if stdout != _CANDIDATE_TEMPLATE["StandardOutPath"]:
        raise ControllerTopologyError("candidate stdout path contract drifted")
    if stderr != _CANDIDATE_TEMPLATE["StandardErrorPath"]:
        raise ControllerTopologyError("candidate stderr path contract drifted")
    if not stdout.endswith(".evidence-result.jsonl"):
        raise ControllerTopologyError(
            "candidate stdout path must end with .evidence-result.jsonl"
        )
    return {
        "filename": relative_path.name,
        "evidence_wrapper_connected": True,
        "label": label,
        "stderr": stderr,
        "stdout": stdout,
        "start_interval": start_interval,
    }, digest


def _validate_setup_script(repo_root: Path) -> tuple[list[str], str]:
    relative_path = _SETUP_SCRIPT_PATH
    payload = _read_repo_file_bytes(
        _repo_file_path(repo_root, relative_path),
        label=relative_path.as_posix(),
    )
    digest = _sha256_bytes(payload)
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ControllerTopologyError("setup_launchd.sh must be valid UTF-8") from exc
    if _CANDIDATE_LABEL in text:
        raise ControllerTopologyError("setup_launchd.sh must not reference the controller shadow label")
    match = _PLISTS_BLOCK_RE.search(text)
    if match is None:
        raise ControllerTopologyError("setup_launchd.sh PLISTS block is missing")
    labels = _QUOTED_LABEL_RE.findall(match.group("body"))
    if labels != list(_EXISTING_LABELS):
        raise ControllerTopologyError("setup_launchd.sh PLISTS block drifted from the required labels")
    if 'src="$REPO/launchd/${label}.plist"' not in text:
        raise ControllerTopologyError("setup_launchd.sh render source drifted from launchd/*.plist")
    if "controller/launchd" in text:
        raise ControllerTopologyError("setup_launchd.sh must not render controller launchd templates")
    return labels, digest


def run_topology_preflight(repo_root: str | Path) -> dict[str, Any]:
    root = _normalize_repo_root(repo_root)
    existing_templates = _validate_existing_templates(root)
    candidate_template, candidate_digest = _validate_candidate_template(root)
    script_labels, setup_digest = _validate_setup_script(root)

    labels = [item["label"] for item in existing_templates]
    labels.append(candidate_template["label"])
    if len(labels) != len(set(labels)):
        raise ControllerTopologyError("launchd labels must be unique")

    log_paths: list[str] = []
    for template in existing_templates:
        log_paths.extend((template["stdout"], template["stderr"]))
    log_paths.extend((candidate_template["stdout"], candidate_template["stderr"]))
    if len(log_paths) != len(set(log_paths)):
        raise ControllerTopologyError("launchd log paths must be unique")

    topology_input = {
        "candidate": candidate_template,
        "existing": existing_templates,
        "script": {
            "labels": script_labels,
            "sha256": setup_digest,
            "source_template": "launchd/${label}.plist",
        },
    }
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "ready": True,
        "mode": "read_only",
        "install_supported": False,
        "evidence_wrapper_connected": True,
        "existing_template_count": len(existing_templates),
        "script_label_count": len(script_labels),
        "total_template_count": len(existing_templates) + 1,
        "candidate_label": _CANDIDATE_LABEL,
        "candidate_template_sha256": candidate_digest,
        "topology_sha256": _sha256_json(topology_input),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Static read-only preflight for the future controller launchd topology"
    )
    parser.add_argument(
        "--repo-root",
        required=True,
        help="Repository root containing launchd/, controller/launchd/, and scripts/setup_launchd.sh",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = run_topology_preflight(args.repo_root)
    except ControllerTopologyError as exc:
        print(f"controller topology preflight failed: {exc}", file=sys.stderr)
        return 2
    json.dump(summary, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
