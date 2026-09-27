from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Sequence

import yaml

from lecture_stt.shared.paths import env_file, repo_root, resolve_config_path, runtime_env
from lecture_stt.downstream.semester import inspect_unqueued_done_since, load_active_semester


PROBLEM_STATUSES = {"UNROUTED", "CONFLICT", "NEEDS_REVIEW", "ERROR"}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect Codex transcript postprocess status")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--db-path")
    parser.add_argument("--active-path")
    subparsers = parser.add_subparsers(dest="command")

    summary = subparsers.add_parser("summary", help="Show aggregate queue status")
    summary.add_argument("--limit", type=int, default=10)

    listing = subparsers.add_parser("list", help="List transcript postprocess rows")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--status")
    listing.add_argument("--only-problems", action="store_true")

    show = subparsers.add_parser("show", help="Show one transcript postprocess row")
    show.add_argument("source_job_id", type=int)
    return parser.parse_args(argv)


def resolve_db_path(args: argparse.Namespace) -> Path:
    if args.db_path:
        return Path(args.db_path).expanduser()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = repo_root() / config_path
    with config_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("paths"), dict):
        raise ValueError(f"Invalid config paths section: {config_path}")
    raw_db_path = payload["paths"].get("db_path")
    if not isinstance(raw_db_path, str) or not raw_db_path.strip():
        raise ValueError(f"Missing paths.db_path: {config_path}")
    env = runtime_env(dotenv_path=env_file())
    return resolve_config_path(raw_db_path, base_dir=repo_root(), env=env)


def resolve_active_path(args: argparse.Namespace) -> Path:
    if args.active_path:
        return Path(args.active_path).expanduser()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = repo_root() / config_path
    with config_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    delivery = payload.get("transcript_delivery") if isinstance(payload, dict) else None
    if not isinstance(delivery, dict):
        raise ValueError(f"Invalid config transcript_delivery section: {config_path}")
    raw_active_path = delivery.get("active_semester_manifest")
    if not isinstance(raw_active_path, str) or not raw_active_path.strip():
        raise ValueError(f"Missing transcript_delivery.active_semester_manifest: {config_path}")
    env = runtime_env(dotenv_path=env_file())
    return resolve_config_path(raw_active_path, base_dir=repo_root(), env=env)


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(f"Delivery DB does not exist: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transcript_postprocess_jobs'"
        ).fetchone()
        is not None
    )


def _truncate(value: object, width: int = 48) -> str:
    text = "" if value is None else str(value)
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def _print_rows(rows: list[sqlite3.Row]) -> None:
    if not rows:
        print("No matching transcript postprocess jobs.")
        return
    print("job_id\tstatus\tcorrection\tsummary\tdelivery\tcourse\tstem\tupdated_at\terror")
    for row in rows:
        print(
            "\t".join(
                [
                    str(row["source_job_id"]),
                    str(row["status"]),
                    _truncate(row["correction_status"], 16),
                    _truncate(row["summary_status"], 16),
                    _truncate(row["delivery_status"], 16),
                    _truncate(row["course_name"] or row["course_code"] or "-", 24),
                    _truncate(row["logical_stem"], 32),
                    _truncate(row["updated_at"], 32),
                    _truncate(row["last_error_code"], 24),
                ]
            )
        )


def _rows(
    conn: sqlite3.Connection,
    *,
    limit: int,
    status: str | None = None,
    only_problems: bool = False,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("status = ?")
        params.append(status.upper())
    if only_problems:
        placeholders = ",".join("?" for _ in PROBLEM_STATUSES)
        clauses.append(f"status IN ({placeholders})")
        params.extend(sorted(PROBLEM_STATUSES))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        "SELECT * FROM transcript_postprocess_jobs "
        f"{where} ORDER BY updated_at DESC, source_job_id DESC LIMIT ?",
        (*params, max(1, min(int(limit), 1000))),
    ).fetchall()


