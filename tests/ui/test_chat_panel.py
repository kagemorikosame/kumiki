"""AI チャットパネル

Claude そのものは呼ばない パネルの仕事は「出来事を見せる」「確認を取る」
「1 つの指示をまとめて 1 段の履歴にする」の 3 つなので、そこを見る
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication

from kumiki.ai.bridge import Approval
from kumiki.ai.session import AgentEvent, EventKind
from kumiki.core.commands import SplitClip
from kumiki.core.model import MediaItem, Project, Transcript
from kumiki.ui.chat import ChatPanel
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
        from kumiki.ui.chat.panel import _to_html

        # Claude の返事は素の Markdown で来る そのまま出すと ** が本文に混ざる
        rendered = _to_html("**強調** と `set_param`")
        assert "<b>強調</b>" in rendered
        assert "<code" in rendered and "set_param" in rendered

    def test_tags_in_the_text_are_neutralised_first(self) -> None:
        from kumiki.ui.chat.panel import _to_html

        rendered = _to_html("<b>これは太字にしない</b>")
        assert "&lt;b&gt;" in rendered
