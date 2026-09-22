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
QODO = "qodo-code-review[bot]"
EARLY = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)
LATE = datetime(2026, 9, 12, 18, 0, tzinfo=UTC)
UPDATE = f"[Code review](x) by qodo was updated up to the latest commit https://github.com/o/r/commit/{HEAD}"


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
    assert gate.is_reviewed(HEAD, [gate.Comment(QODO, UPDATE, EARLY)])


def test_evidence_links_to_the_head_count(gate: ModuleType) -> None:
    # 最初のレビューは、指摘の根拠のリンクに見たコミットの SHA が入る 読めないと、
    # 1 回目のレビューのあとも必ず止まる
    review = f"Code Review by Qodo\n[tools/x.py](https://github.com/o/r/blob/{HEAD}/tools/x.py)"
    assert gate.is_reviewed(HEAD, [gate.Comment(QODO, review, EARLY)])


def test_a_review_without_the_head_does_not_count(gate: ModuleType) -> None:
    # 時刻や見出しで通すと、別のブランチで先に push されたコミットや、古い日時の
    # コミットが、Qodo が見ていないまま通る
    old = "Code Review by Qodo\n... updated up to the latest commit https://x/commit/0123abc"
    assert not gate.is_reviewed(HEAD, [gate.Comment(QODO, old, LATE)])


def test_a_short_sha_does_not_count(gate: ModuleType) -> None:
    # 短い形で探すと、別のコミットの SHA の一部に当たりうる
    assert not gate.is_reviewed(HEAD, [gate.Comment(QODO, f"{HEAD[:7]} まで見ました", LATE)])


@pytest.mark.parametrize("login", ["kagemorikosame", "qodo-code-review-x", "qodo-code-review"])
def test_only_the_real_bot_counts(gate: ModuleType, login: str) -> None:
    # 似た名前の一般アカウントが SHA を書くだけで通ると、Qodo が黙っていてもマージできる
    assert not gate.is_reviewed(HEAD, [gate.Comment(login, f"{HEAD} まで見ました", LATE)])


def test_a_review_before_the_target_changed_does_not_count(gate: ModuleType) -> None:
    # 向き先を変えると、先頭の SHA は同じまま比べる相手が変わる 前のレビューで通すと、
    # Qodo が見ていない差分がマージされる
    assert not gate.is_reviewed(HEAD, [gate.Comment(QODO, UPDATE, EARLY)], since=LATE)
    assert gate.is_reviewed(HEAD, [gate.Comment(QODO, UPDATE, LATE)], since=EARLY)


def test_the_last_target_change_is_found(gate: ModuleType) -> None:
    # 何度か変えたときに最初の時刻を使うと、その間のレビューで通ってしまう
    events = [
        {"event": "base_ref_changed", "created_at": "2026-09-12T16:00:00Z"},
        {"event": "commented", "created_at": "2026-09-12T19:00:00Z"},
        {"event": "base_ref_changed", "created_at": "2026-09-12T18:00:00Z"},
    ]
    assert gate.last_base_change(events) == LATE
    assert gate.last_base_change([{"event": "commented", "created_at": "x"}]) is None


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


class TestMergingTheBase:
    """main を取り込んだだけのときに通すか

    main の保護は「最新の main を取り込んでからマージ」なので、ほかの PR が入るたびに
    先頭の SHA が変わる Qodo は中身の同じ差分を見直さないので、ここで通さないと
    並行して進めている PR が順番待ちで止まり続ける
    一方で、取り込みに見せかけて中身を変えた PR まで通すと、Qodo の見ていないコードが
    main へ入る
    """

    BRANCH = "1111111111111111111111111111111111111111"
    MERGE = "2222222222222222222222222222222222222222"
    MAIN = "3333333333333333333333333333333333333333"
    OWN = "4444444444444444444444444444444444444444"

    def api(
        self,
        commits: dict[str, list[str]],
        in_base: set[str],
        compares: dict[str, list[dict[str, str]]],
    ) -> object:
        def call(path: str) -> dict[str, object]:
            if path.startswith("commits/"):
                return {"parents": [{"sha": s} for s in commits[path.split("/", 1)[1]]]}
            left, right = path.removeprefix("compare/").split("...")
            if right in ("main", "master"):
                return {"status": "behind" if left in in_base else "diverged"}
            return {"files": compares.get(f"{left}...{right}", [])}

        return call

    def test_a_merge_of_main_passes(self, gate: ModuleType) -> None:
        # 通さないと、ほかの PR が入るたびに Qodo の見た差分のままなのに待ちへ戻る
        call = self.api(
            {self.MERGE: [self.BRANCH, self.MAIN]},
            {self.MAIN},
            {
                f"{self.BRANCH}...{self.MERGE}": [{"filename": "a.py", "sha": "aa"}],
                f"{self.BRANCH}...{self.MAIN}": [{"filename": "a.py", "sha": "aa"}],
            },
        )
        assert gate.only_base_merges_since(self.MERGE, "main", lambda s: s == self.BRANCH, call)

    def test_a_commit_of_its_own_does_not_pass(self, gate: ModuleType) -> None:
        # 自分のコミットが積まれていれば、Qodo の見ていない中身が入っている
        call = self.api({self.OWN: [self.BRANCH]}, {self.MAIN}, {})
        assert not gate.only_base_merges_since(self.OWN, "main", lambda s: s == self.BRANCH, call)

    def test_merging_something_outside_main_does_not_pass(self, gate: ModuleType) -> None:
        # main に入っていない物を取り込めば、その中身は誰も見ていない
        call = self.api({self.MERGE: [self.BRANCH, self.OWN]}, {self.MAIN}, {})
        assert not gate.only_base_merges_since(self.MERGE, "main", lambda s: s == self.BRANCH, call)

    def test_resolving_a_conflict_by_hand_does_not_pass(self, gate: ModuleType) -> None:
        # 衝突を手で直すと、main 側とも枝側とも違う中身になる そこは Qodo が見ていない
        call = self.api(
            {self.MERGE: [self.BRANCH, self.MAIN]},
            {self.MAIN},
            {
                f"{self.BRANCH}...{self.MERGE}": [{"filename": "a.py", "sha": "cc"}],
                f"{self.BRANCH}...{self.MAIN}": [{"filename": "a.py", "sha": "aa"}],
            },
        )
        assert not gate.only_base_merges_since(self.MERGE, "main", lambda s: s == self.BRANCH, call)

    def test_a_file_only_the_branch_changed_does_not_pass(self, gate: ModuleType) -> None:
        # main が触っていないファイルが変わっているなら、取り込みでは説明が付かない
        call = self.api(
            {self.MERGE: [self.BRANCH, self.MAIN]},
            {self.MAIN},
            {
                f"{self.BRANCH}...{self.MERGE}": [{"filename": "b.py", "sha": "bb"}],
                f"{self.BRANCH}...{self.MAIN}": [{"filename": "a.py", "sha": "aa"}],
            },
        )
        assert not gate.only_base_merges_since(self.MERGE, "main", lambda s: s == self.BRANCH, call)

    def test_too_many_merges_stop_the_walk(self, gate: ModuleType) -> None:
        # 上限が無いと、作られたコミットの連なりを延々と辿って API を叩き続ける
        chain = {f"{i:040x}": [f"{i + 1:040x}", self.MAIN] for i in range(50)}
        call = self.api(chain, {self.MAIN}, {})
        assert not gate.only_base_merges_since(f"{0:040x}", "main", lambda s: False, call)
