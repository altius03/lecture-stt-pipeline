from __future__ import annotations

import argparse
import json
import math
import stat
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO, Sequence, TextIO

from dotenv import load_dotenv

from lecture_stt.shared import utils
from lecture_stt.shared.paths import env_file
from lecture_stt.stt.main import _normalize_config_paths, load_config
from lecture_stt.stt.watcher import PollingWatcher

CONFIG_SCHEMA_VERSION = "lecture-stt/shadow-config@1"
SCAN_SCHEMA_VERSION = "lecture-stt/shadow-scan@1"
STAT_SCAN_SCHEMA_VERSION = "lecture-stt/controller-stat-scan@1"
LOCKSTEP_SCAN_REQUEST_SCHEMA_VERSION = "lecture-stt/shadow-scan-request@1"
LOCKSTEP_SCAN_RESULT_SCHEMA_VERSION = "lecture-stt/shadow-scan-result@1"
LOCKSTEP_MAX_LINE_BYTES = 64 * 1024
LOCKSTEP_REQUEST_KEYS = ("schema_version", "scan_index")
LOCKSTEP_RESULT_KEYS = ("schema_version", "scan_index", "stable_relative_paths")
STAT_SCAN_ENTRY_KEYS = ("name", "size_bytes", "mtime")


class ShadowProbeError(ValueError):
    """Closed validation error for the read-only shadow probe."""


def _reject_lockstep_protocol() -> ShadowProbeError:
    return ShadowProbeError("invalid lockstep protocol")


