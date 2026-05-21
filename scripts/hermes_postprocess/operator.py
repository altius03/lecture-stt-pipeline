from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .contract import (
    BASELINE_FILENAME,
    CLAIM_ACTIVE_STATUSES,
    CLAIM_TERMINAL_STATUSES,
    CLAIMS_DIR,
    CRON_LOGS_DIR,
    FAILURE_CLASSES,
    POSTPROCESS_STATE_DIR,
    SCHEMA_VERSION,
    STATE_DIR,
)
from .finals import all_final_regular, final_type_name, unsupported_final_entries
from .picker import _candidate_stems, build_action_plan, is_complete  # noqa: PLC2701 - internal scanner reused by operator
from .paths import resolve_candidate_paths
from .schemas import Candidate
from .staging import PromoteError, preflight_safe_staging_artifacts, promote_candidate, write_staging_manifest
from .validators import validate_correction_artifacts, validate_summary_artifact


@dataclass(frozen=True)
class OperatorConfig:
    repo_root: Path
    lecture_root: Path
    hermes_bin: Path
    stable_for_sec: int = 60
    stale_claim_after_sec: int = 60 * 60 * 2
    child_timeout_sec: int = 60 * 45
    source: str = "cron-lecture-stt-postprocess"

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_root", Path(self.repo_root).expanduser())
        object.__setattr__(self, "lecture_root", Path(self.lecture_root).expanduser())
        object.__setattr__(self, "hermes_bin", Path(self.hermes_bin).expanduser())

    @property
    def postprocess_state_dir(self) -> Path:
        return self.repo_root / STATE_DIR / POSTPROCESS_STATE_DIR

    @property
    def baseline_path(self) -> Path:
        return self.postprocess_state_dir / BASELINE_FILENAME

    @property
    def cron_log_dir(self) -> Path:
        return self.postprocess_state_dir / CRON_LOGS_DIR

    @property
    def claims_dir(self) -> Path:
        return self.postprocess_state_dir / CLAIMS_DIR


@dataclass(frozen=True)
class ChildRunResult:
    exit_code: int
    log_path: Path


@dataclass(frozen=True)
class FinalPathSnapshot:
    path: Path
    sha256: str
    content: bytes
    mode: int
    symlink_target: str | None = None


ChildRunner = Callable[[Candidate, Path, OperatorConfig], ChildRunResult | tuple[int, Path]]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _format_ts(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _parse_ts(raw: Any) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _json_dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(_json_dump(payload), encoding="utf-8")
    os.replace(tmp, path)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def load_baseline_skip_stems(config: OperatorConfig) -> set[str]:
    payload = _read_json_object(config.baseline_path)
    if not payload:
        return set()
    stems = payload.get("skip_stems", [])
    if not isinstance(stems, list):
        return set()
    return {str(stem) for stem in stems}


def _claim_payload(path: Path) -> dict[str, Any] | None:
    return _read_json_object(path)


def _claim_updated_at(payload: dict[str, Any]) -> dt.datetime | None:
    return _parse_ts(payload.get("updated_at")) or _parse_ts(payload.get("completed_at")) or _parse_ts(payload.get("claimed_at"))


def _metadata_claim_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "stem": str(payload.get("stem", "")),
        "status": str(payload.get("status", "")),
        "updated_at": payload.get("updated_at"),
        "raw_transcript_body_included": False,
    }
    for key in ("completed_at", "failure", "retryable", "log_path", "final_paths"):
        if key in payload:
            summary[key] = payload[key]
    return summary


def _iter_claim_payloads(config: OperatorConfig) -> list[dict[str, Any]]:
    if not config.claims_dir.exists():
        return []
    payloads: list[dict[str, Any]] = []
    for path in sorted(config.claims_dir.glob("*.json")):
        payload = _claim_payload(path)
        if payload:
            payloads.append(payload)
    return payloads


def _is_stale_claim(path: Path, *, now: dt.datetime, stale_after_sec: int) -> bool:
    payload = _claim_payload(path)
    if not payload:
        return False
    status = str(payload.get("status", ""))
    if status not in CLAIM_ACTIVE_STATUSES:
        return False
    updated_at = _parse_ts(payload.get("updated_at")) or _parse_ts(payload.get("claimed_at"))
    if updated_at is None:
        return False
    return (now - updated_at).total_seconds() >= stale_after_sec


def release_stale_claim(path: Path, *, now: dt.datetime, stale_after_sec: int) -> bool:
    if not path.exists() or not _is_stale_claim(path, now=now, stale_after_sec=stale_after_sec):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    return True


def _claim_blocks_selection(path: Path, *, now: dt.datetime, stale_after_sec: int) -> bool:
    payload = _claim_payload(path)
    if not payload:
        return True
    status = str(payload.get("status", ""))
    if status in CLAIM_ACTIVE_STATUSES:
        return not release_stale_claim(path=path, now=now, stale_after_sec=stale_after_sec)
    if status == "failed" and payload.get("retryable") is True:
        updated_at = _claim_updated_at(payload)
        if updated_at is not None and (now - updated_at).total_seconds() >= stale_after_sec:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return False
    if status in CLAIM_TERMINAL_STATUSES:
        return True
    return True


def select_candidate(config: OperatorConfig, *, now: dt.datetime | None = None) -> Candidate | None:
    now = now or utc_now()
    skip_stems = load_baseline_skip_stems(config)
    for stem in _candidate_stems(config.lecture_root, stable_for_sec=config.stable_for_sec):
        if stem in skip_stems:
            continue
        paths = resolve_candidate_paths(stem, lecture_root=config.lecture_root, repo_root=config.repo_root)
        if paths.claim_path.exists() and _claim_blocks_selection(paths.claim_path, now=now, stale_after_sec=config.stale_claim_after_sec):
            continue
        if is_complete(paths):
            continue
        actions, blocked = build_action_plan(paths)
        return Candidate(paths=paths, actions=actions, blocked_reasons=blocked)
    return None


