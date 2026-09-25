"""Mutation testing のラチェットが検出力の低下を見逃さないこと"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "tools" / "check_mutation.py"


def _report(
    path: Path,
    *,
    total: int = 155,
    zapped: int = 136,
    survived: int = 19,
    reported_percentage: float = 87.7,
) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "summary": {
                    "total": total,
                    "zapped": zapped,
                    "survived": survived,
                    "timeout": 0,
                    "error": 0,
                    "percentage": reported_percentage,
                }
            }
        ),
        encoding="utf-8",
    )


def _run(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )


def test_the_current_baseline_passes(tmp_path: Path) -> None:
    report = tmp_path / "coverage" / "gremlins" / "gremlins.json"
    _report(report)

    assert _run(tmp_path).returncode == 0


def test_a_new_survivor_fails_the_gate(tmp_path: Path) -> None:
    report = tmp_path / "coverage" / "gremlins" / "gremlins.json"
    _report(report, zapped=135, survived=20)

    result = _run(tmp_path)
    assert result.returncode == 1
    assert "19 件を超えた" in result.stderr


def test_a_lower_score_fails_the_gate(tmp_path: Path) -> None:
    report = tmp_path / "coverage" / "gremlins" / "gremlins.json"
    # レポートの割合が誤っていても、実際の件数から低下を検出する
    _report(report, total=154, zapped=135, reported_percentage=100.0)

    result = _run(tmp_path)
    assert result.returncode == 1
    assert "87.7% を下回った" in result.stderr
