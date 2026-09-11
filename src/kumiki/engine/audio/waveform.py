"""波形表示のためのピーク解析。

タイムラインは 1 ピクセルに数百〜数十万サンプルを描く。毎回それだけの音声を
読み直すのは論外なので、あらかじめ min/max のピークを段階的な解像度で作っておき、
表示倍率に応じて使い分ける。

段階を持たせるのが要点。1 段階だけだと、拡大時は粗く、縮小時は読む量が多すぎる。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

from kumiki.engine.decode import AudioDecoder

__all__ = ["PeakLevel", "Waveform", "analyze_waveform"]

#: 最も細かい段階で、1 ピークにまとめるサンプル数。
#: 48kHz なら 1 ピーク約 5.3ms。編集で見る最大倍率でも十分細かい。
BASE_SAMPLES_PER_PEAK = 256

#: 段階ごとの粗さの比。8 倍ずつ粗くする。
LEVEL_RATIO = 8

#: 一度に読むサンプル数。大きすぎるとメモリを、小さすぎると呼び出し回数を食う。
CHUNK_SAMPLES = 1 << 18


@dataclass(frozen=True, slots=True)
class PeakLevel:
    """1 段階分のピーク。

    ``peaks`` の形は ``(ピーク数, チャンネル数, 2)``。最後の次元が ``(最小, 最大)``。
    平均や絶対値の最大ではなく min/max を持つのは、波形の非対称性（打楽器など）を
    潰さないため。
    """

    samples_per_peak: int
    peaks: np.ndarray

    @property
    def count(self) -> int:
        return int(self.peaks.shape[0])

    @property
    def channels(self) -> int:
        return int(self.peaks.shape[1])


@dataclass(frozen=True, slots=True)
class Waveform:
    """1 本の音声ストリームのピーク一式。"""

    sample_rate: int
    channels: int
    total_samples: int
    levels: tuple[PeakLevel, ...]

    def __post_init__(self) -> None:
        if not self.levels:
            raise ValueError("段階が 1 つも無い")

    @property
    def duration(self) -> Fraction:
        return Fraction(self.total_samples, self.sample_rate)

    def level_for(self, samples_per_pixel: float) -> PeakLevel:
        """表示倍率に見合う段階を選ぶ。

        1 ピクセルあたりのサンプル数を超えない中で最も粗い段階を返す。粗すぎると
        ピークが 1 個も入らないピクセルができ、波形が途切れて見える。
        """
        chosen = self.levels[0]
        for level in self.levels:
            if level.samples_per_peak <= samples_per_pixel:
                chosen = level
            else:
                break
        return chosen

    def envelope(self, start_sample: int, end_sample: int, columns: int) -> np.ndarray:
        """``[start_sample, end_sample)`` を ``columns`` 本に束ねた min/max を返す。

        形は ``(columns, チャンネル数, 2)``。範囲外は 0 で埋める。描画側は
        この配列をそのまま縦線として描けばよい。
        """
        if columns <= 0 or end_sample <= start_sample:
            return np.zeros((max(columns, 0), self.channels, 2), dtype=np.float32)

        span = end_sample - start_sample
        level = self.level_for(span / columns)
        out = np.zeros((columns, self.channels, 2), dtype=np.float32)

        # 各列が対応するピーク範囲を一括で求める。列ごとに Python で回すと、
        # 横 2000 ピクセルのタイムラインで描画のたびに効いてくる。
        edges = start_sample + np.linspace(0, span, columns + 1)
        starts = np.floor(edges[:-1] / level.samples_per_peak).astype(np.int64)
        stops = np.ceil(edges[1:] / level.samples_per_peak).astype(np.int64)
        starts = np.clip(starts, 0, level.count)
        stops = np.clip(np.maximum(stops, starts + 1), 0, level.count)

        for column in range(columns):
            begin, end = int(starts[column]), int(stops[column])
            if begin >= end:
                continue
            block = level.peaks[begin:end]
            out[column, :, 0] = block[:, :, 0].min(axis=0)
            out[column, :, 1] = block[:, :, 1].max(axis=0)
        return out


def analyze_waveform(
    path: Path,
    *,
    sample_rate: int = 48000,
    channels: int = 2,
    stream_index: int | None = None,
    progress: Callable[[float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Waveform | None:
    """素材を読み切ってピークを作る。

    重い処理なのでバックグラウンドで呼ぶ前提。``should_cancel`` が真を返したら
    途中で ``None`` を返して抜ける。素材を差し替えたのに前の解析が走り続ける、
    という状態を避けるため。
    """
    with AudioDecoder(
        path, sample_rate=sample_rate, channels=channels, stream_index=stream_index
    ) as decoder:
        total = int(decoder.duration * sample_rate)
        base: list[np.ndarray] = []
        consumed = 0

        for chunk in _chunks(decoder, total):
            if should_cancel is not None and should_cancel():
                return None
            base.append(_reduce(chunk, BASE_SAMPLES_PER_PEAK))
            consumed += len(chunk)
            if progress is not None and total > 0:
                progress(min(1.0, consumed / total))

    peaks = np.concatenate(base, axis=0) if base else np.zeros((0, channels, 2), dtype=np.float32)
    levels = [PeakLevel(BASE_SAMPLES_PER_PEAK, peaks)]

    # 粗い段階は、細かい段階から作る。元の音声を読み直す必要は無い。
    while levels[-1].count > 1:
        coarser = _coarsen(levels[-1])
        if coarser.count == levels[-1].count:
            break
        levels.append(coarser)

    if progress is not None:
        progress(1.0)
    return Waveform(
        sample_rate=sample_rate,
        channels=channels,
        total_samples=max(total, peaks.shape[0] * BASE_SAMPLES_PER_PEAK),
        levels=tuple(levels),
    )


def _chunks(decoder: AudioDecoder, total: int) -> Iterator[np.ndarray]:
    """素材を先頭から順に読み出す。"""
    cursor = 0
    while cursor < total:
        count = min(CHUNK_SAMPLES, total - cursor)
        yield decoder.read(cursor, count)
        cursor += count


def _reduce(samples: np.ndarray, samples_per_peak: int) -> np.ndarray:
    """``(サンプル数, チャンネル数)`` を ``(ピーク数, チャンネル数, 2)`` へ。"""
    count, channels = samples.shape
    groups = (count + samples_per_peak - 1) // samples_per_peak
    padded_length = groups * samples_per_peak
    if padded_length != count:
        # 端数は最後のサンプルで埋める。0 で埋めると、末尾に無い谷が生まれる。
        pad = np.repeat(samples[-1:], padded_length - count, axis=0)
        samples = np.concatenate([samples, pad], axis=0)

    grouped = samples.reshape(groups, samples_per_peak, channels)
    out = np.empty((groups, channels, 2), dtype=np.float32)
    out[:, :, 0] = grouped.min(axis=1)
    out[:, :, 1] = grouped.max(axis=1)
    return out


def _coarsen(level: PeakLevel) -> PeakLevel:
    """1 段階粗いピークを作る。"""
    count = level.count
    groups = (count + LEVEL_RATIO - 1) // LEVEL_RATIO
    padded_length = groups * LEVEL_RATIO
    peaks = level.peaks
    if padded_length != count and count > 0:
        pad = np.repeat(peaks[-1:], padded_length - count, axis=0)
        peaks = np.concatenate([peaks, pad], axis=0)

    grouped = peaks.reshape(groups, LEVEL_RATIO, level.channels, 2)
    out = np.empty((groups, level.channels, 2), dtype=np.float32)
    out[:, :, 0] = grouped[:, :, :, 0].min(axis=1)
    out[:, :, 1] = grouped[:, :, :, 1].max(axis=1)
    return PeakLevel(level.samples_per_peak * LEVEL_RATIO, out)