def _parse_scan_count(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("scan-count must be an integer") from exc
    if parsed <= 0 or parsed > 1000:
        raise argparse.ArgumentTypeError("scan-count must be between 1 and 1000")
    return parsed


def _parse_sleep_sec(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sleep-sec must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0 or parsed > 3600:
        raise argparse.ArgumentTypeError("sleep-sec must be between 0 and 3600")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only PollingWatcher shadow probe")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("config", "scan", "stat-scan", "lockstep-scan"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument(
            "--config",
            default="config/config.yaml",
            help="Path to config.yaml",
        )
        subparser.add_argument(
            "--watch-folder",
            dest="watch_folder",
            help="Override paths.watch_folder in shadow mode",
        )

    scan_parser = subparsers.choices["scan"]
    scan_parser.add_argument(
        "--scan-count",
        type=_parse_scan_count,
        required=True,
        help="Number of bounded PollingWatcher scans to execute",
    )
    scan_parser.add_argument(
        "--sleep-sec",
        type=_parse_sleep_sec,
        help="Sleep interval between scans; defaults to app.polling_interval_sec",
    )
    lockstep_parser = subparsers.choices["lockstep-scan"]
    lockstep_parser.add_argument(
        "--scan-count",
        type=_parse_scan_count,
        required=True,
        help="Number of lockstep scan requests to consume from stdin",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def load_shadow_config(
    config_path: str,
    *,
    watch_folder_override: str | None = None,
) -> dict[str, Any]:
    config = _normalize_config_paths(load_config(config_path))
    if watch_folder_override:
        config.setdefault("paths", {})["watch_folder"] = str(
            Path(watch_folder_override).expanduser().resolve()
        )

    app = config.get("app")
    if not isinstance(app, dict):
        raise ShadowProbeError("invalid config: missing app section")
    paths = config.get("paths")
    if not isinstance(paths, dict):
        raise ShadowProbeError("invalid config: missing paths section")

    try:
        stable_for_sec = int(app["stable_for_sec"])
    except Exception as exc:
        raise ShadowProbeError("invalid config: app.stable_for_sec must be an integer") from exc
    try:
        polling_interval_sec = int(app["polling_interval_sec"])
    except Exception as exc:
        raise ShadowProbeError("invalid config: app.polling_interval_sec must be an integer") from exc

    if stable_for_sec < 0:
        raise ShadowProbeError("invalid config: app.stable_for_sec must be greater than or equal to 0")
    if polling_interval_sec <= 0:
        raise ShadowProbeError("invalid config: app.polling_interval_sec must be greater than 0")

    watch_folder_raw = paths.get("watch_folder")
    if not isinstance(watch_folder_raw, str) or not watch_folder_raw.strip():
        raise ShadowProbeError("invalid config: paths.watch_folder must be a non-empty string")

    watch_folder = Path(watch_folder_raw)
    if not watch_folder.exists():
        raise ShadowProbeError(f"invalid config: watch_folder does not exist: {watch_folder}")
    if not watch_folder.is_dir():
        raise ShadowProbeError(f"invalid config: watch_folder is not a directory: {watch_folder}")

    return {
        "watch_folder": str(watch_folder),
        "stable_for_sec": stable_for_sec,
        "polling_interval_sec": polling_interval_sec,
    }


def shadow_config_payload(config_path: str, *, watch_folder_override: str | None = None) -> dict[str, Any]:
    resolved = load_shadow_config(config_path, watch_folder_override=watch_folder_override)
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        **resolved,
    }


def _build_watcher(resolved: dict[str, Any]) -> PollingWatcher:
    return PollingWatcher(
        watch_folder=resolved["watch_folder"],
        stable_for_sec=int(resolved["stable_for_sec"]),
        polling_interval_sec=int(resolved["polling_interval_sec"]),
    )


def _stable_relative_paths(stable_files: Sequence[Path]) -> list[str]:
    return [path.name for path in stable_files]


def scan_shadow(
    config_path: str,
    *,
    watch_folder_override: str | None = None,
    scan_count: int,
    sleep_sec: float | None,
) -> dict[str, Any]:
    resolved = load_shadow_config(config_path, watch_folder_override=watch_folder_override)
    effective_sleep = float(resolved["polling_interval_sec"] if sleep_sec is None else sleep_sec)
    watcher = _build_watcher(resolved)

    scans: list[dict[str, Any]] = []
    for index in range(scan_count):
        stable_files = watcher.scan_stable_files()
        scans.append(
            {
                "scan_index": index + 1,
                "stable_relative_paths": _stable_relative_paths(stable_files),
            }
        )
        if index + 1 < scan_count and effective_sleep > 0:
            time.sleep(effective_sleep)

    return {
        "schema_version": SCAN_SCHEMA_VERSION,
        "stable_for_sec": resolved["stable_for_sec"],
        "polling_interval_sec": resolved["polling_interval_sec"],
        "scan_count": scan_count,
        "sleep_sec": effective_sleep,
        "scans": scans,
    }


def stat_scan_shadow(
    config_path: str,
    *,
    watch_folder_override: str | None = None,
) -> dict[str, Any]:
    resolved = load_shadow_config(config_path, watch_folder_override=watch_folder_override)
    watch_folder = Path(resolved["watch_folder"])
    entries: list[dict[str, Any]] = []

    try:
        children = list(watch_folder.iterdir())
    except OSError as exc:
        raise ShadowProbeError("unable to read watch_folder entries") from exc

    for child in sorted(children, key=lambda path: path.name):
        if utils.is_temporary_file(child):
            continue
        try:
            observed = child.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(observed.st_mode):
            continue
        if child.name in {"", ".", ".."} or child.name != Path(child.name).name:
            raise ShadowProbeError("watch_folder contains an invalid direct child name")
        entries.append(
            {
                "name": child.name,
                "size_bytes": int(observed.st_size),
                "mtime": float(observed.st_mtime),
            }
        )

    return {
        "schema_version": STAT_SCAN_SCHEMA_VERSION,
        "entries": entries,
    }


def _reject_duplicate_json_object_pairs(pairs: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise _reject_lockstep_protocol()
        seen.add(key)
    return pairs


def _decode_lockstep_request(line: bytes, *, expected_scan_index: int) -> None:
    if not line or len(line) > LOCKSTEP_MAX_LINE_BYTES or not line.endswith(b"\n"):
        raise _reject_lockstep_protocol()
    try:
        decoded = line[:-1].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _reject_lockstep_protocol() from exc
    try:
        payload = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_object_pairs)
    except (ValueError, json.JSONDecodeError) as exc:
        raise _reject_lockstep_protocol() from exc
    if not isinstance(payload, list):
        raise _reject_lockstep_protocol()
    keys = [key for key, _ in payload]
    if keys != list(LOCKSTEP_REQUEST_KEYS):
        raise _reject_lockstep_protocol()
    values = dict(payload)
    schema_version = values.get("schema_version")
    scan_index = values.get("scan_index")
    if schema_version != LOCKSTEP_SCAN_REQUEST_SCHEMA_VERSION:
        raise _reject_lockstep_protocol()
    if isinstance(scan_index, bool) or not isinstance(scan_index, int):
        raise _reject_lockstep_protocol()
    if scan_index != expected_scan_index:
        raise _reject_lockstep_protocol()


def _encode_lockstep_result(scan_index: int, stable_relative_paths: Sequence[str]) -> bytes:
    if scan_index <= 0:
        raise _reject_lockstep_protocol()
    encoded = json.dumps(
        {
            "schema_version": LOCKSTEP_SCAN_RESULT_SCHEMA_VERSION,
            "scan_index": scan_index,
            "stable_relative_paths": list(stable_relative_paths),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) + 1 > LOCKSTEP_MAX_LINE_BYTES:
        raise _reject_lockstep_protocol()
    return encoded + b"\n"


def _resolve_binary_reader(stream: BinaryIO | TextIO | None) -> BinaryIO:
    resolved = stream if stream is not None else sys.stdin
    return resolved.buffer if hasattr(resolved, "buffer") else resolved  # type: ignore[return-value]


def _resolve_binary_writer(stream: BinaryIO | TextIO | None) -> BinaryIO | TextIO:
    resolved = stream if stream is not None else sys.stdout
    return resolved.buffer if hasattr(resolved, "buffer") else resolved


def _write_lockstep_line(stream: BinaryIO | TextIO, payload: bytes) -> None:
    if hasattr(stream, "buffer"):
        stream = stream.buffer  # type: ignore[assignment]
    if isinstance(payload, bytes) and hasattr(stream, "write"):
        try:
            stream.write(payload)  # type: ignore[arg-type]
        except TypeError:
            stream.write(payload.decode("utf-8"))  # type: ignore[arg-type]
    if hasattr(stream, "flush"):
        stream.flush()


def run_lockstep_scan_stream(
    config_path: str,
    *,
    watch_folder_override: str | None = None,
    scan_count: int,
    stdin: BinaryIO,
    stdout: BinaryIO | TextIO,
) -> None:
    resolved = load_shadow_config(config_path, watch_folder_override=watch_folder_override)
    watcher = _build_watcher(resolved)
    for scan_index in range(1, scan_count + 1):
        line = stdin.readline(LOCKSTEP_MAX_LINE_BYTES + 1)
        if not isinstance(line, bytes):
            raise _reject_lockstep_protocol()
        _decode_lockstep_request(line, expected_scan_index=scan_index)
        response = _encode_lockstep_result(scan_index, _stable_relative_paths(watcher.scan_stable_files()))
        _write_lockstep_line(stdout, response)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: BinaryIO | TextIO | None = None,
    stdout: BinaryIO | TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    load_dotenv(str(env_file()), override=False)
    args = parse_args(argv)
    stderr_stream = stderr if stderr is not None else sys.stderr
    stdout_stream = stdout if stdout is not None else sys.stdout
    try:
        if args.command == "config":
            payload = shadow_config_payload(
                args.config,
                watch_folder_override=args.watch_folder,
            )
        elif args.command == "scan":
            payload = scan_shadow(
                args.config,
                watch_folder_override=args.watch_folder,
                scan_count=args.scan_count,
                sleep_sec=args.sleep_sec,
            )
        elif args.command == "stat-scan":
            payload = stat_scan_shadow(
                args.config,
                watch_folder_override=args.watch_folder,
            )
        else:
            run_lockstep_scan_stream(
                args.config,
                watch_folder_override=args.watch_folder,
                scan_count=args.scan_count,
                stdin=_resolve_binary_reader(stdin),
                stdout=_resolve_binary_writer(stdout_stream),
            )
            return 0
    except ShadowProbeError as exc:
        print(f"Shadow probe rejected: {exc}", file=stderr_stream)
        return 2

    print(json.dumps(payload, ensure_ascii=False), file=stdout_stream)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
