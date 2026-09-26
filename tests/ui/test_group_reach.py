"""グループ制御の受け持つ範囲をタイムラインで見せる（利用者の要望）

受け持つトラックの、グループ制御の時間の幅に薄い色を敷き、グループ制御の頭から括弧を引く
対象レイヤー数が 0（手前のすべて）なら並びの端まで 窓は表示しない（オフスクリーン）
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QApplication

from sashimono.core.model import (
    GROUP_KIND,
    GROUP_LAYERS,
    Clip,
    GeneratedSource,
    LayerMode,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.effects.sources import TEXT
from sashimono.engine.cache import MediaAnalyzer
from sashimono.ui.timeline import TimelineView
from sashimono.ui.timeline.group_reach import GroupReach, group_reach
from sashimono.ui.timeline.layout import TimelineLayout


def _group(layers: int) -> Clip:
    return Clip(
        timeline_start=20,
        duration=40,
        source=GeneratedSource(kind=GROUP_KIND, params={GROUP_LAYERS: layers}),
    )


def _text() -> Clip:
    return Clip(timeline_start=0, duration=120, source=TEXT.create())


@pytest.fixture
def views(qt_application: QApplication) -> Iterator[list[TimelineView]]:
    del qt_application
    made: list[TimelineView] = []
    yield made
    for view in made:
        view.deleteLater()


def _view(views: list[TimelineView], *tracks: Track, mode: str = LayerMode.MIXED) -> TimelineView:
    base = Project.create(ProjectSettings(layer_mode=mode))
    project = base.with_timeline(replace(base.timeline, tracks=tracks))
    view = TimelineView(project, MediaAnalyzer(sample_rate=48000, channels=2))
    view.resize(900, 400)
    view._layout = TimelineLayout(pixels_per_frame=4.0)
    views.append(view)
    return view


def _band(view: TimelineView, name: str) -> tuple[int, int]:
    band = next(b for b in view.view_layout.bands(view.project.timeline) if b.track.name == name)
    return band.top, band.bottom


def _reach(view: TimelineView) -> list[GroupReach]:
    return group_reach(view.view_layout, view.project.timeline, view.width(), view.height())


def test_one_layer_in_front_is_marked(views: list[TimelineView]) -> None:
    # 何を受け持つのかが見えないと、対象レイヤー数を変えても何が動くのか分からない
    view = _view(
        views,
        Track(TrackKind.MIXED, "レイヤー 1", (_group(1),)),
        Track(TrackKind.MIXED, "レイヤー 2", (_text(),)),
        Track(TrackKind.MIXED, "レイヤー 3", (_text(),)),
    )
    (reach,) = _reach(view)
    (area,) = reach.areas
    assert (area.top(), area.bottom() + 1) == _band(view, "レイヤー 2")
    # 横はグループ制御の時間の幅だけ
    assert area.left() == int(view.view_layout.frame_to_x(20))
    assert area.right() + 1 == int(view.view_layout.frame_to_x(60))
    assert reach.bracket_top == _band(view, "レイヤー 1")[0]
    assert reach.bracket_bottom == _band(view, "レイヤー 2")[1]


def test_zero_reaches_to_the_end(views: list[TimelineView]) -> None:
    # 0（手前のすべて）のときも、どこまで受け持つかが見えるように並びの端まで
    view = _view(
        views,
        Track(TrackKind.MIXED, "レイヤー 1", ()),
        Track(TrackKind.MIXED, "レイヤー 2", (_group(0),)),
        Track(TrackKind.MIXED, "レイヤー 3", ()),
        Track(TrackKind.MIXED, "レイヤー 4", ()),
    )
    (reach,) = _reach(view)
    assert [area.top() for area in reach.areas] == [
        _band(view, "レイヤー 3")[0],
        _band(view, "レイヤー 4")[0],
    ]
    assert reach.bracket_bottom == _band(view, "レイヤー 4")[1]


def test_separated_video_tracks_mark_the_track_drawn_in_front(views: list[TimelineView]) -> None:
    # 分ける方式では手前の V は画面で上にある 括弧も上へ伸びる
    view = _view(
        views,
        Track(TrackKind.VIDEO, "V1", (_group(1),)),
        Track(TrackKind.VIDEO, "V2", (_text(),)),
        mode=LayerMode.SEPARATED,
    )
    (reach,) = _reach(view)
    (area,) = reach.areas
    assert area.top() == _band(view, "V2")[0]
    assert reach.bracket_top == _band(view, "V2")[0]
    assert reach.bracket_bottom == _band(view, "V1")[1]


def test_the_tint_is_painted(views: list[TimelineView]) -> None:
    # 計算だけあって描かれていないと、見ても分からない
    view = _view(
        views,
        Track(TrackKind.MIXED, "レイヤー 1", (_group(1),)),
        Track(TrackKind.MIXED, "レイヤー 2", ()),
    )
    image = view.grab().toImage()
    top, bottom = _band(view, "レイヤー 2")
    y = (top + bottom) // 2
    inside = image.pixelColor(int(view.view_layout.frame_to_x(40)), y)
    outside = image.pixelColor(int(view.view_layout.frame_to_x(100)), y)
    assert inside != outside


def test_a_disabled_group_has_no_reach(views: list[TimelineView]) -> None:
    # 描画へ効かないグループの範囲を残すと、有効な制御に見える
    disabled = replace(_group(1), enabled=False)
    view = _view(
        views,
        Track(TrackKind.MIXED, "レイヤー 1", (disabled,)),
        Track(TrackKind.MIXED, "レイヤー 2", (_text(),)),
    )
    assert _reach(view) == []
