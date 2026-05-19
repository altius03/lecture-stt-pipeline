"""교정 워커 CLI 진입점.

사용법:
    python -m lecture_stt.correction.main [--once] [--dry-run] [--config PATH]
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from lecture_stt.correction.worker import CorrectionWorker, load_correction_config


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(handler)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lecture transcript correction worker")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List pending files without calling the API",
    )
    parser.add_argument("--config", default="config/config.yaml", help="Path to config.yaml")
    return parser.parse_args()


def main() -> None:
    _setup_logging()
    args = _parse_args()

    config = load_correction_config(args.config)

    if args.dry_run:
        # dry-run: 설정 확인 후 pending 파일 목록만 출력하고 종료
        from lecture_stt.correction.worker import CorrectionWorker as W
        worker = W(config)
        pairs = worker._collect_pending_pairs()
        if not pairs:
            print("dry-run: nothing pending")
        else:
            print(f"dry-run: {len(pairs)} pair(s) pending")
            for p in pairs:
                print(f"  {p.stem}  subject={p.subject_abbr}")
        return

    worker = CorrectionWorker(config)

    try:
        while True:
            stats = worker.scan_once()
            logging.info("correction stats: %s", stats)
            if args.once:
                return
            time.sleep(config.scan_interval_sec)
    except KeyboardInterrupt:
        logging.info("Correction worker interrupted")


if __name__ == "__main__":
    main()
