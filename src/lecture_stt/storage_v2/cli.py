from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Sequence

from lecture_stt.shared.paths import repo_root
from lecture_stt.storage_v2.archive_evidence import (
    apply_archive_evidence,
    parse_historical_root_specs,
    plan_archive_evidence,
    read_archive_evidence_snapshot,
    verify_archive_evidence,
)
from lecture_stt.storage_v2.archive_reconciliation import (
    MAX_REPORTED_ROWS,
    reconcile_archive_from_config,
)
from lecture_stt.storage_v2.classification_materialization import (
    ClassificationMaterializationRecoveryRequiredError,
    apply_classification_materialization,
    plan_classification_materialization,
)
from lecture_stt.storage_v2.importer import (
    ImportPlan,
    discover_candidates,
    import_candidate,
)
from lecture_stt.storage_v2.repository import read_library_snapshot
from lecture_stt.storage_v2.timetable import (
    apply_classification_confirmation,
    apply_recording_classifications,
    apply_timetable_import,
    list_classification_proposals,
    list_timetable_entries,
    plan_classification_confirmation,
    plan_recording_classifications,
    plan_timetable_import,
)
from lecture_stt.storage_v2.title_suggestions import (
    apply_title_suggestion_confirmation,
    apply_title_suggestions,
    list_title_suggestions,
    plan_title_suggestion_confirmation,
    plan_title_suggestions,
    reject_title_suggestion,
)
from lecture_stt.storage_v2.title_materialization import (
    TitleMaterializationRecoveryRequiredError,
    apply_title_materialization,
    plan_title_materialization,
)
from lecture_stt.storage_v2.transcript_recovery import (
    DEFAULT_MAX_ARTIFACT_BYTES,
    HistoricalTranscriptRecoveryRequiredError,
    apply_historical_transcript_recovery,
    plan_historical_transcript_recovery,
)
from lecture_stt.storage_v2.verifier import verify_library


DEFAULT_V2_DB = repo_root() / "state" / "storage-v2.sqlite3"
DEFAULT_TITLE_TRANSCRIPT_MAX_BYTES = 2 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")


def _sha256_arg(value: str) -> str:
    digest = str(value).strip()
    if not _SHA256_RE.fullmatch(digest):
        raise argparse.ArgumentTypeError("must be exactly 64 hexadecimal characters")
    return digest.lower()


def compute_plan_digest(plans: Sequence[ImportPlan]) -> str:
    """Hash the canonical JSON representation of the ordered import plan list."""

    payload = [plan.as_dict() for plan in plans]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _add_legacy_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--legacy-db",
        required=True,
        help="Standalone checkpointed legacy jobs.sqlite3 snapshot",
    )
    parser.add_argument(
        "--legacy-root",
        required=True,
        help="Legacy lecture_recordings root containing 01_audio..04_summarize",
    )
    parser.add_argument("--job-id", type=int, action="append", dest="job_ids")
    parser.add_argument("--canonical-base", action="append", dest="canonical_bases")
    parser.add_argument("--limit", type=int)


