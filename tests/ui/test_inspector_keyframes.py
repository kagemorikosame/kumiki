"""オブジェクト設定のキーフレームの操作と、初期値へ戻す操作（利用者の要望）

キーフレーム 各値の横の ◆ で再生位置にキーを打つ・消す ◀ ▶ で前後のキーへ再生位置を
動かす キーの値は隣の入力欄で直す キーはクリップの頭から数えたフレームで持つ
前は ◆ が再生位置（タイムラインのフレーム）のまま打っていて、頭が 0 より後ろの
クリップでは打った所と違う時刻（多くはクリップの外）に点が入っていた

初期値へ戻す 行の名前のダブルクリック（数のスライダーはスライダーのダブルクリックでも）
キーフレームのある値は、再生位置のキーの値だけを初期値にする（無ければ初期値のキーを打つ）
ほかのキーは残す（利用者の決定） どれも取り消せる

窓は表示しない（オフスクリーン）
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QAbstractSlider, QApplication, QComboBox, QSlider

from sashimono.core.commands import Command, Document, ParamPath
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    Keyframe,
    LayerMode,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.effects import ColorSpec
from sashimono.effects.sources import TEXT
from sashimono.engine.gpu import BlendMode
from sashimono.ui.inspector.panel import InspectorPanel, KeyframeControls, _RowLabel
from sashimono.ui.inspector.widgets import TrackEditor

#: クリップの頭 0 より後ろに置く 0 に置くと、再生位置とクリップの中の時刻が同じになり、
#: 取り違えても試験が通る
START = 60


def _project(opacity: AnimatedValue | None = None) -> tuple[Project, Clip]:
    base = Project.create(ProjectSettings(layer_mode=LayerMode.MIXED))
    clip = Clip(
        timeline_start=START,
        duration=120,
        source=TEXT.create(),
        opacity=opacity if opacity is not None else AnimatedValue(static=1.0),
    )
    track = Track(TrackKind.MIXED, "レイヤー 1", (clip,))
    return base.with_timeline(replace(base.timeline, tracks=(track,))), clip


class _Harness:
    """窓の代わり 出たコマンドを文書に積み、設定パネルへ戻す 再生位置の移動も受ける"""

    def __init__(self, panel: InspectorPanel, project: Project) -> None:
        self.panel = panel
        self.document = Document(project)
        self.labels: list[str] = []
        self.seeks: list[int] = []
        self.focused: list[ParamPath] = []
        panel.commands_requested.connect(self._run)
        panel.seek_requested.connect(self._seek)
        panel.param_focused.connect(self.focused.append)
        panel.set_project(project)

    def _run(self, commands: list[Command], label: str) -> None:
        with self.document.checkpoint(label):
            for command in commands:
                self.document.execute(command)
        self.labels.append(label)
        self._show(self.document.project)

    def _show(self, project: Project) -> None:
        # 作り直した前の部品は後で消える 消えるまで待たないと、探したときに前の部品が当たる
        self.panel.set_project(project)
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _seek(self, frame: int) -> None:
        self.seeks.append(frame)
        self.panel.set_frame(frame)

    def undo(self) -> None:
        self.document.undo()
        self._show(self.document.project)

    def opacity(self, clip: Clip) -> AnimatedValue:
        located = self.document.project.timeline.locate_clip(clip.id)
        assert located is not None
        return located[1].opacity


@pytest.fixture
def panel(qt_application: QApplication) -> Iterator[InspectorPanel]:
    del qt_application
    created = InspectorPanel()
    yield created
    created.close()
    shiboken6.delete(created)


def _open(panel: InspectorPanel, opacity: AnimatedValue | None = None) -> tuple[_Harness, Clip]:
    project, clip = _project(opacity)
    harness = _Harness(panel, project)
    panel.set_selection((clip.id,))
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return harness, clip


def _controls(panel: InspectorPanel, clip: Clip) -> KeyframeControls:
    path = ParamPath.of_clip(clip.id, "opacity")
    found = [c for c in panel.findChildren(KeyframeControls) if c.path == path]
    assert len(found) == 1, "不透明度の ◀ ◆ ▶ が無い"
    return found[0]


def _label(panel: InspectorPanel, text: str) -> _RowLabel:
    found = [label for label in panel.findChildren(_RowLabel) if label.text() == text]
    assert found, f"{text} の行が無い"
    return found[0]


def _double_click(widget: _RowLabel | QSlider) -> None:
    event = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        QPointF(4, 4),
        QPointF(4, 4),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


def _animated(*keys: tuple[int, float]) -> AnimatedValue:
    return AnimatedValue(static=1.0, keyframes=tuple(Keyframe(frame=f, value=v) for f, v in keys))


class TestKeyButton:
    def test_the_key_goes_where_the_playhead_is_inside_the_clip(
        self, panel: InspectorPanel
    ) -> None:
        # 再生位置（タイムラインの 70）のまま打つと、クリップの頭から 70 の所に点が入る
        harness, clip = _open(panel)
        panel.set_frame(START + 10)
        _controls(panel, clip).toggle.click()
        assert [k.frame for k in harness.opacity(clip).keyframes] == [10]
        assert harness.labels == ["キーフレームを打つ"]
        assert harness.focused == [ParamPath.of_clip(clip.id, "opacity")]

    def test_pressing_again_removes_the_key_and_undo_brings_it_back(
        self, panel: InspectorPanel
    ) -> None:
        harness, clip = _open(panel, _animated((10, 0.3), (50, 0.9)))
        panel.set_frame(START + 10)
        controls = _controls(panel, clip)
        assert controls.toggle.text() == "◆", (
            "再生位置にキーがあるのに白抜きだと、消せると分からない"
        )
        controls.toggle.click()
        assert [k.frame for k in harness.opacity(clip).keyframes] == [50]
        harness.undo()
        assert [k.frame for k in harness.opacity(clip).keyframes] == [10, 50]

    def test_the_look_follows_the_playhead(self, panel: InspectorPanel) -> None:
        # 見た目が再生位置に付いてこないと、キーの上にいるのかどうかが分からない
        _, clip = _open(panel, _animated((10, 0.3), (50, 0.9)))
        controls = _controls(panel, clip)
        panel.set_frame(START + 50)
        assert controls.toggle.text() == "◆"
        panel.set_frame(START + 30)
        assert controls.toggle.text() == "◇"
        assert controls.toggle.isEnabled()
        # クリップの外に打ったキーは描く所が無い 押せないようにする
        panel.set_frame(START - 5)
        assert not controls.toggle.isEnabled()

    def test_the_key_button_starts_hollow_without_keys(self, panel: InspectorPanel) -> None:
        _, clip = _open(panel)
        panel.set_frame(START)
        controls = _controls(panel, clip)
        assert controls.toggle.text() == "◇"
        assert not controls.previous.isEnabled()
        assert not controls.next.isEnabled()


class TestJumping:
    def test_the_arrows_move_the_playhead_to_the_neighbouring_keys(
        self, panel: InspectorPanel
    ) -> None:
        # 前後のキーへ飛べないと、キーの値を直すたびにタイムラインで点を探して再生位置を合わせる
        harness, clip = _open(panel, _animated((10, 0.3), (50, 0.9), (90, 0.5)))
        panel.set_frame(START + 30)
        controls = _controls(panel, clip)
        assert controls.previous.isEnabled() and controls.next.isEnabled()
        controls.next.click()
        assert harness.seeks == [START + 50]
        controls.next.click()
        assert harness.seeks[-1] == START + 90
        assert not controls.next.isEnabled(), "最後のキーの先には飛べない"
        controls.previous.click()
        assert harness.seeks[-1] == START + 50

    def test_editing_the_value_changes_the_key_under_the_playhead(
        self, panel: InspectorPanel
    ) -> None:
        # 飛んだ先で値を直すと、そのキーの値が変わる（ほかのキーは動かない）
        harness, clip = _open(panel, _animated((10, 0.3), (50, 0.9)))
        panel.set_frame(START + 30)
        _controls(panel, clip).next.click()
        editor = next(e for e in panel.findChildren(TrackEditor) if e.spec.name == "opacity")
        editor.value_changed.emit(AnimatedValue(static=0.25))
        keys = harness.opacity(clip).keyframes
        assert [(k.frame, k.value) for k in keys] == [(10, 0.3), (50, 0.25)]


class TestReset:
    def test_double_clicking_the_name_resets_a_plain_value(self, panel: InspectorPanel) -> None:
        # 初期値を覚えていないと、戻すのに数を調べて打ち直すことになる
        harness, clip = _open(panel, AnimatedValue(static=0.4))
        _double_click(_label(panel, "不透明度"))
        assert harness.opacity(clip) == AnimatedValue(static=1.0)
        assert harness.labels == ["不透明度を初期値に戻す"]
        harness.undo()
        assert harness.opacity(clip) == AnimatedValue(static=0.4)

    def test_an_animated_value_resets_only_the_key_under_the_playhead(
        self, panel: InspectorPanel
    ) -> None:
        # アニメーションごと消すと、1 か所だけ戻したいのに打ったキーが全部消える（利用者の決定）
        harness, clip = _open(panel, _animated((10, 0.3), (50, 0.2)))
        panel.set_frame(START + 50)
        _double_click(_label(panel, "不透明度"))
        keys = harness.opacity(clip).keyframes
        assert [(k.frame, k.value) for k in keys] == [(10, 0.3), (50, 1.0)]

    def test_between_keys_a_default_key_is_added(self, panel: InspectorPanel) -> None:
        harness, clip = _open(panel, _animated((10, 0.3), (50, 0.2)))
        panel.set_frame(START + 30)
        _double_click(_label(panel, "不透明度"))
        keys = harness.opacity(clip).keyframes
        assert [(k.frame, k.value) for k in keys] == [(10, 0.3), (30, 1.0), (50, 0.2)]

    def test_double_clicking_the_slider_resets_too(self, panel: InspectorPanel) -> None:
        # 数の欄は数字を打ち直すのにダブルクリックを使う スライダーのダブルクリックで戻す
        harness, clip = _open(panel, AnimatedValue(static=0.4))
        editor = next(e for e in panel.findChildren(TrackEditor) if e.spec.name == "opacity")
        slider = editor.findChild(QSlider)
        assert slider is not None
        _double_click(slider)
        assert harness.opacity(clip) == AnimatedValue(static=1.0)

    def test_an_unchanged_value_adds_no_step(self, panel: InspectorPanel) -> None:
        # もう初期値なのに段を積むと、戻しても何も変わらない取り消しが増える
        harness, _ = _open(panel)
        _double_click(_label(panel, "不透明度"))
        assert harness.labels == []

    def test_a_colour_goes_back_to_its_default(self, panel: InspectorPanel) -> None:
        harness, clip = _open(panel)
        spec = next(s for s in TEXT.parameters if isinstance(s, ColorSpec))
        assert clip.source is not None
        changed = replace(clip, source=clip.source.with_param(spec.name, (0.1, 0.2, 0.3, 1.0)))
        project = harness.document.project
        track = project.timeline.tracks[0]
        project = project.with_timeline(
            project.timeline.replace_track(replace(track, clips=(changed,)))
        )
        harness.document = Document(project)
        harness._show(project)
        _double_click(_label(panel, spec.label))
        located = harness.document.project.timeline.locate_clip(clip.id)
        assert located is not None and located[1].source is not None
        assert located[1].source.params[spec.name] == spec.default_value()

    def test_the_blend_mode_goes_back_to_normal(self, panel: InspectorPanel) -> None:
        harness, clip = _open(panel)
        blend = next(
            box
            for box in panel.findChildren(QComboBox)
            if box.findData(BlendMode.ADD) >= 0 and box.findData(BlendMode.NORMAL) >= 0
        )
        blend.setCurrentIndex(blend.findData(BlendMode.ADD))
        _double_click(_label(panel, "合成モード"))
        located = harness.document.project.timeline.locate_clip(clip.id)
        assert located is not None
        assert located[1].blend_mode == BlendMode.NORMAL

    def test_text_is_not_reset_by_a_stray_double_click(self, panel: InspectorPanel) -> None:
        # 打った文字は戻すと消える うっかり起きやすいダブルクリックでは戻さない
        _open(panel)
        assert not _label(panel, "文字").resettable


class TestSlider:
    def test_clicking_the_groove_is_kept(self, panel: InspectorPanel) -> None:
        # 溝を押した分はプレビューにしか出ず、履歴にも保存にも残らなかった
        harness, clip = _open(panel, AnimatedValue(static=0.4))
        editor = next(e for e in panel.findChildren(TrackEditor) if e.spec.name == "opacity")
        slider = editor.findChild(QSlider)
        assert slider is not None
        slider.triggerAction(QAbstractSlider.SliderAction.SliderPageStepAdd)
        assert harness.opacity(clip).static != 0.4
        assert len(harness.labels) == 1

    def test_grabbing_without_moving_adds_no_step(self, panel: InspectorPanel) -> None:
        # 掴んで離しただけ（ダブルクリックの 1 回目も）で同じ値の段が積まれていた
        harness, _ = _open(panel, AnimatedValue(static=0.4))
        editor = next(e for e in panel.findChildren(TrackEditor) if e.spec.name == "opacity")
        slider = editor.findChild(QSlider)
        assert slider is not None
        slider.setSliderDown(True)
        slider.setSliderDown(False)
        assert harness.labels == []
