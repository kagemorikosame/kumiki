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

from sashimono.core.commands import AddClip, Command, SetTrackHeights
from sashimono.core.io import others_holding, project_presence_dir
from sashimono.core.model import Clip, MediaItem, Project, Track, TrackKind
from sashimono.effects.sources import TEXT
from sashimono.engine.cache import MediaAnalyzer
from sashimono.ui.main_window import MainWindow
from sashimono.ui.media_pool import MediaPoolWidget
from sashimono.ui.theme import Metrics
from sashimono.ui.timeline import TimelineView


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
        # 壊れると、右クリックしても編集の操作が出ず、メニューを探しに行くことになる
        labels = _labels(view, _clip_point(view))
        for expected in ("コピー", "切り取り", "削除", "削除して詰める", "再生ヘッドで分割"):
            assert expected in labels

    def test_right_clicking_a_clip_selects_it(self, view: TimelineView) -> None:
        # 選んでいた別のクリップが対象になると、見ていないものを消す
        view.build_context_menu(_clip_point(view))
        assert view.selected_clip == view.project.timeline.tracks[0].clips[0].id

    def test_paste_is_greyed_out_until_something_is_copied(self, view: TimelineView) -> None:
        # 押せるのに何も起きない項目は、壊れているように見える
        empty = QPoint(800, _clip_point(view).y())
        assert _labels(view, empty)["貼り付け（再生ヘッドの位置）"] is False
        view.select(view.project.timeline.tracks[0].clips[0].id)
        assert view.copy_selected()
        assert _labels(view, empty)["貼り付け（再生ヘッドの位置）"] is True

    def test_the_track_toggles_are_there(self, view: TimelineView) -> None:
        # 壊れると、右クリックからトラックをミュートできない
        labels = _labels(view, _clip_point(view))
        assert {"V1 をミュート", "V1 をソロ", "V1 をロック"} <= labels.keys()


class TestCopyPaste:
    def test_paste_goes_to_the_playhead(self, view: TimelineView) -> None:
        # 壊れると、クリップが再生ヘッドとは違う時刻に置かれる
        received = _received(view)
        view.select(view.project.timeline.tracks[0].clips[0].id)
        view.copy_selected()
        view.set_playhead(90)
        assert view.paste_at_playhead()
        (commands,) = received
        assert [c.clip.timeline_start for c in commands if isinstance(c, AddClip)] == [90]

    def test_nothing_copied_says_so(self, view: TimelineView) -> None:
        # 黙って何も起きないと、貼り付けが壊れているのか区別がつかない
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
        # 1 本ずつのコマンドになると、まとめて変えたのに取り消しをトラックの数だけ押す
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
        # 壊れると、貼り付けを 1 回の取り消しで戻せない
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
        path = tmp_path / "本編.sme"
        folder = project_presence_dir(path)
        first = MainWindow(_project(), path=path, confirm_unsaved=False)
        try:
            assert others_holding(folder)
            second = MainWindow(_project(), path=path, confirm_unsaved=False)
            # 尋ねない設定なので開けるが、先の窓がいることは見えている 見えないと、
            # 2 つの窓が警告なしで同じ作品を開き、あとから保存した方の内容だけが残る
            assert second._project_lock is not None
            assert others_holding(folder, second._project_lock.path)
            second.close()
            assert others_holding(folder)
        finally:
            first.close()
        assert not others_holding(folder)

    def test_a_window_opened_anyway_is_still_seen_after_the_first_closes(
        self, qt_application: QApplication, tmp_path: Path
    ) -> None:
        # 「それでも開く」の窓が数に入らないと、先の窓が閉じたあと、まだ開いて
        # いるのに 3 つ目の窓が警告なしで開けてしまう 待ち時間なしで見えること
        del qt_application
        path = tmp_path / "本編.sme"
        folder = project_presence_dir(path)
        first = MainWindow(_project(), path=path, confirm_unsaved=False)
        second = MainWindow(_project(), path=path, confirm_unsaved=False)
        try:
            first.close()
            assert others_holding(folder)
        finally:
            second.close()
        assert not others_holding(folder)
