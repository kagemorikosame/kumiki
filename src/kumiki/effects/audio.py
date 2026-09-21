"""音を加工するエフェクト AviUtl の音声フィルタに当たるもの

映像のエフェクトはシェーダで書くが、音は GPU を通さないので
:attr:`~kumiki.effects.definition.EffectDefinition.audio_process` に
関数を入れる 仕組みは :mod:`kumiki.engine.audio.mixer` が呼ぶ

項目名は AviUtl2 に音声ファイルを置いてフィルタを積み、エイリアスを作らせて
読み取った（推測していない） AviUtl2 v2.1.6a の音声フィルタは
``音量フェード`` ``モノラル化`` ``音量調整`` の **3 つだけ**
（``音声波形表示`` は音ではなく絵を描くので映像の側にある）

サンプルは ``(数, チャンネル)`` の float32 で、-1..1 に収まっているとは限らない
（混ぜた後に整える） 返す形も同じにする
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from kumiki.effects.definition import EffectDefinition, registry
from kumiki.effects.spec import TrackSpec

__all__ = ["AudioContext", "register_audio_effects"]


@dataclass(frozen=True, slots=True)
class AudioContext:
    """音のエフェクトへ渡す、時間まわりの手がかり

    ``offset`` は**クリップ先頭からのサンプル位置** フェードのように
    位置で効き方が変わるものは、これと ``duration`` から進み具合を出す
    """

    #: クリップ先頭から数えた、この塊の先頭のサンプル位置
    offset: int
    #: 1 秒あたりのサンプル数
    sample_rate: int
    #: クリップの長さ（サンプル）
    duration: int


def _volume(samples: np.ndarray, values: dict[str, float], context: AudioContext) -> np.ndarray:
    """音量調整 ``音量`` は %（100 で元のまま） ``左右`` は -100..100"""
    gain = max(values.get("volume", 100.0), 0.0) / 100.0
    out = samples * gain
    return _panned(out, values.get("pan", 0.0) / 100.0)


def _fade(samples: np.ndarray, values: dict[str, float], context: AudioContext) -> np.ndarray:
    """音量フェード 端の ``イン`` ``アウト`` 秒で 0 から 1 へ

    位置ごとに掛ける量が変わるので、塊の中でもサンプルごとに計算する
    塊の先頭の値だけで掛けると、塊の境目で音が階段状に変わる
    """
    rate = max(context.sample_rate, 1)
    fade_in = max(values.get("fade_in", 0.0), 0.0) * rate
    fade_out = max(values.get("fade_out", 0.0), 0.0) * rate
    # 位置は整数で持つ float32 だと 1677 万（48 kHz で 6 分弱）を超えた辺りから
    # 1 サンプル単位を表せなくなり、長いクリップでフェードが階段状になる
    index = np.arange(len(samples), dtype=np.int64) + context.offset

    gain = np.ones(len(samples), dtype=np.float64)
    if fade_in > 0.0:
        gain = np.minimum(gain, index / fade_in)
    if fade_out > 0.0:
        left = context.duration - index
        gain = np.minimum(gain, left / fade_out)
    return samples * np.clip(gain, 0.0, 1.0).astype(np.float32)[:, None]


def _monaural(samples: np.ndarray, values: dict[str, float], context: AudioContext) -> np.ndarray:
    """モノラル化 ``比率`` は左右を混ぜる量（0 で混ぜない 100 で完全にモノラル）

    AviUtl の ``比率`` は **0 が元のまま** 逆に読むと、既定のままで
    ステレオが潰れる
    """
    amount = np.clip(values.get("ratio", 0.0) / 100.0, 0.0, 1.0)
    if amount <= 0.0 or samples.shape[1] < 2:
        return samples
    middle = samples.mean(axis=1, keepdims=True)
    mixed: np.ndarray = samples * (1.0 - amount) + middle * amount
    return mixed


def _panned(samples: np.ndarray, pan: float) -> np.ndarray:
    """左右の振り分け -1 で左だけ 1 で右だけ

    片側を絞るだけにする 反対側を持ち上げると、真ん中に寄せた音が大きくなる
    """
    pan = float(np.clip(pan, -1.0, 1.0))
    if pan == 0.0 or samples.shape[1] != 2:
        return samples
    out = samples.copy()
    if pan > 0.0:
        out[:, 0] *= 1.0 - pan
    else:
        out[:, 1] *= 1.0 + pan
    return out


def register_audio_effects() -> None:
    definitions = (
        EffectDefinition(
            kind="audio_volume",
            label="音量調整",
            category="音",
            parameters=(
                TrackSpec("volume", "音量", 0, 400, 100, unit="%"),
                TrackSpec("pan", "左右", -100, 100, 0, unit="%"),
            ),
            audio_process=_volume,
        ),
        EffectDefinition(
            kind="audio_fade",
            label="音量フェード",
            category="音",
            parameters=(
                TrackSpec("fade_in", "イン", 0, 60, 0, unit="秒"),
                TrackSpec("fade_out", "アウト", 0, 60, 0, unit="秒"),
            ),
            audio_process=_fade,
        ),
        EffectDefinition(
            kind="audio_monaural",
            label="モノラル化",
            category="音",
            parameters=(TrackSpec("ratio", "比率", 0, 100, 0, unit="%"),),
            audio_process=_monaural,
        ),
    )
    for definition in definitions:
        registry.register(definition)
