"""タイムラインの座標変換。

「フレーム番号 ↔ 横方向のピクセル」「トラック ↔ 縦方向のピクセル」を一手に引き受ける。
描画も当たり判定もドラッグもすべて同じ変換を通すので、ここが 1 箇所にまとまって
いれば、拡大率やスクロールを変えても全部が整合したまま動く。

GUI に依存しないので、純粋な計算としてテストできる。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from kumiki.core.model import Timeline, Track, TrackId, TrackKind
from kumiki.ui.theme import Metrics

__all__ = ["TimelineLayout", "TrackBand"]

#: 1 フレームあたりのピクセル数の下限と上限。
#: 下限はフレーム単位の編集ができる程度、上限は 1 時間が画面に収まる程度。
MIN_PIXELS_PER_FRAME = 0.002
MAX_PIXELS_PER_FRAME = 40.0

DEFAULT_PIXELS_PER_FRAME = 2.0


@dataclass(frozen=True, slots=True)
class TrackBand:
    """1 本のトラックが占める縦方向の範囲。"""

    track: Track
    top: int
    height: int

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, y: int) -> bool:
        return self.top <= y < self.bottom


@dataclass(frozen=True, slots=True)
class TimelineLayout:
    """表示状態。拡大率とスクロール位置を持つ。

    frozen なのは、描画中に状態が変わらないことを保証するため。変更は
    新しいインスタンスを返す形にしてある。
    """

    #: 1 フレームあたりのピクセル数。
    pixels_per_frame: float = DEFAULT_PIXELS_PER_FRAME
    #: 画面左端に来るフレーム番号。
    scroll_frame: float = 0.0
    #: 縦スクロール量（ピクセル）。
    scroll_y: int = 0

    def __post_init__(self) -> None:
        clamped = min(max(self.pixels_per_frame, MIN_PIXELS_PER_FRAME), MAX_PIXELS_PER_FRAME)
        object.__setattr__(self, "pixels_per_frame", clamped)
        object.__setattr__(self, "scroll_frame", max(0.0, self.scroll_frame))
        object.__setattr__(self, "scroll_y", max(0, self.scroll_y))

    # --- 横方向 ---

    def frame_to_x(self, frame: float) -> float:
        """フレーム番号をウィジェット内の x 座標へ。トラックヘッダ分を含む。"""
        return Metrics.TRACK_HEADER_WIDTH + (frame - self.scroll_frame) * self.pixels_per_frame

    def x_to_frame(self, x: float) -> float:
        """x 座標をフレーム番号へ。ヘッダより左は負のフレームになる。"""
        return (x - Metrics.TRACK_HEADER_WIDTH) / self.pixels_per_frame + self.scroll_frame

    def frame_at(self, x: float) -> int:
        """x 座標にあるフレーム番号。負にはならない。"""
        return max(0, int(self.x_to_frame(x)))

    def frames_in(self, width: int) -> float:
        """幅 ``width`` のウィジェットに収まるフレーム数。"""
        return max(0.0, (width - Metrics.TRACK_HEADER_WIDTH) / self.pixels_per_frame)

    def visible_range(self, width: int) -> tuple[int, int]:
        """画面に見えているフレーム範囲。描画するクリップを絞るために使う。"""
        start = int(self.scroll_frame)
        end = int(self.scroll_frame + self.frames_in(width)) + 1
        return max(0, start), end

    def zoomed(self, factor: float, *, anchor_x: float) -> TimelineLayout:
        """``anchor_x`` の位置にあるフレームを動かさずに拡大・縮小する。

        マウス位置を基準にしないと、拡大するたびに見ていた場所が画面外へ逃げる。
        """
        anchor_frame = self.x_to_frame(anchor_x)
        zoomed = replace(self, pixels_per_frame=self.pixels_per_frame * factor)
        # 丸め後の実際の倍率で計算し直す。上下限に当たったときにずれないように。
        offset = (anchor_x - Metrics.TRACK_HEADER_WIDTH) / zoomed.pixels_per_frame
        return replace(zoomed, scroll_frame=max(0.0, anchor_frame - offset))

    def scrolled_to(self, frame: float) -> TimelineLayout:
        return replace(self, scroll_frame=max(0.0, frame))

    def scrolled_vertically(self, offset: int) -> TimelineLayout:
        return replace(self, scroll_y=max(0, offset))

    def ensure_visible(self, frame: int, width: int, *, margin: float = 0.15) -> TimelineLayout:
        """``frame`` が画面に入るようスクロールする。

        再生ヘッドの追従に使う。端ぴったりで折り返すと、再生中に細かく
        スクロールが走って見づらいので、少し余裕を持たせる。
        """
        span = self.frames_in(width)
        if span <= 0:
            return self
        left = self.scroll_frame + span * margin
        right = self.scroll_frame + span * (1.0 - margin)
        if left <= frame <= right:
            return self
        return self.scrolled_to(frame - span * margin)

    # --- 縦方向 ---

    def bands(self, timeline: Timeline) -> tuple[TrackBand, ...]:
        """各トラックの縦位置。

        上から V2, V1, A1, A2 の順に並べる。Premiere / AviUtl と同じで、
        映像は番号が大きいほど手前（上）、音声は番号が小さいほど上に来る。

        全トラックを一律に逆順にすると音声が映像より上へ来てしまう。
        種類で分けてから並べる必要がある。
        """
        video = [t for t in timeline.tracks if t.kind is TrackKind.VIDEO]
        audio = [t for t in timeline.tracks if t.kind is TrackKind.AUDIO]
        ordered = [*reversed(video), *audio]
        bands: list[TrackBand] = []
        top = Metrics.RULER_HEIGHT - self.scroll_y
        for track in ordered:
            height = min(max(track.height, Metrics.MIN_TRACK_HEIGHT), Metrics.MAX_TRACK_HEIGHT)
            bands.append(TrackBand(track=track, top=top, height=height))
            top += height
        return tuple(bands)

    def content_height(self, timeline: Timeline) -> int:
        """全トラックを並べたときの高さ。縦スクロールの範囲を決めるのに使う。"""
        total = sum(
            min(max(track.height, Metrics.MIN_TRACK_HEIGHT), Metrics.MAX_TRACK_HEIGHT)
            for track in timeline.tracks
        )
        return total + Metrics.RULER_HEIGHT

    def band_at(self, timeline: Timeline, y: int) -> TrackBand | None:
        for band in self.bands(timeline):
            if band.contains(y):
                return band
        return None

    def track_at(self, timeline: Timeline, y: int) -> TrackId | None:
        band = self.band_at(timeline, y)
        return band.track.id if band is not None else None
