"""AI チャットパネル

Claude そのものは呼ばない パネルの仕事は「出来事を見せる」「確認を取る」
「1 つの指示をまとめて 1 段の履歴にする」の 3 つなので、そこを見る
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QInputMethodEvent, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QWidget

from sashimono.ai.bridge import Approval
from sashimono.ai.models import MODELS
from sashimono.ai.session import AgentEvent, EventKind
from sashimono.core.commands import SplitClip
from sashimono.core.model import MediaItem, Project, Transcript
from sashimono.ui.chat import ChatPanel
from sashimono.ui.workspace import Preferences
from tests.ai.conftest import FakeHost, make_loaded


@pytest.fixture
def loaded(video_media: MediaItem, transcript: Transcript) -> Project:
    return make_loaded(video_media, transcript)


@pytest.fixture
def panel(qt_application: QApplication, loaded: Project) -> Iterator[tuple[ChatPanel, FakeHost]]:
    del qt_application
    host = FakeHost(loaded)
    created = ChatPanel(host)
    yield created, host
    created.close_session()
    created.deleteLater()


def _text(panel: ChatPanel) -> str:
    return panel._view.toPlainText()


class TestConversationView:
    def test_assistant_text_is_shown(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, _ = panel
        widget._handle(AgentEvent(EventKind.TEXT, text="切りました"))
        assert "切りました" in _text(widget)
        assert "Claude" in _text(widget)

    def test_tool_calls_are_visible(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        # 何をされているか分からないまま編集が進むのが一番怖い
        widget, _ = panel
        widget._handle(
            AgentEvent(EventKind.TOOL_USE, tool="split_clip", detail="clip_id=abc, frame=30")
        )
        shown = _text(widget)
        assert "split_clip" in shown
        assert "frame=30" in shown

    def test_failed_tool_results_are_shown(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, _ = panel
        widget._handle(AgentEvent(EventKind.TOOL_RESULT, text="失敗", detail="見つかりません"))
        assert "見つかりません" in _text(widget)

    def test_successful_tool_results_stay_quiet(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        # 成功のたびに中身を出すと、会話が JSON で埋まる
        widget, _ = panel
        widget._handle(AgentEvent(EventKind.TOOL_RESULT, detail='{"ok": true}'))
        assert "ok" not in _text(widget)

    def test_errors_are_labelled(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, _ = panel
        widget._handle(AgentEvent(EventKind.ERROR, text="Claude Code が見つかりません"))
        assert "エラー" in _text(widget)
        assert "Claude Code" in _text(widget)

    def test_html_in_the_text_is_escaped(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, _ = panel
        widget._handle(AgentEvent(EventKind.TEXT, text="<b>太字にはしない</b>"))
        assert "<b>太字にはしない</b>" in _text(widget)


class TestApproval:
    def _pending(self, widget: ChatPanel) -> Approval:
        approval = Approval(tool="split_clip", summary="クリップを分割\nframe=30", arguments={})
        widget._bridge._approvals.put(approval)
        widget._check_approval()
        return approval

    def test_a_request_is_shown_with_its_arguments(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, _ = panel
        self._pending(widget)
        # パネル自体を画面に出していないので isVisible は使えない 表示の指示だけを見る
        assert widget._approval_box.isHidden() is False
        assert "frame=30" in widget._approval_text.text()

    def test_allowing_releases_the_waiting_tool(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, _ = panel
        approval = self._pending(widget)
        widget._answer(True)
        assert approval.allowed is True
        assert approval.done.is_set()
        assert widget._approval_box.isHidden() is True

    def test_denying_is_recorded(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, _ = panel
        approval = self._pending(widget)
        widget._answer(False)
        assert approval.allowed is False
        assert approval.done.is_set()

    def test_always_allow_covers_the_next_call(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, _ = panel
        approval = self._pending(widget)
        widget._answer(True, always=True)
        assert approval.allowed is True
        assert "split_clip" in widget._bridge._always

    def test_auto_approve_is_off_by_default(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, _ = panel
        assert widget._bridge.auto_approve is False
        widget._auto.setChecked(True)
        assert widget._bridge.auto_approve is True

    def test_turning_auto_approve_off_forgets_the_exceptions(
        self, panel: tuple[ChatPanel, FakeHost]
    ) -> None:
        widget, _ = panel
        widget._bridge.allow_always("split_clip")
        widget._auto.setChecked(True)
        widget._auto.setChecked(False)
        assert widget._bridge._always == set()

    def test_an_interrupted_request_disappears(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, _ = panel
        self._pending(widget)
        widget._bridge.cancel()
        widget._check_approval()
        assert widget._approval_box.isHidden() is True


class TestCheckpoint:
    def test_one_prompt_becomes_one_undo_step(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, host = panel
        clip = host.document.project.timeline.tracks[0].clips[0].id

        widget._open_checkpoint("冒頭を切って")
        # AI が 3 回操作した、という想定
        host.apply_commands([SplitClip(clip, 60)], "分割")
        host.apply_commands([SplitClip(clip, 30)], "分割")
        second = host.document.project.timeline.tracks[0].clips[-1].id
        host.apply_commands([SplitClip(second, 120)], "分割")
        widget._close_checkpoint()

        assert len(host.document.project.timeline.tracks[0].clips) == 4
        # 3 回の編集が 1 段 取り消し 1 回で最初の状態へ戻る
        assert host.document.history_labels == ("AI: 冒頭を切って",)
        host.document.undo()
        assert len(host.document.project.timeline.tracks[0].clips) == 1

    def test_the_label_is_trimmed(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, host = panel
        widget._open_checkpoint("あ" * 100)
        clip = host.document.project.timeline.tracks[0].clips[0].id
        host.apply_commands([SplitClip(clip, 60)], "分割")
        widget._close_checkpoint()
        assert len(host.document.history_labels[0]) <= 28

    def test_a_turn_that_changed_nothing_leaves_no_step(
        self, panel: tuple[ChatPanel, FakeHost]
    ) -> None:
        widget, host = panel
        widget._open_checkpoint("何もしないで")
        widget._close_checkpoint()
        assert host.document.can_undo is False

    def test_the_turn_ending_closes_the_checkpoint(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, host = panel
        widget._open_checkpoint("切って")
        clip = host.document.project.timeline.tracks[0].clips[0].id
        host.apply_commands([SplitClip(clip, 60)], "分割")
        widget._handle(AgentEvent(EventKind.TURN_DONE))
        assert host.document.can_undo is True

    def test_closing_the_session_closes_an_open_checkpoint(
        self, panel: tuple[ChatPanel, FakeHost]
    ) -> None:
        # 開いたまま終わると、以降の編集が全部その 1 段に飲み込まれる
        widget, host = panel
        widget._open_checkpoint("途中で閉じる")
        clip = host.document.project.timeline.tracks[0].clips[0].id
        host.apply_commands([SplitClip(clip, 60)], "分割")
        widget.close_session()
        assert host.document.in_checkpoint is False
        assert host.document.can_undo is True


class TestSending:
    def test_an_empty_prompt_does_nothing(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, host = panel
        widget._input.setPlainText("   ")
        widget.send()
        assert host.document.in_checkpoint is False
        assert _text(widget).strip() == "" or "案内" in _text(widget)


class TestFormatting:
    def test_bold_and_code_are_rendered(self) -> None:
        from sashimono.ui.chat.panel import _to_html

        # Claude の返事は素の Markdown で来る そのまま出すと ** が本文に混ざる
        rendered = _to_html("**強調** と `set_param`")
        assert "<b>強調</b>" in rendered
        assert "<code" in rendered and "set_param" in rendered

    def test_tags_in_the_text_are_neutralised_first(self) -> None:
        from sashimono.ui.chat.panel import _to_html

        rendered = _to_html("<b>これは太字にしない</b>")
        assert "&lt;b&gt;" in rendered


def _ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """導入が済んだことにする 本物の SDK と claude の有無に左右されないように"""
    from sashimono.ai import AI_PACK
    from sashimono.runtime import PackageStatus, PackStatus
    from sashimono.ui.setup import SetupSection

    status = PackStatus(pack=AI_PACK, packages=(PackageStatus("claude-agent-sdk", "0.2.152"),))
    monkeypatch.setattr(SetupSection, "status", property(lambda _self: status))


def _press(widget: QWidget, key: Qt.Key, modifiers: Qt.KeyboardModifier) -> None:
    QTest.keyClick(widget, key, modifiers)


class TestSendKey:
    """Enter で送り、Shift+Enter で改行する 日本語の変換を確定する Enter では送らない"""

    @pytest.fixture
    def typed(self, panel: tuple[ChatPanel, FakeHost]) -> tuple[ChatPanel, list[bool]]:
        widget, _ = panel
        sent: list[bool] = []
        widget._input.submitted.disconnect()
        widget._input.submitted.connect(lambda: sent.append(True))
        widget._input.setEnabled(True)
        widget._input.setPlainText("冒頭を切って")
        widget._input.moveCursor(QTextCursor.MoveOperation.End)
        return widget, sent

    def test_enter_sends(self, typed: tuple[ChatPanel, list[bool]]) -> None:
        widget, sent = typed
        _press(widget._input, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        assert sent == [True]
        assert "\n" not in widget._input.toPlainText()

    def test_the_keypad_enter_also_sends(self, typed: tuple[ChatPanel, list[bool]]) -> None:
        widget, sent = typed
        _press(widget._input, Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier)
        assert sent == [True]

    def test_shift_enter_breaks_the_line(self, typed: tuple[ChatPanel, list[bool]]) -> None:
        widget, sent = typed
        _press(widget._input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        assert sent == []
        # 送った文の中で改行として読まれる、ふつうの改行が入る
        assert widget._input.toPlainText() == "冒頭を切って\n"

    def test_ctrl_enter_still_sends(self, typed: tuple[ChatPanel, list[bool]]) -> None:
        # 前の版で覚えた押し方も効く
        widget, sent = typed
        _press(widget._input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        assert sent == [True]

    def test_enter_while_converting_does_not_send(
        self, typed: tuple[ChatPanel, list[bool]]
    ) -> None:
        """変換を確定する Enter で送ると、書きかけの文が飛んでいく"""
        widget, sent = typed
        QApplication.sendEvent(widget._input, QInputMethodEvent("へんかん", []))
        _press(widget._input, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        assert sent == []

        committed = QInputMethodEvent("", [])
        committed.setCommitString("変換")
        QApplication.sendEvent(widget._input, committed)
        _press(widget._input, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        assert sent == [True]

    def test_the_old_way_can_be_chosen(self, typed: tuple[ChatPanel, list[bool]]) -> None:
        widget, sent = typed
        widget.apply_preferences(Preferences(chat_enter_sends=False))
        _press(widget._input, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        assert sent == []
        assert "Ctrl+Enter" in widget._input.placeholderText()
        _press(widget._input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        assert sent == [True]

    def test_the_placeholder_tells_the_keys(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, _ = panel
        assert "Enter で送信" in widget._input.placeholderText()
        assert "Shift+Enter で改行" in widget._input.placeholderText()


class _RecordingSession:
    """会話の差し替え Claude へは繋がない 何で始めたかだけを覚える"""

    made: ClassVar[list[_RecordingSession]] = []

    def __init__(self, bridge: object, *, model: str | None, effort: str | None) -> None:
        del bridge
        self.model = model
        self.effort = effort
        self.prompts: list[str] = []
        self.closed = False
        self.busy = False
        _RecordingSession.made.append(self)

    def send(self, prompt: str) -> None:
        self.prompts.append(prompt)

    def poll(self) -> list[AgentEvent]:
        return []

    def close(self, *, wait: bool = True) -> None:
        del wait
        self.closed = True

    def interrupt(self) -> None:
        return


@pytest.fixture
def recorded(
    panel: tuple[ChatPanel, FakeHost], monkeypatch: pytest.MonkeyPatch
) -> tuple[ChatPanel, list[_RecordingSession]]:
    from sashimono.ui.chat import panel as panel_module

    _ready(monkeypatch)
    _RecordingSession.made = []
    monkeypatch.setattr(panel_module, "AgentSession", _RecordingSession)
    widget, _ = panel
    widget._refresh_availability()
    return widget, _RecordingSession.made


def _choose(box: QComboBox, value: str) -> None:
    box.setCurrentIndex(box.findData(value))


class TestModelChoice:
    def test_the_default_is_what_claude_code_picks(
        self, recorded: tuple[ChatPanel, list[_RecordingSession]]
    ) -> None:
        # 選べるようになる前と同じ動き 何も渡さない
        widget, made = recorded
        widget._input.setPlainText("切って")
        widget.send()
        assert (made[0].model, made[0].effort) == (None, None)

    def test_the_listed_models_are_the_current_ones(self) -> None:
        ids = {model.id for model in MODELS}
        assert {
            "claude-opus-5-5",
            "claude-sonnet-5",
            "claude-haiku-4-5-20251001",
            "claude-fable-5-1",
        } <= ids

    def test_the_chosen_model_and_effort_are_used(
        self, recorded: tuple[ChatPanel, list[_RecordingSession]]
    ) -> None:
        widget, made = recorded
        _choose(widget._model, "claude-sonnet-5")
        _choose(widget._effort, "high")
        widget._input.setPlainText("切って")
        widget.send()
        assert (made[0].model, made[0].effort) == ("claude-sonnet-5", "high")

    def test_haiku_does_not_get_an_effort(
        self, recorded: tuple[ChatPanel, list[_RecordingSession]]
    ) -> None:
        """Haiku 4.5 はエフォートを受け付けない 渡すと会話が始まる前に失敗する"""
        widget, made = recorded
        _choose(widget._effort, "max")
        _choose(widget._model, "claude-haiku-4-5-20251001")
        assert widget._effort.isEnabled() is False
        widget._input.setPlainText("切って")
        widget.send()
        assert (made[0].model, made[0].effort) == ("claude-haiku-4-5-20251001", None)

    def test_changing_the_model_starts_a_new_conversation(
        self, recorded: tuple[ChatPanel, list[_RecordingSession]]
    ) -> None:
        widget, made = recorded
        widget._input.setPlainText("切って")
        widget.send()
        _choose(widget._model, "claude-opus-5-5")
        assert made[0].closed is True
        widget._input.setPlainText("もう一度")
        widget.send()
        assert made[1].model == "claude-opus-5-5"
        assert "新しい会話" in _text(widget)

    def test_a_change_during_a_reply_waits_for_the_reply(
        self, recorded: tuple[ChatPanel, list[_RecordingSession]]
    ) -> None:
        # 応答の途中で畳むと、その応答が途中で切れる
        widget, made = recorded
        widget._input.setPlainText("切って")
        widget.send()
        made[0].busy = True
        _choose(widget._model, "claude-opus-5-5")
        assert made[0].closed is False
        made[0].busy = False
        widget._handle(AgentEvent(EventKind.TURN_DONE))
        assert made[0].closed is True

    def test_choices_are_announced_for_saving(self, panel: tuple[ChatPanel, FakeHost]) -> None:
        widget, _ = panel
        announced: list[tuple[str, str]] = []
        widget.choices_changed.connect(lambda model, effort: announced.append((model, effort)))
        _choose(widget._model, "claude-fable-5-1")
        assert announced[-1] == ("claude-fable-5-1", "")

    def test_preferences_are_shown_without_announcing(
        self, panel: tuple[ChatPanel, FakeHost]
    ) -> None:
        # 読んだだけで「選び直した」と知らせると、起動のたびに保存が走る
        widget, _ = panel
        announced: list[tuple[str, str]] = []
        widget.choices_changed.connect(lambda model, effort: announced.append((model, effort)))
        widget.apply_preferences(Preferences(ai_model="claude-sonnet-5", ai_effort="low"))
        assert (widget.model, widget.effort) == ("claude-sonnet-5", "low")
        assert announced == []


class TestLogin:
    def test_the_login_guide_shows_when_no_credentials_are_found(
        self,
        recorded: tuple[ChatPanel, list[_RecordingSession]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sashimono.ui.chat import panel as panel_module

        widget, _ = recorded
        monkeypatch.setattr(panel_module, "credentials_found", lambda: False)
        widget._refresh_availability()
        assert widget._login_box.isHidden() is False
        assert "ANTHROPIC_API_KEY" in widget._login_text.text()

        monkeypatch.setattr(panel_module, "credentials_found", lambda: True)
        widget._refresh_availability()
        assert widget._login_box.isHidden() is True

    def test_the_login_button_opens_claude_code(
        self,
        recorded: tuple[ChatPanel, list[_RecordingSession]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # ログインは Claude Code 自身の画面で済ませる このソフトは鍵を受け取らない
        from sashimono.ui.chat import panel as panel_module

        widget, _ = recorded
        cli = tmp_path / "claude.exe"
        opened: list[Path] = []
        monkeypatch.setattr(panel_module, "claude_cli", lambda: cli)
        monkeypatch.setattr(panel_module, "open_login_window", opened.append)
        widget._login_button.click()
        assert opened == [cli]


class TestLoginHint:
    def test_a_login_failure_says_where_to_log_in(self) -> None:
        from sashimono.ai.session import with_login_hint

        text = with_login_hint("Invalid API key · Please run /login")
        assert "ログイン…" in text

    def test_other_failures_are_left_alone(self) -> None:
        from sashimono.ai.session import with_login_hint

        assert with_login_hint("接続が切れました") == "接続が切れました"
