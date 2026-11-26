from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.stt.controller_gate import (  # noqa: E402
    ControllerGateError,
    ControllerKillSwitchActiveError,
    ensure_controller_kill_switch_inactive,
    normalize_controller_kill_switch_path,
)


class ControllerGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.marker = self.root / "controller.disabled"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_absent_marker_is_inactive_and_path_is_required(self) -> None:
        normalized = normalize_controller_kill_switch_path(self.marker)
        self.assertEqual(normalized, Path(os.path.abspath(self.marker)))
        ensure_controller_kill_switch_inactive(normalized)
        with self.assertRaisesRegex(ControllerGateError, "requires"):
            normalize_controller_kill_switch_path(None)

    def test_regular_single_link_marker_is_active(self) -> None:
        self.marker.write_text("disabled\n", encoding="utf-8")
        with self.assertRaises(ControllerKillSwitchActiveError):
            ensure_controller_kill_switch_inactive(self.marker)

    def test_symlink_directory_and_hardlink_are_fail_closed(self) -> None:
        target = self.root / "target"
        target.write_text("target", encoding="utf-8")
        self.marker.symlink_to(target)
        normalized = normalize_controller_kill_switch_path(self.marker)
        self.assertEqual(normalized, self.marker)
        with self.assertRaisesRegex(ControllerGateError, "regular single-link"):
            ensure_controller_kill_switch_inactive(normalized)
        self.marker.unlink()

        self.marker.mkdir()
        with self.assertRaisesRegex(ControllerGateError, "regular single-link"):
            ensure_controller_kill_switch_inactive(self.marker)
        self.marker.rmdir()

        os.link(target, self.marker)
        with self.assertRaisesRegex(ControllerGateError, "regular single-link"):
            ensure_controller_kill_switch_inactive(self.marker)


if __name__ == "__main__":
    unittest.main()
