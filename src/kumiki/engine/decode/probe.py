"""素材を開いて :class:`~kumiki.core.model.MediaItem` を組み立てる"""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import av
import av.error

from kumiki.core.model import AudioStreamInfo, MediaItem, VideoStreamInfo
from kumiki.core.timebase import FrameRate

__all__ = ["ProbeError", "probe_media"]

#: 静止画として扱う拡張子 長さを持たず、タイムライン上で任意に伸ばせる
STILL_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"})

#: フレームレートが取れなかったときの既定値 静止画や壊れたヘッダで起きる
FALLBACK_FRAME_RATE = FrameRate(30)


class ProbeError(Exception):
    """素材を開けない、または中身を解釈できない"""


def probe_media(path: Path) -> MediaItem:
    """ファイルを解析して素材情報を返す

    映像・音声の各ストリームを個別に記録する 多言語音声や 5.1ch の素材では
    音声が複数本あり、読み込み時にそれぞれ別トラックへ展開できるようにするため
    """
    path = Path(path)
    if not path.exists():
        raise ProbeError(f"ファイルが見つからない: {path}")

    try:
        container = av.open(str(path))
    except (av.error.FFmpegError, OSError) as exc:
        raise ProbeError(f"素材を開けない: {path} ({exc})") from exc

    with container:
        is_still = path.suffix.lower() in STILL_SUFFIXES
        rotation = 0 if is_still else _probe_rotation(path)

        video_streams = tuple(_video_info(stream, rotation) for stream in container.streams.video)
        audio_streams = tuple(_audio_info(stream) for stream in container.streams.audio)
        if not video_streams and not audio_streams:
            raise ProbeError(f"映像も音声も含まれていない: {path}")

        duration = Fraction(0) if is_still else _container_duration(container)

    return MediaItem(
        path=path,
        duration=duration,
        video_streams=video_streams,
        audio_streams=audio_streams,
    )


def _container_duration(container: av.container.InputContainer) -> Fraction:
    """素材全体の長さ（秒）

    コンテナの長さを優先する ストリームごとの長さは映像と音声で食い違うことがあり、
    どちらを採るかで末尾が欠けたり余ったりするため
    """
    if container.duration is not None:
        return Fraction(container.duration, av.time_base)

    longest = Fraction(0)
    for stream in (*container.streams.video, *container.streams.audio):
        if stream.duration is not None and stream.time_base is not None:
            longest = max(longest, Fraction(stream.duration) * stream.time_base)
    return longest


def _video_info(stream: av.video.stream.VideoStream, rotation: int) -> VideoStreamInfo:
    rate = stream.average_rate or stream.guessed_rate or stream.base_rate
    frame_rate = FrameRate(rate.numerator, rate.denominator) if rate else FALLBACK_FRAME_RATE
    return VideoStreamInfo(
        index=stream.index,
        width=stream.codec_context.width,
        height=stream.codec_context.height,
        frame_rate=frame_rate,
        time_base=Fraction(stream.time_base) if stream.time_base else Fraction(1, 1000),
        codec=stream.codec_context.name,
        pixel_format=stream.format.name if stream.format else "",
        rotation=rotation,
    )


def _audio_info(stream: av.audio.stream.AudioStream) -> AudioStreamInfo:
    context = stream.codec_context
    return AudioStreamInfo(
        index=stream.index,
        sample_rate=context.sample_rate,
        channels=context.layout.nb_channels,
        time_base=Fraction(stream.time_base) if stream.time_base else Fraction(1, 48000),
        codec=context.name,
        language=stream.metadata.get("language") or None,
    )


def _probe_rotation(path: Path) -> int:
    """表示時に適用すべき時計回りの回転角を返す

    PyAV 18 は表示行列に setter しか公開していないので、ここだけ ffprobe に頼る
    スマホの縦撮り素材は回転情報を持つのが普通で、無視すると横倒しで表示される
    取得できない場合は 0 を返し、素材の読み込み自体は続行する
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return 0

    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream_side_data=rotation",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        payload = json.loads(completed.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return 0

    for stream in payload.get("streams", []):
        for side_data in stream.get("side_data_list", []):
            value = side_data.get("rotation")
            if value is None:
                continue
            # ffprobe は反時計回りの角度を返す 表示時に必要なのは時計回りなので反転する
            clockwise = round(-float(value)) % 360
            return clockwise if clockwise in (0, 90, 180, 270) else 0
    return 0
