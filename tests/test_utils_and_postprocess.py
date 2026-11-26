from __future__ import annotations

import errno
import tempfile
import unittest
from unittest import mock

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.shared import utils  # noqa: E402
from lecture_stt.stt.postprocess import postprocess  # noqa: E402
from lecture_stt.stt.watcher import PollingWatcher  # noqa: E402


class UtilsStemTests(unittest.TestCase):
    def test_safe_move_never_replaces_destination_created_during_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.m4a"
            target = root / "target.m4a"
            source.write_bytes(b"ours")
            original_rename = utils._rename_no_replace

            def competing_rename(src: Path, dst: Path) -> None:
                target.write_bytes(b"theirs")
                original_rename(src, dst)

            with mock.patch.object(utils, "_rename_no_replace", side_effect=competing_rename):
                with self.assertRaises(FileExistsError):
                    utils.safe_move_file(source, target)

            self.assertEqual(source.read_bytes(), b"ours")
            self.assertEqual(target.read_bytes(), b"theirs")

    def test_safe_move_cross_device_fails_with_source_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.m4a"
            target = root / "target.m4a"
            source.write_bytes(b"ours")
            with mock.patch.object(utils, "_rename_no_replace", side_effect=OSError(errno.EXDEV, "cross-device rename")):
                with self.assertRaises(OSError) as raised:
                    utils.safe_move_file(source, target)
            self.assertEqual(raised.exception.errno, errno.EXDEV)
            self.assertEqual(source.read_bytes(), b"ours")
            self.assertFalse(target.exists())

    def test_safe_move_restores_source_if_it_changes_during_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.m4a"
            target = root / "target.m4a"
            source.write_bytes(b"ours")
            replacement = root / "replacement.m4a"
            replacement.write_bytes(b"new upload")
            original_rename = utils._rename_no_replace

            def replace_during_rename(src: Path, dst: Path) -> None:
                if src == source:
                    replacement.replace(source)
                original_rename(src, dst)

            with mock.patch.object(utils, "_rename_no_replace", side_effect=replace_during_rename):
                with self.assertRaisesRegex(RuntimeError, "restored"):
                    utils.safe_move_file(source, target)

            self.assertEqual(source.read_bytes(), b"new upload")
            self.assertFalse(target.exists())

    def test_safe_move_reports_sync_failure_after_successful_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.m4a"
            target = root / "target.m4a"
            source.write_bytes(b"ours")

            with mock.patch.object(utils.os, "fsync", side_effect=OSError("sync failed")), mock.patch.object(utils.logger, "warning") as warning:
                utils.safe_move_file(source, target)

            self.assertFalse(source.exists())
            self.assertEqual(target.read_bytes(), b"ours")
            self.assertTrue(warning.called)

    def test_safe_move_rejects_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.m4a"
            outside.write_bytes(b"outside")
            source = root / "source.m4a"
            source.symlink_to(outside)
            target = root / "target.m4a"

            with self.assertRaises(OSError):
                utils.safe_move_file(source, target)

            self.assertTrue(source.is_symlink())
            self.assertFalse(target.exists())
            self.assertEqual(outside.read_bytes(), b"outside")

    def test_watcher_ignores_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.m4a"
            outside.write_bytes(b"outside")
            inbox = root / "inbox"
            inbox.mkdir()
            (inbox / "linked.m4a").symlink_to(outside)
            watcher = PollingWatcher(str(inbox), stable_for_sec=0, polling_interval_sec=1)

            self.assertEqual(watcher.scan_stable_files(), [])
            self.assertEqual(watcher.scan_stable_files(), [])

    def test_sanitize_stem_normalizes_nfd_korean_and_preserves_unicode_letters(self) -> None:
        nfd_name = "한국어 녹음"

        self.assertEqual(utils.sanitize_stem(nfd_name), "한국어_녹음")
        self.assertEqual(utils.sanitize_stem("résumé 녹음"), "résumé_녹음")

    def test_sanitize_stem_strips_invalid_filename_characters_and_collapse_tokens(self) -> None:
        raw_name = "  수업/자료:1*2?3\"4<5>6|7\\8  "

        self.assertEqual(utils.sanitize_stem(raw_name), "수업_자료_1_2_3_4_5_6_7_8")

    def test_sanitize_stem_only_unsafe_chars_falls_back_to_audio(self) -> None:
        self.assertEqual(utils.sanitize_stem(" . / ? : * \" < > | ~ - _ "), "audio")


