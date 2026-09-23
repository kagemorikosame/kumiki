"""検証の道具（tools/verify.py）が CI の要約に書くもの

CI は以前、件数を取るためだけに pytest をもう 1 度回していた いまは verify.py が
1 回の実行から要約を作る ここが壊れると、CI で落ちたときにどのテストか分からない
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
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


@pytest.mark.parametrize(
    "counts",
    [
        "3 skipped in 0.10s",
        "1 error in 0.50s",
        "no tests ran in 0.01s",
        "2 deselected in 0.02s",
        "1 xfailed, 1 xpassed in 0.30s",
    ],
)
def test_a_finished_run_without_passes_is_not_reported_as_stopped(
    verify: ModuleType, counts: str
) -> None:
    # 通ったものが無い回を「途中で止まった」と書くと、集め損ねた（1 error）のか
    # 本当に止まったのかを CI の要約から見分けられない
    (line,) = verify.summarize(f"=== {counts} ===", "3.12")
    assert line == f"### Python 3.12: {counts}"


def test_a_run_that_stopped_early_says_so(verify: ModuleType) -> None:
    # 集計の行が無いのに「通った」と読める要約を書くと、止まった CI を見落とす
    (line,) = verify.summarize("ImportError while loading conftest", "3.14")
    assert "見つからない" in line


class TestReadingThisTree:
    """worktree で検証しても、その木のコードを試すこと（#102）"""

    def test_this_tree_comes_first(self, verify: ModuleType, tmp_path: Path) -> None:
        """立てないと .pth が指す本体の src が読まれ、worktree の変更を試さない"""
        environment = verify.own_environment(tmp_path, {"PATH": "x"})
        assert environment["PYTHONPATH"] == str(tmp_path / "src")

    def test_a_path_already_set_is_kept_behind(self, verify: ModuleType, tmp_path: Path) -> None:
        """手で立てた置き場を消すと、それを頼りにしていた試験が読めなくなる"""
        environment = verify.own_environment(tmp_path, {"PYTHONPATH": "手で足した"})
        assert environment["PYTHONPATH"].split(os.pathsep) == [str(tmp_path / "src"), "手で足した"]

    def test_a_child_python_imports_this_tree(self, verify: ModuleType, tmp_path: Path) -> None:
        """実際に子の Python を起こして、入っている本体ではなくこの木を読むこと

        環境変数を組み立てるだけの試験では、``.pth`` より先に並ぶかまでは分からない
        """
        package = tmp_path / "src" / "sashimono"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("WHERE = 'この木'\n", encoding="utf-8")
        found = subprocess.run(
            [sys.executable, "-c", "import sashimono; print(sashimono.WHERE)"],
            env={**verify.own_environment(tmp_path), "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        assert found.stdout.strip() == "この木"