def _add_archive_evidence_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--legacy-db",
        required=True,
        help="Standalone checkpointed legacy jobs.sqlite3 snapshot",
    )
    parser.add_argument(
        "--legacy-root",
        required=True,
        help="Legacy lecture_recordings root containing 03_correction and 04_summarize",
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config.yaml used to resolve current archive destinations",
    )
    parser.add_argument(
        "--historical-root",
        action="append",
        dest="historical_roots",
        metavar="LABEL=PATH",
        help="Explicitly authorize one historical archive root (repeatable)",
    )
    parser.add_argument(
        "--logical-stem",
        action="append",
        dest="logical_stems",
        help="Select one ownerless archive case by logical stem (repeatable)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_REPORTED_ROWS,
        help=f"Maximum number of archive evidence cases (1-{MAX_REPORTED_ROWS})",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or apply a preserve-first import into storage v2",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan",
        help="Read legacy metadata and files without writing a v2 DB or records",
    )
    _add_legacy_selection(plan_parser)
    plan_parser.add_argument("--json", action="store_true", dest="emit_json")

    apply_parser = subparsers.add_parser(
        "apply",
        help="Copy planned records into a separate storage v2 DB/root",
    )
    _add_legacy_selection(apply_parser)
    apply_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    apply_parser.add_argument("--records-root", required=True)
    apply_parser.add_argument(
        "--expected-count",
        type=int,
        required=True,
        help="Required guard: must equal the current plan count",
    )
    apply_parser.add_argument(
        "--expected-plan-sha256",
        type=_sha256_arg,
        required=True,
        help="Required guard: must equal the SHA-256 digest printed by plan",
    )
    apply_parser.add_argument(
        "--allow-write",
        action="store_true",
        help="Required guard that enables target-only writes",
    )
    apply_parser.add_argument(
        "--allow-missing-source",
        action="store_true",
        help="Acknowledge importing unavailable.json markers for missing legacy audio",
    )
    apply_parser.add_argument("--json", action="store_true", dest="emit_json")

    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="Read the storage v2 library view without modifying the DB",
    )
    snapshot_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    snapshot_parser.add_argument("--limit", type=int, default=20)
    snapshot_parser.add_argument("--json", action="store_true", dest="emit_json")

    verify_parser = subparsers.add_parser(
        "verify",
        help="Read and hash-check every indexed v2 manifest and artifact",
    )
    verify_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    verify_parser.add_argument("--records-root", required=True)
    verify_parser.add_argument("--json", action="store_true", dest="emit_json")

    reconcile_parser = subparsers.add_parser(
        "reconcile-archive",
        help="Read-only verify unmatched ownerless downstream deliveries against archive destinations",
    )
    reconcile_parser.add_argument(
        "--legacy-db",
        required=True,
        help="Standalone checkpointed legacy jobs.sqlite3 snapshot",
    )
    reconcile_parser.add_argument(
        "--legacy-root",
        required=True,
        help="Legacy lecture_recordings root containing 03_correction and 04_summarize",
    )
    reconcile_parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config.yaml used to resolve archive destinations",
    )
    reconcile_parser.add_argument(
        "--limit",
        type=int,
        default=MAX_REPORTED_ROWS,
        help=f"Maximum number of reconciliation rows to emit (1-{MAX_REPORTED_ROWS})",
    )
    reconcile_parser.add_argument("--json", action="store_true", dest="emit_json")

    evidence_plan_parser = subparsers.add_parser(
        "plan-archive-evidence",
        help="Build a read-only preservation plan for ownerless archive revisions",
    )
    _add_archive_evidence_selection(evidence_plan_parser)
    evidence_plan_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    evidence_apply_parser = subparsers.add_parser(
        "apply-archive-evidence",
        help="Copy planned ownerless archive revisions into an isolated evidence store",
    )
    _add_archive_evidence_selection(evidence_apply_parser)
    evidence_apply_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    evidence_apply_parser.add_argument("--evidence-root", required=True)
    evidence_apply_parser.add_argument(
        "--expected-count",
        type=int,
        required=True,
        help="Required guard: must equal the current archive evidence case count",
    )
    evidence_apply_parser.add_argument(
        "--expected-plan-sha256",
        type=_sha256_arg,
        required=True,
        help="Required guard: must equal the digest printed by plan-archive-evidence",
    )
    evidence_apply_parser.add_argument(
        "--allow-write",
        action="store_true",
        help="Required guard that enables target-only evidence writes",
    )
    evidence_apply_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    evidence_snapshot_parser = subparsers.add_parser(
        "snapshot-archive-evidence",
        help="Read the archive evidence review queue without exposing source roots",
    )
    evidence_snapshot_parser.add_argument(
        "--v2-db",
        default=str(DEFAULT_V2_DB),
    )
    evidence_snapshot_parser.add_argument("--limit", type=int, default=20)
    evidence_snapshot_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    evidence_verify_parser = subparsers.add_parser(
        "verify-archive-evidence",
        help="Hash-check archive evidence DB, manifests, revisions, and root inventory",
    )
    evidence_verify_parser.add_argument(
        "--v2-db",
        default=str(DEFAULT_V2_DB),
    )
    evidence_verify_parser.add_argument("--evidence-root", required=True)
    evidence_verify_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    timetable_plan_parser = subparsers.add_parser(
        "plan-timetable",
        help="Normalize one local CSV/JSON timetable without writing storage v2",
    )
    timetable_plan_parser.add_argument("--source", required=True)
    timetable_plan_parser.add_argument("--semester")
    timetable_plan_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    timetable_apply_parser = subparsers.add_parser(
        "apply-timetable",
        help="Apply one guarded local CSV/JSON timetable import",
    )
    timetable_apply_parser.add_argument("--source", required=True)
    timetable_apply_parser.add_argument("--semester")
    timetable_apply_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    timetable_apply_parser.add_argument("--expected-count", type=int, required=True)
    timetable_apply_parser.add_argument(
        "--expected-plan-sha256",
        type=_sha256_arg,
        required=True,
    )
    timetable_apply_parser.add_argument("--allow-write", action="store_true")
    timetable_apply_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    timetable_snapshot_parser = subparsers.add_parser(
        "snapshot-timetable",
        help="Read imported timetable metadata without touching source files",
    )
    timetable_snapshot_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    timetable_snapshot_parser.add_argument("--semester")
    timetable_snapshot_parser.add_argument("--limit", type=int, default=100)
    timetable_snapshot_parser.add_argument("--offset", type=int, default=0)
    timetable_snapshot_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    classification_plan_parser = subparsers.add_parser(
        "plan-timetable-classification",
        help="Plan conservative recording-time timetable suggestions",
    )
    classification_plan_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    classification_plan_parser.add_argument("--semester", required=True)
    classification_plan_parser.add_argument(
        "--storage-key",
        action="append",
        dest="storage_keys",
    )
    classification_plan_parser.add_argument(
        "--margin-minutes",
        type=int,
        default=30,
    )
    classification_plan_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    classification_apply_parser = subparsers.add_parser(
        "apply-timetable-classification",
        help="Persist timetable suggestions and review items without changing canonical metadata",
    )
    classification_apply_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    classification_apply_parser.add_argument("--semester", required=True)
    classification_apply_parser.add_argument(
        "--storage-key",
        action="append",
        dest="storage_keys",
    )
    classification_apply_parser.add_argument(
        "--margin-minutes",
        type=int,
        default=30,
    )
    classification_apply_parser.add_argument(
        "--expected-count",
        type=int,
        required=True,
    )
    classification_apply_parser.add_argument(
        "--expected-plan-sha256",
        type=_sha256_arg,
        required=True,
    )
    classification_apply_parser.add_argument("--allow-write", action="store_true")
    classification_apply_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    classification_snapshot_parser = subparsers.add_parser(
        "snapshot-timetable-classifications",
        help="Read the timetable classification review queue",
    )
    classification_snapshot_parser.add_argument(
        "--v2-db",
        default=str(DEFAULT_V2_DB),
    )
    classification_snapshot_parser.add_argument("--status")
    classification_snapshot_parser.add_argument("--limit", type=int, default=50)
    classification_snapshot_parser.add_argument("--offset", type=int, default=0)
    classification_snapshot_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    confirmation_plan_parser = subparsers.add_parser(
        "plan-timetable-confirmation",
        help="Plan an audit-only explicit classification confirmation",
    )
    confirmation_plan_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    confirmation_plan_parser.add_argument("--proposal-id", type=int, required=True)
    confirmation_plan_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    confirmation_apply_parser = subparsers.add_parser(
        "apply-timetable-confirmation",
        help="Apply an audit-only confirmation; does not rewrite title/context or manifest",
    )
    confirmation_apply_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    confirmation_apply_parser.add_argument("--proposal-id", type=int, required=True)
    confirmation_apply_parser.add_argument(
        "--expected-count",
        type=int,
        required=True,
    )
    confirmation_apply_parser.add_argument(
        "--expected-plan-sha256",
        type=_sha256_arg,
        required=True,
    )
    confirmation_apply_parser.add_argument(
        "--enable-confirmation",
        action="store_true",
        help="Required independent guard for audit-only classification confirmation",
    )
    confirmation_apply_parser.add_argument("--allow-write", action="store_true")
    confirmation_apply_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    title_plan_parser = subparsers.add_parser(
        "plan-title-suggestions",
        help=(
            "Plan transcript-content title suggestions without changing "
            "canonical metadata"
        ),
    )
    title_plan_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    title_plan_parser.add_argument("--records-root", required=True)
    title_plan_parser.add_argument(
        "--storage-key",
        action="append",
        dest="storage_keys",
    )
    title_plan_parser.add_argument(
        "--max-transcript-bytes",
        type=int,
        default=DEFAULT_TITLE_TRANSCRIPT_MAX_BYTES,
    )
    title_plan_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    title_apply_parser = subparsers.add_parser(
        "apply-title-suggestions",
        help=(
            "Persist guarded title suggestions and review items without "
            "changing canonical metadata"
        ),
    )
    title_apply_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    title_apply_parser.add_argument("--records-root", required=True)
    title_apply_parser.add_argument(
        "--storage-key",
        action="append",
        dest="storage_keys",
    )
    title_apply_parser.add_argument(
        "--max-transcript-bytes",
        type=int,
        default=DEFAULT_TITLE_TRANSCRIPT_MAX_BYTES,
    )
    title_apply_parser.add_argument(
        "--expected-count",
        type=int,
        required=True,
    )
    title_apply_parser.add_argument(
        "--expected-plan-sha256",
        type=_sha256_arg,
        required=True,
    )
    title_apply_parser.add_argument(
        "--enable-title-suggestions",
        action="store_true",
        help="Required independent guard for title suggestion writes",
    )
    title_apply_parser.add_argument("--allow-write", action="store_true")
    title_apply_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    title_snapshot_parser = subparsers.add_parser(
        "snapshot-title-suggestions",
        help="Read the metadata-only title suggestion review queue",
    )
    title_snapshot_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    title_snapshot_parser.add_argument("--status")
    title_snapshot_parser.add_argument("--limit", type=int, default=50)
    title_snapshot_parser.add_argument("--offset", type=int, default=0)
    title_snapshot_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    title_confirmation_plan_parser = subparsers.add_parser(
        "plan-title-suggestion-confirmation",
        help="Plan one audit-only explicit title suggestion confirmation",
    )
    title_confirmation_plan_parser.add_argument(
        "--v2-db",
        default=str(DEFAULT_V2_DB),
    )
    title_confirmation_plan_parser.add_argument(
        "--records-root",
        required=True,
    )
    title_confirmation_plan_parser.add_argument(
        "--proposal-id",
        type=int,
        required=True,
    )
    title_confirmation_plan_parser.add_argument(
        "--max-transcript-bytes",
        type=int,
        default=DEFAULT_TITLE_TRANSCRIPT_MAX_BYTES,
    )
    title_confirmation_plan_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    title_confirmation_apply_parser = subparsers.add_parser(
        "apply-title-suggestion-confirmation",
        help=(
            "Confirm one title suggestion for audit only; does not change "
            "the current title or manifest"
        ),
    )
    title_confirmation_apply_parser.add_argument(
        "--v2-db",
        default=str(DEFAULT_V2_DB),
    )
    title_confirmation_apply_parser.add_argument(
        "--records-root",
        required=True,
    )
    title_confirmation_apply_parser.add_argument(
        "--proposal-id",
        type=int,
        required=True,
    )
    title_confirmation_apply_parser.add_argument(
        "--expected-count",
        type=int,
        required=True,
    )
    title_confirmation_apply_parser.add_argument(
        "--expected-plan-sha256",
        type=_sha256_arg,
        required=True,
    )
    title_confirmation_apply_parser.add_argument(
        "--max-transcript-bytes",
        type=int,
        default=DEFAULT_TITLE_TRANSCRIPT_MAX_BYTES,
    )
    title_confirmation_apply_parser.add_argument(
        "--enable-confirmation",
        action="store_true",
        help="Required independent guard for audit-only title confirmation",
    )
    title_confirmation_apply_parser.add_argument(
        "--allow-write",
        action="store_true",
    )
    title_confirmation_apply_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    title_reject_parser = subparsers.add_parser(
        "reject-title-suggestion",
        help="Explicitly reject one suggested title without canonical writes",
    )
    title_reject_parser.add_argument("--v2-db", default=str(DEFAULT_V2_DB))
    title_reject_parser.add_argument(
        "--proposal-id",
        type=int,
        required=True,
    )
    title_reject_parser.add_argument(
        "--enable-status-write",
        action="store_true",
        help="Required independent guard for title suggestion status writes",
    )
    title_reject_parser.add_argument("--allow-write", action="store_true")
    title_reject_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    title_materialization_plan_parser = subparsers.add_parser(
        "plan-title-materialization",
        help=(
            "Plan canonical title/manifest materialization for one confirmed "
            "content-title proposal"
        ),
    )
    title_materialization_plan_parser.add_argument(
        "--v2-db",
        default=str(DEFAULT_V2_DB),
    )
    title_materialization_plan_parser.add_argument(
        "--records-root",
        required=True,
    )
    title_materialization_plan_parser.add_argument(
        "--proposal-id",
        type=int,
        required=True,
    )
    title_materialization_plan_parser.add_argument(
        "--max-transcript-bytes",
        type=int,
        default=DEFAULT_TITLE_TRANSCRIPT_MAX_BYTES,
    )
    title_materialization_plan_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    title_materialization_apply_parser = subparsers.add_parser(
        "apply-title-materialization",
        help=(
            "Apply or recover one guarded canonical content title and "
            "manifest revision"
        ),
    )
    title_materialization_apply_parser.add_argument(
        "--v2-db",
        default=str(DEFAULT_V2_DB),
    )
    title_materialization_apply_parser.add_argument(
        "--records-root",
        required=True,
    )
    title_materialization_apply_parser.add_argument(
        "--proposal-id",
        type=int,
        required=True,
    )
    title_materialization_apply_parser.add_argument(
        "--max-transcript-bytes",
        type=int,
        default=DEFAULT_TITLE_TRANSCRIPT_MAX_BYTES,
    )
    title_materialization_apply_parser.add_argument(
        "--expected-count",
        type=int,
        required=True,
    )
    title_materialization_apply_parser.add_argument(
        "--expected-plan-sha256",
        type=_sha256_arg,
        required=True,
    )
    title_materialization_apply_parser.add_argument(
        "--enable-materialization",
        action="store_true",
        help="Required independent guard for canonical content-title writes",
    )
    title_materialization_apply_parser.add_argument(
        "--allow-write",
        action="store_true",
    )
    title_materialization_apply_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    transcript_recovery_plan_parser = subparsers.add_parser(
        "plan-historical-transcript-recovery",
        help=(
            "Plan recovery of one missing legacy transcript pair from an "
            "explicit historical transcript root"
        ),
    )
    transcript_recovery_plan_parser.add_argument(
        "--v2-db",
        default=str(DEFAULT_V2_DB),
    )
    transcript_recovery_plan_parser.add_argument(
        "--records-root",
        required=True,
    )
    transcript_recovery_plan_parser.add_argument(
        "--historical-transcript-root",
        required=True,
    )
    transcript_recovery_plan_parser.add_argument(
        "--storage-key",
        required=True,
    )
    transcript_recovery_plan_parser.add_argument(
        "--max-artifact-bytes",
        type=int,
        default=DEFAULT_MAX_ARTIFACT_BYTES,
    )
    transcript_recovery_plan_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    transcript_recovery_apply_parser = subparsers.add_parser(
        "apply-historical-transcript-recovery",
        help=(
            "Apply or forward-recover one guarded historical transcript pair"
        ),
    )
    transcript_recovery_apply_parser.add_argument(
        "--v2-db",
        default=str(DEFAULT_V2_DB),
    )
    transcript_recovery_apply_parser.add_argument(
        "--records-root",
        required=True,
    )
    transcript_recovery_apply_parser.add_argument(
        "--historical-transcript-root",
        required=True,
    )
    transcript_recovery_apply_parser.add_argument(
        "--storage-key",
        required=True,
    )
    transcript_recovery_apply_parser.add_argument(
        "--max-artifact-bytes",
        type=int,
        default=DEFAULT_MAX_ARTIFACT_BYTES,
    )
    transcript_recovery_apply_parser.add_argument(
        "--expected-count",
        type=int,
        required=True,
    )
    transcript_recovery_apply_parser.add_argument(
        "--expected-plan-sha256",
        type=_sha256_arg,
        required=True,
    )
    transcript_recovery_apply_parser.add_argument(
        "--enable-recovery",
        action="store_true",
        help="Required independent guard for historical transcript writes",
    )
    transcript_recovery_apply_parser.add_argument(
        "--allow-write",
        action="store_true",
    )
    transcript_recovery_apply_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    materialization_plan_parser = subparsers.add_parser(
        "plan-timetable-materialization",
        help=(
            "Plan canonical title/context/manifest materialization for one "
            "confirmed classification"
        ),
    )
    materialization_plan_parser.add_argument(
        "--v2-db",
        default=str(DEFAULT_V2_DB),
    )
    materialization_plan_parser.add_argument("--records-root", required=True)
    materialization_plan_parser.add_argument(
        "--proposal-id",
        type=int,
        required=True,
    )
    materialization_plan_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )

    materialization_apply_parser = subparsers.add_parser(
        "apply-timetable-materialization",
        help=(
            "Apply or recover one guarded canonical title/context/manifest "
            "materialization"
        ),
    )
    materialization_apply_parser.add_argument(
        "--v2-db",
        default=str(DEFAULT_V2_DB),
    )
    materialization_apply_parser.add_argument("--records-root", required=True)
    materialization_apply_parser.add_argument(
        "--proposal-id",
        type=int,
        required=True,
    )
    materialization_apply_parser.add_argument(
        "--expected-count",
        type=int,
        required=True,
    )
    materialization_apply_parser.add_argument(
        "--expected-plan-sha256",
        type=_sha256_arg,
        required=True,
    )
    materialization_apply_parser.add_argument(
        "--enable-materialization",
        action="store_true",
        help=(
            "Required independent guard for canonical title/context/manifest "
            "writes"
        ),
    )
    materialization_apply_parser.add_argument(
        "--allow-write",
        action="store_true",
    )
    materialization_apply_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
    )
    return parser.parse_args(argv)


