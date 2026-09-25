"""タイムラインのクリップに波形を描く所の速さ（#201）

実素材を 100 本並べて測ると、1 回の描画が 50ms 近くかかり、60fps の予算（16.7ms）を
大きく超えた 重いのは波形で、見えている列ごとに Python で値を切り詰めて drawLine を
呼んでいた（1920 幅に音のトラックが 4 本で 7000 回を超える） 描く線は今までと同じまま、
まとめて 1 度で渡すことを押さえる
"""

from __future__ import annotations

from typing import cast

import numpy as np
from PySide6.QtCore import QLineF, QRect
from PySide6.QtGui import QPainter, QPen

from sashimono.core.model import Clip
from sashimono.core.timebase import FrameRate
from sashimono.effects.sources import TEXT
from sashimono.engine.audio import PeakLevel, Waveform
from sashimono.ui.timeline.layout import TimelineLayout
from sashimono.ui.timeline.painter import _draw_waveform


class _Recorder:
    """描く命令だけを書き留める 線の数と形を数えるため"""

    def __init__(self) -> None:
        self.single = 0
        self.batches: list[list[QLineF]] = []

    def setPen(self, pen: QPen) -> None:  # noqa: N802 - QPainter に合わせる
        del pen

    def drawLine(self, *args: object) -> None:  # noqa: N802 - QPainter に合わせる
        del args
        self.single += 1

    def drawLines(self, lines: list[QLineF]) -> None:  # noqa: N802 - QPainter に合わせる
        self.batches.append(list(lines))


def _waveform(low: float, high: float, seconds: int = 4) -> Waveform:
    count = 48000 * seconds // 256
    peaks = np.empty((count, 2, 2), dtype=np.float32)
    peaks[:, :, 0] = low
    peaks[:, :, 1] = high
    return Waveform(
        sample_rate=48000,
        channels=2,
        total_samples=48000 * seconds,
        levels=(PeakLevel(256, peaks),),
    )


def _draw(waveform: Waveform, rect: QRect) -> _Recorder:
    recorder = _Recorder()
    clip = Clip(timeline_start=0, duration=90, source=TEXT.create())
    _draw_waveform(
        cast("QPainter", recorder),
        rect,
        clip,
        TimelineLayout(pixels_per_frame=10.0),
        FrameRate(30),
        waveform,
    )
    return recorder


def test_the_waveform_is_drawn_in_one_call_not_one_per_column() -> None:
    """列ごとに drawLine を呼ぶと、100 本並べたときの描画が 50ms 近くになる"""
    recorder = _draw(_waveform(-0.5, 0.5), QRect(200, 10, 400, 42))
    assert recorder.single == 0
    assert len(recorder.batches) == 1
    assert len(recorder.batches[0]) == 400


def test_the_batched_lines_keep_the_shape_of_the_waveform() -> None:
    """まとめても線の位置は同じ 真ん中から振幅の分だけ上下へ伸び、1 を超える値は切り詰める"""
    rect = QRect(200, 10, 400, 42)
    centre, half = rect.top() + rect.height() / 2.0, rect.height() / 2.0 - 1.0
    lines = _draw(_waveform(-0.5, 0.5), rect).batches[0]
    assert lines[0].x1() == lines[0].x2() == rect.left()
    assert lines[-1].x1() == rect.left() + 399
    assert lines[0].y1() == centre - 0.5 * half
    assert lines[0].y2() == centre + 0.5 * half
    clipped = _draw(_waveform(-3.0, 3.0), rect).batches[0]
    assert clipped[5].y1() == centre - half
    assert clipped[5].y2() == centre + half


def test_silence_still_leaves_a_one_pixel_line() -> None:
    """無音でも 1 画素の線を残す 何も描かないと、音のクリップなのか見分けられない"""
    rect = QRect(200, 10, 400, 42)
    lines = _draw(_waveform(0.0, 0.0), rect).batches[0]
    assert all(line.y2() - line.y1() == 1.0 for line in lines)
