"""波形ピークの永続化"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from kumiki.engine.audio.waveform import PeakLevel, Waveform

from .store import CacheStore, load_arrays, media_key, save_arrays

__all__ = ["NAMESPACE", "SUFFIX", "load_waveform", "save_waveform", "waveform_key"]

NAMESPACE = "waveform"
SUFFIX = ".peaks.npz"

#: 保存形式の版 段階の作り方を変えたら上げる 読めない版は捨てて作り直す
FORMAT_VERSION = 1


def waveform_key(path: Path, sample_rate: int, channels: int) -> str:
    return media_key(path, extra=f"wf{FORMAT_VERSION}:{sample_rate}:{channels}")


def save_waveform(store: CacheStore, key: str, waveform: Waveform) -> Path:
    """ピークをファイルへ書き出す"""
    arrays: dict[str, np.ndarray] = {
        "version": np.array([FORMAT_VERSION]),
        "sample_rate": np.array([waveform.sample_rate]),
        "channels": np.array([waveform.channels]),
        "total_samples": np.array([waveform.total_samples]),
        "samples_per_peak": np.array([level.samples_per_peak for level in waveform.levels]),
    }
    # 段階ごとに配列の形が違うので、1 つにまとめず個別の名前で入れる
    arrays.update({f"level{index}": level.peaks for index, level in enumerate(waveform.levels)})
    return save_arrays(store.prepare(NAMESPACE, key, SUFFIX), arrays)


def load_waveform(store: CacheStore, key: str) -> Waveform | None:
    """保存済みのピークを読む 無い・壊れている・版が違うなら ``None``"""
    data = load_arrays(store.path_for(NAMESPACE, key, SUFFIX))
    if data is None:
        return None

    try:
        if int(data["version"][0]) != FORMAT_VERSION:
            return None
        samples_per_peak = data["samples_per_peak"]
        levels = tuple(
            PeakLevel(int(samples_per_peak[index]), data[f"level{index}"])
            for index in range(len(samples_per_peak))
        )
        if not levels:
            return None
        # 付帯の値も同じ try の中で読む 外で読むと、欠けたキャッシュで KeyError に
        # なり「壊れていたら None」の約束が守れない（作り直す機会を失う）
        return Waveform(
            sample_rate=int(data["sample_rate"][0]),
            channels=int(data["channels"][0]),
            total_samples=int(data["total_samples"][0]),
            levels=levels,
        )
    except (KeyError, IndexError, ValueError):
        return None
