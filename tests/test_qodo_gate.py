"""Qodo のレビューが済んだかの判定（tools/qodo_gate.py）

main の保護でこの判定を必須にしている 甘いと Qodo が見ていないコミットがマージされ、
厳しいと Qodo が見たのにいつまでもマージできない
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
HEAD = "7162367c3b0e4b1f9f2a6c1d0e9b8a7f6e5d4c3b"
QODO = "qodo-code-review[bot]"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("qodo_gate", ROOT / "tools" / "qodo_gate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_an_update_naming_the_head_counts(gate: ModuleType) -> None:
    # 再レビューは「最新のコミットまで更新した」コメントで分かる 読めないと、
    # 修正のあとはいつまでもマージできない
    note = f"[Code review](x) by qodo was updated up to the latest commit https://github.com/o/r/commit/{HEAD}"
    assert gate.is_reviewed(HEAD, [gate.Comment(QODO, note)])


def test_evidence_links_to_the_head_count(gate: ModuleType) -> None:
    # 最初のレビューは、指摘の根拠のリンクに見たコミットの SHA が入る 読めないと、
    # 1 回目のレビューのあとも必ず止まる
    review = f"Code Review by Qodo\n[tools/x.py](https://github.com/o/r/blob/{HEAD}/tools/x.py)"
    assert gate.is_reviewed(HEAD, [gate.Comment(QODO, review)])


def test_a_review_without_the_head_does_not_count(gate: ModuleType) -> None:
    # 時刻や見出しで通すと、別のブランチで先に push されたコミットや、古い日時の
    # コミットが、Qodo が見ていないまま通る
    old = "Code Review by Qodo\n... updated up to the latest commit https://x/commit/0123abc"
    assert not gate.is_reviewed(HEAD, [gate.Comment(QODO, old)])


def test_a_short_sha_does_not_count(gate: ModuleType) -> None:
    # 短い形で探すと、別のコミットの SHA の一部に当たりうる
    assert not gate.is_reviewed(HEAD, [gate.Comment(QODO, f"{HEAD[:7]} まで見ました")])


@pytest.mark.parametrize("login", ["kagemorikosame", "qodo-code-review-x", "qodo-code-review"])
def test_only_the_real_bot_counts(gate: ModuleType, login: str) -> None:
    # 似た名前の一般アカウントが SHA を書くだけで通ると、Qodo が黙っていてもマージできる
    assert not gate.is_reviewed(HEAD, [gate.Comment(login, f"{HEAD} まで見ました")])


@pytest.mark.parametrize(
    ("event", "number"),
    [
        ({"pull_request": {"number": 14}}, 14),
        ({"issue": {"number": 14, "pull_request": {}}}, 14),
        ({"issue": {"number": 3}}, None),
    ],
)
def test_only_pull_requests_are_looked_at(
    gate: ModuleType, event: dict[str, object], number: int | None
) -> None:
    # Issue のコメントでも走る そこへ status を書こうとすると、PR ではないので落ちる
    assert gate._pull_number(event) == number
