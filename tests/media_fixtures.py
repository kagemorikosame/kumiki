"""テスト用の実素材を ffmpeg で生成する。

デコードや書き出しは実ファイルでしか検証できない。バイナリをリポジトリに置くと
差分が読めなくなるので、必要なものをその場で作る。生成物はセッション内で使い回す。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

__all__ = [
    "SampleMedia",
    "decode_all_frames",
    "ffmpeg_available",
    "make_rotated",
    "make_sample",
    "make_silent_gap",
]


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


@dataclass(frozen=True, slots=True)
class SampleMedia:
    """生成した素材と、その素材が持っているはずの性質。"""

    path: Path
    width: int
    height: int
    fps: str
    duration: float
    has_audio: bool
    sample_rate: int


def make_sample(
    directory: Path,
    name: str,
    *,
    width: int = 320,
    height: int = 240,
    fps: str = "30",
    duration: float = 2.0,
    audio: bool = True,
    sample_rate: int = 44100,
    tone_hz: int = 440,
    #: 音量の増幅（dB）。lavfi の sine は振幅が 0.1 程度しかないので、
    #: 実運用に近い波形が要るときに持ち上げる。
    gain_db: float = 0.0,
    pattern: str = "testsrc2",
    keyframe_interval: int | None = None,
) -> SampleMedia:
    """ffmpeg でテスト素材を作る。すでにあればそれを返す。

    ``testsrc2`` はフレームごとに絵が変わるので、シークが正しい位置に着地したかは
    :func:`decode_all_frames` で作った参照列との一致で確かめられる。
    """
    path = directory / name
    if path.exists():
        return SampleMedia(path, width, height, fps, duration, audio, sample_rate)

    directory.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]

    source = f"{pattern}=size={width}x{height}:rate={fps}:duration={duration}"
    command += ["-f", "lavfi", "-i", source]
    if audio:
        tone = f"sine=frequency={tone_hz}:duration={duration}:sample_rate={sample_rate}"
        if gain_db:
            tone += f",volume={gain_db}dB"
        command += ["-f", "lavfi", "-i", tone]

    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if keyframe_interval is not None:
        command += ["-g", str(keyframe_interval), "-keyint_min", str(keyframe_interval)]
    if audio:
        command += ["-c:a", "aac", "-ac", "2"]

    command.append(str(path))
    subprocess.run(command, check=True, capture_output=True)
    return SampleMedia(path, width, height, fps, duration, audio, sample_rate)


def make_rotated(directory: Path, name: str, source: Path, degrees: int) -> Path:
    """既存の素材に回転情報だけを付けた複製を作る。画素は触らない。"""
    path = directory / name
    if path.exists():
        return path
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-display_rotation",
            str(degrees),
            "-i",
            str(source),
            "-c",
            "copy",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def make_silent_gap(
    directory: Path, name: str, *, duration: float = 6.0, sample_rate: int = 48000
) -> Path:
    """前半と後半に音があり、真ん中が無音の素材。ジェットカットの検証用。"""
    path = directory / name
    if path.exists():
        return path
    third = duration / 3
    filter_complex = (
        f"sine=frequency=440:duration={third}:sample_rate={sample_rate}[a];"
        f"anullsrc=r={sample_rate}:cl=stereo:d={third}[b];"
        f"sine=frequency=880:duration={third}:sample_rate={sample_rate}[c];"
        f"[a][b][c]concat=n=3:v=0:a=1[out]"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def decode_all_frames(path: Path) -> list[tuple[float, np.ndarray]]:
    """先頭から順に全フレームを復号し、``(表示時刻, RGBA 配列)`` の一覧を返す。

    シークの正しさは「飛んだ結果が、順に読んだ結果と一致するか」でしか確かめられない。
    その参照側を作る。
    """
    import av

    frames: list[tuple[float, np.ndarray]] = []
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            pts, time_base = frame.pts, frame.time_base
            time = float(pts * time_base) if pts is not None and time_base else 0.0
            frames.append((time, frame.to_ndarray(format="rgba")))
    return frames
