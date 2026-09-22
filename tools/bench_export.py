r"""書き出しの重さの内訳を測る（Issue #56 の 1 と 2）

    .venv\Scripts\python.exe tools\bench_export.py
    .venv\Scripts\python.exe tools\bench_export.py --layers 3 --frames 60

測るのは 3 つ

1. **1 フレームの内訳** 合成（GPU）／読み戻し（``glReadPixels``）／色変換／
   エンコード／mux を分けて測る どこを並べれば効くのかを、直す前に決めるため
2. **レイヤー数ごとの合成時間** 重ねるとデコードが重さの中心になる、という
   ``engine/cache/proxy.py`` の実測を書き出しの側でも確かめる
3. **PyAV のデコードが GIL を解放するか** 別々の素材を指すデコーダを
   スレッドで並べて、直列と比べる 解放していなければ並列化しても速くならない
   （推測で並列化すると、切り替えの分だけ遅くなる）

``--compare`` を付けると、書き出しそのもの（:func:`export_project`）を
パイプラインの深さを変えて測り、出来上がったファイルのフレームが
一致するかまで見る

ffmpeg が無いか GPU が使えない環境では、何も測らずに終わる（終了コード 0）
"""

from __future__ import annotations

import argparse
import atexit
import io
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 開発者本人の設定やキャッシュに触らない
_base = Path(tempfile.mkdtemp(prefix="sashimono-export-bench-"))
# 終わったら捨てる 素材と書き出したファイルを置くので、回すたびに残すと溜まる
atexit.register(shutil.rmtree, _base, True)
os.environ["APPDATA"] = str(_base / "roaming")
os.environ["LOCALAPPDATA"] = str(_base / "local")

import av  # noqa: E402
import av.video.frame  # noqa: E402
import numpy as np  # noqa: E402
from OpenGL import GL  # noqa: E402

