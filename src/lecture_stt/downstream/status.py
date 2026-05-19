from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable, Sequence

from lecture_stt.shared import db
from lecture_stt.shared.paths import default_db_path


DEFAULT_DB_PATH = default_db_path()
PROBLEM_CORRECTION_STATUSES = {"INCOMPLETE", "CONFLICT", "ERROR"}
PROBLEM_SUMMARY_STATUSES = {"BLOCKED", "CONFLICT", "ERROR"}
ROUTE_SUFFIX_PATTERN = re.compile(r"^(?P<stem>\d{6}[A-Za-z]+(?:_\d+)?)_{1,2}\d{8}_\d{6}_{1,2}[0-9A-Fa-f]{6,}$")
SOURCE_DESTINATION_PAIRS = (
    ("correction_txt", "correction_txt_path", "tuk_origin_txt_path"),
    ("correction_json", "correction_json_path", "tuk_origin_json_path"),
    ("summary_tuk", "summary_md_path", "tuk_summary_path"),
    ("summary_obsidian", "summary_md_path", "obsidian_summary_path"),
)


def _load_worker_config_from_module(config_path: str):
    worker = importlib.import_module("lecture_stt.downstream.worker")
    return worker.load_worker_config(config_path)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect downstream delivery status")
    parser.add_argument("--config", default="config/config.yaml", help="Path to config.yaml")
    parser.add_argument("--db-path", help="Override deliveries DB path")

    subparsers = parser.add_subparsers(dest="command")

    summary_parser = subparsers.add_parser("summary", help="Show aggregate delivery status")
    summary_parser.add_argument("--limit", type=int, default=10, help="Number of recent problem rows to show")

    list_parser = subparsers.add_parser("list", help="List delivery rows")
    list_parser.add_argument("--limit", type=int, default=20, help="Maximum rows to print")
    list_parser.add_argument("--subject", help="Filter by subject abbreviation")
    list_parser.add_argument("--correction-status", help="Filter by correction status")
    list_parser.add_argument("--summary-status", help="Filter by summary status")
    list_parser.add_argument("--only-problems", action="store_true", help="Show only blocked/conflict/error rows")

    show_parser = subparsers.add_parser("show", help="Show one delivery row")
    show_parser.add_argument("logical_stem", help="Logical stem to inspect")

    clear_parser = subparsers.add_parser("clear", help="Delete one delivery row from the DB")
    clear_parser.add_argument("logical_stem", help="Logical stem to delete from deliveries")
    clear_parser.add_argument("--dry-run", action="store_true", help="Show the target row without deleting it")
    clear_parser.add_argument("--yes", action="store_true", help="Actually delete the row")

    diagnose_parser = subparsers.add_parser("diagnose", help="Read-only classify problem rows and current path/hash state")
    diagnose_parser.add_argument("--limit", type=int, default=100, help="Maximum rows to inspect")
    diagnose_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    diagnose_parser.add_argument("--report-path", help="Optional JSON report path to write. Parent directories are created.")
    diagnose_parser.add_argument(
        "--all",
        action="store_true",
        help="Inspect all recent rows, not only problem rows",
    )

    clear_stale_parser = subparsers.add_parser(
        "clear-stale",
        help="Safely clear a source-missing stale delivery row after dry-run review and DB backup",
    )
    clear_stale_parser.add_argument("logical_stem", help="Source-missing logical stem to clear")
    clear_stale_parser.add_argument("--dry-run", action="store_true", help="Show before/after plan without mutation")
    clear_stale_parser.add_argument("--yes", action="store_true", help="Actually clear after writing DB backup")
    clear_stale_parser.add_argument("--backup-path", help="SQLite backup path required with --yes")

    return parser.parse_args(argv)


def resolve_db_path(args: argparse.Namespace) -> Path:
    if args.db_path:
        return Path(args.db_path).expanduser()
    try:
        return _load_worker_config_from_module(args.config).db_path
    except FileNotFoundError:
        return DEFAULT_DB_PATH


