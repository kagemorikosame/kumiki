"""Qodo のレビューが済んだかの判定（tools/qodo_gate.py）

main の保護でこの判定を必須にしている 甘いと Qodo が見ていないコミットがマージされ、
厳しいと Qodo が見たのにいつまでもマージできない
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
HEAD = "7162367c3b0e4b1f9f2a6c1d0e9b8a7f6e5d4c3b"
PUSHED = datetime(2026, 9, 12, 17, 0, tzinfo=UTC)
BEFORE = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)
AFTER = datetime(2026, 9, 12, 17, 5, tzinfo=UTC)


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("qodo_gate", ROOT / "tools" / "qodo_gate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _qodo(gate: ModuleType, body: str, when: datetime) -> object:
    return gate.Comment("qodo-code-review[bot]", body, when)


def test_an_update_naming_the_head_counts(gate: ModuleType) -> None:
    # 再レビューは「最新のコミットまで更新した」コメントで分かる 読めないと、
    # 修正のあとはいつまでもマージできない
    note = f"[Code review](x) by qodo was updated up to the latest commit https://github.com/o/r/commit/{HEAD}"
    assert gate.is_reviewed(HEAD, PUSHED, [_qodo(gate, note, BEFORE)])


def test_the_first_review_after_the_push_counts(gate: ModuleType) -> None:
    # 最初のレビューには SHA が書かれない 時刻で見ないと、1 回目は必ず止まる
    assert gate.is_reviewed(HEAD, PUSHED, [_qodo(gate, "Code Review by Qodo\n...", AFTER)])


def test_a_review_of_an_older_commit_does_not_count(gate: ModuleType) -> None:
    # 古いコミットのレビューで通すと、Qodo が見ていない修正がマージされる
    old = "Code Review by Qodo\n... updated up to the latest commit https://x/commit/0123abc"
    assert not gate.is_reviewed(HEAD, PUSHED, [_qodo(gate, old, BEFORE)])


def test_someone_else_quoting_the_sha_does_not_count(gate: ModuleType) -> None:
    # 人やほかの AI が SHA を書いただけで通ると、Qodo が黙っていてもマージできる
    person = gate.Comment("kagemorikosame", f"{HEAD} で直しました", AFTER)
    assert not gate.is_reviewed(HEAD, PUSHED, [person])


def test_other_qodo_comments_after_the_push_do_not_count(gate: ModuleType) -> None:
    # 要約や会話の返事はレビューではない それで通すと、見ていないコミットが通る
    assert not gate.is_reviewed(HEAD, PUSHED, [_qodo(gate, "PR Summary by Qodo", AFTER)])


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
