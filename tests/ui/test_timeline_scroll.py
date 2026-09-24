"""タイムラインのスクロールバーと、リンクした映像と音声の選択の見え方

スクロールバーは表示の位置（:class:`TimelineLayout`）と 2 か所で同じことを持つ
どちらかだけが動くと、バーの位置と見えている所がずれる 選択の枠は、映像と音声の
組のうち片方にしか出ないと、もう片方も一緒に動くことが見えない
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
import shiboken6
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sashimono.core.commands import insert_media
from sashimono.core.model import Clip, MediaItem, Project, Track, TrackKind
from sashimono.effects.sources import TEXT
from sashimono.engine.cache import MediaAnalyzer
from sashimono.ui.theme import Colors, Metrics
from sashimono.ui.timeline import TimelineArea, TimelineView
from sashimono.ui.timeline.layout import TrackBand

_LEFT = Qt.MouseButton.LeftButton


def _long_project(frames: int = 3000, tracks: int = 2) -> Project:
    """映像トラックに ``frames`` の長さのテキストを 1 本 トラックは ``tracks`` 本"""
    base = Project.create()
    clip = Clip(timeline_start=0, duration=frames, source=TEXT.create())
    made = [Track(TrackKind.VIDEO, "V1", (clip,))]
    made += [Track(TrackKind.VIDEO, f"V{i + 2}") for i in range(tracks - 1)]
    return base.with_timeline(replace(base.timeline, tracks=tuple(made)))


@pytest.fixture
def analyzer() -> Iterator[MediaAnalyzer]:
    created = MediaAnalyzer(sample_rate=48000, channels=2)
    yield created
    created.close()


@pytest.fixture
def area(qt_application: QApplication, analyzer: MediaAnalyzer) -> Iterator[TimelineArea]:
    del qt_application
    created = TimelineArea(TimelineView(_long_project(), analyzer))
    created.resize(900, 300)
    created.show()
    QApplication.processEvents()
    yield created
    created.close()
    # 閉じただけで残すと、いつかのごみ集めで壊され、そのとき走っている別の試験で落ちる
    shiboken6.delete(created)


def _wheel(view: TimelineView, *, x: int = 0, y: int = 0, modifiers: Qt.KeyboardModifier) -> None:
    position = QPointF(400, 100)
    event = QWheelEvent(
        position,
        view.mapToGlobal(position),
        QPoint(0, 0),
        QPoint(x, y),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(view, event)


class TestHorizontalBar:
    def test_the_bar_covers_the_project_and_some_room(self, area: TimelineArea) -> None:
        # バーが無い、または短いと、右の方のクリップへは Shift+ホイールでしか行けない
        view = area.view
        bar = view.horizontal_scroll_bar
        scale = view.view_layout.pixels_per_frame
        visible = view.width() - Metrics.TRACK_HEADER_WIDTH
        assert bar.isVisible()
        assert bar.pageStep() == visible
        # 3000 フレームの末尾が、いちばん右まで寄せたときに画面に入る
        assert bar.maximum() + visible >= 3000 * scale

    def test_moving_the_bar_scrolls_the_view(self, area: TimelineArea) -> None:
        # バーだけ動いて絵が動かないと、見ている時刻を取り違える
        view = area.view
        bar = view.horizontal_scroll_bar
        bar.setValue(bar.maximum() // 2)
        expected = bar.value() / view.view_layout.pixels_per_frame
        assert view.view_layout.scroll_frame == pytest.approx(expected)

    def test_zooming_changes_the_length(self, area: TimelineArea) -> None:
        # 拡大しても長さが変わらないと、バーの端まで寄せても末尾に届かない
        view = area.view
        bar = view.horizontal_scroll_bar
        before = bar.maximum() + bar.pageStep()
        view.zoom(2.0)
        after = bar.maximum() + bar.pageStep()
        assert after > before * 1.8

    def test_shift_wheel_moves_the_bar(self, area: TimelineArea) -> None:
        # 今までの Shift+ホイールは残す 動いたのにバーが付いてこないと、ずれて見える
        view = area.view
        _wheel(view, y=-120, modifiers=Qt.KeyboardModifier.ShiftModifier)
        assert view.view_layout.scroll_frame > 0
        scale = view.view_layout.pixels_per_frame
        assert view.horizontal_scroll_bar.value() == round(view.view_layout.scroll_frame * scale)

    def test_sideways_wheel_scrolls_across(self, area: TimelineArea) -> None:
        # タッチパッドの 2 本指や横に倒すホイールは横の量で来る 受けないと、
        # 横へなぞっても何も起きない
        view = area.view
        _wheel(view, x=-120, modifiers=Qt.KeyboardModifier.NoModifier)
        assert view.view_layout.scroll_frame > 0
        assert view.view_layout.scroll_y == 0
        first = view.view_layout.scroll_frame
        _wheel(view, x=60, modifiers=Qt.KeyboardModifier.NoModifier)
        assert 0 < view.view_layout.scroll_frame < first

    def test_wheeling_at_the_end_stops_at_the_end(self, area: TimelineArea) -> None:
        # 今の位置をバーの範囲に含めていたので、末尾で Shift+ホイールや横ホイールを
        # 回すたびに範囲が伸び、空白へどこまでも進めた（PR #145 の指摘）
        view = area.view
        bar = view.horizontal_scroll_bar
        bar.setValue(bar.maximum())
        limit, where = bar.maximum(), view.view_layout.scroll_frame
        for _ in range(20):
            _wheel(view, y=-120, modifiers=Qt.KeyboardModifier.ShiftModifier)
            _wheel(view, x=-120, modifiers=Qt.KeyboardModifier.NoModifier)
        assert bar.maximum() == limit
        assert view.view_layout.scroll_frame == pytest.approx(where)
        # 末尾はまだ画面に入っている
        assert view.view_layout.frame_to_x(3000) < view.width()

    def test_the_playhead_past_the_end_can_still_be_followed(self, area: TimelineArea) -> None:
        # 範囲を中身だけで決めると、矢印キーで末尾の先へ進めた再生ヘッドを追えず、
        # 画面の外へ出たまま見えなくなる
        view = area.view
        view.set_playhead(6000)
        x = view.view_layout.frame_to_x(6000)
        assert Metrics.TRACK_HEADER_WIDTH <= x < view.width()

    def test_the_bar_starts_after_the_header(self, area: TimelineArea) -> None:
        # バーが動かすのは時間の軸 ヘッダの下まで伸ばすと、何を動かすのか分かりにくい
        bar = area.view.horizontal_scroll_bar
        assert bar.mapTo(area, QPoint(0, 0)).x() == Metrics.TRACK_HEADER_WIDTH


class TestVerticalBar:
    def test_hidden_while_every_track_fits(self, area: TimelineArea) -> None:
        # 使えないバーは幅を取るだけ
        assert not area.view.vertical_scroll_bar.isVisible()

    def test_wheeling_down_stays_on_the_tracks(self, area: TimelineArea) -> None:
        # 今の位置をバーの範囲に含めていたので、全部のトラックが見えていても下へ
        # 回すとバーが現れ、トラックを画面の外へ追い出せた（PR #145 の指摘）
        view = area.view
        for _ in range(10):
            _wheel(view, y=-120, modifiers=Qt.KeyboardModifier.NoModifier)
        assert view.view_layout.scroll_y == 0
        assert not view.vertical_scroll_bar.isVisible()

    def test_removing_tracks_brings_the_view_back(
        self, qt_application: QApplication, analyzer: MediaAnalyzer
    ) -> None:
        # 下まで送ってからトラックを減らすと、前の位置が残って空白を見たまま戻らず、
        # バーも消えなかった（PR #145 の指摘）
        del qt_application
        area = TimelineArea(TimelineView(_long_project(tracks=8), analyzer))
        area.resize(900, 300)
        area.show()
        QApplication.processEvents()
        try:
            view = area.view
            bar = view.vertical_scroll_bar
            bar.setValue(bar.maximum())
            assert view.view_layout.scroll_y > 0
            view.set_project(_long_project(tracks=2))
            assert view.view_layout.scroll_y == 0
            assert bar.value() == 0
            assert not bar.isVisible()
        finally:
            area.close()
            shiboken6.delete(area)

    def test_shown_and_working_when_tracks_overflow(
        self, qt_application: QApplication, analyzer: MediaAnalyzer
    ) -> None:
        # トラックが溢れたときにバーが無いと、下のトラックへはホイールでしか行けない
        del qt_application
        area = TimelineArea(TimelineView(_long_project(tracks=8), analyzer))
        area.resize(900, 300)
        area.show()
        QApplication.processEvents()
        try:
            view = area.view
            bar = view.vertical_scroll_bar
            assert bar.isVisible()
            bar.setValue(bar.maximum())
            assert view.view_layout.scroll_y == bar.maximum()
            bands = view.view_layout.bands(view.project.timeline)
            assert bands[-1].bottom <= view.height() + 1
        finally:
            area.close()
            shiboken6.delete(area)


def _linked_view(view: TimelineView, media: MediaItem) -> tuple[Project, Clip, Clip]:
    base = Project.create()
    for command in insert_media(base, media, at_frame=10):
        base = command.apply(base)
    view.set_project(base)
    video = next(c for t in base.timeline.tracks if t.kind is TrackKind.VIDEO for c in t.clips)
    audio = next(c for t in base.timeline.tracks if t.kind is TrackKind.AUDIO for c in t.clips)
    return base, video, audio


def _band(view: TimelineView, kind: TrackKind) -> TrackBand:
    return next(b for b in view.view_layout.bands(view.project.timeline) if b.track.kind is kind)


def _white_in_column(image: QImage, x: int, band: TrackBand) -> int:
    """帯の中の 1 列で、選択の色（白）の画素の数"""
    selection = Colors.SELECTION.name()
    return sum(
        1
        for y in range(band.top + 2, band.bottom - 3)
        if image.pixelColor(x, y).name() == selection
    )


class TestLinkedSelection:
    @pytest.fixture
    def view(self, qt_application: QApplication, analyzer: MediaAnalyzer) -> Iterator[TimelineView]:
        del qt_application
        created = TimelineView(Project.create(), analyzer)
        created.resize(900, 300)
        yield created
        shiboken6.delete(created)

    def test_selecting_the_picture_outlines_the_sound(
        self, view: TimelineView, video_media: MediaItem
    ) -> None:
        # 映像を選んだとき、枠が映像の側にしか出ないと、音声も一緒に動くことが
        # 見えなかった（Issue #27）
        _, video, audio = _linked_view(view, video_media)
        band = _band(view, TrackKind.VIDEO)
        point = QPoint(int(view.view_layout.frame_to_x(video.timeline_start + 40)), band.top + 30)
        QTest.mouseClick(view, _LEFT, pos=point)
        assert view.selected_clips == (video.id,)

        image = view.grab().toImage()
        left = int(view.view_layout.frame_to_x(audio.timeline_start))
        assert _white_in_column(image, left, _band(view, TrackKind.AUDIO)) > 10
        assert _white_in_column(image, left, _band(view, TrackKind.VIDEO)) > 10

    def test_the_sound_is_not_added_to_the_selection(
        self, view: TimelineView, video_media: MediaItem
    ) -> None:
        # 相手まで選択に入れると、削除や移動が同じ組へ 2 度当たる 枠だけを出す
        _, video, _ = _linked_view(view, video_media)
        view.select(video.id)
        assert view.selected_clips == (video.id,)

    def test_dragging_shows_where_the_sound_lands(
        self, view: TimelineView, video_media: MediaItem
    ) -> None:
        # 掴んで動かしている間、落ちる所の点線が映像にしか出ないと、音声がどこへ
        # 行くのか離すまで分からない
        _, video, audio = _linked_view(view, video_media)
        band = _band(view, TrackKind.VIDEO)
        layout = view.view_layout
        start = QPoint(int(layout.frame_to_x(video.timeline_start + 40)), band.top + 30)
        end = QPoint(int(layout.frame_to_x(video.timeline_start + 80)), band.top + 30)
        QTest.mousePress(view, _LEFT, pos=start)
        QTest.mouseMove(view, end)
        try:
            image = view.grab().toImage()
            # 40 フレーム後ろへ動かした音声の、新しい右端の少し手前
            landing = int(layout.frame_to_x(audio.timeline_end + 40)) - 1
            assert landing < view.width()
            assert _white_in_column(image, landing, _band(view, TrackKind.AUDIO)) > 3
        finally:
            QTest.mouseRelease(view, _LEFT, pos=end)
