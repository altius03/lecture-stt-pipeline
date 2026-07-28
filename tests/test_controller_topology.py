from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.stt import controller_topology  # noqa: E402


_PATH_VALUE = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
_EXISTING_LABELS = (
    "com.geonha.lecture-stt",
    "com.geonha.lecture-stt-cleanup",
    "com.geonha.lecture-stt-distribute",
    "com.geonha.lecture-stt-webpanel",
)


def _plist_xml(mapping: dict[str, object]) -> bytes:
    import plistlib

    return plistlib.dumps(mapping, sort_keys=False)


def _existing_plist(label: str, *, stdout_name: str, stderr_name: str) -> dict[str, object]:
    return {
        "EnvironmentVariables": {"PATH": _PATH_VALUE},
        "Label": label,
        "ProgramArguments": ["/bin/bash", f"__REPO_ROOT__/scripts/{label}.sh"],
        "StandardErrorPath": f"__HOME__/Library/Logs/lecture_stt/{stderr_name}",
        "StandardOutPath": f"__HOME__/Library/Logs/lecture_stt/{stdout_name}",
        "WorkingDirectory": "__REPO_ROOT__",
    }


def _write_repo_fixture(root: Path) -> None:
    (root / "launchd").mkdir(parents=True, exist_ok=True)
    (root / "controller/launchd").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)

    fixtures = {
        "com.geonha.lecture-stt": _existing_plist(
            "com.geonha.lecture-stt",
            stdout_name="launchd.out.log",
            stderr_name="launchd.err.log",
        ),
        "com.geonha.lecture-stt-cleanup": _existing_plist(
            "com.geonha.lecture-stt-cleanup",
            stdout_name="cleanup.out.log",
            stderr_name="cleanup.err.log",
        ),
        "com.geonha.lecture-stt-distribute": _existing_plist(
            "com.geonha.lecture-stt-distribute",
            stdout_name="downstream.out.log",
            stderr_name="downstream.err.log",
        ),
        "com.geonha.lecture-stt-webpanel": _existing_plist(
            "com.geonha.lecture-stt-webpanel",
            stdout_name="webpanel.out.log",
            stderr_name="webpanel.err.log",
        ),
    }
    for label, payload in fixtures.items():
        _replace_path(root / "launchd" / f"{label}.plist", _plist_xml(payload))

    _replace_path(
        root / "controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template",
        (REPO_ROOT / "controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template").read_bytes()
    )
    _replace_text(
        root / "scripts/setup_launchd.sh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            REPO=/tmp/repo
            PLISTS=(
              "com.geonha.lecture-stt"
              "com.geonha.lecture-stt-cleanup"
              "com.geonha.lecture-stt-distribute"
              "com.geonha.lecture-stt-webpanel"
            )
            for label in "${PLISTS[@]}"; do
              src="$REPO/launchd/${label}.plist"
              render_plist "$src" "$dst"
            done
            """
        ),
    )


def _replace_path(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    path.write_bytes(payload)


def _replace_text(path: Path, payload: str) -> None:
    _replace_path(path, payload.encode("utf-8"))


class ControllerTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        _write_repo_fixture(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_temp_repo_summary_is_metadata_only(self) -> None:
        summary = controller_topology.run_topology_preflight(self.root)
        self.assertTrue(summary["ready"])
        self.assertEqual(summary["schema_version"], controller_topology.SUMMARY_SCHEMA_VERSION)
        self.assertEqual(summary["mode"], "read_only")
        self.assertIs(summary["install_supported"], False)
        self.assertIs(summary["evidence_wrapper_connected"], True)
        self.assertEqual(summary["existing_template_count"], 4)
        self.assertEqual(summary["script_label_count"], 4)
        self.assertEqual(summary["total_template_count"], 5)
        self.assertEqual(
            summary["candidate_label"],
            "com.geonha.lecture-stt-controller-shadow",
        )
        self.assertNotIn(str(self.root), json.dumps(summary, ensure_ascii=False))
        self.assertNotIn("__REPO_ROOT__", json.dumps(summary, ensure_ascii=False))
        candidate, _ = controller_topology._read_plist(
            self.root
            / "controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template",
            label="candidate",
        )
        self.assertEqual(
            candidate["WorkingDirectory"],
            "__BUNDLED_RUNTIME_ROOT__",
        )
        self.assertEqual(
            candidate["EnvironmentVariables"]["PYTHONPATH"],
            "__BUNDLED_RUNTIME_ROOT__",
        )
        repo_root_index = candidate["ProgramArguments"].index("--repo-root")
        self.assertEqual(
            candidate["ProgramArguments"][repo_root_index + 1],
            "__REPO_ROOT__",
        )

    def test_real_repo_preflight_passes(self) -> None:
        summary = controller_topology.run_topology_preflight(REPO_ROOT)
        self.assertTrue(summary["ready"])
        self.assertEqual(summary["existing_template_count"], 4)
        self.assertEqual(summary["total_template_count"], 5)

    def test_candidate_placeholder_or_program_drift_fails_closed(self) -> None:
        candidate = self.root / "controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template"
        payload = _plist_xml(
            {
                **controller_topology._CANDIDATE_TEMPLATE,
                "ProgramArguments": [
                    "__CONTROLLER_SHADOW_BIN__",
                    "--python-bin",
                    "__PYTHON_BIN__",
                    "--repo-root",
                    "__REPO_ROOT__",
                    "--config",
                    "__CONFIG_PATH__",
                    "--watch-folder",
                    "__WATCH_ROOT__",
                ],
            }
        )
        candidate.write_bytes(payload)
        with self.assertRaisesRegex(controller_topology.ControllerTopologyError, "ProgramArguments drifted"):
            controller_topology.run_topology_preflight(self.root)

    def test_candidate_unknown_key_and_duplicate_plist_key_fail_closed(self) -> None:
        candidate = self.root / "controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template"
        candidate.write_bytes(
            _plist_xml(
                {
                    **controller_topology._CANDIDATE_TEMPLATE,
                    "KeepAlive": True,
                }
            )
        )
        with self.assertRaisesRegex(controller_topology.ControllerTopologyError, "keys mismatch"):
            controller_topology.run_topology_preflight(self.root)

        candidate.write_bytes(
            textwrap.dedent(
                """\
                <?xml version="1.0" encoding="UTF-8"?>
                <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
                <plist version="1.0">
                <dict>
                  <key>Label</key>
                  <string>com.geonha.lecture-stt-controller-shadow</string>
                  <key>Label</key>
                  <string>dup</string>
                </dict>
                </plist>
                """
            ).encode("utf-8")
        )
        with self.assertRaisesRegex(controller_topology.ControllerTopologyError, "duplicate plist keys"):
            controller_topology.run_topology_preflight(self.root)

    def test_setup_script_wiring_and_log_collision_fail_closed(self) -> None:
        script = self.root / "scripts/setup_launchd.sh"
        script.write_text(
            script.read_text(encoding="utf-8") + '\n"com.geonha.lecture-stt-controller-shadow"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(controller_topology.ControllerTopologyError, "must not reference"):
            controller_topology.run_topology_preflight(self.root)

        _write_repo_fixture(self.root)
        worker = self.root / "launchd/com.geonha.lecture-stt.plist"
        payload = _existing_plist(
            "com.geonha.lecture-stt",
            stdout_name="controller-shadow.evidence-result.jsonl",
            stderr_name="launchd.err.log",
        )
        worker.write_bytes(_plist_xml(payload))
        with self.assertRaisesRegex(controller_topology.ControllerTopologyError, "log paths must be unique"):
            controller_topology.run_topology_preflight(self.root)

    def test_candidate_schedule_and_log_path_drift_fail_closed(self) -> None:
        candidate = self.root / "controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template"

        too_slow = {
            **controller_topology._CANDIDATE_TEMPLATE,
            "StartInterval": 3601,
        }
        candidate.write_bytes(_plist_xml(too_slow))
        with self.assertRaisesRegex(
            controller_topology.ControllerTopologyError,
            "StartInterval is not bounded",
        ):
            controller_topology.run_topology_preflight(self.root)

        same_logs = {
            **controller_topology._CANDIDATE_TEMPLATE,
            "StandardOutPath": controller_topology._CANDIDATE_TEMPLATE["StandardErrorPath"],
        }
        candidate.write_bytes(_plist_xml(same_logs))
        with self.assertRaisesRegex(
            controller_topology.ControllerTopologyError,
            "stdout and stderr paths must differ",
        ):
            controller_topology.run_topology_preflight(self.root)

        colliding_logs = {
            **controller_topology._CANDIDATE_TEMPLATE,
            "StandardOutPath": "__HOME__/Library/Logs/lecture_stt/launchd.out.log",
        }
        candidate.write_bytes(_plist_xml(colliding_logs))
        with self.assertRaisesRegex(
            controller_topology.ControllerTopologyError,
            "stdout path contract drifted",
        ):
            controller_topology.run_topology_preflight(self.root)

    def test_candidate_working_directory_drift_fail_closed(self) -> None:
        candidate = self.root / "controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template"
        drift = {
            **controller_topology._CANDIDATE_TEMPLATE,
            "WorkingDirectory": "__REPO_ROOT__",
        }
        candidate.write_bytes(_plist_xml(drift))

        with self.assertRaisesRegex(
            controller_topology.ControllerTopologyError,
            "candidate working directory contract drifted",
        ):
            controller_topology.run_topology_preflight(self.root)

        bad_pythonpath = {
            **controller_topology._CANDIDATE_TEMPLATE,
            "EnvironmentVariables": {
                "PATH": _PATH_VALUE,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": "__REPO_ROOT__/other",
            },
        }
        candidate.write_bytes(_plist_xml(bad_pythonpath))
        with self.assertRaisesRegex(
            controller_topology.ControllerTopologyError,
            "PYTHONPATH contract drifted",
        ):
            controller_topology.run_topology_preflight(self.root)

        bad_bytecode = {
            **controller_topology._CANDIDATE_TEMPLATE,
            "EnvironmentVariables": {
                "PATH": _PATH_VALUE,
                "PYTHONDONTWRITEBYTECODE": "0",
                "PYTHONPATH": "__BUNDLED_RUNTIME_ROOT__",
            },
        }
        candidate.write_bytes(_plist_xml(bad_bytecode))
        with self.assertRaisesRegex(
            controller_topology.ControllerTopologyError,
            "PYTHONDONTWRITEBYTECODE contract drifted",
        ):
            controller_topology.run_topology_preflight(self.root)

    def test_symlink_hardlink_directory_and_size_bounds_fail_closed(self) -> None:
        target = self.root / "target.plist"
        target.write_bytes((REPO_ROOT / "launchd/com.geonha.lecture-stt.plist").read_bytes())

        worker = self.root / "launchd/com.geonha.lecture-stt.plist"
        worker.unlink()
        worker.symlink_to(target)
        with self.assertRaisesRegex(controller_topology.ControllerTopologyError, "not safely openable"):
            controller_topology.run_topology_preflight(self.root)

        worker.unlink()
        os.link(target, worker)
        with self.assertRaisesRegex(controller_topology.ControllerTopologyError, "single-link"):
            controller_topology.run_topology_preflight(self.root)

        _write_repo_fixture(self.root)
        candidate = self.root / "controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template"
        candidate.unlink()
        candidate.mkdir()
        with self.assertRaisesRegex(controller_topology.ControllerTopologyError, "not safely openable|regular file"):
            controller_topology.run_topology_preflight(self.root)

        _write_repo_fixture(self.root)
        script = self.root / "scripts/setup_launchd.sh"
        script.write_bytes(b"x" * (controller_topology.MAX_REPO_FILE_BYTES + 1))
        with self.assertRaisesRegex(controller_topology.ControllerTopologyError, "size is out of bounds"):
            controller_topology.run_topology_preflight(self.root)

    def test_symlinked_repo_subdirectory_fails_closed(self) -> None:
        real_launchd = self.root / "real-launchd"
        (self.root / "launchd").rename(real_launchd)
        (self.root / "launchd").symlink_to(real_launchd, target_is_directory=True)

        with self.assertRaisesRegex(
            controller_topology.ControllerTopologyError,
            "parent must be a real directory",
        ):
            controller_topology.run_topology_preflight(self.root)

    def test_existing_template_bytes_change_topology_digest(self) -> None:
        before = controller_topology.run_topology_preflight(self.root)
        worker = self.root / "launchd/com.geonha.lecture-stt.plist"
        payload = _existing_plist(
            "com.geonha.lecture-stt",
            stdout_name="launchd.out.log",
            stderr_name="launchd.err.log",
        )
        payload["ThrottleInterval"] = 11
        worker.write_bytes(_plist_xml(payload))

        after = controller_topology.run_topology_preflight(self.root)
        self.assertNotEqual(before["topology_sha256"], after["topology_sha256"])

    def test_unstable_file_fails_closed(self) -> None:
        target = self.root / "launchd/com.geonha.lecture-stt.plist"
        observed = os.stat(target, follow_symlinks=False)
        mutated = os.stat_result(
            (
                observed.st_mode,
                observed.st_ino,
                observed.st_dev,
                observed.st_nlink,
                observed.st_uid,
                observed.st_gid,
                observed.st_size,
                observed.st_atime,
                observed.st_mtime + 1,
                observed.st_ctime,
            )
        )

        original_stat = controller_topology.os.stat

        def fake_stat(path: os.PathLike[str] | str, *args: object, **kwargs: object) -> os.stat_result:
            candidate = os.fspath(path)
            if candidate == os.fspath(target) and kwargs.get("follow_symlinks") is False:
                return mutated
            return original_stat(path, *args, **kwargs)

        with mock.patch.object(controller_topology.os, "stat", side_effect=fake_stat):
            with self.assertRaisesRegex(controller_topology.ControllerTopologyError, "changed while it was read"):
                controller_topology.run_topology_preflight(self.root)

    def test_cli_sanitizes_paths_on_failure(self) -> None:
        script = self.root / "scripts/setup_launchd.sh"
        script.write_text("broken", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "lecture_stt.stt.controller_topology",
                "--repo-root",
                str(self.root),
            ],
            env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("controller topology preflight failed:", result.stderr)
        self.assertNotIn(str(self.root), result.stderr)


if __name__ == "__main__":
    unittest.main()