def fetch_status_counts(conn: sqlite3.Connection, column: str) -> list[tuple[str, int]]:
    rows = conn.execute(
        f"SELECT {column}, COUNT(*) AS cnt FROM deliveries GROUP BY {column} ORDER BY cnt DESC, {column} ASC"
    ).fetchall()
    return [(str(row[0]), int(row[1])) for row in rows]


def fetch_problem_reason_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT last_error_code, COUNT(*) AS cnt FROM deliveries WHERE "
        "(correction_status IN ('INCOMPLETE', 'CONFLICT', 'ERROR') "
        "OR summary_status IN ('BLOCKED', 'CONFLICT', 'ERROR') "
        "OR last_error_code IS NOT NULL) "
        "AND last_error_code IS NOT NULL "
        "GROUP BY last_error_code ORDER BY cnt DESC, last_error_code ASC"
    ).fetchall()
    return [(str(row[0]), int(row[1])) for row in rows]


def fetch_deliveries(
    conn: sqlite3.Connection,
    *,
    limit: int,
    subject: str | None = None,
    correction_status: str | None = None,
    summary_status: str | None = None,
    only_problems: bool = False,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []

    if subject:
        clauses.append("subject_abbr = ?")
        params.append(subject)
    if correction_status:
        clauses.append("correction_status = ?")
        params.append(correction_status)
    if summary_status:
        clauses.append("summary_status = ?")
        params.append(summary_status)
    if only_problems:
        clauses.append(
            "("
            "correction_status IN ('INCOMPLETE', 'CONFLICT', 'ERROR') "
            "OR summary_status IN ('BLOCKED', 'CONFLICT', 'ERROR') "
            "OR last_error_code IS NOT NULL"
            ")"
        )

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        "SELECT logical_stem, subject_abbr, correction_status, summary_status, "
        "tuk_origin_done, tuk_summary_done, obsidian_done, last_error_code, updated_at "
        f"FROM deliveries {where} "
        "ORDER BY updated_at DESC, logical_stem DESC LIMIT ?",
        (*params, max(limit, 1)),
    ).fetchall()
    return rows


def fetch_delivery_detail(conn: sqlite3.Connection, logical_stem: str) -> sqlite3.Row | None:
    return db.get_delivery(conn, logical_stem)


def fetch_diagnostic_rows(
    conn: sqlite3.Connection,
    *,
    limit: int,
    only_problems: bool = True,
) -> list[sqlite3.Row]:
    where = ""
    if only_problems:
        where = (
            "WHERE correction_status IN ('INCOMPLETE', 'CONFLICT', 'ERROR') "
            "OR summary_status IN ('BLOCKED', 'CONFLICT', 'ERROR') "
            "OR last_error_code IS NOT NULL"
        )
    return conn.execute(
        f"SELECT * FROM deliveries {where} ORDER BY updated_at DESC, logical_stem DESC LIMIT ?",
        (max(limit, 1),),
    ).fetchall()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_info(raw_path: object) -> dict[str, object]:
    if raw_path is None or str(raw_path).strip() == "":
        return {"path": None, "exists": False, "sha256": None}
    path = Path(str(raw_path))
    exists = path.is_file()
    return {
        "path": str(path),
        "exists": exists,
        "sha256": _sha256(path) if exists else None,
    }


def _propose_stem(logical_stem: str) -> str | None:
    match = ROUTE_SUFFIX_PATTERN.match(logical_stem)
    if match:
        return match.group("stem")
    return None


def _has_conflict_status(row: sqlite3.Row) -> bool:
    return row["correction_status"] == "CONFLICT" or row["summary_status"] == "CONFLICT"


