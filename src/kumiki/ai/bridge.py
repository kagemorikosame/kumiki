"""AI スレッドと UI スレッドの橋渡し

Claude Agent SDK は自前の asyncio ループを別スレッドで回す そこからウィジェットや
:class:`~kumiki.core.commands.Document` を直に触ると Qt が落ちるので、**やることを
キューへ積んで UI スレッドに実行させる**

同じ仕組みで確認（承認）も運ぶ 変更系のツールは、UI 側が「許可」を返すまでここで
止まる 止まっている間も UI は動き続けるので、内容を見てから決められる

:class:`~kumiki.engine.cache.MediaAnalyzer` や字幕起こしと同じ「キューに積んで
タイマーで拾う」形にしてある Qt のシグナルをスレッドまたぎで使うより、いつ実行
されるかが読みやすい
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kumiki.ai.host import EditorHost, ToolError
from kumiki.ai.operations import Operation

__all__ = ["Approval", "EditorBridge"]

#: UI スレッドが 1 件の仕事を拾って返すまで待つ上限（秒）
#: これを超えるのは、書き出しなど UI 側が長く塞がっているとき
CALL_TIMEOUT = 60.0

#: 承認の待ち時間 人が席を外していることもあるので長めに取る
APPROVAL_TIMEOUT = 600.0


@dataclass(slots=True)
class _Work:
    """UI スレッドにやってもらう 1 件"""

    run: Callable[[], object]
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None


@dataclass(slots=True)
class Approval:
    """確認待ちの 1 件"""

    tool: str
    summary: str
    arguments: dict[str, Any]
    done: threading.Event = field(default_factory=threading.Event)
    allowed: bool = False
    reason: str = ""

    def allow(self) -> None:
        self.allowed = True
        self.done.set()

    def deny(self, reason: str = "ユーザーが許可しませんでした") -> None:
        self.allowed = False
        self.reason = reason
        self.done.set()


class EditorBridge:
    """AI からの操作を、UI スレッドで実行する"""

    def __init__(self, host: EditorHost, *, auto_approve: bool = False) -> None:
        self._host = host
        self._work: queue.Queue[_Work] = queue.Queue()
        self._approvals: queue.Queue[Approval] = queue.Queue()
        self._cancel = threading.Event()
        #: 変更系を確認なしで通すか 既定は偽
        self.auto_approve = auto_approve
        #: 「このツールは以降ずっと許可」と言われたもの
        self._always: set[str] = set()
        #: UI が取り出して、まだ返事をもらっていない確認 中断のときに畳む
        self._outstanding: Approval | None = None

    # --- AI スレッドから ---

    def invoke(self, operation: Operation, arguments: dict[str, Any]) -> object:
        """ツールを 1 つ実行する 確認が要るものは、許可を待ってから動かす"""
        if operation.writes and not self._approved(operation, arguments):
            raise ToolError("ユーザーがこの操作を許可しませんでした")
        return self.call(lambda: operation(self._host, arguments))

    def call(self, work: Callable[[], object]) -> object:
        """UI スレッドで実行して、結果を返す"""
        if self._cancel.is_set():
            raise ToolError("中断されました")

        item = _Work(run=work)
        self._work.put(item)
        if not item.done.wait(CALL_TIMEOUT):
            raise ToolError("編集画面が応答しません（重い処理の最中かもしれません）")
        if item.error is not None:
            raise item.error
        return item.result

    def _approved(self, operation: Operation, arguments: dict[str, Any]) -> bool:
        if self.auto_approve or operation.name in self._always:
            return True

        approval = Approval(
            tool=operation.name,
            summary=describe_call(operation, arguments),
            arguments=arguments,
        )
        self._approvals.put(approval)
        if not approval.done.wait(APPROVAL_TIMEOUT):
            approval.deny("確認の返事がありませんでした")
        return approval.allowed

    def cancel(self) -> None:
        """走っているツールを諦めさせる 中断ボタンから呼ぶ"""
        self._cancel.set()
        # 待っている承認は拒否として畳む 放っておくと AI スレッドが
        # 10 分間止まったままになる UI がすでに取り出して画面に出している分も
        # 対象にする 取り出した時点で手が届かなくなると、中断が効かない
        outstanding = self._outstanding
        if outstanding is not None and not outstanding.done.is_set():
            outstanding.deny("中断されました")
        self._outstanding = None
        while True:
            try:
                self._approvals.get_nowait().deny("中断されました")
            except queue.Empty:
                break

    def resume(self) -> None:
        self._cancel.clear()

    # --- UI スレッドから ---

    def pump(self) -> None:
        """溜まった仕事を実行する タイマーから定期的に呼ぶ"""
        while True:
            try:
                item = self._work.get_nowait()
            except queue.Empty:
                return
            try:
                item.result = item.run()
            except (ToolError, ValueError, KeyError, OSError, RuntimeError) as exc:
                item.error = exc
            finally:
                item.done.set()

    def take_approval(self) -> Approval | None:
        """確認待ちが 1 件あれば取り出す 無ければ ``None``"""
        try:
            approval = self._approvals.get_nowait()
        except queue.Empty:
            return None
        self._outstanding = approval
        return approval

    def allow_always(self, tool: str) -> None:
        """このツールは以降ずっと許可する この会話の間だけ有効"""
        self._always.add(tool)

    def forget_always(self) -> None:
        self._always.clear()


def describe_call(operation: Operation, arguments: dict[str, Any]) -> str:
    """確認ダイアログに出す 1 行

    ツール名だけでは何が起きるか分からない 引数まで見せて、押す前に判断できる
    ようにする
    """
    if not arguments:
        return operation.description
    parts = []
    for name, value in arguments.items():
        text = str(value)
        if len(text) > 40:
            text = text[:39] + "…"
        parts.append(f"{name}={text}")
    return f"{operation.description}\n{', '.join(parts)}"
