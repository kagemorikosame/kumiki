"""オブジェクト設定が出しているクリップを、タイムラインで見分けられること（利用者の要望）

映像と音声を分けて置いたクリップやグループにしたクリップは、1 本を押すと仲間も一緒に
選ばれて同じ白い枠が付く 設定パネルが直すのはそのうちの 1 本（最後に押した物）だけで、
どれなのかが見えないと、音量を変えたつもりで映像の側を触っていることに気付けない
窓は表示しない（オフスクリーン）
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sashimono.core.commands import GroupClips, insert_media
from sashimono.core.model import (
    Clip,
    ClipId,
    LayerMode,
    MediaItem,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.effects.sources import TEXT
from sashimono.engine.cache import MediaAnalyzer
from sashimono.ui.theme import Colors
from sashimono.ui.timeline import TimelineView
from sashimono.ui.timeline.layout import TimelineLayout, TrackBand
from sashimono.ui.timeline.painter import clip_rect_for, draw_dense_clips


@pytest.fixture
def analyzer() -> Iterator[MediaAnalyzer]:
    created = MediaAnalyzer(sample_rate=48000, channels=2)
    yield created
    created.close()


def _split_video(video_media: MediaItem) -> Project:
    project = Project.create(ProjectSettings(layer_mode=LayerMode.MIXED), media=(video_media,))
    for command in insert_media(project, video_media, split_audio=True):
        project = command.apply(project)
    return project


def _open(project: Project, analyzer: MediaAnalyzer) -> TimelineView:
    view = TimelineView(project, analyzer)
    view.resize(1000, 400)
    view.zoom_to_fit()
    return view


def _rect(view: TimelineView, clip_id: ClipId) -> QRect:
    for band in view.view_layout.bands(view.project.timeline):
        for clip in band.track.clips:
            if clip.id == clip_id:
                rect = clip_rect_for(clip, band, view.view_layout, view.width())
                assert rect is not None
                return rect
    raise AssertionError("クリップが画面に無い")


def _accent_pixels(image: QImage, rect: QRect) -> int:
    wanted = Colors.EDITING.rgb()
    return sum(
        1
        for x in range(rect.left(), rect.right() + 1)
        for y in range(rect.top(), rect.bottom() + 1)
        if image.pixel(x, y) == wanted
    )


def _clips(project: Project) -> list[Clip]:
    return [clip for track in project.timeline.tracks for clip in track.clips]


class TestLinkedParts:
    def test_only_the_clicked_part_is_marked(
        self, qt_application: QApplication, analyzer: MediaAnalyzer, video_media: MediaItem
    ) -> None:
        # 仲間まで同じ見た目だと、設定パネルがどちらを直しているのか分からない
        del qt_application
        view = _open(_split_video(video_media), analyzer)
        try:
            picture, sound = _clips(view.project)
            QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=_rect(view, sound.id).center())
            assert view.selected_clip == sound.id
            image = view.grab().toImage()
            assert _accent_pixels(image, _rect(view, sound.id)) > 20
            assert _accent_pixels(image, _rect(view, picture.id)) == 0

            # 映像の側を押し直すと、印も映像の側へ移る
            QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=_rect(view, picture.id).center())
            image = view.grab().toImage()
            assert _accent_pixels(image, _rect(view, picture.id)) > 20
            assert _accent_pixels(image, _rect(view, sound.id)) == 0
        finally:
            view.deleteLater()


class TestGroups:
    def test_only_the_last_clicked_member_is_marked(
        self, qt_application: QApplication, analyzer: MediaAnalyzer
    ) -> None:
        # グループは押すと全員が選ばれる 印が全員に付くと、設定パネルの 1 本がどれか分からない
        del qt_application
        base = Project.create(ProjectSettings(layer_mode=LayerMode.MIXED))
        first = Clip(timeline_start=0, duration=120, source=TEXT.create())
        second = Clip(timeline_start=0, duration=120, source=TEXT.create())
        tracks = (
            Track(TrackKind.MIXED, "レイヤー 1", (first,)),
            Track(TrackKind.MIXED, "レイヤー 2", (second,)),
        )
        project = GroupClips((first.id, second.id)).apply(
            base.with_timeline(replace(base.timeline, tracks=tracks))
        )
        view = _open(project, analyzer)
        try:
            QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=_rect(view, second.id).center())
            assert set(view.selected_clips) == {first.id, second.id}
            assert view.selected_clip == second.id
            image = view.grab().toImage()
            assert _accent_pixels(image, _rect(view, second.id)) > 20
            assert _accent_pixels(image, _rect(view, first.id)) == 0
        finally:
            view.deleteLater()


def test_a_thin_clip_is_marked_too(qt_application: QApplication) -> None:
    # 全体を表示して帯になった細いクリップでも、どれを直しているかが見えないと困る
    del qt_application
    clips = tuple(Clip(timeline_start=n * 10, duration=10, source=TEXT.create()) for n in range(4))
    track = Track(TrackKind.MIXED, "レイヤー 1", clips)
    band = TrackBand(track, 40, 60)
    layout = TimelineLayout(pixels_per_frame=2.0)
    image = QImage(600, 120, QImage.Format.Format_RGB32)
    image.fill(Colors.TIMELINE_BACKGROUND)
    painter = QPainter(image)
    draw_dense_clips(
        painter,
        band,
        clips,
        layout,
        600,
        {c.id for c in clips},
        editing=clips[2].id,
    )
    painter.end()
    start, middle, end = (int(layout.frame_to_x(frame)) for frame in (0, 20, 30))
    marked = QRect(middle, band.top, end - middle, band.height)
    others = QRect(start, band.top, middle - start - 1, band.height)
    assert _accent_pixels(image, marked) > 0
    assert _accent_pixels(image, others) == 0
