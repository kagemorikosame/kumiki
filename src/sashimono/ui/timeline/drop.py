"""タイムラインへの落とし込み（エクスプローラーのファイルと、素材一覧の素材）

ビュー（:class:`~sashimono.ui.timeline.view.TimelineView`）は落とされた物と位置を
信号で外へ出すだけで、置くのは窓の仕事 ファイルは調べてからでないと長さも種類も
分からず、調べるのは窓の読み込みの流れ（裏のスレッド）が持っている
ここには、落とす位置の求め方と、ドラッグ中に落ちる所の目安を描く所をまとめる
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QMimeData, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen

from sashimono.core.commands import AddClip, AddTrack, Command, place_media
from sashimono.core.model import MediaId, MediaItem, Project, Timeline, TrackId
from sashimono.core.timebase import format_timecode
from sashimono.ui.media_pool import media_ids_in
from sashimono.ui.theme import Colors, Metrics
from sashimono.ui.timeline.layout import TimelineLayout, TrackBand

__all__ = ["DropGuide", "DropSpot", "accepts", "local_paths", "paint_drop_guide", "spot_at"]

#: 新しく作るトラックの仮の行 本物のトラックと見分けが付くよう、地より少しだけ明るくする
_GHOST_ROW = QColor(255, 255, 255, 14)


@dataclass(frozen=True, slots=True)
class DropSpot:
    """落とした位置 ``track_id`` はトラックの無い所（下の空き）へ落としたら ``None``"""

    frame: int
    track_id: TrackId | None = None

    def after(self, commands: Sequence[Command]) -> DropSpot:
        """置いたクリップの後ろ 何本かを落としたとき、次の素材はここから並べる

        置けなかった（長さの無い素材だった）ときは動かさない
        """
        ends = [c.clip.timeline_end for c in commands if isinstance(c, AddClip)]
        return replace(self, frame=max([self.frame, *ends]))


@dataclass(frozen=True, slots=True)
class DropGuide:
    """ドラッグ中の目安 描くためだけに持つ"""

    spot: DropSpot
    #: 素材一覧から引いてきた素材 ファイルを引いてきたときは空
    #: ファイルは調べるまで長さが分からないので、落ちる位置の線だけを出す
    media_ids: tuple[MediaId, ...] = ()


def local_paths(mime: QMimeData) -> list[Path]:
    """落とされたファイル ネット上の URL は読み込めないので外す"""
    if not mime.hasUrls():
        return []
    return [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]


def accepts(mime: QMimeData) -> bool:
    """タイムラインが受け取れる物か 受け取れない物は、落とせない印（禁止の指）にする"""
    return bool(media_ids_in(mime)) or bool(local_paths(mime))


def spot_at(layout: TimelineLayout, timeline: Timeline, position: QPointF) -> DropSpot:
    """画面の位置を、落とす先のフレームとトラックへ直す

    トラックの名前の欄（左端）へ落としたら、その時点で見えている左端のフレームに置く
    名前の欄はフレームを持たないが、そこへ落とした人は「このトラックへ」と思っている
    """
    x = max(float(Metrics.TRACK_HEADER_WIDTH), position.x())
    return DropSpot(
        frame=layout.frame_at(x),
        track_id=layout.track_at(timeline, int(position.y())),
    )


def paint_drop_guide(
    painter: QPainter,
    layout: TimelineLayout,
    project: Project,
    guide: DropGuide,
    size: tuple[int, int],
) -> None:
    """落ちる所の目安を描く 落とすトラックを薄く塗り、落ちるフレームに縦線を引く

    素材一覧から引いてきた素材は、窓が置くのと同じ決め方（:func:`place_media`）で
    置く先を求め、クリップの枠を点線で出す 落としたトラックが埋まっていて別の
    トラックへ回る、を離す前に分かるようにする
    """
    width, height = size
    bands = {band.track.id: band for band in layout.bands(project.timeline)}
    painter.save()
    target = bands.get(guide.spot.track_id) if guide.spot.track_id is not None else None
    if target is not None:
        fill = QColor(Colors.ACCENT)
        fill.setAlpha(36)
        painter.fillRect(
            QRectF(
                Metrics.TRACK_HEADER_WIDTH,
                target.top,
                width - Metrics.TRACK_HEADER_WIDTH,
                target.height,
            ),
            fill,
        )

    media = [
        item for media_id in guide.media_ids if (item := project.find_media(media_id)) is not None
    ]
    if media:
        _paint_clip_outlines(painter, layout, project, media, guide.spot, bands, width)

    x = layout.frame_to_x(guide.spot.frame)
    painter.setPen(QPen(Colors.ACCENT, 2))
    painter.drawLine(QPointF(x, Metrics.RULER_HEIGHT), QPointF(x, height))
    # 目盛りの上に落ちる時刻を添える 線だけだと、何秒の所へ落ちるのかを目盛りから読むことになる
    label = format_timecode(guide.spot.frame, project.rate)
    metrics = painter.fontMetrics()
    box = QRectF(x + 4, 2, metrics.horizontalAdvance(label) + 8, Metrics.RULER_HEIGHT - 4)
    if box.right() > width:
        # 右端では線の左へ回す はみ出すと、右端へ落とすときほど時刻が読めない
        box.moveRight(x - 4)
    painter.fillRect(box, Colors.ACCENT)
    painter.setPen(Colors.WINDOW)
    painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
    painter.restore()


def _paint_clip_outlines(
    painter: QPainter,
    layout: TimelineLayout,
    project: Project,
    media: Sequence[MediaItem],
    spot: DropSpot,
    bands: dict[TrackId, TrackBand],
    width: int,
) -> None:
    commands = place_media(project, media, at_frame=spot.frame, track_id=spot.track_id)
    # 新しく作るトラックはまだ画面に無い 並べた最後のトラックの下に仮の行を出して、
    # そこへ枠を描く 出さないと、埋まっていて新しいトラックへ回るときに何も描かれず、
    # 落としても置かれないように見える
    ghosts: dict[TrackId, QRectF] = {}
    bottom = max((band.bottom for band in bands.values()), default=Metrics.RULER_HEIGHT)
    for command in commands:
        if isinstance(command, AddTrack):
            top = bottom + len(ghosts) * Metrics.DEFAULT_TRACK_HEIGHT
            row = QRectF(0, top, width, Metrics.DEFAULT_TRACK_HEIGHT)
            ghosts[command.track.id] = row
            painter.fillRect(row.adjusted(0, 1, 0, -1), _GHOST_ROW)
            painter.setPen(Colors.TEXT_MUTED)
            painter.drawText(
                QRectF(8, top, Metrics.TRACK_HEADER_WIDTH - 8, row.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                f"{command.track.name}（新しく作る）",
            )

    painter.setPen(QPen(Colors.ACCENT, 2, Qt.PenStyle.DashLine))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for command in commands:
        if not isinstance(command, AddClip):
            continue
        band = bands.get(command.track_id)
        if band is not None:
            row = QRectF(0, band.top, width, band.height)
        elif command.track_id in ghosts:
            row = ghosts[command.track_id]
        else:
            continue
        left = layout.frame_to_x(command.clip.timeline_start)
        right = layout.frame_to_x(command.clip.timeline_end)
        painter.drawRect(QRectF(left, row.top() + 1, max(2.0, right - left), row.height() - 3))
