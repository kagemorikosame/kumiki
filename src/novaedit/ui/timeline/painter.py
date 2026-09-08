"""タイムラインの描画。

``QGraphicsView`` を使わず自前で描く。クリップが数千個になっても、見えている範囲
だけを描けば済むからで、シーングラフに全部を載せると生成だけで時間を食う。

描画関数はウィジェットの状態を持たない。引数で受け取ったものだけを描くので、
書き出しプレビューや単体テストからも同じ関数を呼べる。
"""

from __future__ import annotations

from fractions import Fraction

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen

from novaedit.core.model import Clip, MediaItem, Timeline, TrackKind
from novaedit.core.timebase import FrameRate, format_timecode
from novaedit.effects.sources import source_registry
from novaedit.engine.audio import Waveform
from novaedit.engine.cache import Filmstrip
from novaedit.ui.theme import Colors, Metrics
from novaedit.ui.timeline.layout import TimelineLayout, TrackBand

__all__ = [
    "draw_clip",
    "draw_playhead",
    "draw_ruler",
    "draw_track_background",
    "draw_track_header",
    "to_qimage",
]

#: 目盛りの間隔として使える値（フレーム数の基準となる秒数）。
#: 1 目盛りが最低でもこのピクセル数を超えるものを選ぶ。
_MIN_TICK_SPACING = 70
_TICK_SECONDS = (
    Fraction(1, 30),
    Fraction(1, 10),
    Fraction(1, 4),
    Fraction(1, 2),
    Fraction(1),
    Fraction(2),
    Fraction(5),
    Fraction(10),
    Fraction(15),
    Fraction(30),
    Fraction(60),
    Fraction(120),
    Fraction(300),
    Fraction(600),
    Fraction(1800),
    Fraction(3600),
)


def to_qimage(array: np.ndarray) -> QImage:
    """``(高さ, 幅, 4)`` の uint8 配列を :class:`QImage` にする。

    ``QImage`` は渡したバッファを参照するだけでコピーしない。元の配列が
    先に解放されると描画時に落ちるので、必ずコピーを作って渡す。
    """
    data = np.ascontiguousarray(array, dtype=np.uint8)
    height, width = data.shape[:2]
    image = QImage(data.tobytes(), width, height, width * 4, QImage.Format.Format_RGBA8888)
    return image.copy()


