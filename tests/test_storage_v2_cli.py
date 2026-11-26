from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.storage_v2 import cli
from lecture_stt.storage_v2 import archive_reconciliation as reconciliation
from lecture_stt.storage_v2.archive_evidence import (
    ArchiveEvidenceCasePlan,
    ArchiveEvidencePlanBatch,
)
from lecture_stt.downstream.lib import DownstreamConfig, default_subject_routes
from lecture_stt.shared import db


class _FakePlan:
    def __init__(
        self,
        payload: dict[str, object],
        *,
        can_apply: bool = True,
        source_available: bool = True,
        status: str = "done",
        legacy_job_id: int = 1,
    ) -> None:
        self._payload = payload
        self.can_apply = can_apply
        self.candidate = SimpleNamespace(
            source_available=source_available,
            v2_status=status,
            legacy_job_id=legacy_job_id,
        )

    def as_dict(self) -> dict[str, object]:
        return dict(self._payload)


class StorageV2CliTests(unittest.TestCase):
    def _reconcile_config(self, root: Path) -> DownstreamConfig:
        gh_root = root / "GH_archive" / "current"
        obsidian_root = root / "Obsidian" / "current"
        for path in (
            gh_root,
            obsidian_root,
            root / "03_correction",
            root / "04_summarize",
            root / "state",
        ):
            path.mkdir(parents=True, exist_ok=True)
        return DownstreamConfig(
            correction_dir=root / "03_correction",
            summary_dir=root / "04_summarize",
            gh_current_semester_root=gh_root,
            obsidian_semester_root=obsidian_root,
            db_path=root / "state" / "jobs.sqlite3",
            log_jsonl_path=root / "state" / "downstream.jsonl",
            lock_path=root / "state" / "downstream.lock",
            scan_interval_sec=1,
            stable_for_sec=1,
            subjects=default_subject_routes(),
        )

    def _write_delivery_fixture(
        self,
        root: Path,
        *,
        stem: str,
        summary_status: str,
        with_summary_files: bool,
        correction_text: str = "corrected text",
        summary_text: str = "# summary",
    ) -> tuple[Path, DownstreamConfig]:
        legacy_root = root / "legacy"
        for path in (legacy_root / "03_correction", legacy_root / "04_summarize"):
            path.mkdir(parents=True, exist_ok=True)
        config = self._reconcile_config(root)
        route = config.subjects["LC"]

        txt_dst = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.txt"
        json_dst = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"
        md_gh_dst = route.gh_summary_dir(config.gh_current_semester_root) / f"{stem}.md"
        md_obs_dst = route.obsidian_summary_dir(config.obsidian_semester_root) / f"{stem}.md"
        txt_dst.parent.mkdir(parents=True, exist_ok=True)
        json_dst.parent.mkdir(parents=True, exist_ok=True)
        md_gh_dst.parent.mkdir(parents=True, exist_ok=True)
        md_obs_dst.parent.mkdir(parents=True, exist_ok=True)
        txt_dst.write_text(correction_text, encoding="utf-8")
        json_dst.write_text('{"segments":[{"text":"corrected text"}]}', encoding="utf-8")
        txt_sha = cli.hashlib.sha256(txt_dst.read_bytes()).hexdigest()
        json_sha = cli.hashlib.sha256(json_dst.read_bytes()).hexdigest()
        summary_sha = None
        if with_summary_files:
            md_gh_dst.write_text(summary_text, encoding="utf-8")
            md_obs_dst.write_text(summary_text, encoding="utf-8")
            summary_sha = cli.hashlib.sha256(md_gh_dst.read_bytes()).hexdigest()

        live_db = root / "legacy-live.sqlite3"
        legacy_db = root / "legacy.sqlite3"
        conn = db.init_db(str(live_db))
        try:
            conn.execute(
                """
                INSERT INTO deliveries(
                    logical_stem, source_job_id, subject_abbr,
                    correction_txt_path, correction_json_path, summary_md_path,
                    correction_txt_sha256, correction_json_sha256, summary_md_sha256,
                    tuk_origin_txt_path, tuk_origin_json_path, tuk_summary_path, obsidian_summary_path,
                    correction_status, summary_status,
                    tuk_origin_done, tuk_summary_done, obsidian_done,
                    updated_at
                ) VALUES (?, NULL, 'LC', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DELIVERED', ?, 1, ?, ?, '2026-07-23T21:00:00+09:00')
                """,
                (
                    stem,
                    str(legacy_root / "03_correction" / f"{stem}.txt"),
                    str(legacy_root / "03_correction" / f"{stem}.json"),
                    str(legacy_root / "04_summarize" / f"{stem}.md"),
                    txt_sha,
                    json_sha,
                    summary_sha,
                    str(Path("/old/GH_archive") / f"{stem}.txt"),
                    str(Path("/old/GH_archive") / f"{stem}.json"),
                    str(Path("/old/GH_archive") / f"{stem}.md"),
                    str(Path("/old/Obsidian") / f"{stem}.md"),
                    summary_status,
                    1 if with_summary_files else 0,
                    1 if with_summary_files else 0,
                ),
            )
            conn.commit()
            with sqlite3.connect(legacy_db) as snapshot:
                conn.backup(snapshot)
        finally:
            conn.close()
        with sqlite3.connect(legacy_db) as snapshot:
            snapshot.execute("PRAGMA journal_mode=DELETE")
        return legacy_db, config

    def _apply_args(
        self,
        root: Path,
        *,
        target_db: Path,
        expected_plan_sha256: str,
    ) -> list[str]:
        return [
            "apply",
            "--legacy-db",
            str(root / "legacy.sqlite3"),
            "--legacy-root",
            str(root / "legacy"),
            "--v2-db",
            str(target_db),
            "--records-root",
            str(root / "records"),
            "--expected-count",
            "1",
            "--expected-plan-sha256",
            expected_plan_sha256,
            "--allow-write",
        ]

    def _archive_batch(
        self,
        root: Path,
        *,
        case_count: int = 1,
        allow_apply: bool = True,
    ) -> tuple[ArchiveEvidencePlanBatch, str]:
        case = ArchiveEvidenceCasePlan(
            case_key="case_1",
            capture_key="capture_1",
            legacy_delivery_key="delivery_1",
            logical_stem="LC260801_001",
            subject_abbr="LC",
            reconciliation_classification="verified_delivered",
            legacy_database_sha256="a" * 64,
            source_fingerprint="b" * 64,
            plan_sha256="c" * 64,
            manifest_relpath="cases/case_1/captures/capture_1.json",
            snapshot={},
            revisions=(),
            issues=(
                () if allow_apply else ({"severity": "error", "code": "blocked_source", "message": "mock"},)
            ),
        )
        batch = ArchiveEvidencePlanBatch(
            legacy_db_path=root / "legacy.sqlite3",
            legacy_root=root / "legacy",
            legacy_database_snapshot={"main_sha256": "a" * 64},
            source_roots={
                "current_gh": root / "gh",
                "current_obsidian": root / "obs",
            },
            candidate_count=case_count,
            truncated=False,
            cases=(case,),
        )
        return batch, batch.plan_sha256

    def test_plan_digest_is_canonical_stable_and_changes_with_content(self) -> None:
        first = _FakePlan(
            {
                "legacy_job_id": 7,
                "title": "자료구조",
                "artifacts": [
                    {"path": "source/original.m4a", "sha256": "a" * 64}
                ],
            }
        )
        same_content_different_key_order = _FakePlan(
            {
                "artifacts": [
                    {"sha256": "a" * 64, "path": "source/original.m4a"}
                ],
                "title": "자료구조",
                "legacy_job_id": 7,
            }
        )
        changed = _FakePlan(
            {
                "legacy_job_id": 7,
                "title": "운영체제",
                "artifacts": [
                    {"path": "source/original.m4a", "sha256": "a" * 64}
                ],
            }
        )

        digest = cli.compute_plan_digest([first])

        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            digest,
            cli.compute_plan_digest([same_content_different_key_order]),
        )
        self.assertNotEqual(digest, cli.compute_plan_digest([changed]))

        stdout = io.StringIO()
        with mock.patch.object(cli, "_discover", return_value=[first]):
            with redirect_stdout(stdout):
                exit_code = cli.main(
                    [
                        "plan",
                        "--legacy-db",
                        "/unused/legacy.sqlite3",
                        "--legacy-root",
                        "/unused/legacy",
                        "--json",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["plan_sha256"], digest)
        self.assertEqual(payload["plans"], [first.as_dict()])

    def test_apply_requires_expected_plan_digest_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(
                        [
                            "apply",
                            "--legacy-root",
                            str(root / "legacy"),
                            "--records-root",
                            str(root / "records"),
                            "--expected-count",
                            "1",
                            "--allow-write",
                        ]
                    )

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("--expected-plan-sha256", stderr.getvalue())
            self.assertFalse((root / "records").exists())

    def test_plan_requires_explicit_legacy_snapshot_path(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["plan", "--legacy-root", "/unused/legacy"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--legacy-db", stderr.getvalue())

    def test_apply_rejects_plan_digest_mismatch_before_writing(self) -> None:
        plan = _FakePlan({"legacy_job_id": 1, "storage_key": "rec_one"})
        current_digest = cli.compute_plan_digest([plan])
        mismatched_digest = "0" * 64
        self.assertNotEqual(current_digest, mismatched_digest)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "legacy").mkdir()
            (root / "legacy.sqlite3").touch()
            target_db = root / "v2.sqlite3"
            stderr = io.StringIO()
            with (
                mock.patch.object(cli, "_discover", return_value=[plan]),
                mock.patch.object(cli, "import_candidate") as import_mock,
                redirect_stderr(stderr),
            ):
                exit_code = cli.main(
                    self._apply_args(
                        root,
                        target_db=target_db,
                        expected_plan_sha256=mismatched_digest,
                    )
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("does not match", stderr.getvalue())
            self.assertIn(current_digest, stderr.getvalue())
            import_mock.assert_not_called()
            self.assertFalse(target_db.exists())
            self.assertFalse((root / "records").exists())

    def test_apply_rejects_raw_v2_database_symlink_before_discovery(self) -> None:
        plan = _FakePlan({"legacy_job_id": 1, "storage_key": "rec_one"})
        digest = cli.compute_plan_digest([plan])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "legacy").mkdir()
            (root / "legacy.sqlite3").touch()
            real_target = root / "real-v2.sqlite3"
            real_target.write_bytes(b"must remain untouched")
            linked_target = root / "linked-v2.sqlite3"
            try:
                linked_target.symlink_to(real_target)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            stderr = io.StringIO()
            with (
                mock.patch.object(cli, "_discover") as discover_mock,
                mock.patch.object(cli, "import_candidate") as import_mock,
                redirect_stderr(stderr),
            ):
                exit_code = cli.main(
                    self._apply_args(
                        root,
                        target_db=linked_target,
                        expected_plan_sha256=digest,
                    )
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("symlink", stderr.getvalue().lower())
            discover_mock.assert_not_called()
            import_mock.assert_not_called()
            self.assertEqual(real_target.read_bytes(), b"must remain untouched")
            self.assertFalse((root / "records").exists())

    def test_apply_rejects_hardlinked_v2_database_before_discovery(self) -> None:
        plan = _FakePlan({"legacy_job_id": 1, "storage_key": "rec_one"})
        digest = cli.compute_plan_digest([plan])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "legacy").mkdir()
            legacy_db = root / "legacy.sqlite3"
            legacy_db.write_bytes(b"legacy snapshot must remain untouched")
            linked_target = root / "v2.sqlite3"
            try:
                linked_target.hardlink_to(legacy_db)
            except OSError as exc:
                self.skipTest(f"hard-link creation is unavailable: {exc}")

            stderr = io.StringIO()
            with (
                mock.patch.object(cli, "_discover") as discover_mock,
                mock.patch.object(cli, "import_candidate") as import_mock,
                redirect_stderr(stderr),
            ):
                exit_code = cli.main(
                    self._apply_args(
                        root,
                        target_db=linked_target,
                        expected_plan_sha256=digest,
                    )
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("hard-linked", stderr.getvalue())
            discover_mock.assert_not_called()
            import_mock.assert_not_called()
            self.assertEqual(
                legacy_db.read_bytes(),
                b"legacy snapshot must remain untouched",
            )
            self.assertFalse((root / "records").exists())

    def test_reconcile_archive_returns_nonzero_when_review_or_blocked_rows_exist(self) -> None:
        payload = {
            "schema_version": "storage-v2/archive-reconciliation@1",
            "summary": {
                "candidate_rows": 2,
                "reported_rows": 2,
                "truncated": False,
                "result_counts": {
                    "verified_delivered": 1,
                    "verified_correction_only": 0,
                    "manual_review": 1,
                    "blocked": 0,
                },
            },
            "excluded_counts": {
                "delivery_rows_total": 3,
                "owned_delivery_rows": 1,
                "ownerless_matched_rows": 0,
            },
            "rows": [],
            "issues": [],
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(
                cli,
                "reconcile_archive_from_config",
                return_value=payload,
            ) as reconcile_mock,
            redirect_stdout(stdout),
        ):
            exit_code = cli.main(
                [
                    "reconcile-archive",
                    "--legacy-db",
                    "/unused/legacy.sqlite3",
                    "--legacy-root",
                    "/unused/legacy",
                    "--config",
                    "config/config.yaml",
                    "--json",
                ]
            )

        self.assertEqual(exit_code, 1)
        reconcile_mock.assert_called_once_with(
            "/unused/legacy.sqlite3",
            "/unused/legacy",
            config_path="config/config.yaml",
            limit=cli.MAX_REPORTED_ROWS,
        )
        self.assertEqual(json.loads(stdout.getvalue()), payload)

    def test_reconcile_archive_uses_current_route_fallback_and_keeps_info_only_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy_db, config = self._write_delivery_fixture(
                root,
                stem="260723LC_1",
                summary_status="DELIVERED",
                with_summary_files=True,
            )

            payload = reconciliation.reconcile_archive(
                legacy_db,
                root / "legacy",
                downstream_config=config,
            )

        row = payload["rows"][0]
        self.assertEqual(row["classification"], "verified_delivered")
        self.assertTrue(row["issues"])
        self.assertTrue(all(issue["severity"] == "info" for issue in row["issues"]))
        self.assertEqual(
            {issue["code"] for issue in row["issues"]},
            {"destination_relocated"},
        )
        self.assertEqual(
            Path(row["observed_artifacts"]["correction_txt"]["path"]).name,
            "260723LC_1.txt",
        )
        self.assertEqual(
            Path(row["observed_artifacts"]["summary_tuk"]["path"]).name,
            "260723LC_1.md",
        )

    def test_reconcile_archive_fallback_hash_mismatch_stays_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy_db, config = self._write_delivery_fixture(
                root,
                stem="260723LC_2",
                summary_status="DELIVERED",
                with_summary_files=True,
            )
            route = config.subjects["LC"]
            txt_dst = route.gh_origin_dir(config.gh_current_semester_root) / "260723LC_2.txt"
            txt_dst.write_text("tampered text", encoding="utf-8")

            payload = reconciliation.reconcile_archive(
                legacy_db,
                root / "legacy",
                downstream_config=config,
            )

        row = payload["rows"][0]
        self.assertEqual(row["classification"], "blocked")
        self.assertIn(
            "correction_txt_destination_hash_mismatch",
            {issue["code"] for issue in row["issues"]},
        )

    def test_reconcile_archive_never_opens_recorded_outside_root_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy_db, config = self._write_delivery_fixture(
                root,
                stem="260723LC_3",
                summary_status="DELIVERED",
                with_summary_files=True,
            )
            opened_paths: list[str] = []
            real_open = reconciliation.os.open

            def tracking_open(path: str | bytes, flags: int, mode: int = 0o777):
                opened_paths.append(os.fspath(path))
                return real_open(path, flags, mode)

            with mock.patch.object(reconciliation.os, "open", side_effect=tracking_open):
                payload = reconciliation.reconcile_archive(
                    legacy_db,
                    root / "legacy",
                    downstream_config=config,
                )

        row = payload["rows"][0]
        self.assertEqual(row["classification"], "verified_delivered")
        self.assertTrue(any(path.endswith("260723LC_3.txt") for path in opened_paths))
        self.assertFalse(any(path.startswith("/old/") for path in opened_paths))

    def test_reconcile_archive_info_only_row_is_verified_correction_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy_db, config = self._write_delivery_fixture(
                root,
                stem="260723LC_4",
                summary_status="MISSING",
                with_summary_files=False,
            )

            payload = reconciliation.reconcile_archive(
                legacy_db,
                root / "legacy",
                downstream_config=config,
            )

        row = payload["rows"][0]
        self.assertEqual(row["classification"], "verified_correction_only")
        self.assertTrue(row["issues"])
        self.assertTrue(all(issue["severity"] == "info" for issue in row["issues"]))
        self.assertIsNone(row["observed_artifacts"]["summary_tuk"])

    def test_apply_archive_evidence_requires_allow_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = cli.main(
                    [
                        "apply-archive-evidence",
                        "--legacy-db",
                        str(root / "legacy.sqlite3"),
                        "--legacy-root",
                        str(root / "legacy"),
                        "--evidence-root",
                        str(root / "evidence"),
                        "--expected-count",
                        "1",
                        "--expected-plan-sha256",
                        "a" * 64,
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("Refusing archive evidence apply without --allow-write", stderr.getvalue())

    def test_apply_archive_evidence_rejects_expected_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            batch, digest = self._archive_batch(root, case_count=1)
            target_db = root / "storage-v2.sqlite3"

            stderr = io.StringIO()
            with (
                mock.patch.object(
                    cli,
                    "plan_archive_evidence",
                    return_value=batch,
                ),
                mock.patch.object(cli, "apply_archive_evidence") as apply_mock,
                redirect_stderr(stderr),
            ):
                exit_code = cli.main(
                    [
                        "apply-archive-evidence",
                        "--legacy-db",
                        str(root / "legacy.sqlite3"),
                        "--legacy-root",
                        str(root / "legacy"),
                        "--evidence-root",
                        str(root / "evidence"),
                        "--expected-count",
                        "2",
                        "--expected-plan-sha256",
                        digest,
                        "--allow-write",
                    ]
            )

            self.assertEqual(exit_code, 2)
            self.assertIn("--expected-count=2", stderr.getvalue())
            self.assertIn("current plan contains", stderr.getvalue())
            self.assertFalse(apply_mock.called)
            self.assertFalse(target_db.exists())

    def test_apply_archive_evidence_rejects_expected_plan_sha256_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            batch, digest = self._archive_batch(root, case_count=1)
            wrong_digest = "0" * 64
            self.assertNotEqual(digest, wrong_digest)
            target_db = root / "storage-v2.sqlite3"

            stderr = io.StringIO()
            with (
                mock.patch.object(
                    cli,
                    "plan_archive_evidence",
                    return_value=batch,
                ),
                mock.patch.object(cli, "apply_archive_evidence") as apply_mock,
                redirect_stderr(stderr),
            ):
                exit_code = cli.main(
                    [
                        "apply-archive-evidence",
                        "--legacy-db",
                        str(root / "legacy.sqlite3"),
                        "--legacy-root",
                        str(root / "legacy"),
                        "--evidence-root",
                        str(root / "evidence"),
                        "--expected-count",
                        "1",
                        "--expected-plan-sha256",
                        wrong_digest,
                        "--allow-write",
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("does not match the current plan digest", stderr.getvalue())
            self.assertFalse(apply_mock.called)
            self.assertFalse(target_db.exists())


if __name__ == "__main__":
    unittest.main()
