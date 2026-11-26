from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lecture_stt.downstream.lib import DownstreamConfig, SubjectRoute
from lecture_stt.storage_v2 import archive_reconciliation as reconciliation


class StorageV2ArchiveReconciliationTests(unittest.TestCase):
    def _config(
        self,
        root: Path,
        *,
        route: SubjectRoute,
        share_legacy_and_gh: bool = False,
        share_obsidian_root: bool = False,
    ) -> DownstreamConfig:
        gh_root = root / "gh"
        obs_root = gh_root if share_obsidian_root else root / "obsidian"
        gh_root.mkdir(parents=True, exist_ok=True)
        obs_root.mkdir(parents=True, exist_ok=True)
        return DownstreamConfig(
            correction_dir=root,
            summary_dir=root,
            gh_current_semester_root=gh_root,
            obsidian_semester_root=obs_root,
            db_path=root,
            log_jsonl_path=root,
            lock_path=root,
            scan_interval_sec=0,
            stable_for_sec=0,
            subjects={"AB": route},
        )

    def _delivery_paths(self, root: Path, stem: str, *, route: SubjectRoute) -> dict[str, Path]:
        gh_base = root / "gh"
        obs_base = root / "obsidian"
        return {
            "correction_txt": route.gh_origin_dir(Path(".")) / f"{stem}.txt",
            "correction_json": route.gh_origin_dir(Path(".")) / f"{stem}.json",
            "summary": route.gh_summary_dir(Path(".")) / f"{stem}.md",
            "obs_summary": route.obsidian_summary_dir(Path(".")) / f"{stem}.md",
        }

    def _create_legacy_root(self, root: Path, *, exists_ok: bool = False) -> Path:
        legacy_root = root / "legacy_root"
        legacy_root.joinpath("03_correction").mkdir(parents=True, exist_ok=exists_ok)
        legacy_root.joinpath("04_summarize").mkdir(parents=True, exist_ok=exists_ok)
        return legacy_root

    def _create_schema(self, db_path: Path) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE deliveries (
                    canonical_base TEXT,
                    logical_stem TEXT,
                    subject_abbr TEXT,
                    correction_status TEXT,
                    summary_status TEXT,
                    correction_txt_path TEXT,
                    correction_json_path TEXT,
                    summary_md_path TEXT,
                    tuk_origin_txt_path TEXT,
                    tuk_origin_json_path TEXT,
                    tuk_summary_path TEXT,
                    obsidian_summary_path TEXT,
                    correction_txt_sha256 TEXT,
                    correction_json_sha256 TEXT,
                    summary_md_sha256 TEXT,
                    tuk_origin_done INTEGER,
                    tuk_summary_done INTEGER,
                    obsidian_done INTEGER,
                    source_job_id INTEGER,
                    correction_txt_source TEXT,
                    correction_json_source TEXT,
                    summary_md_source TEXT
                )
                """
            )
            conn.execute("CREATE TABLE jobs(canonical_base TEXT)")

    def _insert_rows(self, conn: sqlite3.Connection, rows) -> None:
        for row in rows:
            conn.execute(
                """
                INSERT INTO deliveries(
                    canonical_base,
                    logical_stem,
                    subject_abbr,
                    correction_status,
                    summary_status,
                    correction_txt_path,
                    correction_json_path,
                    summary_md_path,
                    tuk_origin_txt_path,
                    tuk_origin_json_path,
                    tuk_summary_path,
                    obsidian_summary_path,
                    correction_txt_sha256,
                    correction_json_sha256,
                    summary_md_sha256,
                    tuk_origin_done,
                    tuk_summary_done,
                    obsidian_done,
                    source_job_id,
                    correction_txt_source,
                    correction_json_source,
                    summary_md_source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row.get("canonical_base"),
                    row.get("logical_stem"),
                    row.get("subject_abbr"),
                    row.get("correction_status"),
                    row.get("summary_status"),
                    row.get("correction_txt_path"),
                    row.get("correction_json_path"),
                    row.get("summary_md_path"),
                    row.get("tuk_origin_txt_path"),
                    row.get("tuk_origin_json_path"),
                    row.get("tuk_summary_path"),
                    row.get("obsidian_summary_path"),
                    row.get("correction_txt_sha256"),
                    row.get("correction_json_sha256"),
                    row.get("summary_md_sha256"),
                    int(row.get("tuk_origin_done", 0) or 0),
                    int(row.get("tuk_summary_done", 0) or 0),
                    int(row.get("obsidian_done", 0) or 0),
                    row.get("source_job_id"),
                    row.get("correction_txt_source"),
                    row.get("correction_json_source"),
                    row.get("summary_md_source"),
                ),
            )

    def _make_db(self, db_path: Path, rows, job_bases=()) -> None:
        self._create_schema(db_path)
        with sqlite3.connect(db_path) as conn:
            self._insert_rows(conn, rows)
            for base in job_bases:
                conn.execute("INSERT INTO jobs(canonical_base) VALUES (?)", (base,))
            conn.commit()

    def _digest(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _run(
        self,
        root: Path,
        rows,
        *,
        job_bases=(),
        route: SubjectRoute,
        legacy_root: Path | None = None,
        share_obsidian_root: bool = False,
        limit: int = 100,
    ):
        if legacy_root is None:
            legacy_root = self._create_legacy_root(root, exists_ok=True)
        db_path = root / "legacy.sqlite3"
        self._make_db(db_path, rows, job_bases=job_bases)
        config = self._config(
            root,
            route=route,
            share_obsidian_root=share_obsidian_root,
        )
        return reconciliation.reconcile_archive(
            db_path,
            legacy_root,
            downstream_config=config,
            limit=limit,
        )

    def _write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value", encoding="utf-8")

    def _row(self, stem: str, route: SubjectRoute, *, correction_status: str, summary_status: str) -> dict[str, object]:
        return {
            "canonical_base": stem,
            "logical_stem": stem,
            "subject_abbr": "AB",
            "correction_status": correction_status,
            "summary_status": summary_status,
        }

    def test_verified_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            route = SubjectRoute("course_ab", "course_ab_obsidian", "강의")
            stem = "AB1001"
            paths = self._delivery_paths(root, stem, route=route)
            for rel in [paths["correction_txt"], paths["correction_json"], paths["summary"], paths["obs_summary"]]:
                self._write(root / "gh" / rel)
                self._write(root / "obsidian" / rel)

            row = {
                **self._row(stem, route, correction_status="DELIVERED", summary_status="DELIVERED"),
                "correction_txt_path": str(paths["correction_txt"]),
                "correction_json_path": str(paths["correction_json"]),
                "summary_md_path": str(paths["summary"]),
                "tuk_origin_txt_path": str(paths["correction_txt"]),
                "tuk_origin_json_path": str(paths["correction_json"]),
                "tuk_summary_path": str(paths["summary"]),
                "obsidian_summary_path": str(paths["obs_summary"]),
                "correction_txt_sha256": self._digest(root / "gh" / paths["correction_txt"]),
                "correction_json_sha256": self._digest(root / "gh" / paths["correction_json"]),
                "summary_md_sha256": self._digest(root / "gh" / paths["summary"]),
                "tuk_origin_done": 1,
                "tuk_summary_done": 1,
                "obsidian_done": 1,
            }

            result = self._run(root, [row], route=route)
            item = result["rows"][0]
            self.assertEqual(item["classification"], "verified_delivered")
            self.assertEqual(item["issues"], [])

    def test_verified_correction_only_when_summary_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            route = SubjectRoute("course_ab", "course_ab_obsidian", "강의")
            stem = "AB1002"
            paths = self._delivery_paths(root, stem, route=route)
            for rel in [paths["correction_txt"], paths["correction_json"]]:
                self._write(root / "gh" / rel)

            row = {
                **self._row(stem, route, correction_status="DELIVERED", summary_status="MISSING"),
                "correction_txt_path": str(paths["correction_txt"]),
                "correction_json_path": str(paths["correction_json"]),
                "summary_md_path": None,
                "tuk_origin_txt_path": str(paths["correction_txt"]),
                "tuk_origin_json_path": str(paths["correction_json"]),
                "tuk_summary_path": None,
                "obsidian_summary_path": None,
                "correction_txt_sha256": self._digest(root / "gh" / paths["correction_txt"]),
                "correction_json_sha256": self._digest(root / "gh" / paths["correction_json"]),
                "summary_md_sha256": None,
                "tuk_origin_done": 1,
                "tuk_summary_done": 0,
                "obsidian_done": 0,
            }

            result = self._run(root, [row], route=route)
            self.assertEqual(result["rows"][0]["classification"], "verified_correction_only")

    def test_recorded_historical_path_outside_root_falls_back_info_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            route = SubjectRoute("course_ab", "course_ab_obsidian", "강의")
            stem = "AB1003"
            paths = self._delivery_paths(root, stem, route=route)
            self._write(root / "gh" / paths["correction_txt"])
            self._write(root / "gh" / paths["correction_json"])
            historical_root = root / "historical_archive"

            row = {
                **self._row(stem, route, correction_status="DELIVERED", summary_status="MISSING"),
                "correction_txt_path": None,
                "correction_json_path": None,
                "summary_md_path": None,
                "tuk_origin_txt_path": str(historical_root / f"{stem}.txt"),
                "tuk_origin_json_path": str(historical_root / f"{stem}.json"),
                "tuk_summary_path": None,
                "obsidian_summary_path": None,
                "correction_txt_sha256": self._digest(root / "gh" / paths["correction_txt"]),
                "correction_json_sha256": self._digest(root / "gh" / paths["correction_json"]),
                "summary_md_sha256": None,
                "tuk_origin_done": 1,
                "tuk_summary_done": 0,
                "obsidian_done": 0,
            }

            result = self._run(root, [row], route=route)
            item = result["rows"][0]
            self.assertEqual(item["classification"], "verified_correction_only")
            relocation_issues = [
                issue
                for issue in item["issues"]
                if issue["code"] == "destination_relocated"
            ]
            self.assertEqual(len(relocation_issues), 2)
            self.assertTrue(
                all(issue["severity"] == "info" for issue in relocation_issues)
            )
            self.assertEqual(
                item["observed_artifacts"]["correction_txt"]["path"],
                str((root / "gh" / paths["correction_txt"]).resolve()),
            )

    def test_hash_mismatch_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            route = SubjectRoute("course_ab", "course_ab_obsidian", "강의")
            stem = "AB1004"
            paths = self._delivery_paths(root, stem, route=route)
            for rel in [paths["correction_txt"], paths["correction_json"], paths["summary"], paths["obs_summary"]]:
                self._write(root / "gh" / rel)
                self._write(root / "obsidian" / rel)

            row = {
                **self._row(stem, route, correction_status="DELIVERED", summary_status="DELIVERED"),
                "correction_txt_path": str(paths["correction_txt"]),
                "correction_json_path": str(paths["correction_json"]),
                "summary_md_path": str(paths["summary"]),
                "tuk_origin_txt_path": str(paths["correction_txt"]),
                "tuk_origin_json_path": str(paths["correction_json"]),
                "tuk_summary_path": str(paths["summary"]),
                "obsidian_summary_path": str(paths["obs_summary"]),
                "correction_txt_sha256": "0" * 64,
                "correction_json_sha256": self._digest(root / "gh" / paths["correction_json"]),
                "summary_md_sha256": self._digest(root / "gh" / paths["summary"]),
                "tuk_origin_done": 1,
                "tuk_summary_done": 1,
                "obsidian_done": 1,
            }

            result = self._run(root, [row], route=route)
            item = result["rows"][0]
            self.assertEqual(item["classification"], "blocked")
            self.assertIn("correction_txt_destination_hash_mismatch", {issue["code"] for issue in item["issues"]})

    def test_unexpected_summary_present_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            route = SubjectRoute("course_ab", "course_ab_obsidian", "강의")
            stem = "AB1005"
            paths = self._delivery_paths(root, stem, route=route)
            self._write(root / "gh" / paths["summary"])
            self._write(root / "obsidian" / paths["obs_summary"])

            row = {
                **self._row(stem, route, correction_status="MISSING", summary_status="MISSING"),
                "correction_txt_path": None,
                "correction_json_path": None,
                "summary_md_path": str(paths["summary"]),
                "tuk_origin_txt_path": None,
                "tuk_origin_json_path": None,
                "tuk_summary_path": str(paths["summary"]),
                "obsidian_summary_path": str(paths["obs_summary"]),
                "correction_txt_sha256": None,
                "correction_json_sha256": None,
                "summary_md_sha256": self._digest(root / "gh" / paths["summary"]),
                "tuk_origin_done": 0,
                "tuk_summary_done": 0,
                "obsidian_done": 0,
            }

            result = self._run(root, [row], route=route)
            item = result["rows"][0]
            self.assertEqual(item["classification"], "blocked")
            self.assertIn("summary_expected_tuk_destination_present", {issue["code"] for issue in item["issues"]})

    def test_legacy_source_reappears_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            route = SubjectRoute("course_ab", "course_ab_obsidian", "강의")
            stem = "AB1006"
            paths = self._delivery_paths(root, stem, route=route)
            for rel in [paths["correction_txt"], paths["correction_json"], paths["summary"], paths["obs_summary"]]:
                self._write(root / "gh" / rel)
                self._write(root / "obsidian" / rel)

            legacy_root = self._create_legacy_root(root)
            (legacy_root / paths["correction_txt"]).parent.mkdir(parents=True, exist_ok=True)
            (legacy_root / paths["correction_json"]).parent.mkdir(parents=True, exist_ok=True)
            (legacy_root / paths["summary"]).parent.mkdir(parents=True, exist_ok=True)
            (legacy_root / paths["correction_txt"]).write_text("legacy", encoding="utf-8")
            (legacy_root / paths["correction_json"]).write_text("legacy", encoding="utf-8")
            (legacy_root / paths["summary"]).write_text("legacy", encoding="utf-8")

            row = {
                **self._row(stem, route, correction_status="DELIVERED", summary_status="DELIVERED"),
                "correction_txt_path": str(paths["correction_txt"]),
                "correction_json_path": str(paths["correction_json"]),
                "summary_md_path": str(paths["summary"]),
                "tuk_origin_txt_path": str(paths["correction_txt"]),
                "tuk_origin_json_path": str(paths["correction_json"]),
                "tuk_summary_path": str(paths["summary"]),
                "obsidian_summary_path": str(paths["obs_summary"]),
                "correction_txt_sha256": self._digest(root / "gh" / paths["correction_txt"]),
                "correction_json_sha256": self._digest(root / "gh" / paths["correction_json"]),
                "summary_md_sha256": self._digest(root / "gh" / paths["summary"]),
                "tuk_origin_done": 1,
                "tuk_summary_done": 1,
                "obsidian_done": 1,
            }

            result = self._run(root, [row], route=route, legacy_root=legacy_root)
            item = result["rows"][0]
            codes = {issue["code"] for issue in item["issues"]}
            self.assertEqual(item["classification"], "blocked")
            self.assertIn("legacy_correction_txt_source_present", codes)
            self.assertIn("legacy_correction_json_source_present", codes)
            self.assertIn("legacy_summary_source_present", codes)

    def test_hash_malformed_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            route = SubjectRoute("course_ab", "course_ab_obsidian", "강의")
            stem = "AB1007"
            paths = self._delivery_paths(root, stem, route=route)
            for rel in [paths["correction_txt"], paths["correction_json"], paths["summary"], paths["obs_summary"]]:
                self._write(root / "gh" / rel)

            row = {
                **self._row(stem, route, correction_status="DELIVERED", summary_status="DELIVERED"),
                "correction_txt_path": str(paths["correction_txt"]),
                "correction_json_path": str(paths["correction_json"]),
                "summary_md_path": str(paths["summary"]),
                "tuk_origin_txt_path": str(paths["correction_txt"]),
                "tuk_origin_json_path": str(paths["correction_json"]),
                "tuk_summary_path": str(paths["summary"]),
                "obsidian_summary_path": str(paths["obs_summary"]),
                "correction_txt_sha256": "not-a-hash",
                "correction_json_sha256": self._digest(root / "gh" / paths["correction_json"]),
                "summary_md_sha256": self._digest(root / "gh" / paths["summary"]),
                "tuk_origin_done": 1,
                "tuk_summary_done": 1,
                "obsidian_done": 1,
            }

            result = self._run(root, [row], route=route)
            item = result["rows"][0]
            self.assertEqual(item["classification"], "blocked")
            self.assertIn("correction_txt_invalid", {issue["code"] for issue in item["issues"]})

    def test_hardlink_symlink_root_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            route = SubjectRoute("course_ab", "course_ab_obsidian", "강의")
            hard_stem = "AB1008h"
            escape_stem = "AB1008e"
            hard_paths = self._delivery_paths(root, hard_stem, route=route)
            escape_paths = self._delivery_paths(root, escape_stem, route=route)
            source = root / "gh" / hard_paths["correction_txt"].parent / "source.txt"
            self._write(source)

            hard_target = root / "gh" / hard_paths["correction_txt"]
            if hard_target.exists():
                hard_target.unlink()
            os.link(source, hard_target)

            for rel in [hard_paths["correction_json"], escape_paths["correction_txt"], escape_paths["correction_json"]]:
                self._write(root / "gh" / rel)
                self._write(root / "obsidian" / rel)

            alias = root / "gh" / "alias_root"
            alias_target = route.gh_origin_dir(Path("."))
            alias.symlink_to(root / "gh" / alias_target, target_is_directory=True)
            symlinked_json = alias / f"{escape_stem}.json"
            self._write(symlinked_json)

            rows = [
                {
                    **self._row(hard_stem, route, correction_status="DELIVERED", summary_status="MISSING"),
                    "correction_txt_path": str(hard_paths["correction_txt"]),
                    "correction_json_path": str(hard_paths["correction_json"]),
                    "summary_md_path": None,
                    "tuk_origin_txt_path": str(hard_paths["correction_txt"]),
                    "tuk_origin_json_path": str(hard_paths["correction_json"]),
                    "tuk_summary_path": None,
                    "obsidian_summary_path": None,
                    "correction_txt_sha256": self._digest(source),
                    "correction_json_sha256": self._digest(root / "gh" / hard_paths["correction_json"]),
                    "summary_md_sha256": None,
                    "tuk_origin_done": 1,
                    "tuk_summary_done": 0,
                    "obsidian_done": 0,
                },
                {
                    **self._row(escape_stem, route, correction_status="DELIVERED", summary_status="MISSING"),
                    "correction_txt_path": "../outside.txt",
                    "correction_json_path": str(Path("alias_root") / f"{escape_stem}.json"),
                    "summary_md_path": None,
                    "tuk_origin_txt_path": "../outside.txt",
                    "tuk_origin_json_path": str(Path("alias_root") / f"{escape_stem}.json"),
                    "tuk_summary_path": None,
                    "obsidian_summary_path": None,
                    "correction_txt_sha256": "0" * 64,
                    "correction_json_sha256": "0" * 64,
                    "summary_md_sha256": None,
                    "tuk_origin_done": 1,
                    "tuk_summary_done": 0,
                    "obsidian_done": 0,
                },
            ]

            result = self._run(root, rows, route=route)
            codes = [
                issue["code"]
                for row in result["rows"]
                for issue in row["issues"]
            ]
            self.assertIn("correction_txt_destination_hardlinked", set(codes))
            self.assertIn("correction_json_destination_recorded_symlink", set(codes))
            self.assertTrue(
                "correction_txt_destination_recorded_path_traversal" in set(codes)
                or "correction_txt_destination_path_traversal" in set(codes)
            )

    def test_owned_and_matched_rows_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            route = SubjectRoute("course_ab", "course_ab_obsidian", "강의")
            stem = "AB1009"
            paths = self._delivery_paths(root, stem, route=route)
            for rel in [paths["correction_txt"], paths["correction_json"], paths["summary"], paths["obs_summary"]]:
                self._write(root / "gh" / rel)

            owned = {
                **self._row(stem, route, correction_status="DELIVERED", summary_status="MISSING"),
                "source_job_id": 1,
                "correction_txt_path": str(paths["correction_txt"]),
                "correction_json_path": str(paths["correction_json"]),
                "summary_md_path": None,
                "tuk_origin_txt_path": str(paths["correction_txt"]),
                "tuk_origin_json_path": str(paths["correction_json"]),
                "tuk_summary_path": None,
                "obsidian_summary_path": None,
                "correction_txt_sha256": self._digest(root / "gh" / paths["correction_txt"]),
                "correction_json_sha256": self._digest(root / "gh" / paths["correction_json"]),
                "summary_md_sha256": None,
                "tuk_origin_done": 1,
            }
            matched = {
                **owned,
                "source_job_id": None,
                "logical_stem": stem,
                "canonical_base": "matched",
            }
            unmatched = {
                **owned,
                "source_job_id": None,
                "logical_stem": "AB1010",
                "canonical_base": "unmatched",
            }

            result = self._run(root, [owned, matched, unmatched], route=route, job_bases=[stem])
            self.assertEqual(result["summary"]["candidate_rows"], 1)
            self.assertEqual(result["excluded_counts"]["owned_delivery_rows"], 1)
            self.assertEqual(result["excluded_counts"]["ownerless_matched_rows"], 1)
            self.assertEqual(result["rows"][0]["logical_stem"], "AB1010")

    def test_same_gh_and_obsidian_summary_path_marked_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            shared_route = SubjectRoute(
                gh_course_dir="shared",
                obsidian_course_dir="shared/06_lecture_notes",
                display_name="shared",
                obsidian_note_dir="01_summarize",
            )
            stem = "AB1011"
            paths = self._delivery_paths(root, stem, route=shared_route)
            for rel in [paths["correction_txt"], paths["correction_json"], paths["summary"], paths["obs_summary"]]:
                self._write(root / "gh" / rel)
                self._write(root / "obsidian" / rel)

            row = {
                **self._row(stem, shared_route, correction_status="DELIVERED", summary_status="DELIVERED"),
                "correction_txt_path": str(paths["correction_txt"]),
                "correction_json_path": str(paths["correction_json"]),
                "summary_md_path": str(paths["summary"]),
                "tuk_origin_txt_path": str(paths["correction_txt"]),
                "tuk_origin_json_path": str(paths["correction_json"]),
                "tuk_summary_path": str(paths["summary"]),
                "obsidian_summary_path": str(paths["obs_summary"]),
                "correction_txt_sha256": self._digest(root / "gh" / paths["correction_txt"]),
                "correction_json_sha256": self._digest(root / "gh" / paths["correction_json"]),
                "summary_md_sha256": self._digest(root / "gh" / paths["summary"]),
                "tuk_origin_done": 1,
                "tuk_summary_done": 1,
                "obsidian_done": 1,
            }

            config_route = shared_route
            result = self._run(
                root,
                [row],
                route=config_route,
                share_obsidian_root=True,
            )
            item = result["rows"][0]
            self.assertEqual(item["classification"], "verified_delivered")
            self.assertTrue(item["shared_summary_destination"])

    def test_scan_cap_and_read_only_db_stability(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            route = SubjectRoute("course_ab", "course_ab_obsidian", "강의")
            rows = []
            for i in range(3):
                stem = f"AB{i:04d}"
                rows.append(
                    {
                        "canonical_base": stem,
                        "logical_stem": stem,
                        "subject_abbr": "AB",
                        "correction_status": "MISSING",
                        "summary_status": "MISSING",
                        "correction_txt_path": None,
                        "correction_json_path": None,
                        "summary_md_path": None,
                        "tuk_origin_txt_path": None,
                        "tuk_origin_json_path": None,
                        "tuk_summary_path": None,
                        "obsidian_summary_path": None,
                        "tuk_origin_done": 0,
                        "tuk_summary_done": 0,
                        "obsidian_done": 0,
                    }
                )

            legacy_root = self._create_legacy_root(root)
            db_path = root / "legacy.sqlite3"
            self._make_db(db_path, rows)
            config = self._config(root, route=route)

            with mock.patch.object(reconciliation, "MAX_SCANNED_DELIVERY_ROWS", 2):
                with self.assertRaisesRegex(RuntimeError, "deliveries row count exceeds"):
                    reconciliation.reconcile_archive(
                        db_path,
                        legacy_root,
                        downstream_config=config,
                    )

            with mock.patch.object(reconciliation, "MAX_SCANNED_CANDIDATE_ROWS", 2):
                with self.assertRaisesRegex(RuntimeError, "candidate row count exceeds"):
                    reconciliation.reconcile_archive(db_path, legacy_root, downstream_config=config)

            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT INTO jobs(canonical_base) VALUES (?)",
                    ("AB-existing",),
                )
            with mock.patch.object(reconciliation, "MAX_SCANNED_JOB_ROWS", 0):
                with self.assertRaisesRegex(RuntimeError, "jobs row count exceeds"):
                    reconciliation.reconcile_archive(
                        db_path,
                        legacy_root,
                        downstream_config=config,
                    )

            with mock.patch.object(reconciliation, "_assert_legacy_database_stable") as stable_mock:
                stable_mock.side_effect = [None, RuntimeError("changed")]
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    reconciliation.reconcile_archive(db_path, legacy_root, downstream_config=config)
                self.assertGreaterEqual(stable_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
