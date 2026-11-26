from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
import unittest
from typing import Any
from unittest import mock

from lecture_stt.downstream.lib import DownstreamConfig, SubjectRoute
from lecture_stt.storage_v2 import archive_evidence as archive_evidence_module
from lecture_stt.storage_v2.archive_evidence import (
    ArchiveEvidencePlanBatch,
    apply_archive_evidence,
    plan_archive_evidence,
    verify_archive_evidence,
)
from lecture_stt.storage_v2.archive_review import (
    apply_archive_review_promotion,
    plan_archive_review_promotion,
    read_archive_review_case,
)
from lecture_stt.storage_v2.importer import _legacy_database_snapshot



def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()



def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


class StorageV2ArchiveEvidenceTests(unittest.TestCase):
    def _config(self, root: Path) -> tuple[DownstreamConfig, Path, Path, dict[str, Path]]:
        gh_root = root / "gh"
        obs_root = root / "obs"
        gh_root.mkdir(parents=True, exist_ok=True)
        obs_root.mkdir(parents=True, exist_ok=True)
        config = DownstreamConfig(
            correction_dir=root / "legacy" / "03_correction",
            summary_dir=root / "legacy" / "04_summarize",
            gh_current_semester_root=gh_root,
            obsidian_semester_root=obs_root,
            db_path=root / "state" / "jobs.sqlite3",
            log_jsonl_path=root / "state" / "downstream.jsonl",
            lock_path=root / "state" / "downstream.lock",
            scan_interval_sec=1,
            stable_for_sec=1,
            subjects={"LC": SubjectRoute("01_logic_circuits", "01_logic_circuits", "논리회로")},
        )
        return config, gh_root, obs_root, {
            "route": config.subjects["LC"],
        }

    def _legacy_db(self, root: Path) -> Path:
        db_path = root / "legacy.sqlite3"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY)")
            conn.commit()
        return db_path

    def _payload(
        self,
        root: Path,
        row: dict[str, Any],
        legacy_db: Path,
        legacy_root: Path,
        *,
        historic_cases: int = 1,
    ) -> dict[str, Any]:
        return {
            "legacy_db": str(legacy_db),
            "legacy_root": str(legacy_root),
            "legacy_database_snapshot": _legacy_database_snapshot(legacy_db).as_dict(),
            "rows": [row],
            "summary": {
                "candidate_rows": historic_cases,
                "reported_rows": historic_cases,
                "truncated": False,
                "result_counts": {
                    "verified_delivered": 1,
                    "verified_correction_only": 0,
                    "manual_review": 0,
                    "blocked": 0,
                },
            },
        }

    def _plan(self, root: Path, row: dict[str, Any], *, historical_roots: dict[str, Path] | None = None) -> tuple[ArchiveEvidencePlanBatch, DownstreamConfig, Path, Path]:
        legacy_db = self._legacy_db(root)
        legacy_root = root / "legacy"
        legacy_root.mkdir(parents=True, exist_ok=True)
        config, gh_root, obs_root, _ = self._config(root)
        payload = self._payload(
            root,
            row,
            legacy_db=legacy_db,
            legacy_root=legacy_root,
            historic_cases=1,
        )
        with (
            mock.patch.object(
                archive_evidence_module,
                "_load_worker_config_from_module",
                return_value=config,
            ),
            mock.patch.object(
                archive_evidence_module,
                "reconcile_archive",
                return_value=payload,
            ),
        ):
            batch = plan_archive_evidence(
                legacy_db,
                legacy_root,
                historical_roots=historical_roots,
            )
        return batch, config, legacy_db, legacy_root

    def _row(
        self,
        route: SubjectRoute,
        stem: str,
        *,
        summary_status: str = "DELIVERED",
        correction_status: str = "DELIVERED",
        summary_classification: str = "verified_delivered",
        include_summary_current: bool = True,
        include_summary_recorded: bool = False,
        recorded_summary: Path | None = None,
        expected_summary_root: Path | None = None,
        include_recorded_text: bool = False,
        recorded_text: Path | None = None,
        summary_hash: str | None = None,
    ) -> dict[str, Any]:
        gh_root = route.gh_origin_dir(Path("tmp"))
        correction_txt = gh_root / f"{stem}.txt"
        correction_json = gh_root / f"{stem}.json"
        summary = gh_root / f"{stem}.md"
        issues: list[dict[str, Any]] = []
        row: dict[str, Any] = {
            "logical_stem": stem,
            "subject_abbr": "LC",
            "correction_status": correction_status,
            "summary_status": summary_status,
            "classification": summary_classification,
            "recorded_paths": {},
            "expected_paths": {
                "tuk_origin_txt": str(correction_txt),
                "tuk_origin_json": str(correction_json),
            },
            "hashes": {
                "correction_txt_sha256": "",
                "correction_json_sha256": "",
                "summary_md_sha256": _digest(summary) if summary_hash is None else summary_hash,
            },
            "flags": {},
            "shared_summary_destination": False,
            "issues": issues,
        }
        if include_summary_current:
            row["expected_paths"]["tuk_summary"] = str(summary)
        if include_recorded_text:
            row["recorded_paths"]["tuk_origin_txt"] = str(recorded_text or Path())
        if include_summary_recorded:
            row["recorded_paths"]["obsidian_summary"] = str(recorded_summary)
        return row

    @staticmethod
    def _snapshot_files(root: Path) -> dict[str, str]:
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def _promoted_case_with_evidence(
        self,
        root: Path,
        *,
        promote: bool = True,
    ) -> tuple[Path, Path, str]:
        config, _, _, context = self._config(root)
        route = context["route"]
        stem = "LC260801_999"
        correction_txt = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.txt"
        correction_json = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"
        summary = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.md"

        _write_text(correction_txt, "교정")
        _write_text(correction_json, '{"segments": []}')
        _write_text(summary, "# 요약")

        row = {
            "logical_stem": stem,
            "subject_abbr": "LC",
            "correction_status": "DELIVERED",
            "summary_status": "DELIVERED",
            "classification": "verified_delivered",
            "recorded_paths": {},
            "expected_paths": {
                "tuk_origin_txt": str(correction_txt),
                "tuk_origin_json": str(correction_json),
                "tuk_summary": str(summary),
            },
            "hashes": {
                "correction_txt_sha256": _digest(correction_txt),
                "correction_json_sha256": _digest(correction_json),
                "summary_md_sha256": _digest(summary),
            },
            "flags": {},
            "shared_summary_destination": False,
            "issues": [],
        }
        batch, _cfg, _legacy_db, _legacy_root = self._plan(root, row)

        target_db = root / "state" / "storage-v2.sqlite3"
        evidence_root = root / "evidence"
        apply_archive_evidence(
            batch,
            target_db_path=target_db,
            evidence_root=evidence_root,
        )

        with sqlite3.connect(target_db) as conn:
            conn.execute(
                """
                INSERT INTO recordings(
                    storage_key,
                    original_name_raw,
                    original_name_nfc,
                    source_relpath,
                    source_state
                )
                VALUES ('target_record', '원본.m4a', '원본.m4a', 'source/original.m4a', 'missing')
                """
            )
            conn.commit()
            case_key = conn.execute(
                "SELECT case_key FROM archive_evidence_cases LIMIT 1"
            ).fetchone()[0]

        selection_map = {
            item["artifact_kind"]: int(item["revision_id"])
            for item in read_archive_review_case(target_db, case_key)["revisions"]
        }
        plan = plan_archive_review_promotion(
            target_db,
            case_key,
            target_storage_key="target_record",
            selected_revisions=selection_map,
        )
        if promote:
            result = apply_archive_review_promotion(
                target_db,
                case_key,
                target_storage_key="target_record",
                selected_revisions=selection_map,
                expected_count=plan["expected_count"],
                expected_plan_sha256=plan["plan_sha256"],
                promotions_enabled=True,
                allow_write=True,
            )
            self.assertEqual(result["status"], "promoted")

        return target_db, evidence_root, case_key

    def test_plan_digest_changes_when_current_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            config, _, _, context = self._config(root)
            route = context["route"]
            stem = "LC260801_001"
            correction_txt = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.txt"
            correction_json = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"
            summary = route.gh_summary_dir(config.gh_current_semester_root) / f"{stem}.md"
            for path, value in (
                (correction_txt, "안정적인 텍스트"),
                (correction_json, '{"segments":[]}'),
                (summary, "# 요약"),
            ):
                _write_text(path, value)

            row = {
                "logical_stem": stem,
                "subject_abbr": "LC",
                "correction_status": "DELIVERED",
                "summary_status": "DELIVERED",
                "classification": "verified_delivered",
                "recorded_paths": {},
                "expected_paths": {
                    "tuk_origin_txt": str(correction_txt),
                    "tuk_origin_json": str(correction_json),
                    "tuk_summary": str(summary),
                },
                "hashes": {
                    "correction_txt_sha256": _digest(correction_txt),
                    "correction_json_sha256": _digest(correction_json),
                    "summary_md_sha256": _digest(summary),
                },
                "flags": {},
                "shared_summary_destination": False,
                "issues": [],
            }
            batch1, _cfg, _db, _legacy_root = self._plan(root, row)
            _ = batch1
            _write_text(correction_txt, "변경된 텍스트")
            batch2, _cfg2, _db2, _legacy_root2 = self._plan(root, row)
            self.assertNotEqual(batch1.plan_sha256, batch2.plan_sha256)

    def test_plan_marks_summary_as_unexpected_current_when_summary_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            config, _, _, context = self._config(root)
            route = context["route"]
            stem = "LC260801_002"
            correction_txt = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.txt"
            correction_json = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"
            summary = route.gh_summary_dir(config.gh_current_semester_root) / f"{stem}.md"
            _write_text(correction_txt, "교정")
            _write_text(correction_json, '{"segments":[]}')
            _write_text(summary, "# 요약 본문")

            row = {
                "logical_stem": stem,
                "subject_abbr": "LC",
                "correction_status": "DELIVERED",
                "summary_status": "MISSING",
                "classification": "verified_correction_only",
                "recorded_paths": {},
                "expected_paths": {
                    "tuk_origin_txt": str(correction_txt),
                    "tuk_origin_json": str(correction_json),
                    "tuk_summary": str(summary),
                },
                "hashes": {
                    "correction_txt_sha256": _digest(correction_txt),
                    "correction_json_sha256": _digest(correction_json),
                    "summary_md_sha256": _digest(summary),
                },
                "flags": {},
                "shared_summary_destination": False,
                "issues": [],
            }
            batch, _cfg, _db, _legacy_root = self._plan(root, row)
            case = batch.cases[0]
            summary_revision = next(
                (revision for revision in case.revisions if revision.artifact_kind == "summary_markdown"),
                None,
            )
            self.assertIsNotNone(summary_revision)
            self.assertEqual(summary_revision.observations[0].relationship, "unexpected_current")

    def test_plan_marks_summary_as_differs_when_summary_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            config, _, _, context = self._config(root)
            route = context["route"]
            stem = "LC260801_010"
            correction_txt = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.txt"
            correction_json = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"
            summary = route.gh_summary_dir(config.gh_current_semester_root) / f"{stem}.md"
            _write_text(correction_txt, "교정")
            _write_text(correction_json, '{"segments":[]}')
            _write_text(summary, "# 원본 요약")

            row = {
                "logical_stem": stem,
                "subject_abbr": "LC",
                "correction_status": "DELIVERED",
                "summary_status": "DELIVERED",
                "classification": "verified_delivered",
                "recorded_paths": {},
                "expected_paths": {
                    "tuk_origin_txt": str(correction_txt),
                    "tuk_origin_json": str(correction_json),
                    "tuk_summary": str(summary),
                },
                "hashes": {
                    "correction_txt_sha256": _digest(correction_txt),
                    "correction_json_sha256": _digest(correction_json),
                    "summary_md_sha256": "b" * 64,
                },
                "flags": {},
                "shared_summary_destination": False,
                "issues": [],
            }
            batch, _cfg, _db, _legacy_root = self._plan(root, row)
            case = batch.cases[0]
            summary_revision = next(
                (
                    revision for revision in case.revisions
                    if revision.artifact_kind == "summary_markdown"
                ),
                None,
            )
            self.assertIsNotNone(summary_revision)
            self.assertEqual(summary_revision.observations[0].relationship, "differs_from_ledger")
            self.assertTrue(case.can_apply)

    def test_plan_blocks_paths_with_traversal_outside_current_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            config, _, _, context = self._config(root)
            route = context["route"]
            stem = "LC260801_011"
            correction_txt = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.txt"
            correction_json = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"
            outside_source = root / "outside.txt"
            _write_text(correction_json, '{"segments":[]}')
            _write_text(outside_source, "outside")

            row = {
                "logical_stem": stem,
                "subject_abbr": "LC",
                "correction_status": "DELIVERED",
                "summary_status": "MISSING",
                "classification": "verified_correction_only",
                "recorded_paths": {},
                "expected_paths": {
                    "tuk_origin_txt": str(root / "gh" / ".." / "outside.txt"),
                    "tuk_origin_json": str(correction_json),
                },
                "hashes": {
                    "correction_txt_sha256": _digest(outside_source),
                    "correction_json_sha256": _digest(correction_json),
                    "summary_md_sha256": None,
                },
                "flags": {},
                "shared_summary_destination": False,
                "issues": [],
            }
            batch, _cfg, _db, _legacy_root = self._plan(root, row)
            self.assertFalse(batch.cases[0].can_apply)
            self.assertIn(
                "current_path_outside_root",
                {issue["code"] for issue in batch.cases[0].issues},
            )

    def test_plan_merges_current_and_historical_observations_for_same_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            config, _, obs_root, context = self._config(root)
            route = context["route"]
            stem = "LC260801_003"
            history_root = root / "legacy-archive"
            history_root.mkdir()
            summary_current = route.gh_summary_dir(config.gh_current_semester_root) / f"{stem}.md"
            summary_historical = route.obsidian_summary_dir(obs_root) / f"{stem}.md"
            historical_summary = history_root / "obsidian" / f"{stem}.md"
            _write_text(summary_current, "# 같은 요약")
            _write_text(summary_historical, "# 같은 요약")
            correction_txt = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.txt"
            _write_text(correction_txt, "교정")
            _write_text(route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json", '{"segments":[]}')
            _write_text(historical_summary, "# 같은 요약")

            row = {
                "logical_stem": stem,
                "subject_abbr": "LC",
                "correction_status": "DELIVERED",
                "summary_status": "DELIVERED",
                "classification": "verified_delivered",
                "recorded_paths": {
                    "obsidian_summary": str(historical_summary),
                },
                "expected_paths": {
                    "tuk_origin_txt": str(correction_txt),
                    "tuk_origin_json": str(route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"),
                    "tuk_summary": str(summary_current),
                },
                "hashes": {
                    "correction_txt_sha256": _digest(correction_txt),
                    "correction_json_sha256": _digest(route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"),
                    "summary_md_sha256": _digest(summary_current),
                },
                "flags": {},
                "shared_summary_destination": False,
                "issues": [],
            }
            batch, _cfg, _db, _legacy_root = self._plan(
                root,
                row,
                historical_roots={"hist": history_root},
            )
            case = batch.cases[0]
            summary_revision = next(
                (revision for revision in case.revisions if revision.artifact_kind == "summary_markdown"),
                None,
            )
            self.assertIsNotNone(summary_revision)
            self.assertEqual(len(summary_revision.observations), 2)

    def test_plan_ignores_recorded_paths_outside_historical_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            config, _, obs_root, context = self._config(root)
            route = context["route"]
            history_root = root / "history"
            history_root.mkdir()
            history_txt = history_root / "inside.txt"
            outside_source = root / "outside-source.txt"
            outside_recorded = root / "outside" / "obsidian.md"
            _write_text(history_txt, "in history")
            _write_text(outside_source, "outside")
            _write_text(outside_recorded, "outside")

            stem = "LC260801_004"
            correction_txt = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.txt"
            correction_json = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"
            _write_text(correction_txt, "교정")
            _write_text(correction_json, '{"segments":[]}')

            row = {
                "logical_stem": stem,
                "subject_abbr": "LC",
                "correction_status": "DELIVERED",
                "summary_status": "DELIVERED",
                "classification": "verified_delivered",
                "recorded_paths": {
                    "obsidian_summary": str(outside_recorded),
                    "tuk_origin_txt": str(history_txt),
                },
                "expected_paths": {
                    "tuk_origin_txt": str(correction_txt),
                    "tuk_origin_json": str(correction_json),
                },
                "hashes": {
                    "correction_txt_sha256": _digest(correction_txt),
                    "correction_json_sha256": _digest(correction_json),
                    "summary_md_sha256": _digest(history_txt),
                },
                "flags": {},
                "shared_summary_destination": False,
                "issues": [],
            }
            real_open = os.open

            def guarded_open(path: object, *args: object, **kwargs: object) -> int:
                if Path(path) == outside_recorded:
                    raise AssertionError(
                        "recorded path outside explicit historical roots was opened"
                    )
                return real_open(path, *args, **kwargs)

            with mock.patch.object(os, "open", side_effect=guarded_open):
                batch, _cfg, _db, _legacy_root = self._plan(
                    root,
                    row,
                    historical_roots={"hist": history_root},
                )
            issues = {issue["code"] for issue in batch.cases[0].issues}
            self.assertNotIn("historical_source_unsafe", issues)

    def test_plan_blocks_symlink_and_hardlink_current_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            stem = "LC260801_005"

            for mode in ("symlink", "hardlink"):
                with self.subTest(mode=mode):
                    batch_root = root / mode
                    config, _, _, context = self._config(batch_root)
                    route = context["route"]
                    correction_txt = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}_{mode}.txt"
                    correction_json = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}_{mode}.json"
                    if mode == "symlink":
                        source = batch_root / "source.txt"
                        _write_text(source, "원본")
                        correction_txt.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            correction_txt.symlink_to(source)
                        except OSError as exc:
                            self.skipTest(f"symlink unavailable: {exc}")
                    else:
                        source = batch_root / "source.txt"
                        _write_text(source, "원본")
                        correction_txt.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            correction_txt.hardlink_to(source)
                        except OSError as exc:
                            self.skipTest(f"hardlink unavailable: {exc}")

                    _write_text(correction_json, '{"segments":[]}')

                    row = {
                        "logical_stem": f"{stem}_{mode}",
                        "subject_abbr": "LC",
                        "correction_status": "DELIVERED",
                        "summary_status": "DELIVERED",
                        "classification": "verified_delivered",
                        "recorded_paths": {},
                        "expected_paths": {
                            "tuk_origin_txt": str(correction_txt),
                            "tuk_origin_json": str(correction_json),
                        },
                        "hashes": {
                            "correction_txt_sha256": _digest(source),
                            "correction_json_sha256": _digest(correction_json),
                            "summary_md_sha256": "",
                        },
                        "flags": {},
                        "shared_summary_destination": False,
                        "issues": [],
                    }
                    batch, _cfg, _db, _legacy_root = self._plan(batch_root, row)
                    self.assertFalse(batch.cases[0].can_apply)
                    self.assertTrue(
                        any(issue["code"] == "current_source_unsafe" for issue in batch.cases[0].issues)
                    )

    def test_apply_successful_import_is_skipped_on_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            config, _, _, context = self._config(root)
            route = context["route"]
            stem = "LC260801_006"
            correction_txt = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.txt"
            correction_json = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"
            _write_text(correction_txt, "교정")
            _write_text(correction_json, '{"segments":[]}')

            row = {
                "logical_stem": stem,
                "subject_abbr": "LC",
                "correction_status": "DELIVERED",
                "summary_status": "DELIVERED",
                "classification": "verified_delivered",
                "recorded_paths": {},
                "expected_paths": {
                    "tuk_origin_txt": str(correction_txt),
                    "tuk_origin_json": str(correction_json),
                },
                "hashes": {
                    "correction_txt_sha256": _digest(correction_txt),
                    "correction_json_sha256": _digest(correction_json),
                    "summary_md_sha256": None,
                },
                "flags": {},
                "shared_summary_destination": False,
                "issues": [],
            }
            batch, _cfg, _, _legacy_root = self._plan(root, row)
            target_db = root / "state" / "storage-v2.sqlite3"
            evidence_root = root / "evidence"
            results = apply_archive_evidence(
                batch,
                target_db_path=target_db,
                evidence_root=evidence_root,
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].action, "imported")

            verify = verify_archive_evidence(target_db, evidence_root)
            self.assertTrue(verify["ok"])
            second = apply_archive_evidence(
                batch,
                target_db_path=target_db,
                evidence_root=evidence_root,
            )
            self.assertEqual(second[0].action, "skipped")

    def test_apply_fails_if_source_mutates_after_planning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            config, _, _, context = self._config(root)
            route = context["route"]
            stem = "LC260801_007"
            correction_txt = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.txt"
            correction_json = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"
            _write_text(correction_txt, "교정")
            _write_text(correction_json, '{"segments":[]}')

            row = {
                "logical_stem": stem,
                "subject_abbr": "LC",
                "correction_status": "DELIVERED",
                "summary_status": "DELIVERED",
                "classification": "verified_delivered",
                "recorded_paths": {},
                "expected_paths": {
                    "tuk_origin_txt": str(correction_txt),
                    "tuk_origin_json": str(correction_json),
                },
                "hashes": {
                    "correction_txt_sha256": _digest(correction_txt),
                    "correction_json_sha256": _digest(correction_json),
                    "summary_md_sha256": None,
                },
                "flags": {},
                "shared_summary_destination": False,
                "issues": [],
            }
            batch, _cfg, _, _legacy_root = self._plan(root, row)
            _write_text(correction_txt, "변경")
            target_db = root / "state" / "storage-v2.sqlite3"
            evidence_root = root / "evidence"
            with self.assertRaisesRegex(RuntimeError, "changed after planning"):
                apply_archive_evidence(
                    batch,
                    target_db_path=target_db,
                    evidence_root=evidence_root,
                )
            self.assertFalse(target_db.exists())
            self.assertFalse(evidence_root.exists())

    def test_apply_recovered_when_capture_rows_disappear_but_files_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            config, _, _, context = self._config(root)
            route = context["route"]
            stem = "LC260801_008"
            correction_txt = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.txt"
            correction_json = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"
            _write_text(correction_txt, "교정")
            _write_text(correction_json, '{"segments":[]}')

            row = {
                "logical_stem": stem,
                "subject_abbr": "LC",
                "correction_status": "DELIVERED",
                "summary_status": "DELIVERED",
                "classification": "verified_delivered",
                "recorded_paths": {},
                "expected_paths": {
                    "tuk_origin_txt": str(correction_txt),
                    "tuk_origin_json": str(correction_json),
                },
                "hashes": {
                    "correction_txt_sha256": _digest(correction_txt),
                    "correction_json_sha256": _digest(correction_json),
                    "summary_md_sha256": None,
                },
                "flags": {},
                "shared_summary_destination": False,
                "issues": [],
            }
            batch, _cfg, _, _legacy_root = self._plan(root, row)
            target_db = root / "state" / "storage-v2.sqlite3"
            evidence_root = root / "evidence"

            apply_archive_evidence(
                batch,
                target_db_path=target_db,
                evidence_root=evidence_root,
            )

            with sqlite3.connect(target_db) as conn:
                conn.execute("DELETE FROM archive_evidence_observations")
                conn.execute("DELETE FROM archive_evidence_captures")
                conn.execute("DELETE FROM archive_evidence_revisions")
                conn.commit()

            recovered = apply_archive_evidence(
                batch,
                target_db_path=target_db,
                evidence_root=evidence_root,
            )
            self.assertEqual(recovered[0].action, "recovered")

    def test_verify_rejects_matching_manifest_with_non_metadata_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            config, _, _, context = self._config(root)
            route = context["route"]
            stem = "LC260801_010"
            correction_txt = (
                route.gh_origin_dir(config.gh_current_semester_root)
                / f"{stem}.txt"
            )
            correction_json = (
                route.gh_origin_dir(config.gh_current_semester_root)
                / f"{stem}.json"
            )
            _write_text(correction_txt, "교정")
            _write_text(correction_json, '{"segments":[]}')
            row = {
                "logical_stem": stem,
                "subject_abbr": "LC",
                "correction_status": "DELIVERED",
                "summary_status": "MISSING",
                "classification": "verified_correction_only",
                "recorded_paths": {},
                "expected_paths": {
                    "tuk_origin_txt": str(correction_txt),
                    "tuk_origin_json": str(correction_json),
                },
                "hashes": {
                    "correction_txt_sha256": _digest(correction_txt),
                    "correction_json_sha256": _digest(correction_json),
                    "summary_md_sha256": None,
                },
                "flags": {},
                "shared_summary_destination": False,
                "issues": [],
            }
            batch, _cfg, _, _legacy_root = self._plan(root, row)
            target_db = root / "state" / "storage-v2.sqlite3"
            evidence_root = root / "evidence"
            [result] = apply_archive_evidence(
                batch,
                target_db_path=target_db,
                evidence_root=evidence_root,
            )

            with sqlite3.connect(target_db) as conn:
                trigger_sql = str(
                    conn.execute(
                        """
                        SELECT sql
                        FROM sqlite_master
                        WHERE type = 'trigger'
                          AND name = 'archive_evidence_captures_capture_key_immutable'
                        """
                    ).fetchone()[0]
                )
                snapshot = json.loads(
                    str(
                        conn.execute(
                            "SELECT snapshot_json FROM archive_evidence_captures"
                        ).fetchone()[0]
                    )
                )
                snapshot["notes"] = "VERY SECRET TRANSCRIPT BODY " * 20
                conn.execute(
                    "DROP TRIGGER archive_evidence_captures_capture_key_immutable"
                )
                conn.execute(
                    "UPDATE archive_evidence_captures SET snapshot_json = ?",
                    (archive_evidence_module._canonical_json(snapshot),),
                )
                conn.execute(trigger_sql)
                conn.commit()

            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            manifest["snapshot"] = snapshot
            result.manifest_path.write_text(
                archive_evidence_module._canonical_json(manifest),
                encoding="utf-8",
            )

            verification = verify_archive_evidence(target_db, evidence_root)
            self.assertFalse(verification["ok"])
            manifest_issues = [
                issue
                for issue in verification["issues"]
                if issue["code"] == "archive_evidence_manifest_invalid"
            ]
            self.assertTrue(manifest_issues)
            self.assertIn("unexpected fields", manifest_issues[0]["message"])

            del snapshot["notes"]
            snapshot["hashes"]["correction_txt_sha256"] = (
                "VERY SECRET TRANSCRIPT BODY " * 2
            )
            with sqlite3.connect(target_db) as conn:
                trigger_sql = str(
                    conn.execute(
                        """
                        SELECT sql
                        FROM sqlite_master
                        WHERE type = 'trigger'
                          AND name = 'archive_evidence_captures_capture_key_immutable'
                        """
                    ).fetchone()[0]
                )
                conn.execute(
                    "DROP TRIGGER archive_evidence_captures_capture_key_immutable"
                )
                conn.execute(
                    "UPDATE archive_evidence_captures SET snapshot_json = ?",
                    (archive_evidence_module._canonical_json(snapshot),),
                )
                conn.execute(trigger_sql)
                conn.commit()
            manifest["snapshot"] = snapshot
            result.manifest_path.write_text(
                archive_evidence_module._canonical_json(manifest),
                encoding="utf-8",
            )

            verification = verify_archive_evidence(target_db, evidence_root)
            self.assertFalse(verification["ok"])
            manifest_issues = [
                issue
                for issue in verification["issues"]
                if issue["code"] == "archive_evidence_manifest_invalid"
            ]
            self.assertTrue(manifest_issues)
            self.assertIn("canonical SHA-256", manifest_issues[0]["message"])

    def test_verify_rejects_unknown_observation_metadata_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            config, _, _, context = self._config(root)
            route = context["route"]
            stem = "LC260801_011"
            correction_txt = (
                route.gh_origin_dir(config.gh_current_semester_root)
                / f"{stem}.txt"
            )
            correction_json = (
                route.gh_origin_dir(config.gh_current_semester_root)
                / f"{stem}.json"
            )
            _write_text(correction_txt, "교정")
            _write_text(correction_json, '{"segments":[]}')
            row = {
                "logical_stem": stem,
                "subject_abbr": "LC",
                "correction_status": "DELIVERED",
                "summary_status": "MISSING",
                "classification": "verified_correction_only",
                "recorded_paths": {},
                "expected_paths": {
                    "tuk_origin_txt": str(correction_txt),
                    "tuk_origin_json": str(correction_json),
                },
                "hashes": {
                    "correction_txt_sha256": _digest(correction_txt),
                    "correction_json_sha256": _digest(correction_json),
                    "summary_md_sha256": None,
                },
                "flags": {},
                "shared_summary_destination": False,
                "issues": [],
            }
            batch, _cfg, _, _legacy_root = self._plan(root, row)
            target_db = root / "state" / "storage-v2.sqlite3"
            evidence_root = root / "evidence"
            apply_archive_evidence(
                batch,
                target_db_path=target_db,
                evidence_root=evidence_root,
            )

            with sqlite3.connect(target_db) as conn:
                trigger_sql = str(
                    conn.execute(
                        """
                        SELECT sql
                        FROM sqlite_master
                        WHERE type = 'trigger'
                          AND name = 'archive_evidence_observations_immutable'
                        """
                    ).fetchone()[0]
                )
                metadata = json.loads(
                    str(
                        conn.execute(
                            """
                            SELECT metadata_json
                            FROM archive_evidence_observations
                            ORDER BY id
                            LIMIT 1
                            """
                        ).fetchone()[0]
                    )
                )
                metadata["notes"] = "LEAKED BODY " * 20
                conn.execute(
                    "DROP TRIGGER archive_evidence_observations_immutable"
                )
                conn.execute(
                    """
                    UPDATE archive_evidence_observations
                    SET metadata_json = ?
                    WHERE id = (
                        SELECT id
                        FROM archive_evidence_observations
                        ORDER BY id
                        LIMIT 1
                    )
                    """,
                    (archive_evidence_module._canonical_json(metadata),),
                )
                conn.execute(trigger_sql)
                conn.commit()

            verification = verify_archive_evidence(target_db, evidence_root)
            self.assertFalse(verification["ok"])
            manifest_issues = [
                issue
                for issue in verification["issues"]
                if issue["code"] == "archive_evidence_manifest_invalid"
            ]
            self.assertTrue(manifest_issues)
            self.assertIn("unexpected fields", manifest_issues[0]["message"])

    def test_verify_archive_evidence_detects_tampering_and_orphaned_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            config, _, _, context = self._config(root)
            route = context["route"]
            stem = "LC260801_009"
            correction_txt = route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.txt"
            _write_text(correction_txt, "교정")
            _write_text(route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json", '{"segments":[]}')

            row = {
                "logical_stem": stem,
                "subject_abbr": "LC",
                "correction_status": "DELIVERED",
                "summary_status": "DELIVERED",
                "classification": "verified_delivered",
                "recorded_paths": {},
                "expected_paths": {
                    "tuk_origin_txt": str(correction_txt),
                    "tuk_origin_json": str(route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"),
                },
                "hashes": {
                    "correction_txt_sha256": _digest(correction_txt),
                    "correction_json_sha256": _digest(route.gh_origin_dir(config.gh_current_semester_root) / f"{stem}.json"),
                    "summary_md_sha256": None,
                },
                "flags": {},
                "shared_summary_destination": False,
                "issues": [],
            }
            batch, _cfg, _, _legacy_root = self._plan(root, row)
            target_db = root / "state" / "storage-v2.sqlite3"
            evidence_root = root / "evidence"
            results = apply_archive_evidence(
                batch,
                target_db_path=target_db,
                evidence_root=evidence_root,
            )
            case = batch.cases[0]
            revision = case.revisions[0]
            first_revision_path = evidence_root / revision.path_rel

            with self.subTest("tampered_revision_file"):
                first_revision_path.write_text("tampered", encoding="utf-8")
                verification = verify_archive_evidence(target_db, evidence_root)
                self.assertFalse(verification["ok"])
                self.assertIn(
                    "archive_evidence_revision_invalid",
                    {issue["code"] for issue in verification["issues"]},
                )

            with self.subTest("tampered_manifest"):
                manifest_path = results[0].manifest_path
                manifest_path.write_text("{}", encoding="utf-8")
                verification = verify_archive_evidence(target_db, evidence_root)
                self.assertFalse(verification["ok"])
                self.assertIn(
                    "archive_evidence_manifest_invalid",
                    {issue["code"] for issue in verification["issues"]},
                )

            with self.subTest("hardlinked_revision"):
                replacement = root / "replacement.txt"
                replacement.write_text(first_revision_path.read_text(encoding="utf-8"), encoding="utf-8")
                first_revision_path.unlink()
                os.link(replacement, first_revision_path)
                verification = verify_archive_evidence(target_db, evidence_root)
                self.assertFalse(verification["ok"])
                self.assertIn(
                    "archive_evidence_revision_invalid",
                    {issue["code"] for issue in verification["issues"]},
                )

            with self.subTest("revision_without_observation"):
                with sqlite3.connect(target_db) as conn:
                    conn.execute("DELETE FROM archive_evidence_observations")
                    conn.commit()
            verify = verify_archive_evidence(target_db, evidence_root)
            self.assertFalse(verify["ok"])
            self.assertIn(
                "archive_evidence_revision_without_observation",
                {issue["code"] for issue in verify["issues"]},
            )

    def test_no_evidence_files_are_mutated_by_metadata_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()

            target_db, evidence_root, case_key = self._promoted_case_with_evidence(
                root,
                promote=False,
            )
            before = self._snapshot_files(evidence_root)

            selection_map = {
                item["artifact_kind"]: int(item["revision_id"])
                for item in read_archive_review_case(target_db, case_key)["revisions"]
            }
            plan = plan_archive_review_promotion(
                target_db,
                case_key,
                target_storage_key="target_record",
                selected_revisions=selection_map,
            )
            result = apply_archive_review_promotion(
                target_db,
                case_key,
                target_storage_key="target_record",
                selected_revisions=selection_map,
                expected_count=plan["expected_count"],
                expected_plan_sha256=plan["plan_sha256"],
                promotions_enabled=True,
                allow_write=True,
            )
            self.assertEqual(result["status"], "promoted")

            after = self._snapshot_files(evidence_root)
            self.assertEqual(before, after)

    def test_verify_archive_evidence_detects_tampered_canonical_selection_after_trigger_drop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()

            target_db, evidence_root, case_key = self._promoted_case_with_evidence(root)
            baseline = verify_archive_evidence(target_db, evidence_root)
            self.assertTrue(baseline["ok"])

            with sqlite3.connect(target_db) as conn:
                trigger_sql = str(
                    conn.execute(
                        """
                        SELECT sql
                        FROM sqlite_master
                        WHERE type = 'trigger'
                          AND name = 'archive_evidence_canonical_selections_immutable'
                        """
                    ).fetchone()[0]
                )
                conn.execute(
                    "DROP TRIGGER archive_evidence_canonical_selections_immutable"
                )
                conn.execute(
                    """
                    UPDATE archive_evidence_canonical_selections
                    SET promotion_plan_sha256 = 'f' || substr(printf('%064x', 0), 1, 63)
                    WHERE id = (
                        SELECT id FROM archive_evidence_canonical_selections
                        WHERE case_id = (
                            SELECT id FROM archive_evidence_cases WHERE case_key = ?
                        )
                        ORDER BY id
                        LIMIT 1
                    )
                    """,
                    (case_key,),
                )
                conn.execute(trigger_sql)
                conn.commit()

            verification = verify_archive_evidence(target_db, evidence_root)
            self.assertFalse(verification["ok"])
            self.assertIn(
                "archive_evidence_canonical_selection_invalid",
                {issue["code"] for issue in verification["issues"]},
            )


if __name__ == "__main__":
    unittest.main()
