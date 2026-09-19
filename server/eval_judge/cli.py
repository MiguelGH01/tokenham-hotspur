"""UTF-8 eval runner so Windows cp1252 does not choke on YAML accents.

Usage from server/: ``uv run clinic-eval evals/ -v``

``pipecat eval run`` only reads YAML in the directory you pass — not in
subfolders. This wrapper expands each directory argument recursively
(skipping ``__MACOSX`` and AppleDouble ``._*`` files) before handing
files to the Pipecat CLI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SKIP_DIRS = {"__MACOSX", ".git"}
_YAML = {".yaml", ".yml"}


def _yamls_under(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _YAML:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.name.startswith("._"):
            continue
        found.append(path)
    return sorted(found)


def expand_cli_args(argv: list[str]) -> list[str]:
    out: list[str] = []
    for item in argv:
        if item.startswith("-"):
            out.append(item)
            continue
        path = Path(item)
        if path.is_dir():
            found = _yamls_under(path)
            if not found:
                raise SystemExit(
                    f"Error: no .yaml or .yml scenario files found under {item}"
                )
            out.extend(str(p) for p in found)
        else:
            out.append(item)
    return out


def main() -> None:
    if os.environ.get("_CLINIC_EVAL_UTF8") != "1":
        env = os.environ.copy()
        env["_CLINIC_EVAL_UTF8"] = "1"
        env["PYTHONUTF8"] = "1"
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.dirname(os.path.dirname(__file__)), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        raise SystemExit(
            subprocess.call(
                [sys.executable, "-X", "utf8", "-m", "eval_judge.cli", *sys.argv[1:]],
                env=env,
            )
        )
    sys.argv = ["pipecat", "eval", "run", *expand_cli_args(sys.argv[1:])]
    from pipecat.cli.main import run

    run()


if __name__ == "__main__":
    main()