def diagnose_delivery_row(row: sqlite3.Row) -> dict[str, object]:
    path_pairs: dict[str, dict[str, object]] = {}
    different_destinations: list[str] = []
    same_destinations: list[str] = []
    missing_destinations: list[str] = []
    source_seen = False
    source_exists = False
    destination_seen = False
    destination_exists = False

    for label, source_column, destination_column in SOURCE_DESTINATION_PAIRS:
        source = _path_info(row[source_column])
        destination = _path_info(row[destination_column])
        if source["path"] is not None:
            source_seen = True
        if destination["path"] is not None:
            destination_seen = True
        if source["exists"]:
            source_exists = True
        if destination["exists"]:
            destination_exists = True
        if source["path"] is not None or destination["path"] is not None:
            path_pairs[label] = {"source": source, "destination": destination}
        if source["exists"] and destination["exists"]:
            if source["sha256"] == destination["sha256"]:
                same_destinations.append(label)
            else:
                different_destinations.append(label)
        elif source["exists"] and destination["path"] is not None and not destination["exists"]:
            missing_destinations.append(label)

    last_error_code = row["last_error_code"]
    proposed_stem = _propose_stem(row["logical_stem"])
    classification = "manual-review"
    recommended_action = "review row manually; no automatic mutation"

    if last_error_code in {"INVALID_STEM", "UNKNOWN_SUBJECT"}:
        classification = "route/rename-needed"
        recommended_action = "add to manual table before route change, rename, or exclusion"
    elif _has_conflict_status(row):
        if not source_exists:
            classification = "source-missing"
            if row["logical_stem"] == "260422LC":
                recommended_action = "document only; 260422LC is excluded from automatic stale row clear"
            else:
                recommended_action = "DB-only/stale candidate; dry-run table, DB backup, before/after row list, then explicit approval"
        elif different_destinations:
            classification = "hash-conflict"
            recommended_action = "manual compare required; source 03_correction is canonical but destination replacement needs backup/dry-run/approval"
        elif missing_destinations:
            classification = "dest-missing"
            recommended_action = "rerun downstream dry-run before delivery; no overwrite needed for missing destinations"
        elif same_destinations and source_exists and destination_exists:
            classification = "same-content-now"
            recommended_action = "candidate for idempotent delivered/stale DB repair after dry-run and approval"
    elif not source_seen and not destination_seen and last_error_code:
        classification = "source-missing"
        recommended_action = "DB-only/stale candidate; inspect before clearing"

    return {
        "logical_stem": row["logical_stem"],
        "subject_abbr": row["subject_abbr"],
        "correction_status": row["correction_status"],
        "summary_status": row["summary_status"],
        "last_error_code": last_error_code,
        "classification": classification,
        "recommended_action": recommended_action,
        "proposed_stem": proposed_stem,
        "path_pairs": path_pairs,
        "same_destinations": same_destinations,
        "different_destinations": different_destinations,
        "missing_destinations": missing_destinations,
    }


def diagnose_rows(conn: sqlite3.Connection, *, limit: int, only_problems: bool = True) -> list[dict[str, object]]:
    return [diagnose_delivery_row(row) for row in fetch_diagnostic_rows(conn, limit=limit, only_problems=only_problems)]


def delete_delivery(conn: sqlite3.Connection, logical_stem: str) -> int:
    cursor = conn.execute("DELETE FROM deliveries WHERE logical_stem = ?", (logical_stem,))
    conn.commit()
    return int(cursor.rowcount)


def total_delivery_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0])


def backup_sqlite_db(conn: sqlite3.Connection, backup_path: Path) -> None:
    if backup_path.exists():
        raise FileExistsError(f"Backup path already exists: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(backup_path) as backup_conn:
        conn.backup(backup_conn)


def build_diagnostic_payload(conn: sqlite3.Connection, *, limit: int, only_problems: bool) -> dict[str, object]:
    rows = diagnose_rows(conn, limit=limit, only_problems=only_problems)
    return {
        "dry_run": True,
        "row_count": len(rows),
        "rows": rows,
    }


def print_summary(conn: sqlite3.Connection, *, limit: int) -> int:
    total = int(conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0])
    problem_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM deliveries WHERE "
            "correction_status IN ('INCOMPLETE', 'CONFLICT', 'ERROR') "
            "OR summary_status IN ('BLOCKED', 'CONFLICT', 'ERROR') "
            "OR last_error_code IS NOT NULL"
        ).fetchone()[0]
    )

    print("Deliveries summary")
    print(f"total_rows: {total}")
    print(f"problem_rows: {problem_count}")
    print("")
    print("Correction status counts")
    for status, count in fetch_status_counts(conn, "correction_status"):
        print(f"- {status}: {count}")
    print("")
    print("Summary status counts")
    for status, count in fetch_status_counts(conn, "summary_status"):
        print(f"- {status}: {count}")

    reason_counts = fetch_problem_reason_counts(conn)
    if reason_counts:
        print("")
        print("Problem reason counts")
        for reason, count in reason_counts:
            print(f"- {reason}: {count}")

    rows = fetch_deliveries(conn, limit=limit, only_problems=True)
    if rows:
        print("")
        print(f"Recent problem rows (limit={limit})")
        print_rows(rows)
    return 0