def _discover(args: argparse.Namespace) -> list[ImportPlan]:
    return discover_candidates(
        args.legacy_db,
        args.legacy_root,
        job_ids=args.job_ids,
        canonical_bases=args.canonical_bases,
        limit=args.limit,
    )


def _summary(plans: Sequence[ImportPlan]) -> dict[str, int]:
    return {
        "total": len(plans),
        "applicable": sum(plan.can_apply for plan in plans),
        "blocked": sum(not plan.can_apply for plan in plans),
        "missing_source": sum(
            not plan.candidate.source_available for plan in plans
        ),
        "needs_review": sum(
            plan.candidate.v2_status == "needs_review" for plan in plans
        ),
    }


def _print_payload(payload: object, *, emit_json: bool) -> None:
    if emit_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            print(f"{key}: {value}")
        return
    print(payload)


def run_plan(args: argparse.Namespace) -> int:
    plans = _discover(args)
    payload = {
        "schema_version": "storage-v2/import-plan@1",
        "mode": "read_only",
        "plan_sha256": compute_plan_digest(plans),
        "summary": _summary(plans),
        "plans": [plan.as_dict() for plan in plans],
    }
    _print_payload(payload, emit_json=args.emit_json)
    return 1 if any(not plan.can_apply for plan in plans) else 0


