"""グループ制御が受け持つ範囲を、タイムラインの上で見せる（利用者の要望）

グループ制御は自分では描かず、手前の対象レイヤー数ぶんのトラックの物を動かす どこまでが
受け持ちなのかが見えないと、対象レイヤー数を変えても何が動くようになったのか分からない
受け持つトラックの、グループ制御の時間の幅へ薄い色を敷き、グループ制御の頭から受け持つ
トラックの端まで括弧を引く 対象レイヤー数が 0（手前のすべて）なら並びの端まで伸びる

色はグループ制御のクリップと同じ紫（フィルタと同じ 自分の絵を持たない物の色）
選んでいるグループ制御の物は濃くする 常に出すのは、選んでいないグループ制御の受け持ちが
見えないと、ほかのクリップを動かしたときに急に動きが変わる理由が分からないため
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QPainter, QPen

from sashimono.core.model import ClipId, Timeline, group_reaches
from sashimono.ui.theme import Colors, Metrics
from sashimono.ui.timeline.layout import TimelineLayout
from sashimono.ui.timeline.painter import clips_in_range

__all__ = ["GroupReach", "draw_group_reach", "group_reach"]


@dataclass(frozen=True, slots=True)
class GroupReach:
    """1 本のグループ制御の受け持ち（画面の座標）"""

    group: ClipId
    #: 受け持つトラックごとの、グループ制御の時間の幅の矩形
    areas: tuple[QRect, ...]
    #: 括弧の横の位置（グループ制御の頭）と、縦の範囲（グループ制御のトラックから受け持ちの端まで）
    bracket_x: int
    bracket_top: int
    bracket_bottom: int


def group_reach(
    layout: TimelineLayout, timeline: Timeline, width: int, height: int
) -> list[GroupReach]:
    """見えている所にあるグループ制御の受け持ち"""
    bands = layout.bands(timeline)
    by_track = {band.track.id: band for band in bands}
    order = timeline.picture_tracks()
    start, end = layout.visible_range(width)
    found: list[GroupReach] = []
    for band in bands:
        for clip in clips_in_range(band.track, start, end):
            if not clip.enabled or not clip.is_group:
                continue
            left = int(max(layout.frame_to_x(clip.timeline_start), Metrics.TRACK_HEADER_WIDTH))
            right = int(min(layout.frame_to_x(clip.timeline_end), width))
            if right <= left:
                continue
            targets = [
                by_track[track.id]
                for track in order
                if track.id in by_track and group_reaches(order, band.track.id, clip, track.id)
            ]
            if not targets:
                continue
            areas = tuple(
                QRect(left, target.top, right - left, target.height) for target in targets
            )
            top = min(band.top, *(target.top for target in targets))
            bottom = max(band.bottom, *(target.bottom for target in targets))
            found.append(
                GroupReach(
                    group=clip.id,
                    areas=areas,
                    bracket_x=left,
                    bracket_top=max(top, Metrics.RULER_HEIGHT),
                    bracket_bottom=min(bottom, height),
                )
            )
    return found


def draw_group_reach(
    painter: QPainter,
    layout: TimelineLayout,
    timeline: Timeline,
    size: tuple[int, int],
    selected: frozenset[ClipId] | set[ClipId],
) -> None:
    """受け持ちの薄い色と括弧を描く 選んでいるグループ制御の物は濃く"""
    width, height = size
    for reach in group_reach(layout, timeline, width, height):
        chosen = reach.group in selected
        tint = QColor(Colors.FILTER_CLIP_BORDER)
        tint.setAlpha(60 if chosen else 26)
        for area in reach.areas:
            painter.fillRect(area, tint)
        line = QColor(Colors.FILTER_CLIP_BORDER)
        line.setAlpha(255 if chosen else 150)
        painter.save()
        painter.setPen(QPen(line, 3 if chosen else 2))
        x = reach.bracket_x + 1
        painter.drawLine(x, reach.bracket_top + 2, x, reach.bracket_bottom - 2)
        painter.drawLine(x, reach.bracket_top + 2, x + 6, reach.bracket_top + 2)
        painter.drawLine(x, reach.bracket_bottom - 2, x + 6, reach.bracket_bottom - 2)
        painter.restore()
