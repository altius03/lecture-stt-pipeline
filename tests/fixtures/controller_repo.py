import plistlib
import textwrap
from pathlib import Path

from .controller_files import replace_file, write_file

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHD_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"


def plist_xml(mapping: dict[str, object]) -> bytes:
    return plistlib.dumps(mapping, sort_keys=False)


def existing_plist(label: str, *, stdout_name: str, stderr_name: str) -> dict[str, object]:
    return {
        "EnvironmentVariables": {"PATH": LAUNCHD_PATH},
        "Label": label,
        "ProgramArguments": ["/bin/bash", f"__REPO_ROOT__/scripts/{label}.sh"],
        "StandardErrorPath": f"__HOME__/Library/Logs/lecture_stt/{stderr_name}",
        "StandardOutPath": f"__HOME__/Library/Logs/lecture_stt/{stdout_name}",
        "WorkingDirectory": "__REPO_ROOT__",
    }


def write_launchd_repo(root: Path) -> None:
    """Build just the launchd templates and setup script used by topology preflight."""
    (root / "launchd").mkdir(parents=True, exist_ok=True)
    (root / "controller/launchd").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)

    for label, log_prefix in (
        ("com.geonha.lecture-stt", "launchd"),
        ("com.geonha.lecture-stt-cleanup", "cleanup"),
        ("com.geonha.lecture-stt-distribute", "downstream"),
        ("com.geonha.lecture-stt-webpanel", "webpanel"),
    ):
        payload = existing_plist(
            label,
            stdout_name=f"{log_prefix}.out.log",
            stderr_name=f"{log_prefix}.err.log",
        )
        replace_file(root / "launchd" / f"{label}.plist", plist_xml(payload))

    candidate = Path("controller/launchd/com.geonha.lecture-stt-controller-shadow.plist.template")
    replace_file(root / candidate, (REPO_ROOT / candidate).read_bytes())
    setup_script = textwrap.dedent(
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
    )
    replace_file(root / "scripts/setup_launchd.sh", setup_script.encode("utf-8"))


class BundleFixture:
    """Prepare a launchd repository with the runtime and inputs for bundle planning."""

    def __init__(self, root: Path) -> None:
        self.repo_root = root
        write_launchd_repo(root)
        (root / "src/lecture_stt/stt").mkdir(parents=True, exist_ok=True)
        for source in (
            "src/lecture_stt/__init__.py",
            "src/lecture_stt/stt/__init__.py",
            "src/lecture_stt/stt/controller_evidence.py",
            "src/lecture_stt/stt/controller_readiness.py",
        ):
            replace_file(root / source, (REPO_ROOT / source).read_bytes())

        self.output_root = root / "bundle-output"
        self.output_root.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.evidence_root = root / "evidence"
        self.evidence_root.mkdir()
        self.evidence_root.chmod(0o700)

        self.source_binary = root / "source-controller"
        write_file(self.source_binary, "#!/bin/bash\necho source\n", executable=True)
        self.python_binary = root / "python-bin"
        write_file(self.python_binary, "#!/bin/bash\necho python\n", executable=True)
        self.config = root / "config.yaml"
        write_file(self.config, f"watch_root: {root}\n")
        self.kill_switch = root / "controller.disabled"
        self.kill_switch.unlink(missing_ok=True)

    def plan_kwargs(self) -> dict[str, str]:
        return {
            "repo_root": str(self.repo_root),
            "output_root": str(self.output_root),
            "source_binary": str(self.source_binary),
            "python_binary": str(self.python_binary),
            "config": str(self.config),
            "kill_switch": str(self.kill_switch),
            "home": str(self.home),
            "evidence_root": str(self.evidence_root),
        }
