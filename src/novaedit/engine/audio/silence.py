"""無音区間の検出。ジェットカットの土台。

すでに作ってある波形ピーク（:mod:`novaedit.engine.audio.waveform`）を使う。音声を
読み直さないので、素材の長さによらず一瞬で終わる。ピークは min/max なので、
区間内の最大振幅がそのまま得られる。

返すのは**素材内のソース秒**の区間。タイムラインのことは知らない。どのクリップの
どこに当たるかは :mod:`novaedit.core.jetcut` が決める。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np

from novaedit.core.model import Transcript
from novaedit.engine.audio.waveform import Waveform

__all__ = ["SilenceOptions", "TimeRange", "detect_silence", "keep_speech"]

#: ソース秒の区間 ``[start, end)``。
type TimeRange = tuple[Fraction, Fraction]

#: 無音判定の下限。これより静かな環境音は無いものとして扱う。
_FLOOR_DB = -120.0


@dataclass(frozen=True, slots=True)
class SilenceOptions:
    """無音判定の条件。"""

    #: これを下回っていれば無音とみなす（フルスケール基準の dB）。
    threshold_db: float = -40.0
    #: これより短い無音は切らない。息継ぎまで切ると聞いていて忙しい。
    min_silence: Fraction = field(default_factory=lambda: Fraction(1, 2))
    #: 無音の前後に残す余白。発話の立ち上がりを切り落とさないため。
    padding: Fraction = field(default_factory=lambda: Fraction(1, 10))
    #: 無音に挟まれた発話がこれより短ければ、その発話ごと落とす。0 なら落とさない。
    #: 舌打ちやマウスの音だけが残るのを防ぐが、既定では切らない側に倒す。
    min_keep: Fraction = field(default_factory=lambda: Fraction(0))

    def __post_init__(self) -> None:
        if self.min_silence <= 0:
            raise ValueError(f"最短無音長は正でなければならない: {self.min_silence}")
        if self.padding < 0 or self.min_keep < 0:
            raise ValueError("余白と最短発話長は負にできない")

    @property
    def amplitude(self) -> float:
        """判定に使う振幅（0..1）。"""
        return float(10.0 ** (max(self.threshold_db, _FLOOR_DB) / 20.0))


def detect_silence(
    waveform: Waveform, options: SilenceOptions | None = None
) -> tuple[TimeRange, ...]:
    """波形から無音区間を拾う。

    最も細かい段階のピークを使う。粗い段階だと 1 ピークに数秒が入り、短い無音が
    埋もれる。
    """
    resolved = options if options is not None else SilenceOptions()
    level = waveform.levels[0]
    if level.count == 0:
        return ()

    # チャンネルをまたいだ最大振幅。片方だけ鳴っている区間を無音と誤らないため。
    peaks = level.peaks
    loudness = np.maximum(np.abs(peaks[:, :, 0]), np.abs(peaks[:, :, 1])).max(axis=1)
    quiet = loudness < resolved.amplitude

    per_peak = Fraction(level.samples_per_peak, waveform.sample_rate)
    total = waveform.duration

    ranges: list[TimeRange] = []
    for begin, end in _runs(quiet):
        start = begin * per_peak + resolved.padding
        stop = min(end * per_peak, total) - resolved.padding
        if stop - start >= resolved.min_silence:
            ranges.append((start, stop))

    if resolved.min_keep > 0:
        ranges = _absorb_short_speech(ranges, resolved.min_keep)
    return tuple(ranges)


def keep_speech(
    ranges: tuple[TimeRange, ...], transcript: Transcript, *, margin: Fraction = Fraction(1, 10)
) -> tuple[TimeRange, ...]:
    """起こし結果に発話がある区間を、切る候補から外す。

    波形だけでは、小さな声と環境音を区別できない。認識器が文字を起こせた区間は
    確実に発話なので、そこは残す。これがあると閾値を強気に振れる。
    """
    protected = [(segment.start - margin, segment.end + margin) for segment in transcript.segments]
    if not protected:
        return ranges

    result: list[TimeRange] = []
    for start, end in ranges:
        pieces = [(start, end)]
        for guard_start, guard_end in protected:
            pieces = [
                piece for current in pieces for piece in _subtract(current, guard_start, guard_end)
            ]
        result.extend(piece for piece in pieces if piece[1] > piece[0])
    return tuple(result)


def _subtract(piece: TimeRange, start: Fraction, end: Fraction) -> list[TimeRange]:
    """``piece`` から ``[start, end)`` を引く。0 個・1 個・2 個の区間になる。"""
    left, right = piece
    if end <= left or start >= right:
        return [piece]
    remaining: list[TimeRange] = []
    if left < start:
        remaining.append((left, start))
    if end < right:
        remaining.append((end, right))
    return remaining


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """真が続く区間を ``[begin, end)`` の一覧で返す。"""
    if not mask.any():
        return []
    # 端に偽を足してから差分を取る。境界の場合分けをここで消しておく。
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(edges[i]), int(edges[i + 1])) for i in range(0, len(edges), 2)]


def _absorb_short_speech(ranges: list[TimeRange], min_keep: Fraction) -> list[TimeRange]:
    """無音に挟まれた短い発話を、前後の無音ごと 1 つにまとめる。"""
    if not ranges:
        return ranges
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        previous_start, previous_end = merged[-1]
        if start - previous_end < min_keep:
            merged[-1] = (previous_start, end)
        else:
            merged.append((start, end))
    return merged
