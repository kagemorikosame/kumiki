"""素材を開いて :class:`~sashimono.core.model.MediaItem` を組み立てる"""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import av
import av.error

from sashimono.core.model import AudioStreamInfo, MediaItem, VideoStreamInfo
from sashimono.core.timebase import FrameRate

__all__ = ["ProbeError", "media_origin", "probe_media"]

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

        origin = media_origin(container)
        video_streams = tuple(
            _video_info(stream, rotation, origin) for stream in container.streams.video
        )
        audio_streams = tuple(_audio_info(stream) for stream in container.streams.audio)
        if not video_streams and not audio_streams:
            raise ProbeError(f"映像も音声も含まれていない: {path}")

        duration = Fraction(0) if is_still else _container_duration(container, origin)

    return MediaItem(
        path=path,
        duration=duration,
        video_streams=video_streams,
        audio_streams=audio_streams,
    )


def media_origin(container: av.container.InputContainer) -> Fraction:
    """素材の時刻の原点（秒 PTS の数え方） 素材の中の時刻は、PTS からこれを引いて数える

    分割して書き出した物や放送の録画（MPEG-TS）は、最初のフレームの PTS が 0 より後ろにある
    PTS そのままで数えると、置いたクリップ（``source_in`` 0・長さは素材の長さ）が読む時刻が
    すべて最初のフレームより前になり、頭の絵が止まったまま音も鳴らない（Issue #123）
    クリップの ``source_in``・``hold_at``、YMM4 の ``ContentOffset`` は素材の頭から数えるので、
    こちらも頭から数える（YMM4 は本体で測った docs/development.md「素材の中の時刻は頭から数える」）

    原点は素材 1 本に 1 つ 映像と音声で別々に引くと、素材の中の映像と音の食い違い
    （頭の音が映像より早く始まる、など）が消えて音がずれる

    映像があれば最初の映像ストリームの頭にする コンテナの頭（全ストリームの最小）にしないのは、
    AAC の前置き（プライミング）が映像より 20ms ほど早く始まる素材が多いため コンテナの頭を
    原点にすると、映像のフレームの時刻がすべて前置きの分だけ後ろへずれ、フレームの境目の時刻で
    1 つ前の絵が出る 前置きは復号すると捨てる区間なので、原点より前（負の時刻）へ出してよい
    YMM4 も、音が映像より 0.52 秒早く始まる素材で ``ContentOffset`` 0 の頭に映像の最初の
    フレームを出した（映像の頭が原点）
    映像の無い素材は音声の頭の最小、どちらも分からなければコンテナの頭

    負の頭（B フレームの並べ替えで最初の PTS が負になる素材など）は 0 にする この修正の前から
    0 を原点として正しく映っていた素材で、引くと絵と音がずれ動く
    秒は分数で持つ コンテナの頭（マイクロ秒に丸めてある）から作ると、フレームの時刻が
    ちょうどの境目から 1µs 未満ずれ、境目の時刻で 1 つ前の絵が出る
    """
    for stream in container.streams.video[:1]:
        start = _stream_start(stream)
        if start is not None:
            return max(Fraction(0), start)
    starts = [_stream_start(stream) for stream in container.streams.audio]
    known = [start for start in starts if start is not None]
    if known:
        return max(Fraction(0), min(known))
    if container.start_time is not None:
        return max(Fraction(0), Fraction(container.start_time, av.time_base))
    return Fraction(0)


def _stream_start(stream: av.stream.Stream) -> Fraction | None:
    """ストリームの頭の時刻（秒 PTS の数え方） 分からなければ ``None``"""
    if stream.start_time is None or stream.time_base is None:
        return None
    return stream.start_time * Fraction(stream.time_base)


def _container_duration(container: av.container.InputContainer, origin: Fraction) -> Fraction:
    """素材全体の長さ（秒） 原点（:func:`media_origin`）から終わりまで

    コンテナの長さを優先する ストリームごとの長さは映像と音声で食い違うことがあり、
    どちらを採るかで末尾が欠けたり余ったりするため
    コンテナの長さは全ストリームの最小の頭から数えてある 映像より早く始まる音の前置きを
    含んだままにすると、置いたクリップが 1 フレーム長くなり、最後の 1 フレームは映像の
    終わりの後なので何も映らない 原点より前の区間は長さから除く 頭が 0 以下の素材は
    もとから原点が 0 なので、長さは変わらない
    """
    streams = (*container.streams.video, *container.streams.audio)
    if container.duration is not None:
        whole = Fraction(container.duration, av.time_base)
        if container.start_time is not None:
            start: Fraction | None = Fraction(container.start_time, av.time_base)
        else:
            # コンテナが頭を書いていなければ、道の頭の最小で代える どちらも無いときは
            # 長さの数え始めが分からないので原点を引かない 引くと長さが 0 まで縮み、
            # 素材を置いてもクリップができない
            known = [start for start in map(_stream_start, streams) if start is not None]
            start = min(known) if known else None
        return max(Fraction(0), _relative_end(start, whole, origin))

    longest = Fraction(0)
    for stream in streams:
        if stream.duration is not None and stream.time_base is not None:
            length = Fraction(stream.duration) * stream.time_base
            longest = max(longest, _relative_end(_stream_start(stream), length, origin))
    return longest


def _relative_end(start: Fraction | None, length: Fraction, origin: Fraction) -> Fraction:
    """頭が ``start`` で長さ ``length`` の区間の終わりを、原点から数えた時刻にする

    頭を 0 に丸めてから原点を引くと、負の頭から始まる音（前置き）と正の映像の頭を
    持つ素材で、負の区間の分だけ長くなり、終わりに何も無い区間ができる
    原点が 0 の素材（頭が 0 以下）は、この修正の前と同じく負の頭を 0 に丸める
    丸めないと、B フレームの並べ替えで頭が負になっていた素材の長さがすべて縮む
    頭が分からないときは原点から始まるものと見る 0 から始まるものと見ると、
    原点の分だけ短くなる
    """
    if origin <= 0:
        return max(Fraction(0), start or Fraction(0)) + length
    head = start if start is not None else origin
    return head + length - origin


def _video_info(
    stream: av.video.stream.VideoStream, rotation: int, origin: Fraction
) -> VideoStreamInfo:
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
        end_time=_stream_end(stream, origin),
    )


def _stream_end(stream: av.video.stream.VideoStream, origin: Fraction) -> Fraction | None:
    """映像の道の終わりの時刻（秒） 道が長さを書いていなければ ``None``

    デコーダと同じく素材の原点（:func:`media_origin`）から数える 道の長さは道の頭から
    数えてあるので、道の頭を足してから原点を引く 映像より早く始まる音がある素材では、
    道の長さそのものとは道の頭と原点の差の分だけ違う（映像の原点では差は 0）

    道の頭が分からないときも ``None`` にして素材の長さで見る 0 から始まるものと見て
    原点を引くと、原点の分だけ終わりが早まり、最後のフレームより前から絵が出なくなる
    """
    if stream.duration is None or stream.time_base is None or stream.start_time is None:
        return None
    start = stream.start_time
    end = Fraction(start + stream.duration) * Fraction(stream.time_base) - origin
    return end if end > 0 else None


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
