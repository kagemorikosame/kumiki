"""AI チャットパネル

編集ソフトの中で Claude と話し、実際の編集をさせる 会話の見た目より、
**何をされているかが分かること**を優先している ツールの呼び出しは 1 行ずつ
出し、変更系は許可を求めてから実行する

1 つの指示で行われた編集は、まとめて 1 回の Undo で戻せる AI は 1 つの指示で
何十回も操作するので、これが無いと取り消しに同じ回数が要る
"""

from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from sashimono.ai import AI_PACK, Approval, EditorBridge, EditorHost
from sashimono.ai.session import AgentEvent, AgentSession, EventKind, system_prompt
from sashimono.ui.setup import SetupSection
from sashimono.ui.theme import Colors

__all__ = ["ChatPanel"]

#: 太字と等幅だけを拾う Claude の返事は素の Markdown で来るので、
#: そのまま出すと ** が本文に混ざる 見出しや表まで組む必要は無い
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_CODE = re.compile(r"`([^`]+)`")

#: ワーカーからの知らせを拾う間隔（ミリ秒）
#: ここを長くすると、AI の操作が画面へ反映されるまでの間が空く
POLL_MS = 80


class _Input(QPlainTextEdit):
    """Ctrl+Enter で送る入力欄"""

    submitted = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt の命名規約
        modifiers = event.modifiers()
        is_enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if is_enter and modifiers & Qt.KeyboardModifier.ControlModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class ChatPanel(QWidget):
    """Claude と話しながら編集する"""

    #: ステータスバーへ出す文言
    status_message = Signal(str)

    def __init__(self, host: EditorHost, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._host = host
        self._bridge = EditorBridge(host)
        self._session: AgentSession | None = None
        self._approval: Approval | None = None
        self._checkpoint_open = False
        #: 応答が 1 往復終わった回数 無人での確認に使う
        self._turns_done = 0

        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        self._refresh_availability()

    # --- 組み立て ---

    def _build(self) -> None:
        self._setup = SetupSection(AI_PACK, self)
        self._setup.finished.connect(lambda _ok: self._refresh_availability())
        self._setup.changed.connect(lambda _ready: None)

        self._view = QTextBrowser(self)
        self._view.setOpenExternalLinks(False)
        self._view.setStyleSheet(
            f"background-color: {Colors.PANEL_ALT.name()};border: 1px solid {Colors.BORDER.name()};"
        )

        self._approval_box = QFrame(self)
        self._approval_box.setFrameShape(QFrame.Shape.StyledPanel)
        self._approval_box.setVisible(False)
        self._approval_text = QLabel(self._approval_box)
        self._approval_text.setWordWrap(True)

        allow = QPushButton("許可", self._approval_box)
        allow.clicked.connect(lambda: self._answer(True))
        always = QPushButton("以降は確認しない", self._approval_box)
        always.clicked.connect(lambda: self._answer(True, always=True))
        deny = QPushButton("拒否", self._approval_box)
        deny.clicked.connect(lambda: self._answer(False))

        approval_buttons = QHBoxLayout()
        approval_buttons.setContentsMargins(0, 0, 0, 0)
        approval_buttons.addStretch(1)
        approval_buttons.addWidget(deny)
        approval_buttons.addWidget(always)
        approval_buttons.addWidget(allow)

        approval_layout = QVBoxLayout(self._approval_box)
        approval_layout.setContentsMargins(8, 6, 8, 6)
        approval_layout.addWidget(self._approval_text)
        approval_layout.addLayout(approval_buttons)

        self._input = _Input(self)
        self._input.setPlaceholderText("編集の指示を書いて Ctrl+Enter（例: 冒頭 10 秒を切って）")
        self._input.setMaximumHeight(96)
        self._input.submitted.connect(self.send)

        self._auto = QCheckBox("変更を自動で承認", self)
        self._auto.toggled.connect(self._set_auto_approve)
        self._send_button = QPushButton("送信", self)
        self._send_button.clicked.connect(self.send)
        self._stop_button = QPushButton("中断", self)
        self._stop_button.clicked.connect(self.interrupt)
        self._stop_button.setEnabled(False)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(self._auto)
        controls.addStretch(1)
        controls.addWidget(self._stop_button)
        controls.addWidget(self._send_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addWidget(self._setup)
        layout.addWidget(self._view, 1)
        layout.addWidget(self._approval_box)
        layout.addWidget(self._input)
        layout.addLayout(controls)

    # --- 状態 ---

    def _refresh_availability(self) -> None:
        status = self._setup.status
        ready = status.ready
        self._setup.setVisible(not ready)
        self._input.setEnabled(ready)
        self._send_button.setEnabled(ready)
        if not ready:
            self._say("案内", status.summary())

    def _set_auto_approve(self, enabled: bool) -> None:
        self._bridge.auto_approve = enabled
        if not enabled:
            self._bridge.forget_always()

    # --- 送受信 ---

    def send(self) -> None:
        """入力欄の内容を送る"""
        prompt = self._input.toPlainText().strip()
        if not prompt:
            return
        if not self._setup.status.ready:
            self.status_message.emit("AI 連携の環境が入っていません")
            return

        self._input.clear()
        self._say("あなた", prompt)
        self._open_checkpoint(prompt)

        if self._session is None:
            # 会話を始めた時点の方式で指示を書く 分ける方式の説明のまま混合の作品を
            # 触らせると、リンクした音声クリップを探し回る
            layer_mode = self._host.project.settings.layer_mode
            self._session = AgentSession(self._bridge, system_prompt=system_prompt(layer_mode))
        self._session.send(prompt)
        self._stop_button.setEnabled(True)

    def interrupt(self) -> None:
        if self._session is not None:
            self._session.interrupt()
            self._note("中断しています…")

    def close_session(self) -> None:
        """会話を畳む ウィンドウを閉じるときに呼ぶ"""
        self._timer.stop()
        self._setup.cancel()
        if self._session is not None:
            self._session.close()
            self._session = None
        self._close_checkpoint()

    # --- 1 プロンプト = 1 Undo ---

    def _open_checkpoint(self, prompt: str) -> None:
        """この指示による編集をまとめて戻せるようにする"""
        if self._checkpoint_open:
            return
        label = prompt.strip().splitlines()[0]
        if len(label) > 24:
            label = label[:23] + "…"
        self._host.document.begin_checkpoint(f"AI: {label}")
        self._checkpoint_open = True

    def _close_checkpoint(self) -> None:
        if not self._checkpoint_open:
            return
        self._checkpoint_open = False
        self._host.document.end_checkpoint()

    # --- ワーカーの見張り ---

    def _poll(self) -> None:
        # まず AI からの依頼を実行する ここが UI スレッド
        self._bridge.pump()
        self._check_approval()

        session = self._session
        if session is None:
            return
        for event in session.poll():
            self._handle(event)

    def _handle(self, event: AgentEvent) -> None:
        if event.kind is EventKind.TEXT:
            self._say("Claude", event.text)
        elif event.kind is EventKind.TOOL_USE:
            self._note(f"▸ {event.tool} {event.detail}")
        elif event.kind is EventKind.TOOL_RESULT:
            if event.text == "失敗":
                self._note(f"　× {event.detail}")
        elif event.kind is EventKind.ERROR:
            self._say("エラー", event.text)
        elif event.kind is EventKind.TURN_DONE or event.kind is EventKind.CLOSED:
            self._turns_done += 1
            self._stop_button.setEnabled(False)
            self._close_checkpoint()

    def _check_approval(self) -> None:
        showing = self._approval
        if showing is not None:
            # 中断でブリッジ側が畳んだ確認は、画面からも下げる
            if showing.done.is_set():
                self._approval = None
                self._approval_box.setVisible(False)
            return
        approval = self._bridge.take_approval()
        if approval is None:
            return
        self._approval = approval
        self._approval_text.setText(f"この操作を許可しますか？\n{approval.summary}")
        self._approval_box.setVisible(True)

    def _answer(self, allowed: bool, *, always: bool = False) -> None:
        approval = self._approval
        if approval is None:
            return
        self._approval = None
        self._approval_box.setVisible(False)
        if allowed:
            if always:
                self._bridge.allow_always(approval.tool)
            approval.allow()
        else:
            approval.deny()

    # --- 表示 ---

    def _say(self, who: str, text: str) -> None:
        color = {
            "あなた": Colors.TEXT.name(),
            "Claude": Colors.ACCENT.name(),
            "エラー": Colors.PLAYHEAD.name(),
        }.get(who, Colors.TEXT_MUTED.name())
        body = html.escape(text).replace("\n", "<br>")
        self._view.append(f'<b style="color:{color}">{html.escape(who)}</b><br>{body}<br>')
        self._scroll_to_end()

    def _note(self, text: str) -> None:
        """ツールの呼び出しなど、会話の本体ではないもの"""
        self._view.append(
            f'<span style="color:{Colors.TEXT_MUTED.name()}">{html.escape(text)}</span>'
        )
        self._scroll_to_end()

    def _scroll_to_end(self) -> None:
        bar = self._view.verticalScrollBar()
        if bar is not None:
            bar.setValue(bar.maximum())


def _to_html(text: str) -> str:
    """本文を表示用の HTML へ

    先に文字実体へ逃がしてから太字と等幅を当てる 順番を逆にすると、本文に
    書かれた ``<b>`` がそのまま効いてしまう
    """
    escaped = html.escape(text)
    escaped = _BOLD.sub(r"<b>\1</b>", escaped)
    code = rf'<code style="color:{Colors.WAVEFORM.name()}">\1</code>'
    escaped = _CODE.sub(code, escaped)
    return escaped.replace("\n", "<br>")
