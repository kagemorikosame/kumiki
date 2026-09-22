"""タイムラインをファイルへ書き出す

プレビューと同じ :class:`~kumiki.engine.render.FrameRenderer` と
:class:`~kumiki.engine.audio.AudioMixer` を使う 別経路にすると
「プレビューでは出るのに書き出すと出ない」が起きる
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import cast

import av
import av.audio.fifo
import av.audio.frame
import av.audio.stream
import av.error
import av.video.frame
import av.video.stream
import numpy as np

from kumiki.core.model import Project, TrackKind
from kumiki.engine.audio import AudioMixer
from kumiki.engine.colorspace import tag_bt709, to_bt709
from kumiki.engine.gpu import OffscreenGLContext
from kumiki.engine.render import FULL_QUALITY, FrameRenderer

__all__ = ["ExportError", "ExportSettings", "available_video_codecs", "export_project"]

#: 優先順に並べた映像コーデック 前にあるものから、使えるものを選ぶ
#: NVENC は CPU をほとんど使わないので、長尺でも編集を続けながら書き出せる
VIDEO_CODEC_PREFERENCE = ("h264_nvenc", "h264_qsv", "libx264")

_LAYOUTS = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}


class ExportError(RuntimeError):
    """書き出しを開始できない、または途中で失敗した"""


@dataclass(frozen=True, slots=True)
class ExportSettings:
    """書き出しの設定"""

    path: Path
    #: ``None`` なら :func:`available_video_codecs` の先頭を使う
    video_codec: str | None = None
    #: 映像のビットレート（bps） ``None`` ならコーデックの既定に任せる
    video_bitrate: int | None = 12_000_000
    audio_codec: str = "aac"
    audio_bitrate: int = 192_000
    pixel_format: str = "yuv420p"
    #: 書き出すフレーム範囲 ``None`` なら全体
    frame_range: tuple[int, int] | None = None
    #: コーデックへ渡す追加オプション プリセットや品質指定を通す口
    options: dict[str, str] = field(default_factory=dict)


def available_video_codecs() -> list[str]:
    """この環境で使える映像コーデックを、優先順に返す"""
    found = []
    for name in VIDEO_CODEC_PREFERENCE:
        try:
            av.codec.Codec(name, "w")
        except Exception:
            continue
        found.append(name)
    return found


def export_project(
    project: Project,
    settings: ExportSettings,
    *,
    progress: Callable[[float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Path:
    """プロジェクトを 1 本の動画ファイルへ書き出す

    ``should_cancel`` が真を返したら、書きかけのファイルを消して
    :class:`ExportError` を投げる 中途半端なファイルを残すと、書き出せたのか
    どうかが分からなくなる
    """
    start, end = settings.frame_range or (0, project.duration)
    total = end - start
    if total <= 0:
        raise ExportError("書き出す範囲が空")

    codec = settings.video_codec or next(iter(available_video_codecs()), None)
    if codec is None:
        raise ExportError("使える映像コーデックが見つからない")

    settings.path.parent.mkdir(parents=True, exist_ok=True)
    context = OffscreenGLContext()
    renderer = FrameRenderer(project, context=context, quality=FULL_QUALITY)
    mixer = AudioMixer(project)

    try:
        _encode(project, settings, codec, start, end, renderer, mixer, progress, should_cancel)
    except BaseException:
        # 例外でもキャンセルでも、書きかけを残さない
        settings.path.unlink(missing_ok=True)
        raise
    finally:
        renderer.close()
        mixer.close()
        context.release()

    return settings.path


def _encode(
    project: Project,
    settings: ExportSettings,
    codec: str,
    start: int,
    end: int,
    renderer: FrameRenderer,
    mixer: AudioMixer,
    progress: Callable[[float], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> None:
    rate = project.rate
    width, height = project.settings.resolution
    has_audio = any(track.clips for track in project.timeline.active_tracks(TrackKind.AUDIO))

    try:
        container = av.open(str(settings.path), mode="w")
    except (av.error.FFmpegError, OSError) as exc:
        raise ExportError(f"出力ファイルを開けない: {settings.path} ({exc})") from exc

    with container:
        # add_stream は種類の union を返す 以降は映像として扱うので、ここで型を確定させる
        video = cast(
            "av.video.stream.VideoStream",
            container.add_stream(codec, rate=Fraction(rate.num, rate.den)),
        )
        video.width = width
        video.height = height
        video.pix_fmt = settings.pixel_format
        # 付けないと再生側が行列を推測する HD なら BT.709 と当てる再生ソフトが多いが、
        # ブラウザや編集ソフトの一部は BT.601 で読み、書いた値と違う色になる
        tag_bt709(video)
        if settings.video_bitrate:
            video.bit_rate = settings.video_bitrate
        if settings.options:
            video.options = dict(settings.options)

        audio = None
        fifo = None
        if has_audio:
            audio = cast(
                "av.audio.stream.AudioStream",
                container.add_stream(settings.audio_codec, rate=mixer.sample_rate),
            )
            audio.bit_rate = settings.audio_bitrate
            fifo = av.audio.fifo.AudioFifo()

        # フレームの時刻の刻み ストリームの time_base を毎回読み直してはいけない
        # 多重化が始まった時点でコンテナ側の値（MP4 なら 1/15360）に書き換わるので、
        # 読み直すと 2 フレーム目以降の PTS がほぼ 0 に潰れる
        frame_time_base = Fraction(rate.den, rate.num)

        total = end - start
        #: 音声の書き込み位置 出力レートでの通し番号で、そのまま PTS になる
        audio_cursor = 0

        for index, frame_number in enumerate(range(start, end)):
            if should_cancel is not None and should_cancel():
                raise ExportError("書き出しを中止した")

            # 音声を先に流す AAC は先頭にプライミングを持つため最初のパケットの
            # DTS が負になり、映像を先に入れると多重化の順序が逆転して弾かれる
            if audio is not None and fifo is not None:
                audio_cursor = _write_audio(
                    container, audio, fifo, mixer, frame_number, audio_cursor
                )

            image = renderer.render(frame_number)
            frame = av.video.frame.VideoFrame.from_ndarray(
                np.ascontiguousarray(image[:, :, :3]), format="rgb24"
            )
            frame = to_bt709(frame, settings.pixel_format)
            frame.pts = index
            frame.time_base = frame_time_base
            container.mux(video.encode(frame))

            if progress is not None:
                progress((index + 1) / total)

        # エンコーダに溜まっている分を吐き出す これを忘れると末尾が欠ける
        if audio is not None and fifo is not None:
            _flush_audio(container, audio, fifo)
        container.mux(video.encode(None))


def _write_audio(
    container: av.container.OutputContainer,
    stream: av.audio.stream.AudioStream,
    fifo: av.audio.fifo.AudioFifo,
    mixer: AudioMixer,
    frame_number: int,
    cursor: int,
) -> int:
    """1 フレーム分の音声を FIFO へ流し、エンコーダが要求する粒度で切り出す

    AAC は 1024 サンプル単位でしか受け取らない 映像のフレーム境界とは
    一致しないので、FIFO を挟んで詰め替える 戻り値は次の書き込み位置
    """
    block = mixer.render_frames(frame_number, 1)
    if len(block) == 0:
        return cursor

    frame = _to_audio_frame(block, mixer.sample_rate, mixer.channels)
    # PTS を付けないと、エンコーダが時刻を持たないパケットを出して多重化に失敗する
    frame.time_base = Fraction(1, mixer.sample_rate)
    frame.pts = cursor
    fifo.write(frame)

    frame_size = stream.codec_context.frame_size or 1024
    while True:
        chunk = fifo.read(frame_size)
        if chunk is None:
            break
        container.mux(stream.encode(chunk))
    return cursor + len(block)


def _flush_audio(
    container: av.container.OutputContainer,
    stream: av.audio.stream.AudioStream,
    fifo: av.audio.fifo.AudioFifo,
) -> None:
    remainder = fifo.read()
    if remainder is not None:
        container.mux(stream.encode(remainder))
    container.mux(stream.encode(None))


def _to_audio_frame(
    samples: np.ndarray, sample_rate: int, channels: int
) -> av.audio.frame.AudioFrame:
    """``(サンプル数, チャンネル数)`` の float32 を PyAV のフレームへ

    出力段でここだけクリッピングする 合成の途中で頭打ちにすると、後段の
    調整で潰れた音しか扱えなくなる
    """
    clipped = np.clip(samples, -1.0, 1.0).astype(np.float32)
    planar = np.ascontiguousarray(clipped.T)
    frame = av.audio.frame.AudioFrame.from_ndarray(planar, format="fltp", layout=_LAYOUTS[channels])
    frame.sample_rate = sample_rate
    return frame
