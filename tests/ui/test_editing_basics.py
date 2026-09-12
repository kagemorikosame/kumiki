"""右クリック、コピー・貼り付け、トラックの高さ、2 つの窓

どれも操作の入口 中身の正しさは core 側のテスト（test_clipboard.py など）で見るので、
ここでは「入口から正しいコマンドが出るか」と「窓の組み立てに繋がっているか」を見る
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from kumiki.core.commands import AddClip, Command, SetTrackHeights
from kumiki.core.io import is_held, project_lock_path
from kumiki.core.model import Clip, MediaItem, Project, Track, TrackKind
from kumiki.effects.sources import TEXT
from kumiki.engine.cache import MediaAnalyzer
from kumiki.ui.main_window import MainWindow
from kumiki.ui.media_pool import MediaPoolWidget
from kumiki.ui.theme import Metrics
from kumiki.ui.timeline import TimelineView


def _project() -> Project:
    base = Project.create()
    text = Clip(timeline_start=0, duration=30, source=TEXT.create())
    tracks = (Track(TrackKind.VIDEO, "V1", (text,)), Track(TrackKind.AUDIO, "A1"))
    return base.with_timeline(replace(base.timeline, tracks=tracks))


@pytest.fixture
def view(qt_application: QApplication) -> Iterator[TimelineView]:
    del qt_application
    analyzer = MediaAnalyzer(sample_rate=48000, channels=2)
    created = TimelineView(_project(), analyzer)
    created.resize(900, 300)
    yield created
    analyzer.close()


def _received(view: TimelineView) -> list[list[Command]]:
    received: list[list[Command]] = []
    view.commands_requested.connect(lambda commands, _label: received.append(commands))
    return received


def _clip_point(view: TimelineView) -> QPoint:
    band = view._layout.bands(view.project.timeline)[0]
    x = int(view._layout.frame_to_x(10))
    return QPoint(x, band.top + band.height // 2)


def _labels(view: TimelineView, position: QPoint) -> dict[str, bool]:
    menu = view.build_context_menu(position)
    return {action.text(): action.isEnabled() for action in menu.actions() if action.text()}


class TestContextMenu:
    def test_a_clip_offers_the_editing_actions(self, view: TimelineView) -> None:
        labels = _labels(view, _clip_point(view))
        for expected in ("コピー", "切り取り", "削除", "削除して詰める", "再生ヘッドで分割"):
            assert expected in labels

    def test_right_clicking_a_clip_selects_it(self, view: TimelineView) -> None:
        # 選んでいた別のクリップが対象になると、見ていないものを消す
        view.build_context_menu(_clip_point(view))
        assert view.selected_clip == view.project.timeline.tracks[0].clips[0].id

    def test_paste_is_greyed_out_until_something_is_copied(self, view: TimelineView) -> None:
        empty = QPoint(800, _clip_point(view).y())
        assert _labels(view, empty)["貼り付け（再生ヘッドの位置）"] is False
        view.select(view.project.timeline.tracks[0].clips[0].id)
        assert view.copy_selected()
        assert _labels(view, empty)["貼り付け（再生ヘッドの位置）"] is True

    def test_the_track_toggles_are_there(self, view: TimelineView) -> None:
        labels = _labels(view, _clip_point(view))
        assert {"V1 をミュート", "V1 をソロ", "V1 をロック"} <= labels.keys()


class TestCopyPaste:
    def test_paste_goes_to_the_playhead(self, view: TimelineView) -> None:
        received = _received(view)
        view.select(view.project.timeline.tracks[0].clips[0].id)
        view.copy_selected()
        view.set_playhead(90)
        assert view.paste_at_playhead()
        (commands,) = received
        assert [c.clip.timeline_start for c in commands if isinstance(c, AddClip)] == [90]

    def test_nothing_copied_says_so(self, view: TimelineView) -> None:
        messages: list[str] = []
        view.status_message.connect(messages.append)
        assert not view.paste_at_playhead()
        assert messages


class TestTrackHeight:
    def test_dragging_the_border_resizes_that_track(self, view: TimelineView) -> None:
        received = _received(view)
        band = view._layout.bands(view.project.timeline)[0]
        start = QPoint(40, band.bottom)
        QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view, QPoint(40, band.bottom + 30))
        QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(40, band.bottom + 30))

        track = band.track
        assert received == [[SetTrackHeights(((track.id, track.height + 30),))]]
        # 途中の高さは描画のためだけ 離したあと、自分では書き換えていない
        assert view.project.timeline.tracks[0].height == track.height

    def test_the_border_is_only_grabbed_in_the_header(self, view: TimelineView) -> None:
        # タイムラインの側まで広げると、クリップの下端を掴んだつもりが高さの変更になる
        band = view._layout.bands(view.project.timeline)[0]
        assert view._resize_band_at(QPoint(Metrics.TRACK_HEADER_WIDTH + 50, band.bottom)) is None

    def test_all_tracks_move_together(self, view: TimelineView) -> None:
        received = _received(view)
        view.adjust_track_heights(12)
        (commands,) = received
        (command,) = commands
        assert isinstance(command, SetTrackHeights)
        assert len(command.heights) == 2


class TestMediaPoolMenu:
    def test_removing_asks_the_window(
        self, qt_application: QApplication, video_media: MediaItem
    ) -> None:
        del qt_application
        pool = MediaPoolWidget(Project.create(media=(video_media,)))
        asked: list[str] = []
        pool.remove_requested.connect(asked.append)
        menu = pool.build_menu(video_media.id)
        remove = next(action for action in menu.actions() if action.text() == "プールから外す")
        remove.trigger()
        assert asked == [str(video_media.id)]


@pytest.fixture
def window(qt_application: QApplication) -> Iterator[MainWindow]:
    del qt_application
    created = MainWindow(_project(), confirm_unsaved=False)
    yield created
    created.close()


class TestWindow:
    def test_ctrl_v_pastes_as_one_undo_step(self, window: MainWindow) -> None:
        timeline = window._timeline
        timeline.select(window.document.project.timeline.tracks[0].clips[0].id)
        timeline.copy_selected()
        window.seek(30)
        timeline.paste_at_playhead()
        assert len(window.document.project.timeline.tracks[0].clips) == 2
        window.undo()
        assert len(window.document.project.timeline.tracks[0].clips) == 1

    def test_a_file_open_in_another_window_is_noticed(
        self, qt_application: QApplication, tmp_path: Path
    ) -> None:
        del qt_application
        path = tmp_path / "本編.kmk"
        first = MainWindow(_project(), path=path, confirm_unsaved=False)
        try:
            assert is_held(project_lock_path(path))
            second = MainWindow(_project(), path=path, confirm_unsaved=False)
            # 尋ねない設定なので開けはするが、錠は取れない（先の窓が持っている）
            assert second._project_lock is None
            second.close()
            assert is_held(project_lock_path(path))
        finally:
            first.close()
        assert not is_held(project_lock_path(path))
