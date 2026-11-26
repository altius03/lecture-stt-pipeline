from __future__ import annotations

import fcntl
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.stt import controller_activation
from lecture_stt.stt import controller_bundle


_PATH_VALUE = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
_RECORD_RE = re.compile(r"^(?P<sequence>\d+)\.((install)|(uninstall)|(rollback))\.json$")


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


def _replace_path(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    path.write_bytes(payload)


def _replace_text(path: Path, payload: str) -> None:
    _replace_path(path, payload.encode("utf-8"))


def _remove_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    for current in sorted(path.rglob("*"), reverse=True):
        try:
            current.chmod(0o700 if current.is_dir() else 0o600)
        except OSError:
            pass
    try:
        path.chmod(0o700)
    except OSError:
        pass
    shutil.rmtree(path)


def _write_repo_fixture(root: Path) -> None:
    (root / "launchd").mkdir(parents=True, exist_ok=True)
    (root / "controller/launchd").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "src/lecture_stt/stt").mkdir(parents=True, exist_ok=True)
    _replace_path(
        root / "src/lecture_stt/__init__.py",
        (REPO_ROOT / "src/lecture_stt/__init__.py").read_bytes(),
    )
    _replace_path(
        root / "src/lecture_stt/stt/__init__.py",
        (REPO_ROOT / "src/lecture_stt/stt/__init__.py").read_bytes(),
    )

    for label, stdout_name, stderr_name in [
        ("com.geonha.lecture-stt", "launchd.out.log", "launchd.err.log"),
        ("com.geonha.lecture-stt-cleanup", "cleanup.out.log", "cleanup.err.log"),
        ("com.geonha.lecture-stt-distribute", "downstream.out.log", "downstream.err.log"),
        ("com.geonha.lecture-stt-webpanel", "webpanel.out.log", "webpanel.err.log"),
    ]:
        _replace_path(root / "launchd" / f"{label}.plist", _plist_xml(_existing_plist(label, stdout_name=stdout_name, stderr_name=stderr_name)))

    _replace_path(
        root / "controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template",
        (REPO_ROOT / "controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template").read_bytes(),
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
    _replace_path(
        root / "src/lecture_stt/stt/controller_evidence.py",
        (REPO_ROOT / "src/lecture_stt/stt/controller_evidence.py").read_bytes(),
    )
    _replace_path(
        root / "src/lecture_stt/stt/controller_readiness.py",
        (REPO_ROOT / "src/lecture_stt/stt/controller_readiness.py").read_bytes(),
    )


def _write_file(path: Path, payload: bytes | str = b"", *, executable: bool = False) -> None:
    mode = 0o700 if executable else 0o600
    binary = payload.encode("utf-8") if isinstance(payload, str) else payload
    path.write_bytes(binary)
    path.chmod(mode)


class ControllerActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        _write_repo_fixture(self.root)

        self.repo_root = self.root
        self.output_root = self.root / "bundle-output"
        self.output_root.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.evidence_root = self.root / "evidence"
        self.evidence_root.mkdir()
        self.evidence_root.chmod(0o700)

        self.source_binary = self.root / "source-controller"
        _write_file(self.source_binary, b"#!/bin/bash\necho source\n", executable=True)

        self.python_binary = self.root / "python-bin"
        _write_file(self.python_binary, "#!/bin/bash\necho python\n", executable=True)

        self.config = self.root / "config.yaml"
        _write_file(self.config, "watch_root: {}\n".format(self.root))

        self.kill_switch = self.root / "controller.disabled"
        self.kill_switch.unlink(missing_ok=True)

        self.activation_root = self.root / "activation"
        self.activation_root.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _bundle_kwargs(context: "ControllerActivationTests") -> dict[str, str]:
        return {
            "repo_root": str(context.repo_root),
            "output_root": str(context.output_root),
            "source_binary": str(context.source_binary),
            "python_binary": str(context.python_binary),
            "config": str(context.config),
            "kill_switch": str(context.kill_switch),
            "home": str(context.home),
            "evidence_root": str(context.evidence_root),
        }

    @staticmethod
    def _canonical_sha(plan: dict[str, object]) -> str:
        unsigned = dict(plan)
        unsigned.pop("plan_sha256")
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _prepare_bundle(self) -> tuple[dict[str, object], Path]:
        bundle_dir = self.output_root / controller_bundle.BUNDLE_DIRECTORY_NAME
        if bundle_dir.exists() or bundle_dir.is_symlink():
            if bundle_dir.is_dir() and not bundle_dir.is_symlink():
                _remove_tree(bundle_dir)
            else:
                bundle_dir.unlink()
        plan = controller_bundle.plan_controller_bundle(**self._bundle_kwargs(self))
        result = controller_bundle.prepare_controller_bundle(
            **self._bundle_kwargs(self),
            enable_prepare=True,
            allow_write=True,
            expected_count=1,
            expected_plan_sha256=plan["plan_sha256"],
        )
        self.assertEqual(result["status"], "prepared")
        return dict(plan), bundle_dir

    def _plan_install(self, bundle_dir: Path) -> dict[str, object]:
        return controller_activation.plan_install(
            activation_root=str(self.activation_root),
            bundle_root=str(bundle_dir),
        )

    def _apply_install(self, bundle_dir: Path, *, expected_count: int = 1, expected_plan_sha256: str) -> dict[str, object]:
        return controller_activation.apply_install(
            activation_root=str(self.activation_root),
            bundle_root=str(bundle_dir),
            enable_activation=True,
            allow_write=True,
            expected_count=expected_count,
            expected_plan_sha256=expected_plan_sha256,
        )

    def _apply_install_with_plan(self, bundle_dir: Path) -> dict[str, object]:
        plan = self._plan_install(bundle_dir)
        return self._apply_install(
            bundle_dir,
            expected_count=int(plan["expected_count"]),
            expected_plan_sha256=str(plan["plan_sha256"]),
        )

    def _apply_uninstall(self) -> dict[str, object]:
        plan = controller_activation.plan_uninstall(activation_root=str(self.activation_root))
        return controller_activation.apply_uninstall(
            activation_root=str(self.activation_root),
            enable_activation=True,
            allow_write=True,
            expected_count=int(plan["expected_count"]),
            expected_plan_sha256=str(plan["plan_sha256"]),
        )

    def _apply_rollback(self, *, target_plan_sha256: str) -> dict[str, object]:
        plan = controller_activation.plan_rollback(
            activation_root=str(self.activation_root),
            target_plan_sha256=target_plan_sha256,
        )
        return controller_activation.apply_rollback(
            activation_root=str(self.activation_root),
            target_plan_sha256=target_plan_sha256,
            enable_activation=True,
            allow_write=True,
            expected_count=int(plan["expected_count"]),
            expected_plan_sha256=str(plan["plan_sha256"]),
        )

    def _assert_apply_result_contract(
        self,
        result: dict[str, object],
        *,
        expected_action: str,
        expected_active: bool,
    ) -> None:
        self.assertEqual(result["schema_version"], controller_activation.RESULT_SCHEMA_VERSION)
        self.assertEqual(result["action"], expected_action)
        self.assertEqual(result["expected_count"], 1)
        self.assertEqual(result["active"], expected_active)
        self.assertEqual(result["scope"], "temporary_offline")
        self.assertFalse(result["launchctl_invoked"])
        self.assertFalse(result["operational_install_supported"])
        self.assertIs(result["python_polling_authority"], True)
        self.assertNotIn("path", "".join(str(value) for value in result.values()))

    def _latest_sequence_records(self) -> list[str]:
        return sorted(
            p
            for p in self.activation_root.iterdir()
            if _RECORD_RE.match(p.name)
        )

    def _version_dir(self, plan_sha256: str) -> Path:
        return self.activation_root / controller_activation.VERSIONS_DIRECTORY_NAME / plan_sha256

    @staticmethod
    def _expected_runtime_sources() -> list[str]:
        return [
            "lecture_stt/__init__.py",
            "lecture_stt/stt/__init__.py",
            "lecture_stt/stt/controller_evidence.py",
            "lecture_stt/stt/controller_readiness.py",
        ]

    @staticmethod
    def _set_runtime_tree_mode(runtime_root: Path, mode: int) -> None:
        for candidate in (runtime_root, runtime_root / "lecture_stt", runtime_root / "lecture_stt/stt"):
            candidate.chmod(mode)

    def test_plan_install_is_deterministic_with_exact_sha(self) -> None:
        bundle_plan, bundle_dir = self._prepare_bundle()
        _ = self._plan_install(bundle_dir)
        first = self._plan_install(bundle_dir)
        second = self._plan_install(bundle_dir)
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(first["plan_sha256"], self._canonical_sha(first))
        self.assertEqual(bundle_plan["expected_count"], 1)
        self.assertEqual(first["expected_count"], 1)
        self.assertEqual(first["schema_version"], controller_activation.PLAN_SCHEMA_VERSION)
        self.assertEqual(first["scope"], "temporary_offline")

    def test_apply_guards_require_exact_plan_and_flag_contract(self) -> None:
        _, bundle_dir = self._prepare_bundle()
        plan = self._plan_install(bundle_dir)
        with self.assertRaises(controller_activation.ControllerActivationWriteDisabledError):
            controller_activation.apply_install(
                activation_root=str(self.activation_root),
                bundle_root=str(bundle_dir),
                enable_activation=False,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256=str(plan["plan_sha256"]),
            )
        with self.assertRaises(controller_activation.ControllerActivationWriteDisabledError):
            controller_activation.apply_install(
                activation_root=str(self.activation_root),
                bundle_root=str(bundle_dir),
                enable_activation=True,
                allow_write=False,
                expected_count=1,
                expected_plan_sha256=str(plan["plan_sha256"]),
            )
        with self.assertRaises(controller_activation.ControllerActivationError):
            controller_activation.apply_install(
                activation_root=str(self.activation_root),
                bundle_root=str(bundle_dir),
                enable_activation=True,
                allow_write=True,
                expected_count=2,
                expected_plan_sha256=str(plan["plan_sha256"]),
            )
        with self.assertRaises(controller_activation.ControllerActivationError):
            controller_activation.apply_install(
                activation_root=str(self.activation_root),
                bundle_root=str(bundle_dir),
                enable_activation=True,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256="bad-hash",
            )

    def test_install_apply_then_replay_is_skipped(self) -> None:
        _, bundle_dir = self._prepare_bundle()
        plan = self._plan_install(bundle_dir)
        first = self._apply_install(
            bundle_dir,
            expected_count=int(plan["expected_count"]),
            expected_plan_sha256=str(plan["plan_sha256"]),
        )
        next_plan = self._plan_install(bundle_dir)
        second = self._apply_install(
            bundle_dir,
            expected_count=int(next_plan["expected_count"]),
            expected_plan_sha256=str(next_plan["plan_sha256"]),
        )
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(first["action"], "install")
        self.assertEqual(second["action"], "install")
        self.assertTrue(first["active"])
        self._assert_apply_result_contract(first, expected_action="install", expected_active=True)
        self._assert_apply_result_contract(second, expected_action="install", expected_active=True)

    def test_uninstall_apply_then_replay_is_skipped(self) -> None:
        _, bundle_dir = self._prepare_bundle()
        self._apply_install_with_plan(bundle_dir)
        first = self._apply_uninstall()
        second = self._apply_uninstall()
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "skipped")
        self.assertFalse(first["active"])
        self.assertFalse(second["active"])
        self._assert_apply_result_contract(first, expected_action="uninstall", expected_active=False)
        self._assert_apply_result_contract(second, expected_action="uninstall", expected_active=False)

    def test_rollback_apply_then_replay_is_skipped(self) -> None:
        _, bundle_dir = self._prepare_bundle()
        install_plan = self._plan_install(bundle_dir)
        self._apply_install(
            bundle_dir,
            expected_count=int(install_plan["expected_count"]),
            expected_plan_sha256=str(install_plan["plan_sha256"]),
        )
        uninstall = self._apply_uninstall()
        self.assertFalse(uninstall["active"])

        rollback_plan = controller_activation.plan_rollback(
            activation_root=str(self.activation_root),
            target_plan_sha256=str(install_plan["source_version"]["plan_sha256"]),
        )
        first = self._apply_rollback(target_plan_sha256=str(install_plan["source_version"]["plan_sha256"]))
        second = self._apply_rollback(target_plan_sha256=str(install_plan["source_version"]["plan_sha256"]))
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "skipped")
        self.assertTrue(first["active"])
        self.assertTrue(second["active"])
        self._assert_apply_result_contract(first, expected_action="rollback", expected_active=True)
        self._assert_apply_result_contract(second, expected_action="rollback", expected_active=True)

    def test_immutable_mode_contract_for_snapshot_and_records(self) -> None:
        _, bundle_dir = self._prepare_bundle()
        plan = self._plan_install(bundle_dir)
        result = self._apply_install(
            bundle_dir,
            expected_count=int(plan["expected_count"]),
            expected_plan_sha256=str(plan["plan_sha256"]),
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["status"], "applied")
        versions = self.activation_root / controller_activation.VERSIONS_DIRECTORY_NAME
        plan_sha = str(result["active_plan_sha256"])
        version = self._version_dir(plan_sha)
        self.assertEqual(stat.S_IMODE(versions.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(version.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((version / controller_bundle.BINARY_FILENAME).stat().st_mode), 0o500)
        self.assertEqual(stat.S_IMODE((version / controller_bundle.PLIST_FILENAME).stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE((version / controller_bundle.MANIFEST_FILENAME).stat().st_mode), 0o400)
        self.assertEqual(
            stat.S_IMODE(
                (version / controller_bundle.RUNTIME_DIRECTORY_NAME).stat().st_mode
            ),
            controller_bundle.RUNTIME_DIRECTORY_MODE,
        )
        self.assertEqual(stat.S_IMODE((self.activation_root / controller_activation.LOCK_FILENAME).stat().st_mode), 0o600)
        record = self.activation_root / "00000001.install.json"
        self.assertTrue(record.exists())
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o400)

    def test_activation_install_copies_exact_runtime_tree_to_version_store(self) -> None:
        _, bundle_dir = self._prepare_bundle()
        install_plan = self._plan_install(bundle_dir)
        self._apply_install(
            bundle_dir,
            expected_count=int(install_plan["expected_count"]),
            expected_plan_sha256=str(install_plan["plan_sha256"]),
        )

        bundle_manifest = json.loads(
            (bundle_dir / controller_bundle.MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        version = self._version_dir(str(install_plan["source_version"]["plan_sha256"]))
        version_manifest = json.loads(
            (version / controller_bundle.MANIFEST_FILENAME).read_text(encoding="utf-8")
        )

        self.assertEqual(
            version_manifest["runtime"]["source_files"],
            bundle_manifest["runtime"]["source_files"],
        )
        self.assertEqual(
            version_manifest["runtime"]["tree_sha256"],
            bundle_manifest["runtime"]["tree_sha256"],
        )
        runtime_root = version / controller_bundle.RUNTIME_DIRECTORY_NAME
        self.assertEqual(
            runtime_root.stat().st_mode & 0o777, controller_bundle.RUNTIME_DIRECTORY_MODE
        )
        self.assertEqual(
            sorted(
                path.relative_to(runtime_root).as_posix()
                for path in runtime_root.rglob("*")
                if path.is_file()
            ),
            self._expected_runtime_sources(),
        )
        for entry in version_manifest["runtime"]["source_files"]:
            file_path = runtime_root / entry["bundle_relative_path"]
            self.assertEqual(
                file_path.stat().st_mode & 0o777, controller_bundle.RUNTIME_FILE_MODE
            )
            self.assertEqual(
                hashlib.sha256(file_path.read_bytes()).hexdigest(),
                entry["source_sha256"],
            )

    def test_activation_version_runtime_tamper_mode_link_symlink_missing_or_unknown_requires_recovery(self) -> None:
        for scenario in (
            "unknown",
            "unknown_directory",
            "mode",
            "hardlink",
            "symlink",
            "missing",
        ):
            with self.subTest(scenario=scenario):
                if self.activation_root.exists():
                    _remove_tree(self.activation_root)
                self.activation_root.mkdir(mode=0o700)
                _, bundle_dir = self._prepare_bundle()
                install_plan = self._plan_install(bundle_dir)
                self._apply_install(
                    bundle_dir,
                    expected_count=int(install_plan["expected_count"]),
                    expected_plan_sha256=str(install_plan["plan_sha256"]),
                )

                version = self._version_dir(str(install_plan["source_version"]["plan_sha256"]))
                runtime = version / controller_bundle.RUNTIME_DIRECTORY_NAME
                target = runtime / "lecture_stt/__init__.py"
                if scenario == "unknown":
                    self._set_runtime_tree_mode(runtime, 0o700)
                    _write_file(runtime / "unexpected_runtime_file.py", "x = 1\n")
                    self._set_runtime_tree_mode(runtime, 0o500)
                elif scenario == "unknown_directory":
                    self._set_runtime_tree_mode(runtime, 0o700)
                    unexpected = runtime / "unexpected"
                    unexpected.mkdir()
                    unexpected.chmod(0o500)
                    self._set_runtime_tree_mode(runtime, 0o500)
                elif scenario == "mode":
                    self._set_runtime_tree_mode(runtime, 0o700)
                    target.chmod(0o600)
                    self._set_runtime_tree_mode(runtime, 0o500)
                elif scenario == "hardlink":
                    self._set_runtime_tree_mode(runtime, 0o700)
                    hardlink_source = self.root / "activation-runtime-hardlink-source"
                    if hardlink_source.exists():
                        hardlink_source.unlink()
                    os.link(target, hardlink_source)
                    target.unlink()
                    os.link(hardlink_source, target)
                    self._set_runtime_tree_mode(runtime, 0o500)
                    # Keep the extra hardlink to preserve nlink > 1 for drift check.
                elif scenario == "symlink":
                    self._set_runtime_tree_mode(runtime, 0o700)
                    target.unlink()
                    target.symlink_to(
                        str(self.root / "src/lecture_stt/stt/controller_evidence.py")
                    )
                    self._set_runtime_tree_mode(runtime, 0o500)
                else:
                    self._set_runtime_tree_mode(runtime, 0o700)
                    target.unlink()
                    self._set_runtime_tree_mode(runtime, 0o500)

                with self.assertRaises(controller_activation.ControllerActivationRecoveryRequiredError):
                    controller_activation.plan_install(
                        activation_root=str(self.activation_root),
                        bundle_root=str(bundle_dir),
                    )

    def test_orphan_complete_recovery_requires_exact_runtime_tree(self) -> None:
        _, bundle_dir = self._prepare_bundle()
        install_plan = self._plan_install(bundle_dir)
        self._apply_install_with_plan(bundle_dir)
        self._apply_uninstall()
        for record in self._latest_sequence_records():
            record.unlink()

        version = self._version_dir(str(install_plan["source_version"]["plan_sha256"]))
        runtime_target = version / controller_bundle.RUNTIME_DIRECTORY_NAME / "lecture_stt/__init__.py"
        runtime_root = version / controller_bundle.RUNTIME_DIRECTORY_NAME
        self._set_runtime_tree_mode(runtime_root, 0o700)
        runtime_target.unlink()
        _write_file(runtime_target, "tampered = True\n")
        self._set_runtime_tree_mode(runtime_root, 0o500)
        with self.assertRaises(controller_activation.ControllerActivationRecoveryRequiredError):
            self._plan_install(bundle_dir=bundle_dir)

    def test_tamper_bundle_before_apply_fails_closed(self) -> None:
        plan, bundle_dir = self._prepare_bundle()
        manifest = bundle_dir / controller_bundle.MANIFEST_FILENAME
        manifest.chmod(0o600)
        manifest_bytes = manifest.read_bytes()
        manifest.write_bytes(manifest_bytes + b"!!")
        with self.assertRaises(controller_activation.ControllerActivationRecoveryRequiredError):
            self._apply_install(
                bundle_dir,
                expected_count=1,
                expected_plan_sha256=str(plan["plan_sha256"]),
            )
        manifest.chmod(0o400)

    def test_tamper_record_version_manifest_mode_symlink_hardlink_and_hash_chain(self) -> None:
        plan, bundle_dir = self._prepare_bundle()
        apply_plan = self._plan_install(bundle_dir)
        applied = self._apply_install(
            bundle_dir,
            expected_count=int(apply_plan["expected_count"]),
            expected_plan_sha256=str(apply_plan["plan_sha256"]),
        )
        self.assertEqual(applied["status"], "applied")
        version = self._version_dir(str(apply_plan["source_version"]["plan_sha256"]))
        self.assertTrue(version.exists())

        manifest = version / controller_bundle.MANIFEST_FILENAME
        manifest_payload = manifest.read_bytes()
        manifest.chmod(0o600)
        manifest.write_text("bad", encoding="utf-8")
        with self.assertRaises(controller_activation.ControllerActivationRecoveryRequiredError):
            controller_activation.plan_uninstall(activation_root=str(self.activation_root))
        manifest.write_bytes(manifest_payload)
        manifest.chmod(0o400)

        os.chmod(version / controller_bundle.PLIST_FILENAME, 0o600)
        with self.assertRaises(controller_activation.ControllerActivationRecoveryRequiredError):
            controller_activation.plan_uninstall(activation_root=str(self.activation_root))
        os.chmod(version / controller_bundle.PLIST_FILENAME, 0o400)

        external = self.root / "external-target"
        _write_file(external, b"external")
        os.unlink(version / controller_bundle.PLIST_FILENAME)
        (version / controller_bundle.PLIST_FILENAME).symlink_to(external)
        with self.assertRaises(controller_activation.ControllerActivationRecoveryRequiredError):
            controller_activation.plan_uninstall(activation_root=str(self.activation_root))
        (version / controller_bundle.PLIST_FILENAME).unlink()
        _write_file(version / controller_bundle.PLIST_FILENAME, b"plist", )
        os.chmod(version / controller_bundle.PLIST_FILENAME, 0o400)

        linked_copy = version / "plist-copy"
        os.link(version / controller_bundle.PLIST_FILENAME, linked_copy)
        with self.assertRaises(controller_activation.ControllerActivationRecoveryRequiredError):
            controller_activation.plan_uninstall(activation_root=str(self.activation_root))
        linked_copy.unlink()

        runtime_dir = version / controller_bundle.RUNTIME_DIRECTORY_NAME / "lecture_stt"
        runtime_dir.chmod(0o700)
        runtime_extra = runtime_dir / "unexpected.py"
        runtime_extra.write_text("x = 1\n", encoding="utf-8")
        runtime_extra.chmod(0o400)
        with self.assertRaises(controller_activation.ControllerActivationRecoveryRequiredError):
            controller_activation.plan_uninstall(activation_root=str(self.activation_root))
        runtime_extra.unlink()
        runtime_dir.chmod(controller_bundle.RUNTIME_DIRECTORY_MODE)

        # hash-chain tamper: corrupt the only record payload
        records = self._latest_sequence_records()
        self.assertTrue(records)
        first_record = self.activation_root / records[0].name
        first_record.chmod(0o600)
        first_record.write_text("{}", encoding="utf-8")
        with self.assertRaises(controller_activation.ControllerActivationRecoveryRequiredError):
            controller_activation.plan_install(
                activation_root=str(self.activation_root),
                bundle_root=str(bundle_dir),
            )
        first_record.chmod(0o400)

    def test_non_temp_or_non_0700_activation_root_is_rejected(self) -> None:
        _, bundle_dir = self._prepare_bundle()
        with self.assertRaises(controller_activation.ControllerActivationError):
            controller_activation.plan_install(
                activation_root=str(REPO_ROOT),
                bundle_root=str(bundle_dir),
            )

        bad_mode_root = self.root / "non_temp_bad_mode_activation"
        bad_mode_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        bad_mode_root.chmod(0o700)
        bad_mode_root.chmod(0o755)
        with self.assertRaisesRegex(
            controller_activation.ControllerActivationError,
            "owned stable 0700",
        ):
            controller_activation.plan_install(
                activation_root=str(bad_mode_root),
                bundle_root=str(bundle_dir),
            )
        self.activation_root = self.root / "activation"

    def test_busy_lock_blocks_apply(self) -> None:
        _, bundle_dir = self._prepare_bundle()
        plan = self._plan_install(bundle_dir)
        lock_path = self.activation_root / controller_activation.LOCK_FILENAME
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            with self.assertRaises(controller_activation.ControllerActivationBusyError):
                self._apply_install(
                    bundle_dir,
                    expected_count=int(plan["expected_count"]),
                    expected_plan_sha256=str(plan["plan_sha256"]),
                )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def test_orphan_complete_forward_recovery_partial_orphan_recovery_required(self) -> None:
        plan, bundle_dir = self._prepare_bundle()
        self._apply_install_with_plan(bundle_dir)
        self._apply_uninstall()
        for record in self._latest_sequence_records():
            record.unlink()

        # complete orphan is recoverable as a forward install
        orphan_plan = self._plan_install(bundle_dir)
        recovered = self._apply_install_with_plan(bundle_dir)
        self.assertEqual(recovered["status"], "applied")

        version = self._version_dir(str(orphan_plan["source_version"]["plan_sha256"]))
        (version / controller_bundle.PLIST_FILENAME).unlink()
        with self.assertRaises(controller_activation.ControllerActivationRecoveryRequiredError):
            self._plan_install(bundle_dir=bundle_dir)

    def test_cli_path_sanitization(self) -> None:
        _, bundle_dir = self._prepare_bundle()
        temp_root = tempfile.TemporaryDirectory()
        try:
            outside = Path(temp_root.name) / "outside"
            outside.mkdir()
            command = [
                sys.executable,
                "-m",
                "lecture_stt.stt.controller_activation",
                "plan-install",
                "--activation-root",
                str(REPO_ROOT),
                "--bundle-root",
                str(bundle_dir),
            ]
            result = subprocess.run(
                command,
                env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("controller offline activation rejected:", result.stderr)
            self.assertNotIn(str(REPO_ROOT), result.stderr)
        finally:
            temp_root.cleanup()


if __name__ == "__main__":
    unittest.main()