def draw_ruler(painter: QPainter, layout: TimelineLayout, width: int, rate: FrameRate) -> None:
    """時間目盛りを描く。"""
    rect = QRect(0, 0, width, Metrics.RULER_HEIGHT)
    painter.fillRect(rect, Colors.TIMELINE_RULER)
    painter.setPen(QPen(Colors.BORDER, 1))
    painter.drawLine(0, Metrics.RULER_HEIGHT - 1, width, Metrics.RULER_HEIGHT - 1)

    step = _tick_step(layout, rate)
    if step <= 0:
        return

    font = QFont(painter.font())
    font.setPointSizeF(8.5)
    painter.setFont(font)
    metrics = QFontMetrics(font)

    start_frame, end_frame = layout.visible_range(width)
    first_tick = (start_frame // step) * step
    for frame in range(first_tick, end_frame + step, step):
        x = layout.frame_to_x(frame)
        if x < Metrics.TRACK_HEADER_WIDTH - 1:
            continue
        painter.setPen(QPen(Colors.BORDER, 1))
        painter.drawLine(int(x), 4, int(x), Metrics.RULER_HEIGHT - 1)
        painter.setPen(QPen(Colors.TEXT_MUTED, 1))
        label = format_timecode(frame, rate)
        painter.drawText(QPointF(x + 3, Metrics.RULER_HEIGHT - 6 + metrics.descent() - 2), label)


def _tick_step(layout: TimelineLayout, rate: FrameRate) -> int:
    """目盛りの間隔（フレーム数）。

    表示倍率に応じて、ラベルが重ならない中で最も細かい間隔を選ぶ。
    """
    for seconds in _TICK_SECONDS:
        frames = max(1, int(seconds * rate.fps))
        if frames * layout.pixels_per_frame >= _MIN_TICK_SPACING:
            return frames
    return max(1, int(_TICK_SECONDS[-1] * rate.fps))


def draw_track_background(painter: QPainter, band: TrackBand, width: int) -> None:
    """トラック 1 本分の下地と区切り線。"""
    painter.fillRect(
        QRect(Metrics.TRACK_HEADER_WIDTH, band.top, width, band.height),
        Colors.TIMELINE_BACKGROUND,
    )
    painter.setPen(QPen(Colors.TRACK_SEPARATOR, 1))
    painter.drawLine(0, band.bottom - 1, width, band.bottom - 1)


def draw_track_header(painter: QPainter, band: TrackBand) -> None:
    """トラック名とミュート・ソロ・ロックの状態。"""
    rect = QRect(0, band.top, Metrics.TRACK_HEADER_WIDTH, band.height)
    painter.fillRect(rect, Colors.TRACK_HEADER)
    painter.setPen(QPen(Colors.BORDER, 1))
    painter.drawLine(
        Metrics.TRACK_HEADER_WIDTH - 1, band.top, Metrics.TRACK_HEADER_WIDTH - 1, band.bottom
    )

    track = band.track
    painter.setPen(QPen(Colors.TEXT if not track.muted else Colors.TEXT_MUTED, 1))
    name = track.name or ("映像" if track.kind is TrackKind.VIDEO else "音声")
    painter.drawText(QRect(8, band.top + 4, 90, 16), Qt.AlignmentFlag.AlignVCenter, name)

    flags = []
    if track.muted:
        flags.append("M")
    if track.solo:
        flags.append("S")
    if track.locked:
        flags.append("L")
    if flags:
        painter.setPen(QPen(Colors.ACCENT, 1))
        painter.drawText(
            QRect(8, band.top + 20, 90, 14), Qt.AlignmentFlag.AlignVCenter, " ".join(flags)
        )


def draw_clip(
    painter: QPainter,
    clip: Clip,
    band: TrackBand,
    layout: TimelineLayout,
    rate: FrameRate,
    *,
    media: MediaItem | None,
    filmstrip: Filmstrip | None,
    waveform: Waveform | None,
    selected: bool,
    clip_rect: QRect,
) -> None:
    """クリップ 1 個を描く。

    ``clip_rect`` は画面に見えている部分に切り詰めた矩形。クリップ全体の矩形を
    渡すと、長いクリップで画面外まで描こうとして無駄が出る。
    """
    is_video = band.track.kind is TrackKind.VIDEO
    body = Colors.VIDEO_CLIP if is_video else Colors.AUDIO_CLIP
    border = Colors.VIDEO_CLIP_BORDER if is_video else Colors.AUDIO_CLIP_BORDER

    painter.save()
    painter.setClipRect(clip_rect)
    painter.fillRect(clip_rect, body if clip.enabled else _dimmed(body))

    content = QRect(
        clip_rect.left(),
        clip_rect.top() + Metrics.CLIP_LABEL_HEIGHT,
        clip_rect.width(),
        max(0, clip_rect.height() - Metrics.CLIP_LABEL_HEIGHT),
    )
    if content.height() > 4:
        if is_video and filmstrip is not None:
            _draw_filmstrip(painter, content, clip, layout, rate, filmstrip)
        elif not is_video and waveform is not None:
            _draw_waveform(painter, content, clip, layout, rate, waveform)

    _draw_clip_label(painter, clip_rect, clip, media)

    painter.setPen(QPen(Colors.SELECTION if selected else border, 2 if selected else 1))
    painter.drawRect(clip_rect.adjusted(0, 0, -1, -1))
    painter.restore()


def _draw_clip_label(painter: QPainter, rect: QRect, clip: Clip, media: MediaItem | None) -> None:
    label_rect = QRect(rect.left(), rect.top(), rect.width(), Metrics.CLIP_LABEL_HEIGHT)
    painter.fillRect(label_rect, QColor(0, 0, 0, 90))

    name = _clip_name(clip, media)
    if clip.speed != 1:
        name = f"{name}  ×{float(clip.speed):g}"
    painter.setPen(QPen(Colors.CLIP_LABEL, 1))
    font = QFont(painter.font())
    font.setPointSizeF(8.5)
    painter.setFont(font)
    painter.drawText(
        label_rect.adjusted(4, 0, -4, 0),
        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
        name,
    )


def _clip_name(clip: Clip, media: MediaItem | None) -> str:
    """クリップに出す名前。

    生成オブジェクトは素材を持たないので、素材名だけを見ると全部「素材なし」に
    なってしまう。テキストは中身の先頭を添えると、並んだときに見分けが付く。
    """
    if media is not None:
        return media.name
    if clip.source is None:
        return "（空）"

    definition = source_registry.get(clip.source.kind)
    label = definition.label if definition is not None else clip.source.kind
    text = clip.source.params.get("text")
    if isinstance(text, str) and text.strip():
        return f"{label}: {text.splitlines()[0][:16]}"
    return label


def _draw_filmstrip(
    painter: QPainter,
    rect: QRect,
    clip: Clip,
    layout: TimelineLayout,
    rate: FrameRate,
    filmstrip: Filmstrip,
) -> None:
    """クリップの上にサムネイルを敷き詰める。

    サムネイルは元の縦横比のまま並べる。引き伸ばすと、何が映っているのか
    判断できなくなって用を成さない。
    """
    if filmstrip.count == 0 or rect.height() <= 0:
        return

    scale = rect.height() / filmstrip.height
    tile_width = max(1, int(filmstrip.tile_width * scale))

    x = rect.left()
    while x < rect.right():
        # 秒の計算に float を混ぜないよう、まずフレーム番号（整数）へ落とす。
        frame = layout.frame_at(x)
        local_frame = frame - clip.timeline_start
        source_time = clip.source_in + local_frame * rate.frame_duration * clip.speed
        tile = filmstrip.at(source_time)
        if tile is None:
            break
        painter.drawImage(QRectF(x, rect.top(), tile_width, rect.height()), to_qimage(tile))
        x += tile_width


def _draw_waveform(
    painter: QPainter,
    rect: QRect,
    clip: Clip,
    layout: TimelineLayout,
    rate: FrameRate,
    waveform: Waveform,
) -> None:
    """クリップの上に波形を描く。

    見えている範囲だけを、1 ピクセル 1 本の縦線として描く。素材全体の波形を
    毎回描こうとすると、長尺素材で描画が止まる。
    """
    columns = rect.width()
    if columns <= 0 or rect.height() <= 2:
        return

    # 見えている左端・右端が、素材のどのサンプルにあたるかを求める。
    start_frame = layout.frame_at(rect.left()) - clip.timeline_start
    end_frame = layout.frame_at(rect.right()) - clip.timeline_start
    start_seconds = clip.source_in + start_frame * rate.frame_duration * clip.speed
    end_seconds = clip.source_in + end_frame * rate.frame_duration * clip.speed

    start_sample = int(start_seconds * waveform.sample_rate)
    end_sample = int(end_seconds * waveform.sample_rate)
    if end_sample <= start_sample:
        return

    envelope = waveform.envelope(start_sample, end_sample, columns)
    # チャンネルをまとめて 1 本の波形にする。ステレオを上下に分けるのは
    # トラックを高くしたときの表示として P2 で入れる。
    minimum = envelope[:, :, 0].min(axis=1)
    maximum = envelope[:, :, 1].max(axis=1)

    centre = rect.top() + rect.height() / 2.0
    half = rect.height() / 2.0 - 1.0
    painter.setPen(QPen(Colors.WAVEFORM, 1))
    for column in range(columns):
        top = centre - float(np.clip(maximum[column], -1.0, 1.0)) * half
        bottom = centre - float(np.clip(minimum[column], -1.0, 1.0)) * half
        x = rect.left() + column
        painter.drawLine(QPointF(x, top), QPointF(x, max(bottom, top + 1.0)))


def draw_playhead(painter: QPainter, layout: TimelineLayout, frame: int, height: int) -> None:
    """再生ヘッド。上の三角と縦線。"""
    x = layout.frame_to_x(frame)
    if x < Metrics.TRACK_HEADER_WIDTH:
        return

    painter.setPen(QPen(Colors.PLAYHEAD, 1))
    painter.drawLine(QPointF(x, 0), QPointF(x, height))

    painter.setBrush(Colors.PLAYHEAD)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRect(QRectF(x - 4.5, 0, 9, 9))


def clip_rect_for(clip: Clip, band: TrackBand, layout: TimelineLayout, width: int) -> QRect | None:
    """クリップの矩形を、画面に見えている範囲へ切り詰めて返す。

    見えていなければ ``None``。描画対象を絞るのに使う。
    """
    left = layout.frame_to_x(clip.timeline_start)
    right = layout.frame_to_x(clip.timeline_end)
    visible_left = max(left, Metrics.TRACK_HEADER_WIDTH)
    visible_right = min(right, width)
    if visible_right <= visible_left or band.height <= 2:
        return None
    return QRect(
        int(visible_left),
        band.top + 1,
        max(1, int(visible_right) - int(visible_left)),
        band.height - 3,
    )


def _dimmed(color: QColor) -> QColor:
    """無効なクリップ用に彩度と明度を落とす。"""
    dimmed = QColor(color)
    dimmed.setAlpha(110)
    return dimmed


def visible_clips(
    timeline: Timeline, layout: TimelineLayout, width: int
) -> list[tuple[TrackBand, Clip, QRect]]:
    """見えているクリップと、その矩形の一覧。

    描画と当たり判定の両方がこれを使う。別々に計算すると、見えているのに
    掴めないクリップのようなずれが生まれる。
    """
    start_frame, end_frame = layout.visible_range(width)
    found: list[tuple[TrackBand, Clip, QRect]] = []
    for band in layout.bands(timeline):
        if band.bottom <= Metrics.RULER_HEIGHT:
            continue
        for clip in band.track.clips:
            if clip.timeline_end < start_frame or clip.timeline_start > end_frame:
                continue
            rect = clip_rect_for(clip, band, layout, width)
            if rect is not None:
                found.append((band, clip, rect))
    return found
