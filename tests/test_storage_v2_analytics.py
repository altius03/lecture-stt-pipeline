from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unicodedata
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from lecture_stt.storage_v2 import analytics as analytics_module
from lecture_stt.storage_v2.analytics import (
    disabled_transcription_analytics,
    read_transcription_analytics,
)
from lecture_stt.storage_v2.repository import apply_migration


SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 7, 23, 15, 30, tzinfo=SEOUL)
_AUTO_METADATA = object()


class StorageV2AnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "storage-v2.sqlite3"
        self.records_root = self.root / "records"
        self.records_root.mkdir()
        self.conn = apply_migration(self.db_path)

    def tearDown(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.temp_dir.cleanup()

    def _seed_job(
        self,
        storage_key: str,
        *,
        status: str = "done",
        recorded_at: str | None = "2026-07-23T10:00:00+09:00",
        received_at: str = "2026-07-23T10:01:00+09:00",
        queued_at: str = "2026-07-23T10:02:00+09:00",
        original_name_raw: str | None = None,
        original_name_nfc: str | None = None,
        title: str | None = None,
        context: tuple[str, str] | None = None,
        is_current: bool = True,
        recording_archived_at: str | None = None,
        job_archived_at: str | None = None,
        error_message: str | None = None,
        config_json: str | None = None,
    ) -> tuple[int, int]:
        raw_name = original_name_raw or f"{storage_key}.m4a"
        nfc_name = original_name_nfc or unicodedata.normalize("NFC", raw_name)
        cursor = self.conn.execute(
            """
            INSERT INTO recordings(
                storage_key,
                original_name_raw,
                original_name_nfc,
                source_relpath,
                source_state,
                recorded_at,
                received_at,
                archived_at
            )
            VALUES (?, ?, ?, ?, 'missing', ?, ?, ?)
            """,
            (
                storage_key,
                raw_name,
                nfc_name,
                f"source/{storage_key}.json",
                recorded_at,
                received_at,
                recording_archived_at,
            ),
        )
        recording_id = int(cursor.lastrowid)
        job_cursor = self.conn.execute(
            """
            INSERT INTO transcription_jobs(
                recording_id,
                job_key,
                job_relpath,
                status,
                config_json,
                error_message,
                is_current,
                queued_at,
                archived_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recording_id,
                f"job_{storage_key}",
                f"jobs/{storage_key}.json",
                status,
                config_json,
                error_message,
                int(is_current),
                queued_at,
                job_archived_at,
            ),
        )
        job_id = int(job_cursor.lastrowid)
        if title is not None:
            self.conn.execute(
                """
                INSERT INTO recording_titles(
                    recording_id,
                    title,
                    title_source,
                    is_current
                )
                VALUES (?, ?, 'manual', 1)
                """,
                (recording_id, title),
            )
        if context is not None:
            context_type, source = context
            self.conn.execute(
                """
                INSERT INTO recording_contexts(
                    recording_id,
                    context_type,
                    label,
                    source,
                    is_selected
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (recording_id, context_type, context_type, source),
            )
        self.conn.commit()
        return recording_id, job_id

    def _attach_scorecard(
        self,
        recording_id: int,
        job_id: int,
        *,
        path_rel: str = "artifacts/quality.json",
        content_sha256: object = _AUTO_METADATA,
        artifact_bytes: object = _AUTO_METADATA,
    ) -> None:
        scorecard_path = self.records_root / self._storage_key(recording_id) / path_rel
        try:
            scorecard_bytes = scorecard_path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            scorecard_bytes = b""
        if content_sha256 is _AUTO_METADATA:
            content_sha256 = hashlib.sha256(scorecard_bytes).hexdigest()
        if artifact_bytes is _AUTO_METADATA:
            artifact_bytes = len(scorecard_bytes)
        self.conn.execute(
            """
            INSERT INTO artifacts(
                recording_id,
                job_id,
                artifact_kind,
                revision,
                path_rel,
                content_sha256,
                bytes,
                is_latest
            )
            VALUES (?, ?, 'quality_scorecard', 1, ?, ?, ?, 1)
            """,
            (
                recording_id,
                job_id,
                path_rel,
                content_sha256,
                artifact_bytes,
            ),
        )
        self.conn.commit()

    def _storage_key(self, recording_id: int) -> str:
        row = self.conn.execute(
            "SELECT storage_key FROM recordings WHERE id = ?",
            (recording_id,),
        ).fetchone()
        assert row is not None
        return str(row["storage_key"])

    def _scorecard(
        self,
        *,
        score: int = 88,
        health: str = "good",
        audio_duration_sec: object = 60,
        total_processing_sec: object = 12,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "lecture_stt_quality_scorecard",
            "quality_score": score,
            "health": health,
            "metrics": {
                "audio_duration_sec": audio_duration_sec,
            },
            "timings": {
                "total_sec": total_processing_sec,
            },
            "summary": "private scorecard summary",
            "transcript_body": "private transcript body",
            "artifacts": {
                "transcript_json_path": "/private/transcript.json",
            },
            "profile": {
                "engine": "private-engine-parameter",
            },
        }

    def _write_scorecard(
        self,
        storage_key: str,
        *,
        payload: object | None = None,
        raw: str | None = None,
        path_rel: str = "artifacts/quality.json",
    ) -> Path:
        path = self.records_root / storage_key / path_rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        else:
            path.write_text(
                json.dumps(
                    self._scorecard() if payload is None else payload,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return path

    def _seed_scored_job(
        self,
        storage_key: str,
        *,
        payload: object | None = None,
        **job_kwargs: object,
    ) -> tuple[int, int]:
        recording_id, job_id = self._seed_job(
            storage_key,
            **job_kwargs,
        )
        self._write_scorecard(storage_key, payload=payload)
        self._attach_scorecard(recording_id, job_id)
        return recording_id, job_id

    def _read(self, **kwargs: object) -> dict[str, object]:
        return read_transcription_analytics(
            self.db_path,
            self.records_root,
            now=NOW,
            **kwargs,
        )

    def test_disabled_payload_is_complete_with_fixed_period_buckets(self) -> None:
        expected_lengths = {"day": 24, "week": 7, "month": 30}
        expected_buckets = {"day": "hour", "week": "day", "month": "day"}
        expected_keys = {
            "schema_version",
            "available",
            "disabled_reason",
            "period",
            "timezone",
            "window",
            "freshness",
            "limits",
            "coverage",
            "totals",
            "quality_distribution",
            "status_distribution",
            "classification_distribution",
            "timeline",
            "recent_attention",
        }

        for period in ("day", "week", "month"):
            with self.subTest(period=period):
                payload = disabled_transcription_analytics(period, now=NOW)

                self.assertEqual(set(payload), expected_keys)
                self.assertFalse(payload["available"])
                self.assertEqual(
                    payload["disabled_reason"],
                    "transcription_analytics_disabled",
                )
                self.assertEqual(payload["window"]["bucket"], expected_buckets[period])
                self.assertEqual(len(payload["timeline"]), expected_lengths[period])
                self.assertEqual(payload["totals"]["jobs"], 0)
                self.assertEqual(payload["coverage"]["audio_duration_known"], 0)
                self.assertEqual(
                    payload["coverage"]["processing_duration_known"],
                    0,
                )
                self.assertEqual(
                    sum(row["count"] for row in payload["quality_distribution"]),
                    0,
                )
                self.assertEqual(
                    sum(row["count"] for row in payload["status_distribution"]),
                    0,
                )
                self.assertEqual(
                    payload["classification_distribution"],
                    [
                        {
                            "context_type": None,
                            "source": None,
                            "label": "미분류",
                            "count": 0,
                        }
                    ],
                )
                self.assertTrue(
                    all(
                        not any(key.startswith("_") for key in bucket)
                        for bucket in payload["timeline"]
                    )
                )

    def test_enabled_query_includes_only_current_nonarchived_jobs(self) -> None:
        self._seed_job("included")
        self._seed_job("not_current", is_current=False)
        self._seed_job(
            "archived_job",
            job_archived_at="2026-07-23T11:00:00+09:00",
        )
        self._seed_job(
            "archived_recording",
            recording_archived_at="2026-07-23T11:00:00+09:00",
        )

        payload = self._read(period="day")

        self.assertEqual(payload["totals"]["jobs"], 1)
        self.assertEqual(payload["totals"]["done"], 1)
        self.assertEqual(payload["coverage"]["jobs_total"], 1)

    def test_event_time_fallback_and_local_bucket_boundaries(self) -> None:
        self._seed_job(
            "recorded",
            recorded_at="2026-07-23T01:15:00+09:00",
        )
        self._seed_job(
            "received",
            recorded_at="invalid-recorded",
            received_at="2026-07-23T02:20:00+09:00",
        )
        self._seed_job(
            "queued",
            recorded_at="invalid-recorded",
            received_at="invalid-received",
            queued_at="2026-07-22 18:30:00",
        )
        self._seed_job(
            "naive_utc_start",
            recorded_at="2026-07-22 15:00:00",
        )
        self._seed_job(
            "before_start",
            recorded_at="2026-07-22 14:59:59",
        )
        self._seed_job(
            "at_end",
            recorded_at=NOW.isoformat(),
        )
        self._seed_job(
            "after_end",
            recorded_at=(NOW + timedelta(seconds=1)).isoformat(),
        )

        payload = self._read(period="day")

        self.assertEqual(payload["totals"]["jobs"], 5)
        self.assertEqual(payload["coverage"]["event_time_recorded"], 3)
        self.assertEqual(payload["coverage"]["event_time_received"], 1)
        self.assertEqual(payload["coverage"]["event_time_queued"], 1)
        self.assertEqual(payload["coverage"]["event_time_invalid"], 0)
        self.assertEqual(payload["timeline"][0]["jobs"], 1)
        self.assertEqual(payload["timeline"][1]["jobs"], 1)
        self.assertEqual(payload["timeline"][2]["jobs"], 1)
        self.assertEqual(payload["timeline"][3]["jobs"], 1)
        self.assertEqual(payload["timeline"][15]["jobs"], 1)

    def test_status_quality_and_distribution_sums_are_consistent(self) -> None:
        statuses = (
            "queued",
            "processing",
            "done",
            "needs_review",
            "error",
            "canceled",
        )
        scores = (95, 85, 75, 65, 55)
        for index, status in enumerate(statuses):
            kwargs: dict[str, object] = {
                "status": status,
                "recorded_at": (
                    NOW - timedelta(minutes=index + 1)
                ).isoformat(),
            }
            if index < len(scores):
                kwargs["context"] = (
                    "class_session",
                    "schedule_import" if index == 0 else "manual",
                )
                self._seed_scored_job(
                    f"distribution_{index}",
                    payload=self._scorecard(
                        score=scores[index],
                        health="good",
                    ),
                    **kwargs,
                )
            else:
                self._seed_job(f"distribution_{index}", **kwargs)

        payload = self._read(period="day")

        self.assertEqual(payload["totals"]["jobs"], 6)
        self.assertTrue(
            all(payload["totals"][status] == 1 for status in statuses)
        )
        self.assertEqual(payload["coverage"]["quality_scored"], 5)
        self.assertEqual(payload["coverage"]["quality_missing"], 1)
        self.assertEqual(payload["coverage"]["quality_invalid"], 0)
        self.assertEqual(
            [
                (
                    row["band"],
                    row["label"],
                    row["min"],
                    row["max"],
                    row["count"],
                )
                for row in payload["quality_distribution"]
            ],
            [
                ("90-100", "90–100점", 90, 100, 1),
                ("80-89", "80–89점", 80, 89, 1),
                ("70-79", "70–79점", 70, 79, 1),
                ("60-69", "60–69점", 60, 69, 1),
                ("0-59", "0–59점", 0, 59, 1),
                ("unscored", "점수 없음", None, None, 1),
            ],
        )
        self.assertEqual(
            sum(row["count"] for row in payload["quality_distribution"]),
            payload["totals"]["jobs"],
        )
        self.assertEqual(
            sum(row["count"] for row in payload["status_distribution"]),
            payload["totals"]["jobs"],
        )
        self.assertEqual(
            sum(row["count"] for row in payload["classification_distribution"]),
            payload["totals"]["jobs"],
        )
        self.assertEqual(
            sum(bucket["jobs"] for bucket in payload["timeline"]),
            payload["totals"]["jobs"],
        )
        self.assertEqual(
            payload["coverage"]["classification_known"]
            + payload["coverage"]["classification_unclassified"],
            payload["coverage"]["jobs_total"],
        )

    def test_selected_context_preserves_source_and_unclassified_is_null_null(self) -> None:
        self._seed_job(
            "classified",
            context=("meeting", "schedule_import"),
        )
        self._seed_job("unclassified")

        payload = self._read(period="day")
        classified = next(
            row
            for row in payload["classification_distribution"]
            if row["context_type"] == "meeting"
        )
        unclassified = next(
            row
            for row in payload["classification_distribution"]
            if row["context_type"] is None
        )

        self.assertEqual(
            classified,
            {
                "context_type": "meeting",
                "source": "schedule_import",
                "label": "회의·대화",
                "count": 1,
            },
        )
        self.assertEqual(
            unclassified,
            {
                "context_type": None,
                "source": None,
                "label": "미분류",
                "count": 1,
            },
        )
        self.assertEqual(payload["coverage"]["classification_known"], 1)
        self.assertEqual(payload["coverage"]["classification_unclassified"], 1)

    def test_display_name_prefers_title_then_nfc_original_name(self) -> None:
        nfd_name = unicodedata.normalize("NFD", "강의.m4a")
        self._seed_job(
            "with_title",
            status="needs_review",
            original_name_raw=nfd_name,
            original_name_nfc="강의.m4a",
            title="2026-07-23 자료구조 5교시",
        )
        self._seed_job(
            "fallback_name",
            status="needs_review",
            original_name_raw=nfd_name,
            original_name_nfc="강의.m4a",
        )

        payload = self._read(period="day")
        names = {
            row["storage_key"]: row["display_name"]
            for row in payload["recent_attention"]
        }

        self.assertEqual(names["with_title"], "2026-07-23 자료구조 5교시")
        self.assertEqual(names["fallback_name"], "강의.m4a")
        self.assertTrue(unicodedata.is_normalized("NFC", names["fallback_name"]))

    def test_recent_attention_is_bounded_and_excludes_private_fields(self) -> None:
        private_values = (
            str(self.records_root),
            "private transcript body",
            "private scorecard summary",
            "private job error message",
            "private-engine-parameter",
        )
        for index in range(25):
            storage_key = f"attention_{index:02d}"
            recording_id, job_id = self._seed_job(
                storage_key,
                status="needs_review",
                recorded_at=(NOW - timedelta(minutes=index)).isoformat(),
                error_message="private job error message",
                config_json=json.dumps(
                    {"engine_parameter": "private-engine-parameter"}
                ),
            )
            if index == 0:
                self._write_scorecard(
                    storage_key,
                    payload=self._scorecard(score=72, health="warn"),
                )
                self._attach_scorecard(recording_id, job_id)

        payload = self._read(period="day")
        recent = payload["recent_attention"]
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(len(recent), 20)
        self.assertEqual(recent[0]["storage_key"], "attention_00")
        self.assertEqual(recent[-1]["storage_key"], "attention_19")
        expected_item_keys = {
            "storage_key",
            "display_name",
            "status",
            "event_at",
            "quality_score",
            "health",
            "context_type",
            "classification_source",
        }
        self.assertTrue(all(set(row) == expected_item_keys for row in recent))
        self.assertFalse(any("path" in key for key in payload))
        for private_value in private_values:
            self.assertNotIn(private_value, serialized)
        self.assertNotIn("error_message", serialized)
        self.assertNotIn("scorecard_state", serialized)
        self.assertNotIn("_quality_counts", serialized)
        self.assertNotIn("_status_counts", serialized)
        self.assertNotIn("_classification_counts", serialized)

    def test_valid_missing_malformed_and_oversized_scorecards_are_accounted(self) -> None:
        self._seed_scored_job(
            "valid_scorecard",
            payload=self._scorecard(
                score=91,
                health="good",
                audio_duration_sec=120.5,
                total_processing_sec=30.25,
            ),
        )
        missing_recording, missing_job = self._seed_job("missing_scorecard")
        self._attach_scorecard(missing_recording, missing_job)
        malformed_recording, malformed_job = self._seed_job("malformed_scorecard")
        self._write_scorecard("malformed_scorecard", raw="{not-json")
        self._attach_scorecard(malformed_recording, malformed_job)
        oversized_recording, oversized_job = self._seed_job("oversized_scorecard")
        self._write_scorecard("oversized_scorecard", raw="{" + "x" * 2000 + "}")
        self._attach_scorecard(oversized_recording, oversized_job)

        payload = self._read(period="day", scorecard_max_bytes=1024)

        self.assertEqual(payload["coverage"]["quality_scored"], 1)
        self.assertEqual(payload["coverage"]["quality_missing"], 1)
        self.assertEqual(payload["coverage"]["quality_invalid"], 2)
        self.assertEqual(payload["coverage"]["audio_duration_known"], 1)
        self.assertEqual(payload["coverage"]["processing_duration_known"], 1)
        self.assertEqual(payload["totals"]["average_quality_score"], 91)
        self.assertEqual(payload["totals"]["audio_duration_sec"], 120.5)
        self.assertEqual(payload["totals"]["total_processing_sec"], 30.25)

    def test_scorecard_metadata_is_required_and_matches_stable_file_bytes(self) -> None:
        self._seed_scored_job("valid_metadata")

        tampered_recording, tampered_job = self._seed_job("tampered_metadata")
        tampered_path = self._write_scorecard(
            "tampered_metadata",
            payload=self._scorecard(score=88),
        )
        registered_sha256 = hashlib.sha256(tampered_path.read_bytes()).hexdigest()
        registered_bytes = tampered_path.stat().st_size
        self._attach_scorecard(tampered_recording, tampered_job)
        tampered_path.write_text(
            json.dumps(self._scorecard(score=99), ensure_ascii=False),
            encoding="utf-8",
        )
        self.assertEqual(tampered_path.stat().st_size, registered_bytes)

        metadata_cases = (
            ("null_hash", {"content_sha256": None}),
            ("null_bytes", {"artifact_bytes": None}),
            ("malformed_hash", {"content_sha256": "not-a-sha256"}),
            ("malformed_bytes", {"artifact_bytes": 1.5}),
            ("mismatched_hash", {"content_sha256": "0" * 64}),
            ("mismatched_bytes", {"artifact_bytes": 1}),
        )
        for storage_key, metadata in metadata_cases:
            recording_id, job_id = self._seed_job(storage_key)
            self._write_scorecard(storage_key)
            self._attach_scorecard(
                recording_id,
                job_id,
                **metadata,
            )

        payload = self._read(period="day")
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)

        self.assertEqual(payload["coverage"]["quality_scored"], 1)
        self.assertEqual(payload["coverage"]["quality_missing"], 0)
        self.assertEqual(payload["coverage"]["quality_invalid"], 7)
        self.assertNotIn(registered_sha256, serialized)
        self.assertNotIn("quality_scorecard_sha256", serialized)
        self.assertNotIn("quality_scorecard_path", serialized)

    def test_deeply_nested_scorecard_is_isolated_as_invalid(self) -> None:
        self._seed_scored_job("valid_alongside_deep")
        recording_id, job_id = self._seed_job("deeply_nested")
        self._write_scorecard(
            "deeply_nested",
            raw="[" * 10_000 + "0" + "]" * 10_000,
        )
        self._attach_scorecard(recording_id, job_id)

        payload = self._read(period="day")

        self.assertEqual(payload["totals"]["jobs"], 2)
        self.assertEqual(payload["coverage"]["quality_scored"], 1)
        self.assertEqual(payload["coverage"]["quality_invalid"], 1)

    def test_optional_duration_coverage_exposes_partial_sum_denominators(self) -> None:
        self._seed_scored_job(
            "duration_known",
            payload=self._scorecard(
                audio_duration_sec=120.5,
                total_processing_sec=30.25,
            ),
        )
        duration_unknown = self._scorecard()
        duration_unknown.pop("metrics")
        duration_unknown.pop("timings")
        self._seed_scored_job(
            "duration_unknown",
            payload=duration_unknown,
        )

        payload = self._read(period="day")

        self.assertEqual(payload["coverage"]["quality_scored"], 2)
        self.assertEqual(payload["coverage"]["audio_duration_known"], 1)
        self.assertEqual(payload["coverage"]["processing_duration_known"], 1)
        self.assertEqual(payload["totals"]["audio_duration_sec"], 120.5)
        self.assertEqual(payload["totals"]["total_processing_sec"], 30.25)

    def test_extreme_durations_fail_closed_and_payload_remains_strict_json(self) -> None:
        for index in range(2):
            self._seed_scored_job(
                f"duration_overflow_{index}",
                payload=self._scorecard(
                    audio_duration_sec=1e308,
                    total_processing_sec=1e308,
                ),
            )

        payload = self._read(period="day")

        self.assertEqual(payload["coverage"]["quality_scored"], 0)
        self.assertEqual(payload["coverage"]["quality_invalid"], 2)
        self.assertEqual(payload["coverage"]["audio_duration_known"], 0)
        self.assertEqual(payload["coverage"]["processing_duration_known"], 0)
        self.assertIsNone(payload["totals"]["average_quality_score"])
        self.assertIsNone(payload["totals"]["audio_duration_sec"])
        self.assertIsNone(payload["totals"]["total_processing_sec"])
        json.dumps(payload, ensure_ascii=False, allow_nan=False)

    def test_scorecard_symlinks_hardlinks_and_nonregular_files_are_invalid(self) -> None:
        component_recording, component_job = self._seed_job("symlink_component")
        component_root = self.records_root / "symlink_component"
        component_root.mkdir()
        outside_component = self.root / "outside-component"
        outside_component.mkdir()
        (outside_component / "quality.json").write_text(
            json.dumps(self._scorecard()),
            encoding="utf-8",
        )
        (component_root / "artifacts").symlink_to(
            outside_component,
            target_is_directory=True,
        )
        self._attach_scorecard(component_recording, component_job)

        file_recording, file_job = self._seed_job("symlink_file")
        file_root = self.records_root / "symlink_file" / "artifacts"
        file_root.mkdir(parents=True)
        outside_file = self.root / "outside-scorecard.json"
        outside_file.write_text(json.dumps(self._scorecard()), encoding="utf-8")
        (file_root / "quality.json").symlink_to(outside_file)
        self._attach_scorecard(file_recording, file_job)

        hardlink_recording, hardlink_job = self._seed_job("hardlink")
        hardlink_root = self.records_root / "hardlink" / "artifacts"
        hardlink_root.mkdir(parents=True)
        hardlink_source = hardlink_root / "original.json"
        hardlink_source.write_text(json.dumps(self._scorecard()), encoding="utf-8")
        os.link(hardlink_source, hardlink_root / "quality.json")
        self._attach_scorecard(hardlink_recording, hardlink_job)

        nonregular_recording, nonregular_job = self._seed_job("nonregular")
        nonregular_path = self.records_root / "nonregular" / "artifacts" / "quality.json"
        nonregular_path.mkdir(parents=True)
        self._attach_scorecard(nonregular_recording, nonregular_job)

        payload = self._read(period="day")

        self.assertEqual(payload["coverage"]["quality_scored"], 0)
        self.assertEqual(payload["coverage"]["quality_missing"], 0)
        self.assertEqual(payload["coverage"]["quality_invalid"], 4)

    def test_changed_and_failed_scorecard_reads_are_invalid(self) -> None:
        recording_id, job_id = self._seed_job("changed_read")
        scorecard_path = self._write_scorecard("changed_read")
        self._attach_scorecard(recording_id, job_id)
        original_read = os.read
        changed = False

        def change_before_read(descriptor: int, size: int) -> bytes:
            nonlocal changed
            if not changed:
                changed = True
                scorecard_path.write_text(
                    json.dumps(self._scorecard()) + " ",
                    encoding="utf-8",
                )
            return original_read(descriptor, size)

        with mock.patch.object(
            analytics_module.os,
            "read",
            side_effect=change_before_read,
        ):
            changed_payload = self._read(period="day")
        self.assertEqual(changed_payload["coverage"]["quality_invalid"], 1)

        with mock.patch.object(
            analytics_module.os,
            "read",
            side_effect=OSError("isolated read failure"),
        ):
            failed_payload = self._read(period="day")
        self.assertEqual(failed_payload["coverage"]["quality_invalid"], 1)

    def test_scorecard_boolean_nonfinite_and_negative_numbers_are_invalid(self) -> None:
        bool_score = self._scorecard()
        bool_score["quality_score"] = True
        self._seed_scored_job("bool_score", payload=bool_score)

        bool_metric = self._scorecard(audio_duration_sec=True)
        self._seed_scored_job("bool_metric", payload=bool_metric)

        nonfinite_recording, nonfinite_job = self._seed_job("nonfinite")
        self._write_scorecard(
            "nonfinite",
            raw=json.dumps(
                self._scorecard(audio_duration_sec=float("nan")),
                ensure_ascii=False,
            ),
        )
        self._attach_scorecard(nonfinite_recording, nonfinite_job)

        self._seed_scored_job(
            "negative_timing",
            payload=self._scorecard(total_processing_sec=-1),
        )

        payload = self._read(period="day")

        self.assertEqual(payload["coverage"]["quality_scored"], 0)
        self.assertEqual(payload["coverage"]["quality_missing"], 0)
        self.assertEqual(payload["coverage"]["quality_invalid"], 4)
        self.assertEqual(
            next(
                row["count"]
                for row in payload["quality_distribution"]
                if row["band"] == "unscored"
            ),
            4,
        )

    def test_scorecard_schema_version_requires_exact_integer_one(self) -> None:
        for index, schema_version in enumerate((True, 1.0)):
            payload = self._scorecard()
            payload["schema_version"] = schema_version
            self._seed_scored_job(
                f"invalid_schema_version_{index}",
                payload=payload,
            )

        result = self._read(period="day")

        self.assertEqual(result["coverage"]["quality_scored"], 0)
        self.assertEqual(result["coverage"]["quality_missing"], 0)
        self.assertEqual(result["coverage"]["quality_invalid"], 2)

    def test_row_limit_bounds_results_and_marks_truncation(self) -> None:
        for index in range(3):
            self._seed_job(
                f"bounded_{index}",
                recorded_at=(NOW - timedelta(minutes=index)).isoformat(),
            )

        bounded = self._read(period="day", row_limit=2)
        complete = self._read(period="day", row_limit=3)

        self.assertEqual(bounded["totals"]["jobs"], 2)
        self.assertTrue(bounded["limits"]["truncated"])
        self.assertEqual(
            [row["storage_key"] for row in bounded["recent_attention"]],
            ["bounded_0", "bounded_1"],
        )
        self.assertEqual(complete["totals"]["jobs"], 3)
        self.assertFalse(complete["limits"]["truncated"])

    def test_period_filter_precedes_row_limit_and_uses_timestamp_fallback(self) -> None:
        self._seed_job(
            "future_0",
            status="needs_review",
            recorded_at=(NOW + timedelta(days=2)).isoformat(),
        )
        self._seed_job(
            "future_1",
            status="needs_review",
            recorded_at=(NOW + timedelta(days=1)).isoformat(),
        )
        self._seed_job(
            "valid_recorded",
            status="needs_review",
            recorded_at=(NOW - timedelta(minutes=2)).isoformat(),
        )
        self._seed_job(
            "valid_received_fallback",
            status="needs_review",
            recorded_at="0001-01-01T00:00:00+14:00",
            received_at=(NOW - timedelta(minutes=1)).isoformat(),
        )
        self._seed_job(
            "invalid_all_timestamps",
            status="needs_review",
            recorded_at="zzzz",
            received_at="also-invalid",
            queued_at="still-invalid",
        )

        payload = self._read(period="day", row_limit=2)

        self.assertEqual(payload["totals"]["jobs"], 2)
        self.assertFalse(payload["limits"]["truncated"])
        self.assertEqual(
            [row["storage_key"] for row in payload["recent_attention"]],
            ["valid_received_fallback", "valid_recorded"],
        )
        self.assertEqual(payload["coverage"]["event_time_received"], 1)
        self.assertEqual(payload["coverage"]["event_time_recorded"], 1)

    def test_invalid_arguments_fail_before_reading_storage(self) -> None:
        invalid_kwargs = (
            {"period": ""},
            {"period": "year"},
            {"period": "week", "row_limit": True},
            {"period": "week", "row_limit": 0},
            {"period": "week", "row_limit": 5001},
            {"period": "week", "scorecard_max_bytes": True},
            {"period": "week", "scorecard_max_bytes": 0},
            {"period": "week", "scorecard_max_bytes": 1048577},
            {"period": "week", "timezone_name": "Not/A-Timezone"},
        )

        for kwargs in invalid_kwargs:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    read_transcription_analytics(
                        self.db_path,
                        self.records_root,
                        now=NOW,
                        **kwargs,
                    )

    def test_records_root_must_exist_and_be_a_real_directory(self) -> None:
        missing = self.root / "missing-records"
        linked = self.root / "linked-records"
        linked.symlink_to(self.records_root, target_is_directory=True)
        regular_file = self.root / "records-file"
        regular_file.write_text("not a directory", encoding="utf-8")

        for candidate in (missing, linked, regular_file):
            with self.subTest(candidate=candidate.name):
                with self.assertRaises(RuntimeError):
                    read_transcription_analytics(
                        self.db_path,
                        candidate,
                        now=NOW,
                    )

    def test_database_must_exist_and_have_a_valid_v2_schema(self) -> None:
        missing_db = self.root / "missing.sqlite3"
        empty_db = self.root / "empty.sqlite3"
        sqlite3.connect(empty_db).close()
        invalid_db = self.root / "invalid.sqlite3"
        invalid_db.write_bytes(b"not a sqlite database")

        with self.assertRaisesRegex(RuntimeError, "not available"):
            read_transcription_analytics(
                missing_db,
                self.records_root,
                now=NOW,
            )
        with self.assertRaises(RuntimeError):
            read_transcription_analytics(
                empty_db,
                self.records_root,
                now=NOW,
            )
        with self.assertRaises((RuntimeError, sqlite3.DatabaseError)):
            read_transcription_analytics(
                invalid_db,
                self.records_root,
                now=NOW,
            )

    def test_read_preserves_main_and_wal_while_seeing_wal_visible_rows(self) -> None:
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("PRAGMA wal_autocheckpoint = 0")
        self._seed_job("readonly")
        wal_path = self.db_path.with_name(f"{self.db_path.name}-wal")
        shm_path = self.db_path.with_name(f"{self.db_path.name}-shm")

        immutable = sqlite3.connect(
            f"{self.db_path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            self.assertEqual(
                immutable.execute("SELECT COUNT(*) FROM recordings").fetchone()[0],
                0,
            )
        finally:
            immutable.close()

        def durable_snapshot(path: Path) -> tuple[bytes, tuple[int, ...]]:
            metadata = path.lstat()
            return (
                path.read_bytes(),
                (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_nlink,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                ),
            )

        def coordination_identity(path: Path) -> tuple[int, ...]:
            metadata = path.lstat()
            return (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_nlink,
                metadata.st_size,
            )

        before_main = durable_snapshot(self.db_path)
        before_wal = durable_snapshot(wal_path)
        before_shm_identity = coordination_identity(shm_path)

        with mock.patch.object(
            analytics_module,
            "connect_v2",
            wraps=analytics_module.connect_v2,
        ) as connect_v2:
            payload = read_transcription_analytics(
                self.db_path,
                self.records_root,
                period="day",
                now=NOW,
            )

        self.assertEqual(payload["totals"]["jobs"], 1)
        connect_v2.assert_called_once_with(self.db_path, readonly=True)
        self.assertEqual(durable_snapshot(self.db_path), before_main)
        self.assertEqual(durable_snapshot(wal_path), before_wal)
        # SQLite mode=ro may update SHM read-lock coordination bytes/timestamps.
        self.assertEqual(coordination_identity(shm_path), before_shm_identity)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