class PostprocessTests(unittest.TestCase):
    def test_postprocess_keeps_global_repeat_removal_after_segment_sync(self) -> None:
        repeated = "가상메모리관리"
        segments = [{"id": 0, "start": 0.0, "end": 3.0, "text": repeated * 3}]

        cleaned_segments, cleaned_text = postprocess(segments, repeated * 3)

        self.assertEqual(cleaned_segments[0]["text"], repeated)
        self.assertEqual(cleaned_text, repeated)

    def test_postprocess_cleans_segments_without_dropping_segment_shape(self) -> None:
        segments = [
            {"id": 0, "start": 0.0, "end": 1.0, "text": "..."},
            {"id": 1, "start": 1.0, "end": 4.0, "text": "모금투"},
        ]

        cleaned_segments, cleaned_text = postprocess(segments, "모금투")

        self.assertEqual(cleaned_segments[0]["text"], "")
        self.assertEqual(cleaned_segments[1]["text"], "우분투")
        self.assertEqual(cleaned_text, "우분투")

    def test_postprocess_returns_empty_text_when_all_segments_become_dot_noise(self) -> None:
        segments = [
            {"id": 0, "start": 0.0, "end": 1.0, "text": "..."},
            {"id": 1, "start": 1.0, "end": 2.0, "text": "、、、"},
        ]

        cleaned_segments, cleaned_text = postprocess(segments, "...、、、")

        self.assertEqual(cleaned_segments[0]["text"], "")
        self.assertEqual(cleaned_segments[1]["text"], "")
        self.assertEqual(cleaned_text, "")

    def test_postprocess_reduces_repeated_korean_phrases_without_dropping_segments(self) -> None:
        segments = [
            {"id": 0, "text": "강의노트샘플강의노트샘플강의노트샘플"},
            {"id": 1, "text": "강의노트샘플강의노트샘플강의노트샘플"},
        ]

        cleaned_segments, cleaned_text = postprocess(
            segments,
            "강의노트샘플강의노트샘플강의노트샘플강의노트샘플강의노트샘플강의노트샘플강의노트샘플",
        )

        self.assertEqual(cleaned_segments[0]["text"], "강의노트샘플")
        self.assertEqual(cleaned_segments[1]["text"], "강의노트샘플")
        self.assertEqual(len(cleaned_segments), 2)
        self.assertEqual(cleaned_segments[0]["id"], 0)
        self.assertEqual(cleaned_segments[1]["id"], 1)
        self.assertEqual(cleaned_text, "강의노트샘플\n강의노트샘플")

    def test_postprocess_clears_dot_and_punctuation_noise_and_preserves_content(self) -> None:
        segments = [
            {"id": 0, "text": "..."},
            {"id": 1, "text": "테스트"},
            {"id": 2, "text": "。、…"},
            {"id": 3, "text": "정상"},
        ]

        cleaned_segments, cleaned_text = postprocess(segments, "... 。、… 테스트 정상")

        self.assertEqual(cleaned_segments[0]["text"], "")
        self.assertEqual(cleaned_segments[2]["text"], "")
        self.assertEqual(cleaned_segments[1]["text"], "테스트")
        self.assertEqual(cleaned_segments[3]["text"], "정상")
        self.assertEqual(cleaned_text, "테스트\n정상")


if __name__ == "__main__":
    unittest.main()