def acquire_claim(candidate: Candidate, config: OperatorConfig, *, now: dt.datetime | None = None) -> bool:
    now = now or utc_now()
    claim_path = Path(candidate.claim_path)
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "stem": candidate.stem,
        "subject": candidate.subject,
        "status": "claimed",
        "operator": "hermes-cron-script",
        "source": config.source,
        "claimed_at": _format_ts(now),
        "updated_at": _format_ts(now),
        "raw_transcript_body_included": False,
    }
    fd: int | None = None
    try:
        fd = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(_json_dump(payload))
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except FileExistsError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def write_claim_status(
    candidate: Candidate,
    status: str,
    *,
    config: OperatorConfig,
    now: dt.datetime | None = None,
    **extra: Any,
) -> None:
    now = now or utc_now()
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "stem": candidate.stem,
        "subject": candidate.subject,
        "status": status,
        "operator": "hermes-cron-script",
        "source": config.source,
        "updated_at": _format_ts(now),
        "raw_transcript_body_included": False,
    }
    payload.update(extra)
    _atomic_write_json(Path(candidate.claim_path), payload)


def write_candidate_and_manifest(candidate: Candidate) -> tuple[Path, Path]:
    staging_dir = Path(candidate.staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = staging_dir / "candidate.json"
    candidate_path.write_text(_json_dump(candidate.to_dict()), encoding="utf-8")
    manifest_path = write_staging_manifest(candidate)
    return candidate_path, manifest_path


def build_child_prompt(candidate: Candidate, candidate_path: Path, config: OperatorConfig) -> str:
    staging_dir = Path(candidate.staging_dir)
    return textwrap.dedent(
        f"""
        You are the lecture_stt Hermes postprocess operator running from cron.
        Process exactly one candidate stem and do not look for other candidates.

        Repository: {config.repo_root}
        Candidate JSON: {candidate_path}
        Stem: {candidate.stem}
        Subject: {candidate.subject}
        Staging dir: {staging_dir}
        Operator docs dir: {candidate.operator_docs_dir}
        Prompt dir: {candidate.prompt_dir}

        Final writes are not approved for the child run. Generate staged
        artifacts only; the parent operator owns deterministic validation and
        promotion after the child exits. Do not write final outputs,
        validation-*.json, or promote.json. Do not process backlog or any other
        stem. Do not create/edit cron jobs. Do not commit, push, tag, release,
        restart services, or modify 05_prompt. Do not print raw transcript body,
        full corrected transcript, or full summary body in your final answer or
        routine reports.

        Required read order before generation:
        1. docs/operators/hermes-postprocess/operator-runbook.md
        2. docs/operators/hermes-postprocess/output-contract.md
        3. docs/operators/hermes-postprocess/failure-policy.md
        4. docs/operators/hermes-postprocess/correction-prompt.md
        5. docs/operators/hermes-postprocess/summary-prompt.md
        6. candidate prompt_dir/00_base_prompt.txt
        7. candidate prompt_dir/01_common_glossary.txt
        8. candidate prompt_dir/glossary_{{subject}}.txt if present

        Required actions:
        - Read candidate JSON and raw inputs from the candidate paths.
        - Generate only the required staged artifacts according to candidate actions.
        - Leave deterministic validation, promotion, and final output writes to
          the parent operator.

        Output contract for your final message:
        Return metadata only, no excerpts. Include stem, staged artifact paths
        written, and a brief caveat that parent validation/promotion is pending
        if needed.
        """
    ).strip()


def _sandbox_escape(value: Path) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _child_sandbox_profile(candidate: Candidate, config: OperatorConfig) -> str:
    staging_dir = Path(candidate.staging_dir)
    writable_subpaths = [
        staging_dir,
    ]
    for tmp_dir in (os.environ.get("TMPDIR"), "/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp"):
        if tmp_dir:
            writable_subpaths.append(Path(tmp_dir))
    read_allowed_subpaths = [
        staging_dir,
        Path(candidate.operator_docs_dir),
        Path(candidate.prompt_dir),
        Path(config.hermes_bin).parent,
        Path("/bin"),
        Path("/usr"),
        Path("/System"),
        Path("/Library"),
        Path("/opt"),
        Path("/private/var/db"),
    ]
    read_allowed_literals = [
        Path(candidate.raw_txt_path),
        Path(candidate.raw_json_path),
        staging_dir / "candidate.json",
        staging_dir / "manifest.json",
    ]
    if candidate.actions.get("summary", {}).get("input_source") == "final_correction":
        read_allowed_literals.append(Path(candidate.correction_txt_path))
    read_only_subpaths = [
        Path(candidate.correction_txt_path).parent,
        Path(candidate.summary_md_path).parent,
        Path(candidate.prompt_dir),
        Path(candidate.raw_txt_path).parent,
        config.claims_dir,
    ]
    read_only_literals = [
        staging_dir / "validation-correction.json",
        staging_dir / "validation-summary.json",
        staging_dir / "promote.json",
    ]
    lines = [
        "(version 1)",
        "; Child LLM may write only staged artifacts and runtime logs/temp files.",
        "; Parent owns validation, claims, prompts, transcript sources, and final outputs.",
        "(deny default)",
        "(allow process*)",
        "(allow network*)",
        "(allow mach-lookup)",
        "(allow sysctl*)",
        "(deny file-write*)",
    ]
    for path in read_allowed_subpaths:
        lines.append(f'(allow file-read* (subpath "{_sandbox_escape(path)}"))')
    for path in read_allowed_literals:
        lines.append(f'(allow file-read* (literal "{_sandbox_escape(path)}"))')
    for path in writable_subpaths:
        lines.append(f'(allow file-write* (subpath "{_sandbox_escape(path)}"))')
    for path in read_only_subpaths:
        lines.append(f'(deny file-write* (subpath "{_sandbox_escape(path)}"))')
    for path in read_only_literals:
        lines.append(f'(deny file-write* (literal "{_sandbox_escape(path)}"))')
    return "\n".join(lines) + "\n"


def _wrap_child_command_with_sandbox(
    cmd: list[str],
    *,
    candidate: Candidate,
    config: OperatorConfig,
    run_id: str,
) -> list[str]:
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        if sys.platform == "darwin":
            raise RuntimeError("sandbox_exec_missing")
        return cmd
    profile_path = config.cron_log_dir / f"{run_id}-{candidate.stem}.sb"
    profile_path.write_text(_child_sandbox_profile(candidate, config), encoding="utf-8")
    return [sandbox_exec, "-f", str(profile_path), *cmd]


def run_child_hermes(candidate: Candidate, candidate_path: Path, config: OperatorConfig) -> ChildRunResult:
    config.cron_log_dir.mkdir(parents=True, exist_ok=True)
    run_id = utc_now().strftime('%Y%m%dT%H%M%SZ')
    log_path = config.cron_log_dir / f"{run_id}-{candidate.stem}.log"
    prompt = build_child_prompt(candidate, candidate_path, config)
    cmd = [
        str(config.hermes_bin),
        "chat",
        "-Q",
        "-t",
        "terminal,file",
        "--source",
        config.source,
        "-q",
        prompt,
    ]
    cmd = _wrap_child_command_with_sandbox(cmd, candidate=candidate, config=config, run_id=run_id)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(log_fd, "w", encoding="utf-8") as log:
        os.chmod(log_path, 0o600)
        log.write(f"# lecture_stt postprocess child run\n# stem={candidate.stem}\n# started_at={_format_ts(utc_now())}\n")
        log.write("# child stdout/stderr suppressed by metadata-only policy\n")
        log.flush()
        proc = subprocess.run(
            cmd,
            cwd=config.repo_root,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=config.child_timeout_sec,
            env={**os.environ, "LECTURE_RECORDINGS_ROOT": str(config.lecture_root)},
            check=False,
        )
        log.write(f"\n# exit_code={proc.returncode}\n# finished_at={_format_ts(utc_now())}\n")
    return ChildRunResult(exit_code=proc.returncode, log_path=log_path)


def _normalize_child_result(result: ChildRunResult | tuple[int, Path]) -> ChildRunResult:
    if isinstance(result, ChildRunResult):
        return result
    exit_code, log_path = result
    return ChildRunResult(exit_code=int(exit_code), log_path=Path(log_path))


def _all_final_output_paths(candidate: Candidate) -> list[Path]:
    return [Path(candidate.correction_txt_path), Path(candidate.correction_json_path), Path(candidate.summary_md_path)]


def _planned_final_output_paths(candidate: Candidate) -> list[Path]:
    paths: list[Path] = []
    if candidate.actions.get("correction", {}).get("needed") is True:
        paths.extend([Path(candidate.correction_txt_path), Path(candidate.correction_json_path)])
    if candidate.actions.get("summary", {}).get("needed") is True:
        paths.append(Path(candidate.summary_md_path))
    return paths


def _unsafe_single_link_regular_artifact(path: Path, kind: str) -> dict[str, str] | None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return {"kind": kind, "path": str(path), "reason": "missing"}
    except OSError as exc:
        return {"kind": kind, "path": str(path), "reason": type(exc).__name__}
    file_type = final_type_name(path_stat.st_mode)
    if not stat.S_ISREG(path_stat.st_mode):
        return {"kind": kind, "path": str(path), "reason": "non_regular", "file_type": file_type}
    if path_stat.st_nlink != 1:
        return {
            "kind": kind,
            "path": str(path),
            "reason": "unexpected_link_count",
            "file_type": file_type,
            "link_count": str(path_stat.st_nlink),
        }
    return None


def _raw_input_preflight_issues(candidate: Candidate) -> list[dict[str, str]]:
    checks = [
        (Path(candidate.raw_txt_path), "raw_txt"),
        (Path(candidate.raw_json_path), "raw_json"),
    ]
    return [issue for path, kind in checks if (issue := _unsafe_single_link_regular_artifact(path, kind)) is not None]


def _prompt_preflight_issue(candidate: Candidate) -> tuple[str, dict[str, Any]] | None:
    prompt_dir = Path(candidate.prompt_dir)
    try:
        prompt_stat = prompt_dir.lstat()
    except FileNotFoundError:
        return "missing_prompt_dir", {"path": str(prompt_dir), "reason": "missing"}
    except OSError as exc:
        return "missing_prompt_dir", {"path": str(prompt_dir), "reason": type(exc).__name__}
    if not stat.S_ISDIR(prompt_stat.st_mode):
        return "missing_prompt_dir", {"path": str(prompt_dir), "reason": "non_directory", "file_type": final_type_name(prompt_stat.st_mode)}

    required = [
        (prompt_dir / "00_base_prompt.txt", "missing_base_prompt", "base_prompt"),
        (prompt_dir / "01_common_glossary.txt", "missing_common_glossary", "common_glossary"),
    ]
    for path, failure_class, kind in required:
        issue = _unsafe_single_link_regular_artifact(path, kind)
        if issue is not None:
            return failure_class, issue
    return None


def _pre_child_failed_status(candidate: Candidate, failure_class: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    staging_dir = Path(candidate.staging_dir)
    final_paths = [Path(candidate.correction_txt_path), Path(candidate.correction_json_path), Path(candidate.summary_md_path)]
    _write_parent_promote_failure(
        staging_dir,
        candidate,
        failure_class=failure_class,
        message=f"parent preflight failed before child run: {failure_class}",
        details=details or {},
    )
    return {
        "validation_correction_passed": False,
        "validation_summary_passed": False,
        "promote_passed": False,
        "finals_exist": all_final_regular(final_paths),
        "final_paths": [str(path) for path in final_paths],
        "promote_failure_class": failure_class,
        "preflight": details or {},
        "raw_transcript_body_included": False,
    }


def _pre_child_failed_outcome(
    candidate: Candidate,
    config: OperatorConfig,
    *,
    now: dt.datetime,
    failure_class: str,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    status = _pre_child_failed_status(candidate, failure_class, details)
    write_claim_status(
        candidate,
        "failed",
        config=config,
        now=now,
        failure=failure_class,
        verification=status,
        retryable=retryable,
    )
    alert = {
        "kind": "lecture_stt_postprocess_failed",
        "stem": candidate.stem,
        "failure": failure_class,
        "promote_failure_class": failure_class,
        "retryable": retryable,
        "raw_transcript_body_included": False,
    }
    return _run_once_outcome("failed", stem=candidate.stem, alert=alert)


class UnsafeQuarantineDirError(RuntimeError):
    pass


class UnsafeFinalParentError(RuntimeError):
    def __init__(self, path: Path, issue: dict[str, str]):
        self.path = path
        self.issue = issue
        super().__init__(issue.get("reason", "unsafe_final_parent_after_child"))


def _final_parent_issue(path: Path, *, reason: str = "unsafe_final_parent_after_child") -> dict[str, str]:
    parent = path.parent
    issue = {"final_path": str(path), "final_parent_path": str(parent), "reason": reason}
    try:
        mode = parent.lstat().st_mode
    except FileNotFoundError:
        issue["file_type"] = "missing"
        return issue
    except OSError as exc:
        issue["file_type"] = "unknown"
        issue["error"] = type(exc).__name__
        return issue
    issue["file_type"] = final_type_name(mode)
    if stat.S_ISLNK(mode):
        try:
            issue["symlink_target"] = os.readlink(parent)
        except OSError as exc:
            issue["error"] = type(exc).__name__
    return issue


def _open_existing_real_final_parent(path: Path) -> int | None:
    parent = path.parent
    try:
        mode = parent.lstat().st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UnsafeFinalParentError(path, _final_parent_issue(path, reason=f"lstat_final_parent_failed:{type(exc).__name__}")) from exc
    if not stat.S_ISDIR(mode):
        raise UnsafeFinalParentError(path, _final_parent_issue(path))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(parent, flags)
    except OSError as exc:
        raise UnsafeFinalParentError(path, _final_parent_issue(path, reason=f"open_final_parent_failed:{type(exc).__name__}")) from exc


def _lstat_final_entry_from_real_parent(path: Path) -> tuple[int, os.stat_result] | None:
    parent_fd = _open_existing_real_final_parent(path)
    if parent_fd is None:
        return None
    try:
        try:
            return parent_fd, os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.close(parent_fd)
            return None
        except Exception:
            os.close(parent_fd)
            raise
    except Exception:
        raise


def _read_regular_final_bytes(parent_fd: int, path: Path) -> bytes:
    fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _ensure_real_quarantine_dir(staging_dir: Path) -> Path:
    quarantine_dir = staging_dir / "unauthorized-final-artifacts"
    if not os.path.lexists(quarantine_dir):
        quarantine_dir.mkdir(parents=True, exist_ok=True)
    try:
        mode = quarantine_dir.lstat().st_mode
    except OSError as exc:
        raise UnsafeQuarantineDirError(f"unsafe quarantine directory: {quarantine_dir}: {type(exc).__name__}") from exc
    if not stat.S_ISDIR(mode):
        raise UnsafeQuarantineDirError(f"unsafe quarantine directory: {quarantine_dir} ({final_type_name(mode)})")
    return quarantine_dir


def _quarantine_target(staging_dir: Path, final_path: Path) -> Path:
    quarantine_dir = _ensure_real_quarantine_dir(staging_dir)
    base_name = f"{final_path.parent.name}__{final_path.name}"
    target = quarantine_dir / base_name
    suffix = 1
    while os.path.lexists(target):
        target = quarantine_dir / f"{base_name}.{suffix}"
        suffix += 1
    return target


def _move_final_entry_to_quarantine(staging_dir: Path, path: Path) -> Path | None:
    entry = _lstat_final_entry_from_real_parent(path)
    if entry is None:
        return None
    parent_fd, _current = entry
    try:
        target = _quarantine_target(staging_dir, path)
        os.rename(path.name, str(target), src_dir_fd=parent_fd)
        return target
    finally:
        os.close(parent_fd)


def quarantine_unauthorized_final_outputs(candidate: Candidate, expected_absent_paths: list[Path]) -> list[dict[str, str]]:
    staging_dir = Path(candidate.staging_dir)
    quarantined: list[dict[str, str]] = []
    for path in expected_absent_paths:
        try:
            target = _move_final_entry_to_quarantine(staging_dir, path)
            if target is None:
                continue
            quarantined.append({"final_path": str(path), "quarantine_path": str(target), "reason": "created_by_child"})
        except UnsafeFinalParentError as exc:
            quarantined.append(exc.issue)
        except UnsafeQuarantineDirError as exc:
            quarantined.append({"final_path": str(path), "reason": "quarantine_failed_unsafe_quarantine_dir", "error": str(exc)})
        except OSError as exc:
            quarantined.append({"final_path": str(path), "reason": "quarantine_failed", "error": type(exc).__name__})
    return quarantined


def _symlink_target(path: Path, mode: int) -> str | None:
    if stat.S_ISLNK(mode):
        return os.readlink(path)
    return None


def snapshot_existing_final_outputs(candidate: Candidate) -> dict[Path, FinalPathSnapshot]:
    snapshots: dict[Path, FinalPathSnapshot] = {}
    for path in _all_final_output_paths(candidate):
        if not os.path.lexists(path):
            continue
        path_stat = path.lstat()
        mode = path_stat.st_mode
        content = path.read_bytes() if stat.S_ISREG(mode) else b""
        snapshots[path] = FinalPathSnapshot(
            path=path,
            sha256=hashlib.sha256(content).hexdigest(),
            content=content,
            mode=mode,
            symlink_target=_symlink_target(path, mode),
        )
    return snapshots


def _atomic_restore_snapshot(snapshot: FinalPathSnapshot) -> None:
    path = snapshot.path
    parent_fd = _open_existing_real_final_parent(path)
    if parent_fd is None:
        raise OSError(f"unsafe final parent missing during restore: {path.parent}")
    tmp_name = f".{path.name}.{os.getpid()}.restore.tmp"
    try:
        if snapshot.symlink_target is not None:
            try:
                os.unlink(path.name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.symlink(snapshot.symlink_target, path.name, dir_fd=parent_fd)
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        fd = os.open(tmp_name, flags, stat.S_IMODE(snapshot.mode), dir_fd=parent_fd)
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(snapshot.content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except Exception:
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(parent_fd)


def quarantine_modified_final_outputs(candidate: Candidate, snapshots: dict[Path, FinalPathSnapshot]) -> list[dict[str, str]]:
    staging_dir = Path(candidate.staging_dir)
    repaired: list[dict[str, str]] = []
    for path, snapshot in snapshots.items():
        reason: str | None = None
        current_sha: str | None = None
        try:
            current_entry = _lstat_final_entry_from_real_parent(path)
        except UnsafeFinalParentError as exc:
            entry = dict(exc.issue)
            entry["restored_sha256"] = snapshot.sha256
            repaired.append(entry)
            continue
        if current_entry is None:
            reason = "deleted_by_child"
        else:
            parent_fd, current_stat = current_entry
            try:
                current_mode = current_stat.st_mode
                current_target = _symlink_target(path, current_mode)
                if stat.S_IFMT(current_mode) != stat.S_IFMT(snapshot.mode) or current_target != snapshot.symlink_target:
                    reason = "replaced_by_child"
                elif stat.S_IMODE(current_mode) != stat.S_IMODE(snapshot.mode):
                    reason = "mode_modified_by_child"
                elif stat.S_ISREG(current_mode):
                    current = _read_regular_final_bytes(parent_fd, path)
                    current_sha = hashlib.sha256(current).hexdigest()
                    if current_sha != snapshot.sha256:
                        reason = "modified_by_child"
            except OSError:
                reason = "replaced_by_child"
            finally:
                os.close(parent_fd)

        if reason is None:
            continue

        entry: dict[str, str] = {
            "final_path": str(path),
            "reason": reason,
            "restored_sha256": snapshot.sha256,
        }
        if current_sha is not None:
            entry["tampered_sha256"] = current_sha
        try:
            target = _move_final_entry_to_quarantine(staging_dir, path)
            if target is not None:
                entry["quarantine_path"] = str(target)
        except UnsafeFinalParentError as exc:
            entry["quarantine_error"] = exc.issue.get("reason", "unsafe_final_parent_after_child")
            entry["quarantine_error_detail"] = json.dumps(exc.issue, ensure_ascii=False, sort_keys=True)
        except UnsafeQuarantineDirError as exc:
            entry["quarantine_error"] = "quarantine_failed_unsafe_quarantine_dir"
            entry["quarantine_error_detail"] = str(exc)
        except OSError as exc:
            entry["quarantine_error"] = "quarantine_failed"
            entry["quarantine_error_detail"] = type(exc).__name__
        try:
            _atomic_restore_snapshot(snapshot)
        except (OSError, UnsafeFinalParentError) as exc:
            entry["restore_error"] = type(exc).__name__
        repaired.append(entry)
    return repaired


def unsupported_existing_final_outputs(candidate: Candidate) -> list[dict[str, str]]:
    return unsupported_final_entries(_all_final_output_paths(candidate))


def _preexisting_final_artifact_status(candidate: Candidate, unsupported: list[dict[str, str]]) -> dict[str, Any]:
    staging_dir = Path(candidate.staging_dir)
    failure_class = "preexisting_final_artifact_unsupported_type"
    message = "pre-existing final artifact is not a regular file; child run blocked without mutation"
    details: dict[str, Any] = {"unsupported_final_outputs": unsupported, "stem": candidate.stem}
    correction_needed = candidate.actions.get("correction", {}).get("needed") is True
    summary_needed = candidate.actions.get("summary", {}).get("needed") is True
    if correction_needed:
        _write_validation_failure_report(
            staging_dir,
            "validation-correction.json",
            candidate,
            details,
            failure_class=failure_class,
            message=message,
        )
    if summary_needed:
        _write_validation_failure_report(
            staging_dir,
            "validation-summary.json",
            candidate,
            details,
            failure_class=failure_class,
            message=message,
        )
    _write_parent_promote_failure(
        staging_dir,
        candidate,
        failure_class=failure_class,
        message=message,
        details=details,
    )
    final_paths = _all_final_output_paths(candidate)
    return {
        "validation_correction_passed": not correction_needed,
        "validation_summary_passed": not summary_needed,
        "promote_passed": False,
        "finals_exist": all_final_regular(final_paths),
        "final_paths": [str(path) for path in final_paths],
        "promote_failure_class": failure_class,
        "unsupported_final_outputs": unsupported,
        "raw_transcript_body_included": False,
    }


def _write_validation_failure_report(
    staging_dir: Path,
    name: str,
    candidate: Candidate,
    details: dict[str, Any],
    *,
    failure_class: str,
    message: str,
) -> None:
    _atomic_write_json(
        staging_dir / name,
        {
            "schema_version": SCHEMA_VERSION,
            "passed": False,
            "failure_class": failure_class,
            "message": message,
            "details": details,
            "stem": candidate.stem,
            "raw_transcript_body_included": False,
        },
    )


def _unauthorized_final_status(
    candidate: Candidate,
    quarantined: list[dict[str, str]],
    *,
    failure_class: str,
    message: str,
) -> dict[str, Any]:
    staging_dir = Path(candidate.staging_dir)
    details: dict[str, Any] = {"quarantined": quarantined, "stem": candidate.stem}
    correction_needed = candidate.actions.get("correction", {}).get("needed") is True
    summary_needed = candidate.actions.get("summary", {}).get("needed") is True
    if correction_needed:
        _write_validation_failure_report(
            staging_dir,
            "validation-correction.json",
            candidate,
            details,
            failure_class=failure_class,
            message=message,
        )
    if summary_needed:
        _write_validation_failure_report(
            staging_dir,
            "validation-summary.json",
            candidate,
            details,
            failure_class=failure_class,
            message=message,
        )
    _write_parent_promote_failure(
        staging_dir,
        candidate,
        failure_class=failure_class,
        message=message,
        details=details,
    )
    final_paths = [Path(candidate.correction_txt_path), Path(candidate.correction_json_path), Path(candidate.summary_md_path)]
    return {
        "validation_correction_passed": not correction_needed,
        "validation_summary_passed": not summary_needed,
        "promote_passed": False,
        "finals_exist": all_final_regular(final_paths),
        "final_paths": [str(path) for path in final_paths],
        "promote_failure_class": failure_class,
        "unauthorized_final_outputs": quarantined,
        "raw_transcript_body_included": False,
    }


def _post_child_unauthorized_final_status(
    candidate: Candidate,
    expected_absent_paths: list[Path],
    snapshots: dict[Path, FinalPathSnapshot],
) -> dict[str, Any] | None:
    created_outputs = quarantine_unauthorized_final_outputs(candidate, expected_absent_paths)
    modified_outputs = quarantine_modified_final_outputs(candidate, snapshots)
    unauthorized_outputs = [*created_outputs, *modified_outputs]
    if not unauthorized_outputs:
        return None
    if modified_outputs:
        failure_class = "unauthorized_final_output_modified_by_child"
        message = "child modified pre-existing final output; tampered outputs were quarantined and originals restored"
    else:
        failure_class = "unauthorized_final_output_created_by_child"
        message = "child created final output before parent promote; outputs were quarantined"
    return _unauthorized_final_status(candidate, unauthorized_outputs, failure_class=failure_class, message=message)


def _write_parent_promote_failure(
    staging_dir: Path,
    candidate: Candidate,
    *,
    failure_class: Any,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    _atomic_write_json(
        staging_dir / "promote.json",
        {
            "schema_version": SCHEMA_VERSION,
            "passed": False,
            "failure_class": str(failure_class or "postprocess_verification_failed"),
            "message": message,
            "details": details or {},
            "stem": candidate.stem,
            "raw_transcript_body_included": False,
        },
    )


def verify_success(candidate: Candidate) -> dict[str, Any]:
    staging_dir = Path(candidate.staging_dir)
    correction_needed = candidate.actions.get("correction", {}).get("needed") is True
    summary_needed = candidate.actions.get("summary", {}).get("needed") is True
    final_paths = [Path(candidate.correction_txt_path), Path(candidate.correction_json_path), Path(candidate.summary_md_path)]
    status = {
        "validation_correction_passed": not correction_needed,
        "validation_summary_passed": not summary_needed,
        "promote_passed": False,
        "finals_exist": False,
        "final_paths": [str(path) for path in final_paths],
        "promote_failure_class": None,
        "raw_transcript_body_included": False,
    }

    try:
        preflight_safe_staging_artifacts(candidate)
    except PromoteError as exc:
        status["promote_failure_class"] = exc.failure_class
        _write_parent_promote_failure(
            staging_dir,
            candidate,
            failure_class=exc.failure_class,
            message=str(exc),
            details=exc.details,
        )
        status["finals_exist"] = all_final_regular(final_paths)
        return status

    if correction_needed:
        correction_result = validate_correction_artifacts(
            raw_json_path=candidate.raw_json_path,
            correction_txt_path=staging_dir / "correction.txt",
            correction_json_path=staging_dir / "correction.json",
            final_txt_path=candidate.correction_txt_path,
            final_json_path=candidate.correction_json_path,
        ).to_dict()
        _atomic_write_json(staging_dir / "validation-correction.json", correction_result)
        status["validation_correction_passed"] = correction_result.get("passed") is True
        if not status["validation_correction_passed"]:
            status["promote_failure_class"] = correction_result.get("failure_class")
            _write_parent_promote_failure(
                staging_dir,
                candidate,
                failure_class=status["promote_failure_class"],
                message="parent correction validation failed before promote",
                details={"stage": "correction"},
            )
            if summary_needed:
                _atomic_write_json(
                    staging_dir / "validation-summary.json",
                    {
                        "schema_version": SCHEMA_VERSION,
                        "passed": False,
                        "failure_class": "summary_validation_skipped",
                        "message": "summary validation skipped because correction validation failed",
                        "details": {"stem": candidate.stem},
                        "raw_transcript_body_included": False,
                    },
                )
            status["finals_exist"] = all_final_regular(final_paths)
            return status

    if summary_needed:
        input_source = candidate.actions.get("summary", {}).get("input_source")
        if input_source == "staging_correction":
            summary_result = validate_summary_artifact(
                summary_md_path=staging_dir / "summary.md",
                corrected_txt_path=staging_dir / "correction.txt",
                final_md_path=candidate.summary_md_path,
            ).to_dict()
        elif input_source == "final_correction":
            summary_result = validate_summary_artifact(
                summary_md_path=staging_dir / "summary.md",
                corrected_txt_path=candidate.correction_txt_path,
                final_md_path=candidate.summary_md_path,
            ).to_dict()
        else:
            summary_result = {
                "schema_version": SCHEMA_VERSION,
                "passed": False,
                "failure_class": "summary_validation_failed",
                "message": "summary input source is not available",
                "details": {"input_source": input_source, "stem": candidate.stem},
                "raw_transcript_body_included": False,
            }
        _atomic_write_json(staging_dir / "validation-summary.json", summary_result)
        status["validation_summary_passed"] = summary_result.get("passed") is True
        if not status["validation_summary_passed"]:
            status["promote_failure_class"] = summary_result.get("failure_class")
            _write_parent_promote_failure(
                staging_dir,
                candidate,
                failure_class=status["promote_failure_class"],
                message="parent summary validation failed before promote",
                details={"stage": "summary"},
            )
            status["finals_exist"] = all_final_regular(final_paths)
            return status

    try:
        promote = promote_candidate(candidate, allow_promote=True)
    except PromoteError as exc:
        promote = {
            "schema_version": SCHEMA_VERSION,
            "passed": False,
            "failure_class": exc.failure_class,
            "message": str(exc),
            "details": exc.details,
            "stem": candidate.stem,
            "raw_transcript_body_included": False,
        }
        _atomic_write_json(staging_dir / "promote.json", promote)

    status["promote_passed"] = promote.get("passed") is True
    status["promote_failure_class"] = promote.get("failure_class")
    status["finals_exist"] = all_final_regular(final_paths)
    return status


def _blocked_alert(candidate: Candidate) -> dict[str, Any]:
    return {
        "kind": "lecture_stt_postprocess_failed",
        "stem": candidate.stem,
        "failure": candidate.blocked_reasons[0] if candidate.blocked_reasons else "blocked",
        "retryable": False,
        "raw_transcript_body_included": False,
    }


def _run_once_outcome(kind: str, *, stem: str | None = None, alert: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "alert": alert,
        "raw_transcript_body_included": False,
    }
    if stem is not None:
        payload["stem"] = stem
    return payload


def _exception_failure_class(exc: Exception) -> str:
    explicit = getattr(exc, "failure_class", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    message = str(exc)
    if message == "sandbox_exec_missing":
        return "sandbox_exec_missing"
    type_name = type(exc).__name__
    if type_name in FAILURE_CLASSES:
        return type_name
    return "operator_wrapper_failed"


def run_once(
    config: OperatorConfig,
    *,
    child_runner: ChildRunner = run_child_hermes,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    try:
        lecture_root_stat = config.lecture_root.lstat()
    except FileNotFoundError:
        alert = {
            "kind": "lecture_stt_postprocess_failed",
            "failure": "icloud_unavailable",
            "retryable": True,
            "raw_transcript_body_included": False,
        }
        return _run_once_outcome("failed", alert=alert)
    except OSError as exc:
        alert = {
            "kind": "lecture_stt_postprocess_failed",
            "failure": "icloud_unavailable",
            "retryable": True,
            "reason": type(exc).__name__,
            "raw_transcript_body_included": False,
        }
        return _run_once_outcome("failed", alert=alert)
    if not stat.S_ISDIR(lecture_root_stat.st_mode):
        alert = {
            "kind": "lecture_stt_postprocess_failed",
            "failure": "icloud_unavailable",
            "retryable": True,
            "reason": "non_directory",
            "raw_transcript_body_included": False,
        }
        return _run_once_outcome("failed", alert=alert)

    candidate = select_candidate(config, now=now)
    if candidate is None:
        return _run_once_outcome("no_candidate")

    candidate_path, _manifest_path = write_candidate_and_manifest(candidate)

    unsupported_final_outputs = unsupported_existing_final_outputs(candidate)
    if unsupported_final_outputs:
        status = _preexisting_final_artifact_status(candidate, unsupported_final_outputs)
        write_claim_status(
            candidate,
            "failed",
            config=config,
            now=now,
            failure="preexisting_final_artifact_unsupported_type",
            verification=status,
            retryable=False,
        )
        alert = {
            "kind": "lecture_stt_postprocess_failed",
            "stem": candidate.stem,
            "failure": "preexisting_final_artifact_unsupported_type",
            "promote_failure_class": status.get("promote_failure_class"),
            "retryable": False,
            "raw_transcript_body_included": False,
        }
        return _run_once_outcome("failed", stem=candidate.stem, alert=alert)

    if candidate.blocked_reasons:
        write_claim_status(
            candidate,
            "blocked",
            config=config,
            now=now,
            blocked_reasons=list(candidate.blocked_reasons),
        )
        alert = _blocked_alert(candidate)
        return _run_once_outcome("blocked", stem=candidate.stem, alert=alert)

    if not acquire_claim(candidate, config, now=now):
        return _run_once_outcome("claim_race_lost", stem=candidate.stem)

    raw_input_issues = _raw_input_preflight_issues(candidate)
    if raw_input_issues:
        return _pre_child_failed_outcome(
            candidate,
            config,
            now=now,
            failure_class="unsafe_raw_artifact",
            details={"artifacts": raw_input_issues},
            retryable=False,
        )

    prompt_issue = _prompt_preflight_issue(candidate)
    if prompt_issue is not None:
        failure_class, details = prompt_issue
        return _pre_child_failed_outcome(
            candidate,
            config,
            now=now,
            failure_class=failure_class,
            details=details,
            retryable=True,
        )

    expected_absent_final_paths = [path for path in _planned_final_output_paths(candidate) if not os.path.lexists(path)]
    final_snapshots = snapshot_existing_final_outputs(candidate)

    try:
        child_result = _normalize_child_result(child_runner(candidate, candidate_path, config))
    except subprocess.TimeoutExpired:
        quarantine_status = _post_child_unauthorized_final_status(candidate, expected_absent_final_paths, final_snapshots)
        extra: dict[str, Any] = {"verification": quarantine_status} if quarantine_status else {}
        write_claim_status(candidate, "failed", config=config, now=utc_now(), failure="model_timeout", retryable=True, **extra)
        alert = {
            "kind": "lecture_stt_postprocess_failed",
            "stem": candidate.stem,
            "failure": "model_timeout",
            "retryable": True,
            "raw_transcript_body_included": False,
        }
        if quarantine_status:
            alert["promote_failure_class"] = quarantine_status.get("promote_failure_class")
        return _run_once_outcome("failed", stem=candidate.stem, alert=alert)
    except Exception as exc:  # fail short, metadata only
        err_path = config.cron_log_dir / f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{candidate.stem}-wrapper-error.log"
        err_path.parent.mkdir(parents=True, exist_ok=True)
        err_path.write_text(traceback.format_exc(), encoding="utf-8")
        quarantine_status = _post_child_unauthorized_final_status(candidate, expected_absent_final_paths, final_snapshots)
        extra = {"verification": quarantine_status} if quarantine_status else {}
        failure_class = _exception_failure_class(exc)
        write_claim_status(
            candidate,
            "failed",
            config=config,
            now=utc_now(),
            failure=failure_class,
            retryable=False,
            log_path=str(err_path),
            **extra,
        )
        alert = {
            "kind": "lecture_stt_postprocess_failed",
            "stem": candidate.stem,
            "failure": failure_class,
            "retryable": False,
            "log_path": str(err_path),
            "raw_transcript_body_included": False,
        }
        if quarantine_status:
            alert["promote_failure_class"] = quarantine_status.get("promote_failure_class")
        return _run_once_outcome("failed", stem=candidate.stem, alert=alert)

    if child_result.exit_code != 0:
        quarantine_status = _post_child_unauthorized_final_status(candidate, expected_absent_final_paths, final_snapshots)
        extra = {"verification": quarantine_status} if quarantine_status else {}
        write_claim_status(
            candidate,
            "failed",
            config=config,
            now=utc_now(),
            failure="child_hermes_failed",
            exit_code=child_result.exit_code,
            retryable=False,
            log_path=str(child_result.log_path),
            **extra,
        )
        alert = {
            "kind": "lecture_stt_postprocess_failed",
            "stem": candidate.stem,
            "failure": "child_hermes_failed",
            "exit_code": child_result.exit_code,
            "retryable": False,
            "log_path": str(child_result.log_path),
            "raw_transcript_body_included": False,
        }
        if quarantine_status:
            alert["promote_failure_class"] = quarantine_status.get("promote_failure_class")
        return _run_once_outcome("failed", stem=candidate.stem, alert=alert)

    status = _post_child_unauthorized_final_status(candidate, expected_absent_final_paths, final_snapshots) or verify_success(candidate)
    if not all(
        [
            status["validation_correction_passed"],
            status["validation_summary_passed"],
            status["promote_passed"],
            status["finals_exist"],
        ]
    ):
        write_claim_status(
            candidate,
            "failed",
            config=config,
            now=utc_now(),
            failure="postprocess_verification_failed",
            verification=status,
            retryable=False,
            log_path=str(child_result.log_path),
        )
        alert = {
            "kind": "lecture_stt_postprocess_failed",
            "stem": candidate.stem,
            "failure": "postprocess_verification_failed",
            "promote_failure_class": status.get("promote_failure_class"),
            "retryable": False,
            "log_path": str(child_result.log_path),
            "raw_transcript_body_included": False,
        }
        return _run_once_outcome("failed", stem=candidate.stem, alert=alert)

    write_claim_status(
        candidate,
        "completed",
        config=config,
        now=utc_now(),
        completed_at=_format_ts(utc_now()),
        final_paths=status["final_paths"],
        log_path=str(child_result.log_path),
    )
    alert = {
        "kind": "lecture_stt_postprocess_complete",
        "stem": candidate.stem,
        "validation": {"correction": "pass", "summary": "pass", "promote": "pass"},
        "outputs": status["final_paths"],
        "raw_transcript_body_included": False,
    }
    return _run_once_outcome("completed", stem=candidate.stem, alert=alert)


def build_status(config: OperatorConfig, *, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    skip_stems = load_baseline_skip_stems(config)
    counts = {
        "pending": 0,
        "claimed": 0,
        "stale_claim": 0,
        "blocked": 0,
        "failed": 0,
        "completed_claim": 0,
        "complete": 0,
        "baseline_skipped": 0,
    }
    stems = _candidate_stems(config.lecture_root, stable_for_sec=0)
    for stem in stems:
        if stem in skip_stems:
            counts["baseline_skipped"] += 1
            continue
        paths = resolve_candidate_paths(stem, lecture_root=config.lecture_root, repo_root=config.repo_root)
        if is_complete(paths):
            counts["complete"] += 1
            continue
        if paths.claim_path.exists():
            if _is_stale_claim(paths.claim_path, now=now, stale_after_sec=config.stale_claim_after_sec):
                counts["stale_claim"] += 1
            else:
                claim = _claim_payload(paths.claim_path) or {}
                status = str(claim.get("status", "claimed"))
                if status == "blocked":
                    counts["blocked"] += 1
                elif status == "failed":
                    counts["failed"] += 1
                elif status == "completed":
                    counts["completed_claim"] += 1
                else:
                    counts["claimed"] += 1
            continue
        actions, blocked = build_action_plan(paths)
        if blocked or any(action.get("needed") for action in actions.values()):
            if blocked:
                counts["blocked"] += 1
            else:
                counts["pending"] += 1

    claims = _iter_claim_payloads(config)
    completed_claims = [payload for payload in claims if str(payload.get("status")) == "completed"]
    failed_claims = [payload for payload in claims if str(payload.get("status")) == "failed"]
    completed_claims.sort(key=lambda payload: _claim_updated_at(payload) or dt.datetime.min.replace(tzinfo=dt.UTC), reverse=True)
    failed_claims.sort(key=lambda payload: _claim_updated_at(payload) or dt.datetime.min.replace(tzinfo=dt.UTC), reverse=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "lecture_stt_postprocess_status",
        "generated_at": _format_ts(now),
        "repo_root": str(config.repo_root),
        "lecture_root": str(config.lecture_root),
        "candidate_counts": counts,
        "baseline_path": str(config.baseline_path),
        "cron_log_dir": str(config.cron_log_dir),
        "no_noise_mode": True,
        "routine_output_policy": "run-once prints nothing when there is no candidate",
        "last_completed": _metadata_claim_summary(completed_claims[0]) if completed_claims else None,
        "last_failed": _metadata_claim_summary(failed_claims[0]) if failed_claims else None,
        "raw_transcript_body_included": False,
    }


def _config_from_args(args: argparse.Namespace) -> OperatorConfig:
    return OperatorConfig(
        repo_root=Path(args.repo_root),
        lecture_root=Path(args.lecture_root),
        hermes_bin=Path(args.hermes_bin),
        stable_for_sec=args.stable_for_sec,
        stale_claim_after_sec=args.stale_claim_after_sec,
        child_timeout_sec=args.child_timeout_sec,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="lecture_stt Hermes postprocess operator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("run-once", "Select and process at most one candidate"),
        ("status", "Print metadata-only operator status"),
    ):
        sub = subparsers.add_parser(command, help=help_text)
        sub.add_argument("--repo-root", default=str(Path.cwd()))
        sub.add_argument("--lecture-root", required=True)
        sub.add_argument("--hermes-bin", default=str(Path.home() / ".local" / "bin" / "hermes"))
        sub.add_argument("--stable-for-sec", type=int, default=60)
        sub.add_argument("--stale-claim-after-sec", type=int, default=60 * 60 * 2)
        sub.add_argument("--child-timeout-sec", type=int, default=60 * 45)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config_from_args(args)
    if args.command == "status":
        sys.stdout.write(_json_dump(build_status(config)))
        return 0
    if args.command == "run-once":
        outcome = run_once(config)
        alert = outcome.get("alert")
        if isinstance(alert, dict):
            sys.stdout.write(json.dumps(alert, ensure_ascii=False, sort_keys=True) + "\n")
        return 0
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