def run_apply(args: argparse.Namespace) -> int:
    if not args.allow_write:
        print("Refusing apply without --allow-write", file=sys.stderr)
        return 2
    target_db = Path(args.v2_db).expanduser()
    if target_db.is_symlink():
        print("Refusing apply to a storage v2 DB symlink", file=sys.stderr)
        return 2
    legacy_db = Path(args.legacy_db).expanduser().resolve(strict=True)
    if os.path.lexists(target_db) and target_db.stat().st_nlink != 1:
        print("Refusing apply to a hard-linked storage v2 DB", file=sys.stderr)
        return 2
    if legacy_db == target_db.resolve() or (
        os.path.lexists(target_db) and os.path.samefile(legacy_db, target_db)
    ):
        print("Legacy and storage v2 DB paths must be different", file=sys.stderr)
        return 2

    plans = _discover(args)
    summary = _summary(plans)
    plan_sha256 = compute_plan_digest(plans)
    if args.expected_count != len(plans):
        print(
            f"Refusing apply: --expected-count={args.expected_count} "
            f"but current plan contains {len(plans)} records",
            file=sys.stderr,
        )
        return 2
    if args.expected_plan_sha256 != plan_sha256:
        print(
            "Refusing apply: --expected-plan-sha256 does not match the current "
            f"plan digest ({plan_sha256})",
            file=sys.stderr,
        )
        return 2
    blocked = [plan for plan in plans if not plan.can_apply]
    if blocked:
        print(
            f"Refusing apply because {len(blocked)} plans have blocking issues",
            file=sys.stderr,
        )
        return 2
    if summary["missing_source"] and not args.allow_missing_source:
        print(
            "Refusing apply because legacy source audio is missing for "
            f"{summary['missing_source']} records; review the plan and pass "
            "--allow-missing-source to create explicit unavailable markers",
            file=sys.stderr,
        )
        return 2

    results = []
    for plan in plans:
        try:
            results.append(
                import_candidate(
                    plan,
                    target_db_path=target_db,
                    records_root=args.records_root,
                )
            )
        except Exception as exc:
            failure = {
                "schema_version": "storage-v2/import-result@1",
                "summary": {
                    **summary,
                    "completed": len(results),
                    "imported": sum(
                        result.action == "imported" for result in results
                    ),
                    "recovered": sum(
                        result.action == "recovered" for result in results
                    ),
                    "skipped": sum(
                        result.action == "skipped" for result in results
                    ),
                    "failed_legacy_job_id": plan.candidate.legacy_job_id,
                },
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                "results": [result.as_dict() for result in results],
            }
            if args.emit_json:
                print(
                    json.dumps(
                        failure,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
            else:
                print(
                    "Import stopped after "
                    f"{len(results)} completed records; "
                    f"legacy job {plan.candidate.legacy_job_id} failed: {exc}",
                    file=sys.stderr,
                )
            return 1
    payload = {
        "schema_version": "storage-v2/import-result@1",
        "summary": {
            **summary,
            "imported": sum(result.action == "imported" for result in results),
            "recovered": sum(result.action == "recovered" for result in results),
            "skipped": sum(result.action == "skipped" for result in results),
        },
        "results": [result.as_dict() for result in results],
    }
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_snapshot(args: argparse.Namespace) -> int:
    payload = read_library_snapshot(args.v2_db, limit=args.limit)
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_verify(args: argparse.Namespace) -> int:
    payload = verify_library(args.v2_db, args.records_root)
    _print_payload(payload, emit_json=args.emit_json)
    return 0 if payload["ok"] else 1


def run_reconcile_archive(args: argparse.Namespace) -> int:
    payload = reconcile_archive_from_config(
        args.legacy_db,
        args.legacy_root,
        config_path=args.config,
        limit=args.limit,
    )
    _print_payload(payload, emit_json=args.emit_json)
    counts = payload["summary"]["result_counts"]
    return 0 if counts["manual_review"] == 0 and counts["blocked"] == 0 else 1


def _archive_evidence_plan(args: argparse.Namespace):
    historical_roots = parse_historical_root_specs(args.historical_roots)
    return plan_archive_evidence(
        args.legacy_db,
        args.legacy_root,
        config_path=args.config,
        historical_roots=historical_roots,
        logical_stems=args.logical_stems,
        limit=args.limit,
    )


def run_plan_archive_evidence(args: argparse.Namespace) -> int:
    batch = _archive_evidence_plan(args)
    payload = batch.as_dict()
    _print_payload(payload, emit_json=args.emit_json)
    return 1 if payload["summary"]["blocked"] else 0


def run_apply_archive_evidence(args: argparse.Namespace) -> int:
    if not args.allow_write:
        print(
            "Refusing archive evidence apply without --allow-write",
            file=sys.stderr,
        )
        return 2
    target_db = Path(args.v2_db).expanduser()
    if target_db.is_symlink():
        print(
            "Refusing archive evidence apply to a storage v2 DB symlink",
            file=sys.stderr,
        )
        return 2
    if os.path.lexists(target_db):
        metadata = target_db.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            print(
                "Refusing archive evidence apply to an unsafe storage v2 DB",
                file=sys.stderr,
            )
            return 2
    batch = _archive_evidence_plan(args)
    if args.expected_count != len(batch.cases):
        print(
            f"Refusing archive evidence apply: --expected-count={args.expected_count} "
            f"but current plan contains {len(batch.cases)} cases",
            file=sys.stderr,
        )
        return 2
    if args.expected_plan_sha256 != batch.plan_sha256:
        print(
            "Refusing archive evidence apply: --expected-plan-sha256 does not "
            f"match the current plan digest ({batch.plan_sha256})",
            file=sys.stderr,
        )
        return 2
    blocked = [case for case in batch.cases if not case.can_apply]
    if blocked:
        print(
            f"Refusing archive evidence apply because {len(blocked)} cases "
            "have unsafe source issues",
            file=sys.stderr,
        )
        return 2
    try:
        results = apply_archive_evidence(
            batch,
            target_db_path=target_db,
            evidence_root=args.evidence_root,
        )
    except Exception as exc:
        failure = {
            "schema_version": "storage-v2/archive-evidence-result@1",
            "summary": {
                "total": len(batch.cases),
                "completed": 0,
                "imported": 0,
                "recovered": 0,
                "skipped": 0,
            },
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
            "results": [],
        }
        if args.emit_json:
            print(
                json.dumps(
                    failure,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        else:
            print(f"Archive evidence apply failed: {exc}", file=sys.stderr)
        return 1
    payload = {
        "schema_version": "storage-v2/archive-evidence-result@1",
        "summary": {
            "total": len(results),
            "imported": sum(result.action == "imported" for result in results),
            "recovered": sum(result.action == "recovered" for result in results),
            "skipped": sum(result.action == "skipped" for result in results),
        },
        "results": [result.as_dict() for result in results],
    }
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_snapshot_archive_evidence(args: argparse.Namespace) -> int:
    payload = read_archive_evidence_snapshot(args.v2_db, limit=args.limit)
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_verify_archive_evidence(args: argparse.Namespace) -> int:
    payload = verify_archive_evidence(args.v2_db, args.evidence_root)
    _print_payload(payload, emit_json=args.emit_json)
    return 0 if payload["ok"] else 1


def run_plan_timetable(args: argparse.Namespace) -> int:
    payload = plan_timetable_import(
        args.source,
        semester=args.semester,
    )
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_apply_timetable(args: argparse.Namespace) -> int:
    try:
        payload = apply_timetable_import(
            args.source,
            args.v2_db,
            semester=args.semester,
            expected_count=args.expected_count,
            expected_plan_sha256=args.expected_plan_sha256,
            allow_write=args.allow_write,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Timetable import refused: {exc}", file=sys.stderr)
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_snapshot_timetable(args: argparse.Namespace) -> int:
    payload = list_timetable_entries(
        args.v2_db,
        semester=args.semester,
        limit=args.limit,
        offset=args.offset,
    )
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_plan_timetable_classification(args: argparse.Namespace) -> int:
    payload = plan_recording_classifications(
        args.v2_db,
        semester=args.semester,
        storage_keys=args.storage_keys,
        margin_minutes=args.margin_minutes,
    )
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_apply_timetable_classification(args: argparse.Namespace) -> int:
    try:
        payload = apply_recording_classifications(
            args.v2_db,
            semester=args.semester,
            storage_keys=args.storage_keys,
            margin_minutes=args.margin_minutes,
            expected_count=args.expected_count,
            expected_plan_sha256=args.expected_plan_sha256,
            allow_write=args.allow_write,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Timetable classification refused: {exc}", file=sys.stderr)
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_snapshot_timetable_classifications(args: argparse.Namespace) -> int:
    payload = list_classification_proposals(
        args.v2_db,
        status=args.status,
        limit=args.limit,
        offset=args.offset,
    )
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_plan_timetable_confirmation(args: argparse.Namespace) -> int:
    payload = plan_classification_confirmation(
        args.v2_db,
        args.proposal_id,
    )
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_apply_timetable_confirmation(args: argparse.Namespace) -> int:
    try:
        payload = apply_classification_confirmation(
            args.v2_db,
            args.proposal_id,
            expected_count=args.expected_count,
            expected_plan_sha256=args.expected_plan_sha256,
            confirmations_enabled=args.enable_confirmation,
            allow_write=args.allow_write,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Timetable confirmation refused: {exc}", file=sys.stderr)
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_plan_title_suggestions(args: argparse.Namespace) -> int:
    try:
        payload = plan_title_suggestions(
            args.v2_db,
            args.records_root,
            storage_keys=args.storage_keys,
            max_transcript_bytes=args.max_transcript_bytes,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Title suggestion plan refused: {exc}", file=sys.stderr)
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_apply_title_suggestions(args: argparse.Namespace) -> int:
    try:
        payload = apply_title_suggestions(
            args.v2_db,
            args.records_root,
            storage_keys=args.storage_keys,
            max_transcript_bytes=args.max_transcript_bytes,
            expected_count=args.expected_count,
            expected_plan_sha256=args.expected_plan_sha256,
            suggestions_enabled=args.enable_title_suggestions,
            allow_write=args.allow_write,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Title suggestion apply refused: {exc}", file=sys.stderr)
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_snapshot_title_suggestions(args: argparse.Namespace) -> int:
    try:
        payload = list_title_suggestions(
            args.v2_db,
            status=args.status,
            limit=args.limit,
            offset=args.offset,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Title suggestion snapshot refused: {exc}", file=sys.stderr)
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_plan_title_suggestion_confirmation(
    args: argparse.Namespace,
) -> int:
    try:
        payload = plan_title_suggestion_confirmation(
            args.v2_db,
            args.records_root,
            args.proposal_id,
            max_transcript_bytes=args.max_transcript_bytes,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Title suggestion confirmation plan refused: {exc}", file=sys.stderr)
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_apply_title_suggestion_confirmation(
    args: argparse.Namespace,
) -> int:
    try:
        payload = apply_title_suggestion_confirmation(
            args.v2_db,
            args.records_root,
            args.proposal_id,
            expected_count=args.expected_count,
            expected_plan_sha256=args.expected_plan_sha256,
            confirmations_enabled=args.enable_confirmation,
            allow_write=args.allow_write,
            max_transcript_bytes=args.max_transcript_bytes,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Title suggestion confirmation refused: {exc}", file=sys.stderr)
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_reject_title_suggestion(args: argparse.Namespace) -> int:
    try:
        payload = reject_title_suggestion(
            args.v2_db,
            args.proposal_id,
            status_writes_enabled=args.enable_status_write,
            allow_write=args.allow_write,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Title suggestion rejection refused: {exc}", file=sys.stderr)
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_plan_title_materialization(args: argparse.Namespace) -> int:
    try:
        payload = plan_title_materialization(
            args.v2_db,
            args.records_root,
            args.proposal_id,
            max_transcript_bytes=args.max_transcript_bytes,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Title materialization plan refused: {exc}", file=sys.stderr)
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_apply_title_materialization(args: argparse.Namespace) -> int:
    try:
        payload = apply_title_materialization(
            args.v2_db,
            args.records_root,
            args.proposal_id,
            expected_count=args.expected_count,
            expected_plan_sha256=args.expected_plan_sha256,
            materializations_enabled=args.enable_materialization,
            allow_write=args.allow_write,
            max_transcript_bytes=args.max_transcript_bytes,
        )
    except TitleMaterializationRecoveryRequiredError as exc:
        print(
            "Title materialization entered a recovery-required state: "
            f"{exc}",
            file=sys.stderr,
        )
        return 3
    except (ValueError, RuntimeError) as exc:
        print(f"Title materialization refused: {exc}", file=sys.stderr)
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_plan_historical_transcript_recovery(
    args: argparse.Namespace,
) -> int:
    try:
        payload = plan_historical_transcript_recovery(
            args.v2_db,
            args.records_root,
            args.historical_transcript_root,
            args.storage_key,
            max_artifact_bytes=args.max_artifact_bytes,
        )
    except (ValueError, RuntimeError) as exc:
        print(
            f"Historical transcript recovery plan refused: {exc}",
            file=sys.stderr,
        )
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_apply_historical_transcript_recovery(
    args: argparse.Namespace,
) -> int:
    try:
        payload = apply_historical_transcript_recovery(
            args.v2_db,
            args.records_root,
            args.historical_transcript_root,
            args.storage_key,
            expected_count=args.expected_count,
            expected_plan_sha256=args.expected_plan_sha256,
            recovery_enabled=args.enable_recovery,
            allow_write=args.allow_write,
            max_artifact_bytes=args.max_artifact_bytes,
        )
    except HistoricalTranscriptRecoveryRequiredError as exc:
        print(
            "Historical transcript recovery entered a recovery-required "
            f"state: {exc}",
            file=sys.stderr,
        )
        return 3
    except (ValueError, RuntimeError) as exc:
        print(
            f"Historical transcript recovery refused: {exc}",
            file=sys.stderr,
        )
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_plan_timetable_materialization(args: argparse.Namespace) -> int:
    try:
        payload = plan_classification_materialization(
            args.v2_db,
            args.records_root,
            args.proposal_id,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Timetable materialization plan refused: {exc}", file=sys.stderr)
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def run_apply_timetable_materialization(args: argparse.Namespace) -> int:
    try:
        payload = apply_classification_materialization(
            args.v2_db,
            args.records_root,
            args.proposal_id,
            expected_count=args.expected_count,
            expected_plan_sha256=args.expected_plan_sha256,
            materializations_enabled=args.enable_materialization,
            allow_write=args.allow_write,
        )
    except ClassificationMaterializationRecoveryRequiredError as exc:
        print(
            "Timetable materialization entered a recovery-required state: "
            f"{exc}",
            file=sys.stderr,
        )
        return 3
    except (ValueError, RuntimeError) as exc:
        print(f"Timetable materialization refused: {exc}", file=sys.stderr)
        return 2
    _print_payload(payload, emit_json=args.emit_json)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "plan":
        return run_plan(args)
    if args.command == "apply":
        return run_apply(args)
    if args.command == "snapshot":
        return run_snapshot(args)
    if args.command == "verify":
        return run_verify(args)
    if args.command == "reconcile-archive":
        return run_reconcile_archive(args)
    if args.command == "plan-archive-evidence":
        return run_plan_archive_evidence(args)
    if args.command == "apply-archive-evidence":
        return run_apply_archive_evidence(args)
    if args.command == "snapshot-archive-evidence":
        return run_snapshot_archive_evidence(args)
    if args.command == "verify-archive-evidence":
        return run_verify_archive_evidence(args)
    if args.command == "plan-timetable":
        return run_plan_timetable(args)
    if args.command == "apply-timetable":
        return run_apply_timetable(args)
    if args.command == "snapshot-timetable":
        return run_snapshot_timetable(args)
    if args.command == "plan-timetable-classification":
        return run_plan_timetable_classification(args)
    if args.command == "apply-timetable-classification":
        return run_apply_timetable_classification(args)
    if args.command == "snapshot-timetable-classifications":
        return run_snapshot_timetable_classifications(args)
    if args.command == "plan-timetable-confirmation":
        return run_plan_timetable_confirmation(args)
    if args.command == "apply-timetable-confirmation":
        return run_apply_timetable_confirmation(args)
    if args.command == "plan-title-suggestions":
        return run_plan_title_suggestions(args)
    if args.command == "apply-title-suggestions":
        return run_apply_title_suggestions(args)
    if args.command == "snapshot-title-suggestions":
        return run_snapshot_title_suggestions(args)
    if args.command == "plan-title-suggestion-confirmation":
        return run_plan_title_suggestion_confirmation(args)
    if args.command == "apply-title-suggestion-confirmation":
        return run_apply_title_suggestion_confirmation(args)
    if args.command == "reject-title-suggestion":
        return run_reject_title_suggestion(args)
    if args.command == "plan-title-materialization":
        return run_plan_title_materialization(args)
    if args.command == "apply-title-materialization":
        return run_apply_title_materialization(args)
    if args.command == "plan-historical-transcript-recovery":
        return run_plan_historical_transcript_recovery(args)
    if args.command == "apply-historical-transcript-recovery":
        return run_apply_historical_transcript_recovery(args)
    if args.command == "plan-timetable-materialization":
        return run_plan_timetable_materialization(args)
    if args.command == "apply-timetable-materialization":
        return run_apply_timetable_materialization(args)
    raise ValueError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