from sashimono.core.commands import AddClip, AddMedia, AddTrack  # noqa: E402
from sashimono.core.model import (  # noqa: E402
    AnimatedValue,
    Clip,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate  # noqa: E402
from sashimono.engine.colorspace import VideoReformatter, tag_bt709, to_bt709  # noqa: E402
from sashimono.engine.decode import VideoDecoder, probe_media  # noqa: E402
from sashimono.engine.encode import (  # noqa: E402
    ExportSettings,
    available_video_codecs,
    export_project,
)
from sashimono.engine.gpu import GLContextError, OffscreenGLContext  # noqa: E402
from sashimono.engine.render import FULL_QUALITY, FrameRenderer  # noqa: E402


def _make_source(
    directory: Path, *, width: int, height: int, seconds: float, tag: str
) -> Path | None:
    """測る用の素材を ffmpeg で作る 動きのある絵にして、圧縮で楽をさせない

    作れなければ ``None`` 使えるコーデックの判定は PyAV のエンコーダを見ているので、
    外の ffmpeg に libx264 が入っているかは別の話 落とさずに、測らずに終える
    """
    path = directory / f"{tag}-{width}x{height}.mp4"
    if path.exists():
        return path
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate=30:duration={seconds}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    # 組み立てているのは固定の文字列と argparse が受けた数値、一時フォルダの中の
    # パスだけで、外から来る文字列は混ざらない shell は通さない（list 渡し）
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    if subprocess.run(command, check=False).returncode != 0:
        return None
    return path


def _project(sources: list[Path], width: int, height: int, frames: int) -> Project:
    """``sources`` を 1 枚ずつ別のトラックへ重ねたプロジェクト"""
    project = Project.create(ProjectSettings(width=width, height=height, frame_rate=FrameRate(30)))
    for index, source in enumerate(sources):
        media = probe_media(source)
        project = AddMedia(media).apply(project)
        track = Track(kind=TrackKind.VIDEO, name=f"V{index + 1}")
        project = AddTrack(track).apply(project)
        clip = Clip(
            timeline_start=0,
            duration=frames,
            media_id=media.id,
            # 重ねた下の絵も描かせる 不透明のままだと、隠れた分を飛ばす作りに
            # なったときに重ねた意味が無くなる
            opacity=AnimatedValue(70.0),
        )
        project = AddClip(track.id, clip).apply(project)
    return project


def _summary(times: list[float]) -> str:
    return f"中央 {statistics.median(times):6.2f} ms  合計 {sum(times):7.1f} ms"


def _measure(step: Callable[[], None], frames: int) -> list[float]:
    """1 回目は温まっていないので捨てる"""
    times: list[float] = []
    for number in range(frames + 1):
        started = time.perf_counter()
        step()
        elapsed = (time.perf_counter() - started) * 1000
        if number:
            times.append(elapsed)
    return times


def breakdown(
    project: Project, context: OffscreenGLContext, codec: str, frames: int, output: Path
) -> dict[str, list[float]]:
    """1 フレームの内訳（ミリ秒） 書き出しと同じ順序で 1 段ずつ測る

    合成のあとに ``glFinish`` を入れる 入れないと命令を投げた時間だけを測り、
    実際の待ちが読み戻しの側へ寄って見える
    """
    renderer = FrameRenderer(project, context=context, quality=FULL_QUALITY)
    width, height = project.settings.resolution
    container = av.open(str(output), mode="w")
    # 書き出しの本体（_FrameWriter）と同じく、swscale の表を使い回す
    # ここだけ毎フレーム作り直すと、直さなくてよい所が重く見える
    reformatter = VideoReformatter()
    times: dict[str, list[float]] = {
        "合成": [],
        "読み戻し": [],
        "色変換": [],
        "エンコード": [],
        "mux": [],
    }
    try:
        stream = container.add_stream(codec, rate=Fraction(30, 1))
        video = stream
        video.width, video.height = width, height
        video.pix_fmt = "yuv420p"
        tag_bt709(video)
        video.bit_rate = 12_000_000
        video.codec_context.open()
        for number in range(frames + 1):
            keep = number > 0
            with context:
                started = time.perf_counter()
                renderer.compose(number % max(project.duration, 1))
                GL.glFinish()
                composed = time.perf_counter()
                image = renderer.compositor.read()
                read = time.perf_counter()
            frame = av.video.frame.VideoFrame.from_ndarray(image, format="rgba")
            frame = to_bt709(frame, "yuv420p", reformatter=reformatter)
            frame.pts = number
            frame.time_base = Fraction(1, 30)
            converted = time.perf_counter()
            packets = video.encode(frame)
            encoded = time.perf_counter()
            container.mux(packets)
            muxed = time.perf_counter()
            if keep:
                times["合成"].append((composed - started) * 1000)
                times["読み戻し"].append((read - composed) * 1000)
                times["色変換"].append((converted - read) * 1000)
                times["エンコード"].append((encoded - converted) * 1000)
                times["mux"].append((muxed - encoded) * 1000)
        container.mux(video.encode(None))
    finally:
        container.close()
        renderer.close()
    return times


def decode_by_layers(
    sources: list[Path], width: int, height: int, frames: int, context: OffscreenGLContext
) -> None:
    """レイヤー数ごとの合成時間 1 枚ずつ増やして、増え方を見る"""
    print("\nレイヤー数ごとの合成（読み戻しを含まない GPU 合成 デコードはこの中）")
    previous = 0.0
    for count in range(1, len(sources) + 1):
        project = _project(sources[:count], width, height, frames + 2)
        renderer = FrameRenderer(project, context=context, quality=FULL_QUALITY)
        number = [0]
        try:

            def step(
                renderer: FrameRenderer = renderer,
                project: Project = project,
                number: list[int] = number,
            ) -> None:
                # 毎回同じフレームを描くと、2 枚目以降がデコーダの手前の絵で済んでしまい
                # デコードの重さが消える 1 枚ずつ進めて実際の書き出しに近づける
                number[0] += 1
                with context:
                    renderer.compose(number[0] % max(project.duration, 1))
                    GL.glFinish()

            times = _measure(step, frames)
        finally:
            renderer.close()
        median = statistics.median(times)
        delta = f"  (+{median - previous:5.2f} ms)" if previous else ""
        print(f"  {count} 枚  {_summary(times)}{delta}")
        previous = median


def gil_release(sources: list[Path], frames: int) -> None:
    """PyAV のデコードが GIL を解放するか 直列と、スレッドで並べた場合を比べる

    解放していなければ並べても合計は変わらない（むしろ切り替えの分だけ増える）
    その場合、レイヤーごとの並列デコードは効かないので入れない
    """
    print("\nPyAV のデコードが GIL を解放するか（別々の素材を同じ枚数だけデコードする）")

    def decode_one(path: Path) -> None:
        with VideoDecoder(path) as decoder:
            for number in range(frames):
                decoder.frame_at(Fraction(number, 30))

    started = time.perf_counter()
    for path in sources:
        decode_one(path)
    serial = (time.perf_counter() - started) * 1000

    threads = [threading.Thread(target=decode_one, args=(path,)) for path in sources]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    parallel = (time.perf_counter() - started) * 1000

    speedup = serial / parallel if parallel else 0.0
    verdict = "解放している（並列化が効く）" if speedup >= 1.3 else "解放していない（効かない）"
    print(
        f"  {len(sources)} 本 × {frames} 枚  直列 {serial:7.1f} ms  "
        f"並列 {parallel:7.1f} ms  {speedup:.2f} 倍  {verdict}"
    )


def _frames_of(path: Path, limit: int) -> list[np.ndarray]:
    frames: list[np.ndarray] = []
    with av.open(str(path)) as container:
        for frame in container.decode(video=0):
            frames.append(frame.to_ndarray(format="rgb24"))
            if len(frames) >= limit:
                break
    return frames


def compare_pipeline(
    project: Project, codec: str, frames: int, directory: Path, depths: list[int]
) -> None:
    """パイプラインの深さを変えて書き出し、時間と中身を比べる"""
    print("\n書き出し全体（パイプラインの深さごと）")
    reference: list[np.ndarray] | None = None
    for depth in depths:
        path = directory / f"out-depth{depth}.mp4"
        settings = ExportSettings(
            path=path,
            video_codec=codec,
            frame_range=(0, frames),
            pipeline_depth=depth,
        )
        started = time.perf_counter()
        export_project(project, settings)
        elapsed = (time.perf_counter() - started) * 1000
        produced = _frames_of(path, frames)
        if reference is None:
            reference = produced
            same = "基準"
        else:
            same = (
                "同じ絵"
                if len(produced) == len(reference)
                and all(np.array_equal(a, b) for a, b in zip(produced, reference, strict=True))
                else "**絵が違う**"
            )
        label = "直列" if depth == 0 else f"深さ {depth}"
        print(f"  {label:<8} {elapsed:8.1f} ms  ({elapsed / frames:6.2f} ms/枚)  {same}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--frames", type=int, default=60, help="測るフレーム数")
    parser.add_argument("--layers", type=int, default=3, help="重ねる素材の枚数")
    parser.add_argument("--codec", default=None, help="使う映像コーデック 既定は先頭の候補")
    parser.add_argument(
        "--compare",
        action="store_true",
        help="書き出しそのものを、パイプラインの深さを変えて測って比べる",
    )
    arguments = parser.parse_args(argv)
    for name in ("width", "height", "frames", "layers"):
        if getattr(arguments, name) <= 0:
            parser.error(f"--{name} は 1 以上にしてください")

    if shutil.which("ffmpeg") is None:
        print("ffmpeg が無いので測れない")
        return 0
    codecs = available_video_codecs()
    if not codecs:
        print("使える映像コーデックが無いので測れない")
        return 0
    codec = arguments.codec or codecs[0]

    seconds = max(2.0, (arguments.frames + 4) / 30)
    # 層ごとに別のファイルにする 同じファイルだとレンダラがデコーダを使い回し、
    # 重ねた分のデコードが 1 回で済んでしまう
    made = [
        _make_source(
            _base, width=arguments.width, height=arguments.height, seconds=seconds, tag=f"s{index}"
        )
        for index in range(arguments.layers)
    ]
    if any(path is None for path in made):
        # 手元の ffmpeg に libx264 が無い（または testsrc2 が無い）ときにここへ来る
        # 落とさずに終える 測れない環境で「壊れた」と読まれないように
        print("ffmpeg で素材を作れないので測れない（libx264 が入っているか見る）")
        return 0
    sources = [path for path in made if path is not None]

    try:
        context = OffscreenGLContext()
    except GLContextError as error:
        print(f"OpenGL コンテキストを作れないので測れない: {error}")
        return 0

    print(
        f"素材 {arguments.width}x{arguments.height} testsrc2 30fps を {arguments.layers} 枚重ね / "
        f"{arguments.frames} 枚 / コーデック {codec}"
    )
    project = _project(sources, arguments.width, arguments.height, arguments.frames + 2)
    try:
        times = breakdown(project, context, codec, arguments.frames, _base / "breakdown.mp4")
        print("\n1 フレームの内訳")
        total = sum(sum(values) for values in times.values())
        for name, values in times.items():
            share = sum(values) / total * 100 if total else 0.0
            print(f"  {name:<8} {_summary(values)}  {share:5.1f} %")
        print(f"  {'合計':<8} {total / arguments.frames:6.2f} ms/枚")
        decode_by_layers(sources, arguments.width, arguments.height, arguments.frames, context)
    finally:
        context.release()

    gil_release(sources, min(arguments.frames, 30))
    if arguments.compare:
        compare_pipeline(project, codec, arguments.frames, _base, [0, 2, 4])
    return 0


if __name__ == "__main__":
    sys.exit(main())
