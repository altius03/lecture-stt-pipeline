from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import stat
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for import_root in (REPO_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from lecture_stt.stt import controller_bundle  # noqa: E402
from tests.fixtures.controller_files import remove_tree, set_runtime_tree_mode, write_file
from tests.fixtures.controller_repo import BundleFixture


class ControllerBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bundle = BundleFixture(self.root)
        if self.bundle.kill_switch.exists():
            raise RuntimeError("kill switch cleanup failed")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_plan_is_deterministic_with_exact_sha(self) -> None:
        first = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())
        second = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())
        unsigned = dict(first)
        plan_sha256 = unsigned.pop("plan_sha256")
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(plan_sha256, hashlib.sha256(canonical).hexdigest())
        self.assertEqual(first["schema_version"], "lecture-stt/controller-shadow-bundle-plan@1")
        self.assertEqual(first["expected_count"], 1)
        self.assertEqual(first["policy"]["stdout_is_readiness_ledger"], False)
        self.assertIs(first["policy"]["evidence_wrapper_connected"], True)
        self.assertEqual(first["artifacts"]["binary_filename"], "lecture-stt-shadow")
        self.assertIn("evidence_root_path_sha256", first["bindings"])
        self.assertIn("expected_count", first)

    def test_prepare_guards_require_exact_plan_and_flag_contract(self) -> None:
        plan = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())

        with self.assertRaises(controller_bundle.ControllerBundleWriteDisabledError):
            controller_bundle.prepare_controller_bundle(
                **self.bundle.plan_kwargs(),
                enable_prepare=False,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
            )
        with self.assertRaises(controller_bundle.ControllerBundleWriteDisabledError):
            controller_bundle.prepare_controller_bundle(
                **self.bundle.plan_kwargs(),
                enable_prepare=True,
                allow_write=False,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
            )
        with self.assertRaises(controller_bundle.ControllerBundleError):
            controller_bundle.prepare_controller_bundle(
                **self.bundle.plan_kwargs(),
                enable_prepare=True,
                allow_write=True,
                expected_count=2,
                expected_plan_sha256=plan["plan_sha256"],
            )
        with self.assertRaises(controller_bundle.ControllerBundleError):
            controller_bundle.prepare_controller_bundle(
                **self.bundle.plan_kwargs(),
                enable_prepare=True,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256="bad-hash",
            )
        self.assertFalse(
            (self.bundle.output_root / controller_bundle.BUNDLE_DIRECTORY_NAME).exists()
        )

    def test_prepare_then_replay_returns_skipped(self) -> None:
        plan = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())
        first = controller_bundle.prepare_controller_bundle(
            **self.bundle.plan_kwargs(),
            enable_prepare=True,
            allow_write=True,
            expected_count=1,
            expected_plan_sha256=plan["plan_sha256"],
        )
        second = controller_bundle.prepare_controller_bundle(
            **self.bundle.plan_kwargs(),
            enable_prepare=True,
            allow_write=True,
            expected_count=1,
            expected_plan_sha256=plan["plan_sha256"],
        )

        bundle_dir = self.bundle.output_root / controller_bundle.BUNDLE_DIRECTORY_NAME
        manifest = (bundle_dir / controller_bundle.MANIFEST_FILENAME).read_text(encoding="utf-8")
        parsed_manifest = json.loads(manifest)

        self.assertEqual(first["status"], "prepared")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(parsed_manifest["schema_version"], controller_bundle.MANIFEST_SCHEMA_VERSION)
        self.assertEqual(parsed_manifest["policy"]["stdout_is_readiness_ledger"], False)
        self.assertEqual(second["plan_sha256"], plan["plan_sha256"])
        self.assertTrue((bundle_dir / controller_bundle.BINARY_FILENAME).exists())
        self.assertTrue((bundle_dir / controller_bundle.PLIST_FILENAME).exists())

    def test_plan_and_manifest_contracts_include_evidence_stdout_no_install(self) -> None:
        plan = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())
        result = controller_bundle.prepare_controller_bundle(
            **self.bundle.plan_kwargs(),
            enable_prepare=True,
            allow_write=True,
            expected_count=1,
            expected_plan_sha256=plan["plan_sha256"],
        )
        bundle_dir = self.bundle.output_root / controller_bundle.BUNDLE_DIRECTORY_NAME
        plist = plistlib.loads((bundle_dir / controller_bundle.PLIST_FILENAME).read_bytes())
        manifest = json.loads((bundle_dir / controller_bundle.MANIFEST_FILENAME).read_text())

        self.assertEqual(result["status"], "prepared")
        self.assertEqual(manifest["expected_count"], 1)
        self.assertEqual(plan["policy"]["install_supported"], False)
        self.assertEqual(plan["policy"]["launchd_install_supported"], False)
        self.assertEqual(plan["policy"]["operational_install_supported"], False)
        self.assertEqual(plan["policy"]["execution_supported"], False)
        self.assertEqual(plan["policy"]["uninstall_supported"], False)
        self.assertEqual(plan["policy"]["rollback_supported"], False)
        self.assertEqual(
            plist["StandardOutPath"],
            os.path.realpath(
                self.bundle.home / "Library/Logs/lecture_stt/controller-shadow.evidence-result.jsonl"
            ),
        )
        self.assertEqual(
            plist["StandardErrorPath"],
            os.path.realpath(self.bundle.home / "Library/Logs/lecture_stt/controller-shadow.err.log"),
        )
        self.assertEqual(manifest["policy"]["stdout_is_readiness_ledger"], False)
        self.assertEqual(
            plist["EnvironmentVariables"]["PYTHONDONTWRITEBYTECODE"],
            "1",
        )
        self.assertEqual(
            plist["EnvironmentVariables"]["PYTHONPATH"],
            os.path.realpath(
                bundle_dir / controller_bundle.RUNTIME_DIRECTORY_NAME
            ),
        )
        self.assertEqual(
            plist["WorkingDirectory"],
            os.path.realpath(
                bundle_dir / controller_bundle.RUNTIME_DIRECTORY_NAME
            ),
        )
        self.assertEqual(
            plist["ProgramArguments"][:4],
            [
                os.path.realpath(self.bundle.python_binary),
                "-m",
                "lecture_stt.stt.controller_evidence",
                "run",
            ],
        )
        self.assertIn(os.path.realpath(self.bundle.evidence_root), plist["ProgramArguments"])
        self.assertIn("--enable-observation", plist["ProgramArguments"])
        self.assertIn("--allow-write", plist["ProgramArguments"])
        self.assertIn("--expected-count", plist["ProgramArguments"])
        self.assertIn("--timeout-sec", plist["ProgramArguments"])
        self.assertEqual(manifest["policy"]["evidence_wrapper_connected"], True)
        self.assertEqual(
            manifest["runtime"]["directory_name"],
            controller_bundle.RUNTIME_DIRECTORY_NAME,
        )
        self.assertEqual(
            len(manifest["runtime"]["source_files"]),
            4,
        )
        self.assertNotIn(
            "launchctl",
            (bundle_dir / controller_bundle.PLIST_FILENAME).read_text(encoding="utf-8"),
        )

    def test_rendered_plist_executes_only_through_the_evidence_wrapper(self) -> None:
        report = {
            "completed_at": "2026-07-27T00:00:01.000000Z",
            "kill_switch_configured": True,
            "mode": "read_only",
            "ok": True,
            "plan_checks": [
                {
                    "plan_sha256": "2" * 64,
                    "relative_path": "alpha.m4a",
                    "scan_index": 1,
                    "status": "verified",
                },
                {
                    "plan_sha256": "3" * 64,
                    "relative_path": "beta.m4a",
                    "scan_index": 2,
                    "status": "verified",
                },
            ],
            "polling_match": True,
            "run_id": "1" * 32,
            "scan_comparisons": [
                {
                    "go_stable_relative_paths": ["alpha.m4a", "beta.m4a"],
                    "match": True,
                    "python_stable_relative_paths": ["alpha.m4a", "beta.m4a"],
                    "scan_index": 1,
                },
                {
                    "go_stable_relative_paths": ["alpha.m4a", "beta.m4a"],
                    "match": True,
                    "python_stable_relative_paths": ["alpha.m4a", "beta.m4a"],
                    "scan_index": 2,
                },
            ],
            "scan_count": 2,
            "schema_version": "lecture-stt/controller-shadow-report@4",
            "started_at": "2026-07-27T00:00:00.000000Z",
            "verified_count": 2,
        }
        report_json = json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        write_file(
            self.bundle.source_binary,
            f"#!/bin/sh\nprintf '%s\\n' '{report_json}'\n",
            executable=True,
        )
        self.bundle.python_binary = Path(sys.executable)
        plan = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())
        controller_bundle.prepare_controller_bundle(
            **self.bundle.plan_kwargs(),
            enable_prepare=True,
            allow_write=True,
            expected_count=1,
            expected_plan_sha256=plan["plan_sha256"],
        )
        bundle_dir = self.bundle.output_root / controller_bundle.BUNDLE_DIRECTORY_NAME
        plist = plistlib.loads(
            (bundle_dir / controller_bundle.PLIST_FILENAME).read_bytes()
        )
        result = subprocess.run(
            plist["ProgramArguments"],
            cwd=plist["WorkingDirectory"],
            env={**os.environ, **plist["EnvironmentVariables"]},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        wrapper_result = json.loads(result.stdout)
        self.assertEqual(wrapper_result["outcome"], "success")
        self.assertEqual(wrapper_result["attempt_count"], 1)
        self.assertEqual(
            sorted(path.name for path in self.bundle.evidence_root.iterdir()),
            [
                "00000001.finish.json",
                "00000001.start.json",
                "journal.lock",
            ],
        )
        self.assertNotEqual(
            plist["ProgramArguments"][0],
            str(bundle_dir / controller_bundle.BINARY_FILENAME),
        )
        self.assertFalse(
            any(
                path.name == "__pycache__"
                for path in (bundle_dir / controller_bundle.RUNTIME_DIRECTORY_NAME).rglob("*")
            )
        )

    def test_prepared_bundle_runtime_is_immune_to_repo_source_drift(self) -> None:
        report = {
            "completed_at": "2026-07-27T00:00:01.000000Z",
            "kill_switch_configured": True,
            "mode": "read_only",
            "ok": True,
            "plan_checks": [
                {
                    "plan_sha256": "5" * 64,
                    "relative_path": "alpha.m4a",
                    "scan_index": 1,
                    "status": "verified",
                }
            ],
            "polling_match": True,
            "run_id": "4" * 32,
            "scan_comparisons": [
                {
                    "go_stable_relative_paths": ["alpha.m4a"],
                    "match": True,
                    "python_stable_relative_paths": ["alpha.m4a"],
                    "scan_index": 1,
                }
            ],
            "scan_count": 1,
            "schema_version": "lecture-stt/controller-shadow-report@4",
            "started_at": "2026-07-27T00:00:00.000000Z",
            "verified_count": 1,
        }
        write_file(
            self.bundle.source_binary,
            "#!/bin/sh\nprintf '%s\\n' '{}'\n".format(
                json.dumps(
                    report,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
            executable=True,
        )
        self.bundle.python_binary = Path(sys.executable)
        plan = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())
        controller_bundle.prepare_controller_bundle(
            **self.bundle.plan_kwargs(),
            enable_prepare=True,
            allow_write=True,
            expected_count=1,
            expected_plan_sha256=plan["plan_sha256"],
        )
        bundle_dir = self.bundle.output_root / controller_bundle.BUNDLE_DIRECTORY_NAME
        (self.bundle.repo_root / "src/lecture_stt/stt/controller_evidence.py").write_text(
            "raise SystemExit(99)\n",
            encoding="utf-8",
        )
        poison_package = self.bundle.repo_root / "lecture_stt/stt"
        poison_package.mkdir(parents=True)
        (poison_package.parent / "__init__.py").write_text("", encoding="utf-8")
        (poison_package / "__init__.py").write_text("", encoding="utf-8")
        (poison_package / "controller_evidence.py").write_text(
            "raise SystemExit(98)\n",
            encoding="utf-8",
        )
        plist = plistlib.loads(
            (bundle_dir / controller_bundle.PLIST_FILENAME).read_bytes()
        )
        result = subprocess.run(
            plist["ProgramArguments"],
            cwd=plist["WorkingDirectory"],
            env={**os.environ, **plist["EnvironmentVariables"]},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        wrapper_result = json.loads(result.stdout)
        self.assertEqual(wrapper_result["outcome"], "success")

    def test_source_config_template_tamper_before_prepare_fails_closed(self) -> None:
        plan = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())
        self.bundle.source_binary.write_bytes(b"modified")
        with self.assertRaises(controller_bundle.ControllerBundleError):
            controller_bundle.prepare_controller_bundle(
                **self.bundle.plan_kwargs(),
                enable_prepare=True,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
            )
        write_file(self.bundle.source_binary, b"#!/bin/bash\necho source\n", executable=True)

        plan = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())
        self.bundle.config.write_text("changed: true\n", encoding="utf-8")
        with self.assertRaises(controller_bundle.ControllerBundleError):
            controller_bundle.prepare_controller_bundle(
                **self.bundle.plan_kwargs(),
                enable_prepare=True,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
            )
        write_file(self.bundle.config, "watch_root: {}\n".format(self.root))

        plan = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())
        template = self.bundle.repo_root / "controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template"
        template.write_bytes(b"<plist><dict/></plist>")
        with self.assertRaises(controller_bundle.ControllerBundleError):
            controller_bundle.prepare_controller_bundle(
                **self.bundle.plan_kwargs(),
                enable_prepare=True,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
            )
        self.assertFalse(
            (self.bundle.output_root / controller_bundle.BUNDLE_DIRECTORY_NAME).exists()
        )

    def test_kill_switch_tamper_before_write_fails_closed(self) -> None:
        plan = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())
        self.bundle.kill_switch.write_text("disabled", encoding="utf-8")
        with self.assertRaises(controller_bundle.ControllerBundleError):
            controller_bundle.prepare_controller_bundle(
                **self.bundle.plan_kwargs(),
                enable_prepare=True,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
            )
        self.assertFalse(
            (self.bundle.output_root / controller_bundle.BUNDLE_DIRECTORY_NAME).exists()
        )

    def _prepare_fresh_bundle(self) -> tuple[dict[str, object], Path]:
        bundle_dir = self.bundle.output_root / controller_bundle.BUNDLE_DIRECTORY_NAME
        remove_tree(bundle_dir)
        plan = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())
        controller_bundle.prepare_controller_bundle(
            **self.bundle.plan_kwargs(),
            enable_prepare=True,
            allow_write=True,
            expected_count=1,
            expected_plan_sha256=plan["plan_sha256"],
        )
        return plan, bundle_dir

    def _assert_replay_requires_recovery(self, plan: dict[str, object]) -> None:
        with self.assertRaises(
            controller_bundle.ControllerBundleRecoveryRequiredError
        ):
            controller_bundle.prepare_controller_bundle(
                **self.bundle.plan_kwargs(),
                enable_prepare=True,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256=str(plan["plan_sha256"]),
            )

    @staticmethod
    def _runtime_tree_sha256(entries: list[dict[str, object]]) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "directory_name": controller_bundle.RUNTIME_DIRECTORY_NAME,
                    "source_files": [
                        {
                            "bundle_relative_path": entry["bundle_relative_path"],
                            "sha256": entry["source_sha256"],
                        }
                        for entry in entries
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def _expected_runtime_sources(self) -> list[dict[str, object]]:
        return [
            {
                "bundle_relative_path": "lecture_stt/__init__.py",
            },
            {
                "bundle_relative_path": "lecture_stt/stt/__init__.py",
            },
            {
                "bundle_relative_path": "lecture_stt/stt/controller_evidence.py",
            },
            {
                "bundle_relative_path": "lecture_stt/stt/controller_readiness.py",
            },
        ]

    def test_plan_runtime_sources_are_ordered_and_tree_hash_exact(self) -> None:
        plan = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())
        runtime_contract = plan["runtime"]["source_files"]
        expected = self._expected_runtime_sources()
        self.assertEqual(
            [entry["bundle_relative_path"] for entry in runtime_contract],
            [entry["bundle_relative_path"] for entry in expected],
        )
        self.assertEqual(
            self._runtime_tree_sha256([dict(item) for item in runtime_contract]),
            plan["runtime"]["runtime_tree_sha256"],
        )

        bundle_dir = self.bundle.output_root / controller_bundle.BUNDLE_DIRECTORY_NAME
        result = controller_bundle.prepare_controller_bundle(
            **self.bundle.plan_kwargs(),
            enable_prepare=True,
            allow_write=True,
            expected_count=1,
            expected_plan_sha256=plan["plan_sha256"],
        )
        manifest = json.loads(
            (bundle_dir / controller_bundle.MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["runtime"]["source_files"],
            [dict(item) for item in runtime_contract],
        )
        self.assertEqual(
            manifest["runtime"]["tree_sha256"],
            plan["runtime"]["runtime_tree_sha256"],
        )

    def test_prepared_bundle_runtime_tree_is_exact_nested_immutable(self) -> None:
        plan = controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())
        controller_bundle.prepare_controller_bundle(
            **self.bundle.plan_kwargs(),
            enable_prepare=True,
            allow_write=True,
            expected_count=1,
            expected_plan_sha256=plan["plan_sha256"],
        )
        bundle_dir = self.bundle.output_root / controller_bundle.BUNDLE_DIRECTORY_NAME
        manifest = json.loads(
            (bundle_dir / controller_bundle.MANIFEST_FILENAME).read_text(encoding="utf-8")
        )

        runtime_root = bundle_dir / manifest["runtime"]["directory_name"]
        self.assertEqual(runtime_root.name, controller_bundle.RUNTIME_DIRECTORY_NAME)
        self.assertEqual(stat.S_IMODE(runtime_root.stat().st_mode), 0o500)
        self.assertEqual(stat.S_IMODE((runtime_root / "lecture_stt").stat().st_mode), 0o500)
        self.assertEqual(
            stat.S_IMODE((runtime_root / "lecture_stt/stt").stat().st_mode),
            0o500,
        )
        runtime_files = manifest["runtime"]["source_files"]
        observed_paths = sorted(
            relative_path.relative_to(runtime_root).as_posix()
            for relative_path in runtime_root.rglob("*")
            if relative_path.is_file()
        )
        self.assertEqual(
            observed_paths,
            [
                "lecture_stt/__init__.py",
                "lecture_stt/stt/__init__.py",
                "lecture_stt/stt/controller_evidence.py",
                "lecture_stt/stt/controller_readiness.py",
            ],
        )
        for entry in runtime_files:
            file_path = runtime_root / entry["bundle_relative_path"]
            self.assertTrue(file_path.is_file())
            self.assertEqual(stat.S_IMODE(file_path.stat().st_mode), 0o400)
            self.assertEqual(
                hashlib.sha256(file_path.read_bytes()).hexdigest(),
                entry["source_sha256"],
            )

    def test_prepared_runtime_tamper_unknown_entry_or_mode_hardlink_or_symlink_requires_recovery(self) -> None:
        plan, bundle_dir = self._prepare_fresh_bundle()
        runtime_root = bundle_dir / controller_bundle.RUNTIME_DIRECTORY_NAME
        set_runtime_tree_mode(runtime_root, 0o700)
        unknown = runtime_root / "unexpected.py"
        write_file(unknown, "x = 1\n",)
        set_runtime_tree_mode(runtime_root, 0o500)
        self._assert_replay_requires_recovery(plan)

        plan, bundle_dir = self._prepare_fresh_bundle()
        runtime_root = bundle_dir / controller_bundle.RUNTIME_DIRECTORY_NAME
        set_runtime_tree_mode(runtime_root, 0o700)
        unknown_directory = runtime_root / "unexpected"
        unknown_directory.mkdir()
        unknown_directory.chmod(0o500)
        set_runtime_tree_mode(runtime_root, 0o500)
        self._assert_replay_requires_recovery(plan)

        plan, bundle_dir = self._prepare_fresh_bundle()
        runtime_root = bundle_dir / controller_bundle.RUNTIME_DIRECTORY_NAME
        runtime_file = runtime_root / "lecture_stt/__init__.py"
        set_runtime_tree_mode(runtime_root, 0o700)
        runtime_file.chmod(0o600)
        set_runtime_tree_mode(runtime_root, 0o500)
        self._assert_replay_requires_recovery(plan)

        plan, bundle_dir = self._prepare_fresh_bundle()
        runtime_root = bundle_dir / controller_bundle.RUNTIME_DIRECTORY_NAME
        runtime_file = runtime_root / "lecture_stt/__init__.py"
        set_runtime_tree_mode(runtime_root, 0o700)
        hardlink_source = self.root / "runtime-hardlink-source"
        if hardlink_source.exists():
            hardlink_source.unlink()
        os.link(runtime_file, hardlink_source)
        runtime_file.unlink()
        os.link(hardlink_source, runtime_file)
        set_runtime_tree_mode(runtime_root, 0o500)
        self._assert_replay_requires_recovery(plan)
        hardlink_source.unlink()

        plan, bundle_dir = self._prepare_fresh_bundle()
        runtime_root = bundle_dir / controller_bundle.RUNTIME_DIRECTORY_NAME
        runtime_file = runtime_root / "lecture_stt/__init__.py"
        set_runtime_tree_mode(runtime_root, 0o700)
        runtime_file.unlink()
        runtime_file.symlink_to(
            str(self.root / "src/lecture_stt/stt/controller_evidence.py")
        )
        set_runtime_tree_mode(runtime_root, 0o500)
        self._assert_replay_requires_recovery(plan)

    def test_partial_extra_tampered_and_linked_artifacts_require_recovery(self) -> None:
        plan, bundle_dir = self._prepare_fresh_bundle()
        (bundle_dir / controller_bundle.MANIFEST_FILENAME).unlink()
        self._assert_replay_requires_recovery(plan)

        plan, bundle_dir = self._prepare_fresh_bundle()
        (bundle_dir / "extra.jsonl").write_text("x", encoding="utf-8")
        self._assert_replay_requires_recovery(plan)

        plan, bundle_dir = self._prepare_fresh_bundle()
        plist_path = bundle_dir / controller_bundle.PLIST_FILENAME
        plist_path.chmod(0o600)
        plist_path.write_text("tampered", encoding="utf-8")
        self._assert_replay_requires_recovery(plan)

        plan, bundle_dir = self._prepare_fresh_bundle()
        (bundle_dir / controller_bundle.MANIFEST_FILENAME).chmod(0o600)
        self._assert_replay_requires_recovery(plan)

        plan, bundle_dir = self._prepare_fresh_bundle()
        target = self.root / "artifact-target"
        write_file(target, b"target")
        plist_path = bundle_dir / controller_bundle.PLIST_FILENAME
        plist_path.unlink()
        plist_path.symlink_to(target)
        self._assert_replay_requires_recovery(plan)

        plan, bundle_dir = self._prepare_fresh_bundle()
        linked_copy = self.root / "linked-copy"
        linked_copy.unlink(missing_ok=True)
        os.link(bundle_dir / controller_bundle.PLIST_FILENAME, linked_copy)
        self._assert_replay_requires_recovery(plan)
        linked_copy.unlink()

    def test_output_root_must_be_temporary_root(self) -> None:
        with self.assertRaises(controller_bundle.ControllerBundleError):
            controller_bundle.plan_controller_bundle(
                **self.bundle.plan_kwargs() | {"output_root": str(REPO_ROOT)}
            )

    def test_review_artifact_bindings_must_stay_inside_system_temp(self) -> None:
        with self.assertRaises(controller_bundle.ControllerBundleError):
            controller_bundle.plan_controller_bundle(
                **self.bundle.plan_kwargs() | {"source_binary": "/bin/sh"}
            )

        with self.assertRaises(controller_bundle.ControllerBundleError):
            controller_bundle.plan_controller_bundle(
                **self.bundle.plan_kwargs()
                | {"config": str(REPO_ROOT / "controller/README.md")}
            )

        with self.assertRaises(controller_bundle.ControllerBundleError):
            controller_bundle.plan_controller_bundle(
                **self.bundle.plan_kwargs() | {"home": str(REPO_ROOT)}
            )

        outside_kill_switch = (
            REPO_ROOT / "controller/launchd/__review_only_never_create__.disabled"
        )
        self.assertFalse(outside_kill_switch.exists())
        with self.assertRaises(controller_bundle.ControllerBundleError):
            controller_bundle.plan_controller_bundle(
                **self.bundle.plan_kwargs()
                | {"kill_switch": str(outside_kill_switch)}
            )

        with self.assertRaises(controller_bundle.ControllerBundleError):
            controller_bundle.plan_controller_bundle(
                **self.bundle.plan_kwargs()
                | {"evidence_root": str(REPO_ROOT)}
            )

        self.bundle.evidence_root.chmod(0o755)
        with self.assertRaisesRegex(
            controller_bundle.ControllerBundleError,
            "owned stable 0700",
        ):
            controller_bundle.plan_controller_bundle(**self.bundle.plan_kwargs())

    def test_python_binary_leaf_symlink_is_resolved_but_source_symlink_still_fails_closed(self) -> None:
        real_python = self.root / "python-real"
        write_file(real_python, "#!/bin/sh\necho real-python\n", executable=True)
        linked_python = self.root / "python-link"
        linked_python.symlink_to(real_python.name)

        plan = controller_bundle.plan_controller_bundle(
            **self.bundle.plan_kwargs() | {"python_binary": str(linked_python)}
        )
        self.assertEqual(
            plan["bindings"]["python_binary_path_sha256"],
            hashlib.sha256(os.fsencode(os.path.realpath(linked_python))).hexdigest(),
        )

        real_source = self.root / "source-real"
        write_file(real_source, "#!/bin/sh\necho real-source\n", executable=True)
        linked_source = self.root / "source-link"
        linked_source.symlink_to(real_source.name)

        with self.assertRaises(controller_bundle.ControllerBundleError):
            controller_bundle.plan_controller_bundle(
                **self.bundle.plan_kwargs()
                | {"source_binary": str(linked_source)}
            )

    def test_cli_error_sanitizes_paths(self) -> None:
        temp_root = tempfile.TemporaryDirectory()
        try:
            outside = Path(temp_root.name) / "outside"
            outside.mkdir()
            payload = self.bundle.plan_kwargs()
            payload["repo_root"] = str(outside)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "lecture_stt.stt.controller_bundle",
                    "plan",
                    "--repo-root",
                    payload["repo_root"],
                    "--output-root",
                    payload["output_root"],
                    "--source-binary",
                    payload["source_binary"],
                    "--python-bin",
                    payload["python_binary"],
                    "--config",
                    payload["config"],
                    "--kill-switch",
                    payload["kill_switch"],
                    "--home",
                    payload["home"],
                    "--evidence-root",
                    payload["evidence_root"],
                ],
                env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("controller shadow bundle rejected:", result.stderr)
            self.assertNotIn(str(payload["repo_root"]), result.stderr)
        finally:
            temp_root.cleanup()


if __name__ == "__main__":
    unittest.main()