def _row_flags(row: sqlite3.Row) -> str:
    return f"{int(row['tuk_origin_done'])}/{int(row['tuk_summary_done'])}/{int(row['obsidian_done'])}"


def _truncate(value: object, width: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    if value is None:
        return []
    return [str(value)]


def print_rows(rows: Iterable[sqlite3.Row]) -> None:
    headers = ("logical_stem", "subject", "correction", "summary", "flags", "error", "updated_at")
    widths = {
        "logical_stem": 16,
        "subject": 7,
        "correction": 12,
        "summary": 10,
        "flags": 7,
        "error": 22,
        "updated_at": 25,
    }
    print("  ".join(header.ljust(widths[header]) for header in headers))
    print("  ".join("-" * widths[header] for header in headers))
    for row in rows:
        values = {
            "logical_stem": row["logical_stem"],
            "subject": row["subject_abbr"],
            "correction": row["correction_status"],
            "summary": row["summary_status"],
            "flags": _row_flags(row),
            "error": row["last_error_code"] or "",
            "updated_at": row["updated_at"] or "",
        }
        print("  ".join(_truncate(values[header], widths[header]).ljust(widths[header]) for header in headers))


def print_list(
    conn: sqlite3.Connection,
    *,
    limit: int,
    subject: str | None,
    correction_status: str | None,
    summary_status: str | None,
    only_problems: bool,
) -> int:
    rows = fetch_deliveries(
        conn,
        limit=limit,
        subject=subject,
        correction_status=correction_status,
        summary_status=summary_status,
        only_problems=only_problems,
    )
    if not rows:
        print("No matching deliveries.")
        return 0
    print_rows(rows)
    return 0


def print_show(conn: sqlite3.Connection, logical_stem: str) -> int:
    row = fetch_delivery_detail(conn, logical_stem)
    if row is None:
        print(f"Delivery not found: {logical_stem}", file=sys.stderr)
        return 1
    payload = {key: row[key] for key in row.keys()}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def print_clear(conn: sqlite3.Connection, logical_stem: str, *, dry_run: bool, confirmed: bool) -> int:
    row = fetch_delivery_detail(conn, logical_stem)
    if row is None:
        print(f"Delivery not found: {logical_stem}", file=sys.stderr)
        return 1

    payload = {key: row[key] for key in row.keys()}
    if dry_run:
        print(f"Would delete delivery row: {logical_stem}")
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if not confirmed:
        print("Refusing to delete without --yes. Use --dry-run to inspect first.", file=sys.stderr)
        return 2

    deleted = delete_delivery(conn, logical_stem)
    if deleted != 1:
        print(f"Delivery not found: {logical_stem}", file=sys.stderr)
        return 1
    print(f"Deleted delivery row: {logical_stem}")
    return 0


def print_diagnose(
    conn: sqlite3.Connection,
    *,
    limit: int,
    emit_json: bool,
    only_problems: bool,
    report_path: str | None = None,
) -> int:
    payload = build_diagnostic_payload(conn, limit=limit, only_problems=only_problems)
    rows_obj = payload["rows"]
    rows = rows_obj if isinstance(rows_obj, list) else []
    if report_path:
        path = Path(report_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if emit_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print("Downstream diagnostic dry-run")
    print(f"row_count: {payload['row_count']}")
    print("No files or DB rows were modified.")
    if report_path:
        print(f"report_path: {Path(report_path).expanduser()}")
    if not rows:
        return 0
    headers = ("logical_stem", "classification", "different", "missing", "action")
    widths = {
        "logical_stem": 28,
        "classification": 20,
        "different": 24,
        "missing": 24,
        "action": 60,
    }
    print("  ".join(header.ljust(widths[header]) for header in headers))
    print("  ".join("-" * widths[header] for header in headers))
    for row in rows:
        if not isinstance(row, dict):
            continue
        values = {
            "logical_stem": row["logical_stem"],
            "classification": row["classification"],
            "different": ",".join(_string_list(row["different_destinations"])),
            "missing": ",".join(_string_list(row["missing_destinations"])),
            "action": row["recommended_action"],
        }
        print("  ".join(_truncate(values[header], widths[header]).ljust(widths[header]) for header in headers))
    return 0


def print_clear_stale(
    conn: sqlite3.Connection,
    logical_stem: str,
    *,
    dry_run: bool,
    confirmed: bool,
    backup_path: str | None,
) -> int:
    row = fetch_delivery_detail(conn, logical_stem)
    if row is None:
        print(f"Delivery not found: {logical_stem}", file=sys.stderr)
        return 1

    diagnostic = diagnose_delivery_row(row)
    if logical_stem == "260422LC":
        print("Refusing to clear 260422LC: document-only exception from D3.", file=sys.stderr)
        return 2
    if diagnostic["classification"] != "source-missing":
        print(
            f"Refusing to clear {logical_stem}: classification is {diagnostic['classification']}, not source-missing.",
            file=sys.stderr,
        )
        return 2
    if not dry_run and not confirmed:
        print("Refusing to clear without --yes. Run --dry-run first.", file=sys.stderr)
        return 2
    if not dry_run and not backup_path:
        print("Refusing to clear without --backup-path for DB snapshot.", file=sys.stderr)
        return 2

    before_total = total_delivery_rows(conn)
    before_payload = {key: row[key] for key in row.keys()}
    resolved_backup_path = Path(backup_path).expanduser() if backup_path else None

    if dry_run:
        print(f"Would clear stale delivery row: {logical_stem}")
        print(f"before_total_rows: {before_total}")
        print(f"after_total_rows: {before_total - 1}")
        print(f"backup_path: {resolved_backup_path if resolved_backup_path else '<required with --yes>'}")
        print("before_row:")
        print(json.dumps(before_payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    assert resolved_backup_path is not None
    try:
        backup_sqlite_db(conn, resolved_backup_path)
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    deleted = delete_delivery(conn, logical_stem)
    after_total = total_delivery_rows(conn)
    if deleted != 1:
        print(f"Delivery not found during clear: {logical_stem}", file=sys.stderr)
        return 1

    print(f"Cleared stale delivery row: {logical_stem}")
    print(f"backup_path: {resolved_backup_path}")
    print(f"before_total_rows: {before_total}")
    print(f"after_total_rows: {after_total}")
    print("before_row:")
    print(json.dumps(before_payload, ensure_ascii=False, indent=2, sort_keys=True))
    print("after_row: null")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    command = args.command or "summary"
    db_path = resolve_db_path(args)
    conn = db.connect_db(str(db_path))
    try:
        if command == "summary":
            return print_summary(conn, limit=args.limit)
        if command == "list":
            return print_list(
                conn,
                limit=args.limit,
                subject=args.subject,
                correction_status=args.correction_status,
                summary_status=args.summary_status,
                only_problems=args.only_problems,
            )
        if command == "show":
            return print_show(conn, args.logical_stem)
        if command == "clear":
            return print_clear(conn, args.logical_stem, dry_run=args.dry_run, confirmed=args.yes)
        if command == "diagnose":
            return print_diagnose(
                conn,
                limit=args.limit,
                emit_json=args.json,
                only_problems=not args.all,
                report_path=args.report_path,
            )
        if command == "clear-stale":
            return print_clear_stale(
                conn,
                args.logical_stem,
                dry_run=args.dry_run,
                confirmed=args.yes,
                backup_path=args.backup_path,
            )
        raise ValueError(f"Unsupported command: {command}")
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
