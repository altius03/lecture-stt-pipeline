from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from lecture_stt.storage_v2 import cli
from lecture_stt.storage_v2.classification_materialization import (
    ClassificationMaterializationConflictError,
    ClassificationMaterializationPostCommitVerificationError,
    ClassificationMaterializationRecoveryRequiredError,
    ClassificationMaterializationWriteDisabledError,
    apply_classification_materialization,
    plan_classification_materialization,
)
from lecture_stt.storage_v2.timetable import (
    apply_classification_confirmation,
    apply_recording_classifications,
    apply_timetable_import,
    list_classification_proposals,
    plan_classification_confirmation,
    plan_recording_classifications,
    plan_timetable_import,
    read_classification_proposal,
)
from lecture_stt.storage_v2.verifier import verify_library
from tests import test_storage_v2_verifier as verifier_fixtures


class StorageV2ClassificationMaterializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path, self.records_root, self.manifest_path = (
            verifier_fixtures.StorageV2VerifierTests()._fixture(self.root)
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE recordings
                SET recorded_at = '2026-03-02T10:10:00+09:00'
                WHERE storage_key = 'rec_verifier'
                """
            )
            conn.commit()

        schedule_path = self.root / "시간표.csv"
        schedule_path.write_text(
            "\n".join(
                [
                    "학기,과목명,과목코드,요일,시작시간,종료시간,교시,강의실",
                    "2026-1,자료구조,CS201,월,10:00,11:15,2교시,E동 101호",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        timetable_plan = plan_timetable_import(schedule_path)
        apply_timetable_import(
            schedule_path,
            self.db_path,
            expected_count=timetable_plan["expected_count"],
            expected_plan_sha256=timetable_plan["plan_sha256"],
            allow_write=True,
        )
        classification_plan = plan_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_verifier"],
            margin_minutes=0,
        )
        apply_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_verifier"],
            margin_minutes=0,
            expected_count=classification_plan["expected_count"],
            expected_plan_sha256=classification_plan["plan_sha256"],
            allow_write=True,
        )
        self.proposal_id = int(
            list_classification_proposals(self.db_path)["proposals"][0]["id"]
        )
        confirmation_plan = plan_classification_confirmation(
            self.db_path,
            self.proposal_id,
        )
        apply_classification_confirmation(
            self.db_path,
            self.proposal_id,
            expected_count=confirmation_plan["expected_count"],
            expected_plan_sha256=confirmation_plan["plan_sha256"],
            confirmations_enabled=True,
            allow_write=True,
        )
        self.original_manifest = self.manifest_path.read_bytes()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _plan(self) -> dict[str, object]:
        return plan_classification_materialization(
            self.db_path,
            self.records_root,
            self.proposal_id,
        )

    def _apply(self, plan: dict[str, object]) -> dict[str, object]:
        return apply_classification_materialization(
            self.db_path,
            self.records_root,
            self.proposal_id,
            expected_count=1,
            expected_plan_sha256=str(plan["plan_sha256"]),
            materializations_enabled=True,
            allow_write=True,
        )

    def _journal_state(self) -> tuple[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT state, COUNT(*)
                FROM recording_classification_materializations
                """
            ).fetchone()
        assert row is not None
        return str(row[0]), int(row[1])

    def test_plan_is_read_only_root_bound_and_metadata_only(self) -> None:
        plan = self._plan()

        self.assertEqual(plan["mode"], "read_only")
        self.assertEqual(plan["expected_count"], 1)
        self.assertEqual(plan["materialization_state"], "not_prepared")
        self.assertEqual(
            plan["materialization"],
            "canonical_title_context_manifest",
        )
        self.assertEqual(
            plan["target"]["title"]["value"],
            "2026-03-02 자료구조 2교시",
        )
        self.assertEqual(plan["target"]["title"]["source"], "schedule")
        self.assertEqual(
            plan["target"]["context"]["source"],
            "schedule_import",
        )
        serialized = json.dumps(plan, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("source/original.m4a", serialized)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_classification_materializations"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(self.manifest_path.read_bytes(), self.original_manifest)

    def test_apply_requires_enable_allow_count_and_digest_guards(self) -> None:
        plan = self._plan()
        digest = str(plan["plan_sha256"])
        with self.assertRaises(ClassificationMaterializationWriteDisabledError):
            apply_classification_materialization(
                self.db_path,
                self.records_root,
                self.proposal_id,
                expected_count=1,
                expected_plan_sha256=digest,
                allow_write=True,
            )
        with self.assertRaises(ClassificationMaterializationWriteDisabledError):
            apply_classification_materialization(
                self.db_path,
                self.records_root,
                self.proposal_id,
                expected_count=1,
                expected_plan_sha256=digest,
                materializations_enabled=True,
            )
        with self.assertRaises(ClassificationMaterializationConflictError):
            apply_classification_materialization(
                self.db_path,
                self.records_root,
                self.proposal_id,
                expected_count=2,
                expected_plan_sha256=digest,
                materializations_enabled=True,
                allow_write=True,
            )
        with self.assertRaises(ClassificationMaterializationConflictError):
            apply_classification_materialization(
                self.db_path,
                self.records_root,
                self.proposal_id,
                expected_count=1,
                expected_plan_sha256="0" * 64,
                materializations_enabled=True,
                allow_write=True,
            )

        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_classification_materializations"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(self.manifest_path.read_bytes(), self.original_manifest)

    def test_apply_updates_canonical_revisions_manifest_and_detail(self) -> None:
        plan = self._plan()
        result = self._apply(plan)
        repeated = self._apply(plan)
        verification = verify_library(self.db_path, self.records_root)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        detail = read_classification_proposal(
            self.db_path,
            self.proposal_id,
        )

        self.assertEqual(result["action"], "materialized")
        self.assertTrue(result["canonical_metadata_changed"])
        self.assertEqual(repeated["action"], "skipped")
        self.assertFalse(repeated["canonical_metadata_changed"])
        self.assertTrue(verification["ok"], verification["issues"])
        self.assertEqual(
            manifest["title"],
            {
                "value": "2026-03-02 자료구조 2교시",
                "source": "schedule",
            },
        )
        self.assertEqual(manifest["context"]["type"], "class_session")
        self.assertEqual(manifest["context"]["source"], "schedule_import")
        self.assertTrue(detail["canonical_metadata_changed"])
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            journal = conn.execute(
                """
                SELECT *
                FROM recording_classification_materializations
                WHERE proposal_id = ?
                """,
                (self.proposal_id,),
            ).fetchone()
            self.assertIsNotNone(journal)
            assert journal is not None
            self.assertEqual(journal["state"], "applied")
            self.assertIsNotNone(journal["applied_at"])
            current_title = conn.execute(
                """
                SELECT title, title_source
                FROM recording_titles
                WHERE recording_id = ? AND is_current = 1
                """,
                (journal["recording_id"],),
            ).fetchone()
            selected_context = conn.execute(
                """
                SELECT context_type, source, context_json
                FROM recording_contexts
                WHERE recording_id = ? AND is_selected = 1
                """,
                (journal["recording_id"],),
            ).fetchone()
        assert current_title is not None and selected_context is not None
        self.assertEqual(current_title["title"], "2026-03-02 자료구조 2교시")
        self.assertEqual(current_title["title_source"], "schedule")
        self.assertEqual(selected_context["context_type"], "class_session")
        self.assertEqual(selected_context["source"], "schedule_import")
        provenance = json.loads(str(selected_context["context_json"]))
        self.assertEqual(provenance["proposal_id"], self.proposal_id)
        self.assertEqual(
            provenance["confirmation_plan_sha256"],
            plan["confirmation_plan_sha256"],
        )

    def test_replays_after_prepare_before_manifest_write(self) -> None:
        plan = self._plan()
        with mock.patch(
            "lecture_stt.storage_v2.classification_materialization."
            "_replace_record_manifest",
            side_effect=RuntimeError("simulated manifest write crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                self._apply(plan)

        self.assertEqual(self._journal_state(), ("prepared", 1))
        self.assertEqual(self.manifest_path.read_bytes(), self.original_manifest)
        pending = verify_library(self.db_path, self.records_root)
        self.assertFalse(pending["ok"])
        self.assertIn(
            "classification_materialization_recovery_required",
            {issue["code"] for issue in pending["issues"]},
        )

        recovered = self._apply(plan)
        self.assertEqual(recovered["action"], "recovered")
        self.assertEqual(self._journal_state(), ("applied", 1))
        self.assertTrue(
            verify_library(self.db_path, self.records_root)["ok"]
        )

    def test_replays_after_manifest_write_before_finalize(self) -> None:
        plan = self._plan()
        with mock.patch(
            "lecture_stt.storage_v2.classification_materialization."
            "_finalize_materialization",
            side_effect=RuntimeError("simulated finalize crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                self._apply(plan)

        self.assertEqual(self._journal_state(), ("prepared", 1))
        self.assertNotEqual(
            self.manifest_path.read_bytes(),
            self.original_manifest,
        )
        pending = verify_library(self.db_path, self.records_root)
        self.assertFalse(pending["ok"])
        self.assertIn(
            "classification_materialization_recovery_required",
            {issue["code"] for issue in pending["issues"]},
        )

        recovered = self._apply(plan)
        self.assertEqual(recovered["action"], "recovered")
        self.assertEqual(self._journal_state(), ("applied", 1))
        self.assertTrue(
            verify_library(self.db_path, self.records_root)["ok"]
        )

    def test_prepared_replay_rechecks_proposal_when_timestamp_is_reused(self) -> None:
        plan = self._plan()
        with mock.patch(
            "lecture_stt.storage_v2.classification_materialization."
            "_replace_record_manifest",
            side_effect=RuntimeError("simulated manifest write crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                self._apply(plan)

        with sqlite3.connect(self.db_path) as conn:
            original_updated_at = conn.execute(
                """
                SELECT updated_at
                FROM recording_classification_proposals
                WHERE id = ?
                """,
                (self.proposal_id,),
            ).fetchone()[0]
            conn.execute(
                """
                UPDATE recording_classification_proposals
                SET proposed_title = '변조된 제목'
                WHERE id = ?
                """,
                (self.proposal_id,),
            )
            conn.execute(
                """
                UPDATE recording_classification_proposals
                SET updated_at = ?
                WHERE id = ?
                """,
                (original_updated_at, self.proposal_id),
            )
            conn.commit()

        with self.assertRaisesRegex(
            ClassificationMaterializationConflictError,
            "metadata changed",
        ):
            self._apply(plan)
        self.assertEqual(self._journal_state(), ("prepared", 1))
        self.assertEqual(self.manifest_path.read_bytes(), self.original_manifest)

    def test_proposal_touch_trigger_is_recursive_trigger_safe(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA recursive_triggers = ON")
            conn.execute(
                """
                UPDATE recording_classification_proposals
                SET proposed_title = proposed_title
                WHERE id = ?
                """,
                (self.proposal_id,),
            )
            conn.commit()

    def test_post_commit_verification_failure_has_distinct_error(self) -> None:
        plan = self._plan()
        with mock.patch(
            "lecture_stt.storage_v2.classification_materialization.verify_library",
            side_effect=[
                {"ok": True, "issues": []},
                {
                    "ok": False,
                    "issues": [{"code": "simulated_post_commit_failure"}],
                },
            ],
        ):
            with self.assertRaises(
                ClassificationMaterializationPostCommitVerificationError
            ):
                self._apply(plan)

        self.assertEqual(self._journal_state(), ("applied", 1))
        self.assertTrue(
            verify_library(self.db_path, self.records_root)["ok"]
        )

    def test_preflight_verification_failure_does_not_create_recovery_state(
        self,
    ) -> None:
        with mock.patch(
            "lecture_stt.storage_v2.classification_materialization.verify_library",
            side_effect=RuntimeError("simulated preflight outage"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "simulated preflight outage",
            ):
                self._plan()

        self.assertEqual(self._journal_state(), ("None", 0))
        self.assertEqual(self.manifest_path.read_bytes(), self.original_manifest)

    def test_superseded_materialization_replay_remains_idempotent(self) -> None:
        first_plan = self._plan()
        self._apply(first_plan)

        second_schedule = self.root / "시간표-2026-2.csv"
        second_schedule.write_text(
            "\n".join(
                [
                    "학기,과목명,과목코드,요일,시작시간,종료시간,교시,강의실",
                    "2026-2,알고리즘,CS202,월,10:00,11:15,3교시,E동 102호",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        import_plan = plan_timetable_import(second_schedule)
        apply_timetable_import(
            second_schedule,
            self.db_path,
            expected_count=1,
            expected_plan_sha256=import_plan["plan_sha256"],
            allow_write=True,
        )
        classification_plan = plan_recording_classifications(
            self.db_path,
            semester="2026-2",
            storage_keys=["rec_verifier"],
            margin_minutes=0,
        )
        apply_recording_classifications(
            self.db_path,
            semester="2026-2",
            storage_keys=["rec_verifier"],
            margin_minutes=0,
            expected_count=1,
            expected_plan_sha256=classification_plan["plan_sha256"],
            allow_write=True,
        )
        second_proposal_id = int(
            next(
                proposal["id"]
                for proposal in list_classification_proposals(
                    self.db_path
                )["proposals"]
                if proposal["semester"] == "2026-2"
            )
        )
        confirmation = plan_classification_confirmation(
            self.db_path,
            second_proposal_id,
        )
        apply_classification_confirmation(
            self.db_path,
            second_proposal_id,
            expected_count=1,
            expected_plan_sha256=confirmation["plan_sha256"],
            confirmations_enabled=True,
            allow_write=True,
        )
        second_plan = plan_classification_materialization(
            self.db_path,
            self.records_root,
            second_proposal_id,
        )
        apply_classification_materialization(
            self.db_path,
            self.records_root,
            second_proposal_id,
            expected_count=1,
            expected_plan_sha256=second_plan["plan_sha256"],
            materializations_enabled=True,
            allow_write=True,
        )

        replay = self._apply(first_plan)
        self.assertEqual(replay["action"], "skipped")
        self.assertTrue(
            verify_library(self.db_path, self.records_root)["ok"]
        )
        manifest = json.loads(
            self.manifest_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["title"]["value"],
            "2026-03-02 알고리즘 3교시",
        )

    def test_cli_reports_recovery_required_separately_from_refusal(self) -> None:
        with mock.patch(
            "lecture_stt.storage_v2.cli.apply_classification_materialization",
            side_effect=ClassificationMaterializationRecoveryRequiredError(
                "prepared state exists"
            ),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = cli.main(
                    [
                        "apply-timetable-materialization",
                        "--v2-db",
                        str(self.db_path),
                        "--records-root",
                        str(self.records_root),
                        "--proposal-id",
                        str(self.proposal_id),
                        "--expected-count",
                        "1",
                        "--expected-plan-sha256",
                        "0" * 64,
                        "--enable-materialization",
                        "--allow-write",
                        "--json",
                    ]
                )

        self.assertEqual(exit_code, 3)
        self.assertIn("recovery-required", stderr.getvalue())

    def test_verifier_rejects_tampered_closed_journal_plan(self) -> None:
        plan = self._plan()
        self._apply(plan)
        with sqlite3.connect(self.db_path) as conn:
            trigger_sql = conn.execute(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'trigger'
                  AND name = 'recording_classification_materializations_identity_immutable'
                """
            ).fetchone()[0]
            conn.execute(
                """
                DROP TRIGGER
                recording_classification_materializations_identity_immutable
                """
            )
            stored = json.loads(
                conn.execute(
                    """
                    SELECT plan_json
                    FROM recording_classification_materializations
                    WHERE proposal_id = ?
                    """,
                    (self.proposal_id,),
                ).fetchone()[0]
            )
            stored["unexpected"] = "tampered"
            conn.execute(
                """
                UPDATE recording_classification_materializations
                SET plan_json = ?
                WHERE proposal_id = ?
                """,
                (
                    json.dumps(stored, ensure_ascii=False, sort_keys=True),
                    self.proposal_id,
                ),
            )
            conn.execute(str(trigger_sql))
            conn.commit()

        result = verify_library(self.db_path, self.records_root)
        self.assertFalse(result["ok"])
        self.assertIn(
            "classification_materialization_plan_invalid",
            {issue["code"] for issue in result["issues"]},
        )

    def test_verifier_rejects_invalid_journal_state_domain(self) -> None:
        plan = self._plan()
        self._apply(plan)
        with sqlite3.connect(self.db_path) as conn:
            trigger_sql = conn.execute(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'trigger'
                  AND name = 'recording_classification_materializations_state_transition'
                """
            ).fetchone()[0]
            conn.execute(
                """
                DROP TRIGGER
                recording_classification_materializations_state_transition
                """
            )
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                """
                UPDATE recording_classification_materializations
                SET state = 'tampered', applied_at = NULL
                WHERE proposal_id = ?
                """,
                (self.proposal_id,),
            )
            conn.execute("PRAGMA ignore_check_constraints = OFF")
            conn.execute(str(trigger_sql))
            conn.commit()

        result = verify_library(self.db_path, self.records_root)
        self.assertFalse(result["ok"])
        self.assertIn(
            "classification_materialization_state_invalid",
            {issue["code"] for issue in result["issues"]},
        )

    def test_cli_plan_and_apply_preserve_explicit_write_gates(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = cli.main(
                [
                    "plan-timetable-materialization",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--proposal-id",
                    str(self.proposal_id),
                    "--json",
                ]
            )
        self.assertEqual(exit_code, 0)
        plan = json.loads(stdout.getvalue())

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            refused = cli.main(
                [
                    "apply-timetable-materialization",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--proposal-id",
                    str(self.proposal_id),
                    "--expected-count",
                    "1",
                    "--expected-plan-sha256",
                    plan["plan_sha256"],
                    "--json",
                ]
            )
        self.assertEqual(refused, 2)
        self.assertIn("disabled", stderr.getvalue())
        self.assertEqual(self.manifest_path.read_bytes(), self.original_manifest)

        apply_stdout = io.StringIO()
        with redirect_stdout(apply_stdout):
            applied = cli.main(
                [
                    "apply-timetable-materialization",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--proposal-id",
                    str(self.proposal_id),
                    "--expected-count",
                    "1",
                    "--expected-plan-sha256",
                    plan["plan_sha256"],
                    "--enable-materialization",
                    "--allow-write",
                    "--json",
                ]
            )
        self.assertEqual(applied, 0)
        self.assertEqual(
            json.loads(apply_stdout.getvalue())["action"],
            "materialized",
        )
        self.assertTrue(
            verify_library(self.db_path, self.records_root)["ok"]
        )

    def test_fresh_plan_refuses_replaced_timetable(self) -> None:
        replacement = self.root / "replacement.csv"
        replacement.write_text(
            "\n".join(
                [
                    "학기,과목명,과목코드,요일,시작시간,종료시간,교시,강의실",
                    "2026-1,알고리즘,CS202,월,15:00,16:15,6교시,E동 102호",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        replacement_plan = plan_timetable_import(replacement)
        apply_timetable_import(
            replacement,
            self.db_path,
            expected_count=replacement_plan["expected_count"],
            expected_plan_sha256=replacement_plan["plan_sha256"],
            allow_write=True,
        )

        with self.assertRaisesRegex(
            ClassificationMaterializationConflictError,
            "Timetable changed",
        ):
            self._plan()
        self.assertEqual(self.manifest_path.read_bytes(), self.original_manifest)

    def test_plan_refuses_hard_linked_manifest(self) -> None:
        hard_link = self.root / "manifest-hard-link.json"
        os.link(self.manifest_path, hard_link)

        with self.assertRaisesRegex(
            ClassificationMaterializationConflictError,
            "verify cleanly",
        ):
            self._plan()
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_classification_materializations"
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
