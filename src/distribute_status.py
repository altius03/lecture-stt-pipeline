from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterable, Sequence

import db


DEFAULT_DB_PATH = Path("/Users/geonha/lecture_stt/state/jobs.sqlite3")
PROBLEM_CORRECTION_STATUSES = {"INCOMPLETE", "CONFLICT", "ERROR"}
PROBLEM_SUMMARY_STATUSES = {"BLOCKED", "CONFLICT", "ERROR"}


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

    return parser.parse_args(argv)


def resolve_db_path(args: argparse.Namespace) -> Path:
    if args.db_path:
        return Path(args.db_path).expanduser()
    try:
        from distribute_worker import load_worker_config

        return load_worker_config(args.config).db_path
    except FileNotFoundError:
        return DEFAULT_DB_PATH


def fetch_status_counts(conn: sqlite3.Connection, column: str) -> list[tuple[str, int]]:
    rows = conn.execute(
        f"SELECT {column}, COUNT(*) AS cnt FROM deliveries GROUP BY {column} ORDER BY cnt DESC, {column} ASC"
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


def delete_delivery(conn: sqlite3.Connection, logical_stem: str) -> int:
    cursor = conn.execute("DELETE FROM deliveries WHERE logical_stem = ?", (logical_stem,))
    conn.commit()
    return int(cursor.rowcount)


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
        raise ValueError(f"Unsupported command: {command}")
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
