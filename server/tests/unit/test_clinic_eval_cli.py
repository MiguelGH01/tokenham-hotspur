"""Tests for clinic-eval directory expansion."""

from pathlib import Path

import pytest

from eval_judge.cli import expand_cli_args


def test_expands_nested_yaml_and_skips_macosx(tmp_path: Path):
    (tmp_path / "PR-01").mkdir()
    (tmp_path / "PR-01" / "a.yaml").write_text("name: a\nturns: []\n", encoding="utf-8")
    nested = tmp_path / "__MACOSX" / "evals"
    nested.mkdir(parents=True)
    (nested / "._a.yaml").write_text("junk", encoding="utf-8")

    out = expand_cli_args(["-v", str(tmp_path), "-d"])

    assert out[0] == "-v"
    assert out[-1] == "-d"
    assert len(out) == 3
    assert Path(out[1]).name == "a.yaml"


def test_empty_dir_exits(tmp_path: Path):
    with pytest.raises(SystemExit):
        expand_cli_args([str(tmp_path)])