def _count(conn: sqlite3.Connection, query: str, params: tuple[object, ...] = ()) -> int:
    return int(conn.execute(query, params).fetchone()[0])


def print_summary(
    conn: sqlite3.Connection,
    *,
    limit: int,
    active_path: Path,
) -> int:
    active = load_active_semester(active_path)
    total = int(conn.execute("SELECT COUNT(*) FROM transcript_postprocess_jobs").fetchone()[0])
    active_stt = _count(
        conn,
        "SELECT COUNT(*) FROM jobs WHERE status IN ('PENDING', 'PROCESSING')",
    )
    stt_problems = _count(
        conn,
        "SELECT COUNT(*) FROM jobs WHERE status IN ('NEEDS_REVIEW', 'ERROR')",
    )
    pending_postprocess = _count(
        conn,
        "SELECT COUNT(*) FROM transcript_postprocess_jobs WHERE status = 'PENDING'",
    )
    problem_placeholders = ",".join("?" for _ in PROBLEM_STATUSES)
    postprocess_problems = _count(
        conn,
        "SELECT COUNT(*) FROM transcript_postprocess_jobs "
        f"WHERE status IN ({problem_placeholders})",
        tuple(sorted(PROBLEM_STATUSES)),
    )
    unqueued_inspection = inspect_unqueued_done_since(conn, active.activated_at)
    unqueued_done = unqueued_inspection["unqueued_done_jobs"]
    invalid_unqueued_done = unqueued_inspection["invalid_unqueued_done_jobs"]
    if stt_problems or postprocess_problems or unqueued_done or invalid_unqueued_done:
        overall = "ATTENTION"
    elif active_stt or pending_postprocess:
        overall = "BUSY"
    else:
        overall = "HEALTHY"

    print("Transcript postprocess summary")
    print(f"overall_status: {overall}")
    print(f"active_semester: {active.semester}")
    print(f"activated_at: {active.activated_at}")
    print(f"effective_cutoff: {active.activated_at}")
    print(f"course_count: {active.expected_course_count}")
    print(f"active_stt_jobs: {active_stt}")
    print(f"stt_problem_jobs: {stt_problems}")
    print(f"pending_postprocess_jobs: {pending_postprocess}")
    print(f"unqueued_done_jobs: {unqueued_done}")
    print(f"invalid_unqueued_done_jobs: {invalid_unqueued_done}")
    print(f"postprocess_problem_jobs: {postprocess_problems}")
    print(f"total_rows: {total}")
    for row in conn.execute(
        "SELECT status, COUNT(*) AS count FROM transcript_postprocess_jobs "
        "GROUP BY status ORDER BY status"
    ).fetchall():
        print(f"- {row['status']}: {row['count']}")
    problems = _rows(conn, limit=limit, only_problems=True)
    if problems:
        print("")
        print(f"Recent problems (limit={max(1, limit)})")
        _print_rows(problems)
    return 0


def show_row(conn: sqlite3.Connection, source_job_id: int) -> int:
    row = conn.execute(
        "SELECT * FROM transcript_postprocess_jobs WHERE source_job_id = ?",
        (source_job_id,),
    ).fetchone()
    if row is None:
        print(f"Transcript postprocess job not found: job {source_job_id}")
        return 1
    for key in row.keys():
        print(f"{key}: {row[key]}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = resolve_db_path(args)
    with connect_read_only(db_path) as conn:
        if not _table_exists(conn):
            print("Transcript postprocess queue is not initialized.")
            return 1
        command = args.command or "summary"
        if command == "summary":
            return print_summary(
                conn,
                limit=int(getattr(args, "limit", 10)),
                active_path=resolve_active_path(args),
            )
        if command == "list":
            _print_rows(
                _rows(
                    conn,
                    limit=args.limit,
                    status=args.status,
                    only_problems=args.only_problems,
                )
            )
            return 0
        if command == "show":
            return show_row(conn, args.source_job_id)
    raise AssertionError(f"Unsupported command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
