from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.hermes_postprocess.cli import main
else:  # pragma: no cover - exercised by module execution
    from .cli import main


if __name__ == "__main__":
    raise SystemExit(main(["validate-summary", *sys.argv[1:]]))
