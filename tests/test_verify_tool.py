"""検証の道具（tools/verify.py）が CI の要約に書くもの

CI は以前、件数を取るためだけに pytest をもう 1 度回していた いまは verify.py が
1 回の実行から要約を作る ここが壊れると、CI で落ちたときにどのテストか分からない
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def verify() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify", ROOT / "tools" / "verify.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_counts_and_the_failed_names_are_kept(verify: ModuleType) -> None:
    output = "\n".join(
        [
            "....F.",
            "FAILED tests/test_x.py::test_y - AssertionError",
            "1 failed, 5 passed, 2 skipped in 3.10s",
        ]
    )
    assert verify.summarize(output, "3.12") == [
        "### Python 3.12: 1 failed, 5 passed, 2 skipped in 3.10s",
        "- FAILED tests/test_x.py::test_y - AssertionError",
    ]


def test_a_run_that_stopped_early_says_so(verify: ModuleType) -> None:
    # 集計の行が無いのに「通った」と読める要約を書くと、止まった CI を見落とす
    (line,) = verify.summarize("ImportError while loading conftest", "3.14")
    assert "見つからない" in line
