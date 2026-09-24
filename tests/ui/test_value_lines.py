"""タイムラインの値の線（#27 P8） 不透明度と音量をクリップの上で直接動かす

絵のクリップは不透明度、音のクリップは固定の音量の線を引く 線を上下にドラッグで値、
Ctrl+クリックで点を打つ、点をドラッグで動かす、点の右クリックで消す
ここが崩れると、線を触っても値が変わらないか、取り消しで戻らない値が残る
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
import shiboken6
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QContextMenuEvent, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu

from sashimono.core.commands import (
    AddEffect,
    Command,
    Document,
    MoveClip,
    MoveKeyframe,
    RemoveKeyframe,
    SetKeyframe,
    SetParam,
    TrimClip,
)
from sashimono.core.commands.insert import VOLUME_EFFECT_KIND, default_volume_effect
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    ClipId,
    Keyframe,
    MediaItem,
    Project,
    Track,
    TrackKind,
)
from sashimono.effects.sources import TEXT
from sashimono.engine.cache import MediaAnalyzer
from sashimono.ui.theme import Metrics
from sashimono.ui.timeline import TimelineArea, TimelineView
from sashimono.ui.timeline.painter import clip_rect_for
from sashimono.ui.workspace import Preferences, PreferenceStore

_LEFT = Qt.MouseButton.LeftButton
_NONE = Qt.KeyboardModifier.NoModifier
_CTRL = Qt.KeyboardModifier.ControlModifier


@pytest.fixture
def analyzer() -> Iterator[MediaAnalyzer]:
    created = MediaAnalyzer(sample_rate=48000, channels=2)
    yield created
    created.close()


class _Harness:
    """窓の代わり 出たコマンドを 1 段ずつ履歴に積み、取り消しもできる"""

    def __init__(self, view: TimelineView) -> None:
        self.view = view
        self.document = Document(view.project)
        self.received: list[list[Command]] = []
        self.previewed: list[Command] = []
        view.commands_requested.connect(self._apply)
        view.preview_requested.connect(self.previewed.append)

    def _apply(self, commands: list[Command], label: str) -> None:
        self.received.append(list(commands))
        with self.document.checkpoint(label):
            for command in commands:
                self.document.execute(command)
        self.view.set_project(self.document.project)

    def undo(self) -> None:
        self.view.set_project(self.document.undo())


@pytest.fixture
def made(qt_application: QApplication) -> Iterator[list[TimelineArea]]:
    del qt_application
    areas: list[TimelineArea] = []
    yield areas
    for area in areas:
        area.close()
        shiboken6.delete(area)


def _open(
    areas: list[TimelineArea], analyzer: MediaAnalyzer, project: Project
) -> tuple[TimelineView, _Harness]:
    area = TimelineArea(TimelineView(project, analyzer))
    area.resize(900, 300)
    area.show()
    QApplication.processEvents()
    areas.append(area)
    harness = _Harness(area.view)
    area.view.setProperty("harness", harness)
    return area.view, harness


def _project(*tracks: Track, media: tuple[MediaItem, ...] = ()) -> Project:
    base = Project.create(media=media)
    return base.with_timeline(replace(base.timeline, tracks=tracks))


def _text(opacity: AnimatedValue) -> Clip:
    """10 フレームから 90 フレームの長さのテキスト"""
    return Clip(timeline_start=10, duration=90, source=TEXT.create(), opacity=opacity)


def _area_of(view: TimelineView, clip: Clip) -> tuple[int, int, int]:
    """線を引く範囲の ``(上, 下, 高さ)``"""
    from sashimono.ui.timeline.value_line import line_area

    located = view.project.timeline.locate_clip(clip.id)
    assert located is not None
    band = next(b for b in view.view_layout.bands(view.project.timeline) if b.track is located[0])
    rect = clip_rect_for(located[1], band, view.view_layout, view.width())
    assert rect is not None
    area = line_area(rect)
    assert area is not None, "既定の高さのトラックには線を引く範囲がある"
    return area.top(), area.bottom(), area.height()


def _line_point(view: TimelineView, clip: Clip, frame: int, value: float, maximum: float) -> QPoint:
    """``frame``（タイムラインの位置）で値が ``value`` の所の画面の点"""
    _, bottom, height = _area_of(view, clip)
    x = int(view.view_layout.frame_to_x(frame))
    return QPoint(x, round(bottom - value / maximum * height))


def _drag(
    view: TimelineView, start: QPoint, end: QPoint, modifiers: Qt.KeyboardModifier = _NONE
) -> None:
    QTest.mousePress(view, _LEFT, modifiers, start)
    QTest.mouseMove(view, QPoint(start.x(), (start.y() + end.y()) // 2))
    QTest.mouseMove(view, end)
    QTest.mouseRelease(view, _LEFT, modifiers, end)


def _opacity(view: TimelineView, clip: Clip) -> AnimatedValue:
    located = view.project.timeline.locate_clip(clip.id)
    assert located is not None
    return located[1].opacity


class TestDragTheLine:
    def test_dragging_the_line_changes_the_value_and_undoes_in_one_step(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer
    ) -> None:
        # 途中を命令にすると、ドラッグ 1 回で取り消しの段が動かした分だけ積もる
        clip = _text(AnimatedValue(0.5))
        view, harness = _open(made, analyzer, _project(Track(TrackKind.VIDEO, "V1", (clip,))))
        _, _, height = _area_of(view, clip)
        start = _line_point(view, clip, 50, 0.5, 1.0)
        _drag(view, start, start + QPoint(0, -height // 4))

        assert len(harness.received) == 1
        (command,) = harness.received[0]
        assert isinstance(command, SetParam)
        raised = _opacity(view, clip).static
        assert raised == pytest.approx(0.75, abs=0.05)
        assert harness.previewed, "ドラッグの途中の値をプレビューへ渡していない"
        harness.undo()
        assert _opacity(view, clip) == AnimatedValue(0.5)

    def test_while_dragging_the_project_is_not_rewritten(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer
    ) -> None:
        # 途中で履歴へ積むと、離す前にほかの操作をしたときに途中の値が残る
        clip = _text(AnimatedValue(0.5))
        view, harness = _open(made, analyzer, _project(Track(TrackKind.VIDEO, "V1", (clip,))))
        start = _line_point(view, clip, 50, 0.5, 1.0)
        QTest.mousePress(view, _LEFT, _NONE, start)
        QTest.mouseMove(view, start + QPoint(0, 8))
        assert harness.received == []
        assert _opacity(view, clip).static < 0.5, "途中の値を描いていない"
        QTest.mouseRelease(view, _LEFT, _NONE, start + QPoint(0, 8))
        assert len(harness.received) == 1

    def test_with_keyframes_every_point_moves_by_the_same_amount(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer
    ) -> None:
        # 掴んだ所の点だけが動くと、フェードの形が崩れる
        value = AnimatedValue(1.0, (Keyframe(10, 0.2), Keyframe(70, 0.6)))
        clip = _text(value)
        view, harness = _open(made, analyzer, _project(Track(TrackKind.VIDEO, "V1", (clip,))))
        _, _, height = _area_of(view, clip)
        # 点と点の真ん中（クリップの頭から 40 フレーム 値 0.4）を掴む
        start = _line_point(view, clip, 50, value.at(40), 1.0)
        _drag(view, start, start + QPoint(0, -round(height * 0.2)))

        assert len(harness.received) == 1
        moved = _opacity(view, clip)
        deltas = [
            after.value - before.value
            for before, after in zip(value.keyframes, moved.keyframes, strict=True)
        ]
        assert deltas[0] == pytest.approx(deltas[1])
        assert deltas[0] == pytest.approx(0.2, abs=0.05)
        assert [k.frame for k in moved.keyframes] == [10, 70]


class TestKeyframes:
    def test_ctrl_click_on_the_line_sets_a_keyframe_without_changing_the_shape(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer
    ) -> None:
        clip = _text(AnimatedValue(0.5))
        view, harness = _open(made, analyzer, _project(Track(TrackKind.VIDEO, "V1", (clip,))))
        point = _line_point(view, clip, 40, 0.5, 1.0)
        QTest.mouseClick(view, _LEFT, _CTRL, point)

        assert len(harness.received) == 1
        (command,) = harness.received[0]
        assert isinstance(command, SetKeyframe)
        keyframes = _opacity(view, clip).keyframes
        assert [k.frame for k in keyframes] == [30]
        assert keyframes[0].value == pytest.approx(0.5)

    def test_dragging_a_point_moves_its_time_and_value(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer
    ) -> None:
        value = AnimatedValue(1.0, (Keyframe(10, 0.2), Keyframe(70, 0.6)))
        clip = _text(value)
        view, harness = _open(made, analyzer, _project(Track(TrackKind.VIDEO, "V1", (clip,))))
        start = _line_point(view, clip, 20, 0.2, 1.0)
        end = _line_point(view, clip, 40, 0.8, 1.0)
        _drag(view, start, end)

        assert len(harness.received) == 1
        (command,) = harness.received[0]
        assert isinstance(command, MoveKeyframe)
        moved = _opacity(view, clip).keyframes
        assert moved[0].frame == pytest.approx(30, abs=1)
        assert moved[0].value == pytest.approx(0.8, abs=0.05)
        assert moved[1] == value.keyframes[1], "掴んでいない点まで動いた"

    def test_a_point_does_not_pass_its_neighbour(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer
    ) -> None:
        # 越えさせると MoveKeyframe が行き先の点を置き換え、隣の点が消える
        value = AnimatedValue(1.0, (Keyframe(10, 0.2), Keyframe(30, 0.6)))
        clip = _text(value)
        view, _ = _open(made, analyzer, _project(Track(TrackKind.VIDEO, "V1", (clip,))))
        _drag(view, _line_point(view, clip, 20, 0.2, 1.0), _line_point(view, clip, 80, 0.2, 1.0))
        frames = [k.frame for k in _opacity(view, clip).keyframes]
        assert frames == [29, 30]

    def test_right_click_on_a_point_removes_it(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer
    ) -> None:
        value = AnimatedValue(1.0, (Keyframe(10, 0.2), Keyframe(70, 0.6)))
        clip = _text(value)
        view, harness = _open(made, analyzer, _project(Track(TrackKind.VIDEO, "V1", (clip,))))
        point = _line_point(view, clip, 80, 0.6, 1.0)
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, view.mapToGlobal(point))
        view.contextMenuEvent(event)

        assert len(harness.received) == 1
        (command,) = harness.received[0]
        assert isinstance(command, RemoveKeyframe)
        assert [k.frame for k in _opacity(view, clip).keyframes] == [10]


class TestWhereTheLineIsNotGrabbed:
    def test_near_the_edge_the_press_trims(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer
    ) -> None:
        # 線は端まで引いてある 線を先に取ると、クリップの端を掴んで削れなくなる
        clip = _text(AnimatedValue(0.5))
        view, harness = _open(made, analyzer, _project(Track(TrackKind.VIDEO, "V1", (clip,))))
        start = _line_point(view, clip, 10, 0.5, 1.0) + QPoint(2, 0)
        _drag(view, start, start + QPoint(40, 0))

        (command,) = harness.received[0]
        assert isinstance(command, TrimClip)
        assert _opacity(view, clip) == AnimatedValue(0.5)

    def test_a_low_track_has_no_line(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer
    ) -> None:
        # 低い帯では線が名前やひし形に重なり、掴んだのが何か分からない 引かずに移動に使う
        from sashimono.ui.timeline.value_line import MIN_LINE_TRACK_HEIGHT

        clip = _text(AnimatedValue(0.5))
        track = replace(Track(TrackKind.VIDEO, "V1", (clip,)), height=MIN_LINE_TRACK_HEIGHT - 8)
        view, harness = _open(made, analyzer, _project(track))
        assert not _bright_in_clip(view, clip), "低い帯に線を描いた"

        band = next(iter(view.view_layout.bands(view.project.timeline)))
        start = QPoint(int(view.view_layout.frame_to_x(50)), band.top + band.height // 2)
        _drag(view, start, start + QPoint(30, 0))
        (command,) = harness.received[0]
        assert isinstance(command, MoveClip)

    def test_the_line_is_drawn_on_a_default_track(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer
    ) -> None:
        # 上の試験が「何も描いていない」ことで通らないよう、描く側も見ておく
        clip = _text(AnimatedValue(0.5))
        view, _ = _open(made, analyzer, _project(Track(TrackKind.VIDEO, "V1", (clip,))))
        assert _bright_in_clip(view, clip)


def _bright_in_clip(view: TimelineView, clip: Clip) -> bool:
    """クリップの真ん中の縦の列に、線の明るい色の画素があるか（名前の帯より下だけを見る）"""
    image: QImage = view.grab().toImage()
    band = next(iter(view.view_layout.bands(view.project.timeline)))
    x = int(view.view_layout.frame_to_x(clip.timeline_start + clip.duration // 2))
    top = band.top + 1 + Metrics.CLIP_LABEL_HEIGHT + 1
    for y in range(top, band.bottom - 2):
        color = image.pixelColor(x, y)
        if min(color.red(), color.green(), color.blue()) > 200:
            return True
    return False


class TestPreferences:
    def test_turning_the_lines_off_hides_them_and_the_press_moves_the_clip(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer
    ) -> None:
        # 切っても掴めるなら、設定がある方が質が悪い
        clip = _text(AnimatedValue(0.5))
        view, harness = _open(made, analyzer, _project(Track(TrackKind.VIDEO, "V1", (clip,))))
        start = _line_point(view, clip, 50, 0.5, 1.0)
        view.set_value_lines(False)
        assert not _bright_in_clip(view, clip)
        _drag(view, start, start + QPoint(30, -10))
        (command,) = harness.received[0]
        assert isinstance(command, MoveClip)
        assert _opacity(view, clip) == AnimatedValue(0.5)

    def test_the_setting_is_saved_and_defaults_to_shown(self, tmp_path: object) -> None:
        from pathlib import Path

        assert isinstance(tmp_path, Path)
        assert Preferences().value_lines is True
        store = PreferenceStore(tmp_path / "preferences.json")
        store.save(Preferences(value_lines=False))
        assert store.load().value_lines is False

    def test_the_dialog_carries_the_setting(self, qt_application: QApplication) -> None:
        from sashimono.ui.preferences_dialog import PreferencesDialog

        del qt_application
        dialog = PreferencesDialog(Preferences(value_lines=False))
        try:
            assert dialog.preferences().value_lines is False
        finally:
            dialog.deleteLater()


class TestVolume:
    def test_an_old_audio_clip_gets_a_fixed_volume_in_the_same_step(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer, audio_media: MediaItem
    ) -> None:
        # 足すのと値を変えるのが別の段だと、1 回戻したときに 100% の音量調整だけが残る
        clip = Clip(timeline_start=10, duration=90, media_id=audio_media.id)
        project = _project(Track(TrackKind.AUDIO, "A1", (clip,)), media=(audio_media,))
        view, harness = _open(made, analyzer, project)
        _, _, height = _area_of(view, clip)
        start = _line_point(view, clip, 50, 100.0, 400.0)
        _drag(view, start, start + QPoint(0, -height // 4))

        assert len(harness.received) == 1
        (command,) = harness.received[0]
        assert isinstance(command, AddEffect)
        assert command.effect.fixed
        located = view.project.timeline.locate_clip(clip.id)
        assert located is not None
        (effect,) = located[1].effects
        volume = effect.params["volume"]
        assert isinstance(volume, AnimatedValue)
        assert volume.static == pytest.approx(200.0, abs=15)
        harness.undo()
        located = view.project.timeline.locate_clip(clip.id)
        assert located is not None
        assert located[1].effects == ()

    def test_a_fixed_volume_is_changed_in_place(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer, audio_media: MediaItem
    ) -> None:
        clip = Clip(
            timeline_start=10,
            duration=90,
            media_id=audio_media.id,
            effects=(default_volume_effect(),),
        )
        project = _project(Track(TrackKind.AUDIO, "A1", (clip,)), media=(audio_media,))
        view, harness = _open(made, analyzer, project)
        _, _, height = _area_of(view, clip)
        start = _line_point(view, clip, 50, 100.0, 400.0)
        _drag(view, start, start + QPoint(0, height // 8))

        (command,) = harness.received[0]
        assert isinstance(command, SetParam)
        located = view.project.timeline.locate_clip(clip.id)
        assert located is not None
        (effect,) = located[1].effects
        assert effect.kind == VOLUME_EFFECT_KIND and effect.fixed
        volume = effect.params["volume"]
        assert isinstance(volume, AnimatedValue)
        assert volume.static == pytest.approx(50.0, abs=15)


class TestMixedClip:
    def _mixed(self, media: MediaItem) -> Clip:
        return Clip(
            timeline_start=10,
            duration=90,
            media_id=media.id,
            stream_index=media.video_streams[0].index,
            audio_stream=media.audio_streams[0].index,
            opacity=AnimatedValue(0.5),
        )

    def test_a_video_with_sound_shows_opacity_and_switches_to_volume(
        self, qt_application: QApplication, video_media: MediaItem
    ) -> None:
        # 利用者の決定 絵は不透明度、音は音量 両方を持つ音付きの動画は右クリックで切り替える
        # 混合トラックはまだタイムラインに並ばない（P4b） 並ぶまでは線の受け持ちを直に確かめる
        from sashimono.ui.timeline.value_line import (
            SHOW_OPACITY_TEXT,
            SHOW_VOLUME_TEXT,
            ValueKind,
            ValueLineEditor,
        )

        del qt_application
        clip = self._mixed(video_media)
        other = replace(self._mixed(video_media), id=ClipId("other"), timeline_start=200)
        track = Track(TrackKind.MIXED, "レイヤー 1", (clip, other))
        project = _project(track, media=(video_media,))
        redrawn: list[None] = []
        editor = ValueLineEditor(
            lambda *_: None, lambda _: None, lambda _: None, lambda: redrawn.append(None)
        )
        assert editor.kind_for(project, track, clip) is ValueKind.OPACITY

        menu = QMenu()
        editor.add_menu_items(menu, project, track, clip, (clip.id, other.id))
        actions = {a.text(): a for a in menu.actions()}
        assert actions[SHOW_OPACITY_TEXT].isChecked()
        assert not actions[SHOW_VOLUME_TEXT].isChecked()
        actions[SHOW_VOLUME_TEXT].trigger()

        assert editor.kind_for(project, track, clip) is ValueKind.VOLUME
        assert editor.kind_for(project, track, other) is ValueKind.VOLUME, "選んだ仲間も切り替える"
        assert redrawn, "切り替えても描き直さない"
        menu.deleteLater()

    def test_a_mixed_clip_offers_only_what_it_has(
        self, video_media: MediaItem, audio_media: MediaItem
    ) -> None:
        # 音だけの素材に不透明度の線を出しても、描く絵が無いので何も変わらない
        from sashimono.ui.timeline.value_line import ValueKind, value_kinds

        sound = Clip(timeline_start=0, duration=30, media_id=audio_media.id, audio_stream=0)
        mute = replace(self._mixed(video_media), audio_stream=None)
        text = _text(AnimatedValue(1.0))
        track = Track(TrackKind.MIXED, "レイヤー 1", (sound,))
        project = _project(track, media=(video_media, audio_media))
        assert value_kinds(track, sound, project) == (ValueKind.VOLUME,)
        assert value_kinds(track, mute, project) == (ValueKind.OPACITY,)
        assert value_kinds(track, text, project) == (ValueKind.OPACITY,)
        both = value_kinds(track, self._mixed(video_media), project)
        assert both == (ValueKind.OPACITY, ValueKind.VOLUME)

    def test_a_clip_with_only_one_value_has_no_switch(
        self, made: list[TimelineArea], analyzer: MediaAnalyzer
    ) -> None:
        # 切り替える先の無い項目を出すと、選んでも何も変わらない
        from sashimono.ui.timeline.value_line import SHOW_VOLUME_TEXT

        clip = _text(AnimatedValue(0.5))
        view, _ = _open(made, analyzer, _project(Track(TrackKind.VIDEO, "V1", (clip,))))
        point = _line_point(view, clip, 50, 0.9, 1.0)
        menu = view.build_context_menu(point)
        assert SHOW_VOLUME_TEXT not in [a.text() for a in menu.actions()]
