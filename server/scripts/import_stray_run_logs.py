#!/usr/bin/env python3
"""Normalize stray flat run-logs (e.g. from a teammate's older/Windows build)
into the standard per-call `run-logs/<transport>-<label>-<timestamp>/bot.log`
directory layout used by the current bot.

A stray log is a loose `run-logs/*.log.txt` file, produced when a bot instance
predates the per-call subdirectory convention. This script:

1. Finds stray `*.log.txt` files directly under run-logs/.
2. Deduplicates byte-identical copies (only one gets imported).
3. Derives a timestamp from the "Writing logs to ...bot-<timestamp>.log" line
   in the file content (falling back to the filename, then mtime).
4. Derives a label from the Windows username in that same path
   (`C:\\Users\\<name>\\...`), falling back to "unknown".
5. Creates `run-logs/<transport>-<label>-<timestamp>/bot.log` and moves the
   content there, removing the stray file(s).

Usage:
    uv run python scripts/import_stray_run_logs.py [--transport twilio] [--dry-run]
"""

import argparse
import hashlib
import re
from pathlib import Path

RUN_LOGS_DIR = Path(__file__).resolve().parent.parent / "run-logs"

TIMESTAMP_RE = re.compile(r"bot-(\d{8}-\d{6})\.log")
WINDOWS_USER_RE = re.compile(r"[Uu]sers\\([^\\]+)\\")


def derive_timestamp(text: str, path: Path) -> str:
    match = TIMESTAMP_RE.search(text) or TIMESTAMP_RE.search(path.name)
    if match:
        return match.group(1)
    return __import__("datetime").datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d-%H%M%S")


def derive_label(text: str) -> str:
    match = WINDOWS_USER_RE.search(text)
    return match.group(1).lower() if match else "unknown"


def import_stray_logs(transport: str, dry_run: bool) -> None:
    stray_files = sorted(RUN_LOGS_DIR.glob("*.log.txt"))
    if not stray_files:
        print("No stray *.log.txt files found.")
        return

    seen_hashes: dict[str, Path] = {}
    for path in stray_files:
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()

        if digest in seen_hashes:
            print(f"Duplicate of {seen_hashes[digest].name}, removing {path.name}")
            if not dry_run:
                path.unlink()
            continue
        seen_hashes[digest] = path

        text = content.decode("utf-8", errors="replace")
        timestamp = derive_timestamp(text, path)
        label = derive_label(text)
        dest_dir = RUN_LOGS_DIR / f"{transport}-{label}-{timestamp}"
        dest_file = dest_dir / "bot.log"

        print(f"{path.name} -> {dest_file.relative_to(RUN_LOGS_DIR.parent)}")
        if not dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_file.write_bytes(content)
            path.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", default="twilio", help="Transport prefix to use (default: twilio)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without writing/deleting")
    args = parser.parse_args()

    import_stray_logs(args.transport, args.dry_run)
