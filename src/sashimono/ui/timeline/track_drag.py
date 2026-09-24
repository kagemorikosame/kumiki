"""ヘッダ（トラック名の所）を掴んで、トラックの順序を上下に入れ替える

どこへ落とせるかは :func:`~sashimono.core.commands.reorder_group` の仲間の中だけ
（いまは同じ種類どうし） 画面の並び（映像は上ほど手前、音声は下へ）とモデルの並びの
向きは、仲間の並びを見比べて決める 向きを決め打ちすると、1 本のレイヤーに映像も音声も
置ける形へ広げたときに、ここも書き直すことになる

ドラッグの間はプロジェクトを変えず、入る位置の線だけを描く 離したときに
:class:`~sashimono.core.commands.MoveTrack` を 1 つ出す（取り消しの 1 段）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QColor, QPainter

from sashimono.core.commands import Command, MoveTrack, reorder_group
from sashimono.core.model import Timeline, TrackId
from sashimono.ui.theme import Colors, Metrics
from sashimono.ui.timeline.layout import TimelineLayout, TrackBand

__all__ = ["TrackDragger", "drop_index"]

#: 掴んでからこれだけ動かしたら、並べ替えのドラッグとみなす（画素） 名前を押しただけの
#: 手ぶれで並べ替えが始まらないように
START_DISTANCE = 4


@dataclass(frozen=True, slots=True)
class _Gap:
    """落とせる隙間 ``y`` は線を引く高さ ``index`` はそこへ落としたときの仲間の中の番号"""

    y: int
    index: int


def _group_bands(layout: TimelineLayout, timeline: Timeline, track_id: TrackId) -> list[TrackBand]:
    """掴んだトラックと入れ替えられる仲間の帯 画面の上から"""
    track = timeline.find_track(track_id)
    if track is None:
        return []
    group = {t.id for t in reorder_group(timeline, track)}
    return [band for band in layout.bands(timeline) if band.track.id in group]


def _gaps(layout: TimelineLayout, timeline: Timeline, track_id: TrackId) -> list[_Gap]:
    """落とせる隙間すべて 画面の上から

    ``index`` はモデルの並び（:attr:`Timeline.tracks`）での仲間の中の番号 画面の上から
    数えた並びとモデルの並びが逆向き（映像）なら、数え方も逆にする
    """
    bands = _group_bands(layout, timeline, track_id)
    if not bands:
        return []
    order = [t.id for t in timeline.tracks]
    shown = [band.track.id for band in bands]
    reversed_ = len(shown) > 1 and order.index(shown[0]) > order.index(shown[1])
    moving = shown.index(track_id)
    gaps: list[_Gap] = []
    for slot in range(len(bands) + 1):
        y = bands[slot].top if slot < len(bands) else bands[-1].bottom
        # 自分を抜いた並びへ入れる番号 自分より下の隙間は、自分が抜けた分だけ上へ詰まる
        placed = slot - 1 if slot > moving else slot
        index = len(bands) - 1 - placed if reversed_ else placed
        gaps.append(_Gap(y, index))
    return gaps


def drop_index(
    layout: TimelineLayout, timeline: Timeline, track_id: TrackId, y: int
) -> tuple[int, int] | None:
    """``y`` で離したときに入る ``(仲間の中の番号, 線の高さ)`` 動かせなければ ``None``

    仲間の外（映像を音声の欄の上）で離したときは、仲間のうち近い方の端に入る
    """
    gaps = _gaps(layout, timeline, track_id)
    if not gaps:
        return None
    nearest = min(gaps, key=lambda gap: abs(gap.y - y))
    return nearest.index, nearest.y


class TrackDragger:
    """ヘッダの並べ替えのドラッグ 押す・動かす・離す・描くを受け持つ"""

    def __init__(self, request: Callable[[list[Command], str], None]) -> None:
        self._request = request
        self._track: TrackId | None = None
        self._origin = QPoint()
        self._moved = False
        self._target: tuple[int, int] | None = None

    @property
    def pressed(self) -> bool:
        """ヘッダを掴んでいる（まだ動かしていなくても）"""
        return self._track is not None

    @property
    def dragging(self) -> bool:
        """並べ替えのドラッグ中（入る位置の線を出している）"""
        return self._track is not None and self._moved

    @property
    def track(self) -> TrackId | None:
        return self._track

    def press(self, layout: TimelineLayout, timeline: Timeline, position: QPoint) -> bool:
        """ヘッダの名前の所なら掴んで真を返す ロックしたトラックは掴まない

        ロックしたトラックの順を変えると重ね順が変わり、ロックで守っている絵が変わる
        ほかのトラックがロックしたトラックを越えていくのは構わない（そのトラックの
        中身も、ほかのトラックとの上下も変わらない）
        """
        if position.x() >= Metrics.TRACK_HEADER_WIDTH or position.y() < Metrics.RULER_HEIGHT:
            return False
        band = layout.band_at(timeline, position.y())
        if band is None or band.track.locked:
            return False
        self._track = band.track.id
        self._origin = QPoint(position)
        self._moved = False
        self._target = None
        return True

    def move(self, layout: TimelineLayout, timeline: Timeline, position: QPoint) -> None:
        if self._track is None:
            return
        if not self._moved:
            if (position - self._origin).manhattanLength() < START_DISTANCE:
                return
            self._moved = True
        self._target = drop_index(layout, timeline, self._track, position.y())

    def release(self, timeline: Timeline) -> None:
        """離した 入る位置が今と違えば :class:`MoveTrack` を出す"""
        track_id, target, moved = self._track, self._target, self._moved
        self.cancel()
        if track_id is None or target is None or not moved:
            return
        track = timeline.find_track(track_id)
        if track is None:
            return
        group = [t.id for t in reorder_group(timeline, track)]
        if group.index(track_id) == target[0]:
            return
        command = MoveTrack(track_id, target[0])
        self._request([command], command.label)

    def cancel(self) -> None:
        self._track = None
        self._moved = False
        self._target = None

    def paint(
        self, painter: QPainter, layout: TimelineLayout, timeline: Timeline, width: int
    ) -> None:
        """掴んだヘッダを薄く覆い、入る位置に線を引く"""
        if not self.dragging or self._track is None:
            return
        band = next((b for b in layout.bands(timeline) if b.track.id == self._track), None)
        painter.save()
        if band is not None:
            shade = QColor(Colors.SELECTION)
            shade.setAlpha(36)
            painter.fillRect(QRect(0, band.top, Metrics.TRACK_HEADER_WIDTH, band.height), shade)
        if self._target is not None:
            y = self._target[1]
            painter.fillRect(QRect(0, y - 1, width, 3), Colors.SELECTION)
        painter.restore()
