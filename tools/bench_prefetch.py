r"""プレビューの先読みが画面をどれだけ止めるかを測る（Issue #56 の 3）

    .venv\Scripts\python.exe tools\bench_prefetch.py
    .venv\Scripts\python.exe tools\bench_prefetch.py --width 3840 --height 2160 --layers 2

測るのは 2 つ

1. **先読みの 1 コマが呼び出し元を止める長さ** ``PreviewCache.step`` を 1 コマずつ呼び、
   呼んでから戻るまで（GL の命令を投げ終えるまで）と、``glFinish`` で GPU の終わりまで
   待った長さを分けて出す 画面のスレッドで貯めると、前者の間イベントループが止まる
2. **本物のプレビュー窓** :class:`PreviewWidget` を画面へ出さずに作り
   （``WA_DontShowOnScreen``）、5ms おきの見張りのタイマーがどれだけ遅れて届くか
   （＝その間は操作を受け付けられなかった）を測る 先読みなし・画面のスレッドで先読み・
   別のスレッドで先読み の 3 つを、貯める間と、再生ヘッドを 30fps で送る間とで比べる
   送る間は、``set_frame`` から描き終わる（``glFinish``）までの長さも出す

素材は ffmpeg の ``testsrc2`` を libx264 で焼いた mp4 を、層ごとに別ファイルで重ねた物
（不透明度 0.7） 本人の素材は使わない

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
import time
from dataclasses import dataclass, field
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 開発者本人の設定やキャッシュに触らない
_base = Path(tempfile.mkdtemp(prefix="sashimono-prefetch-bench-"))
# 終わったら捨てる 4K の素材は 1 本数十 MB あり、回すたびに残すと溜まる
atexit.register(shutil.rmtree, _base, True)
os.environ["APPDATA"] = str(_base / "roaming")
os.environ["LOCALAPPDATA"] = str(_base / "local")

from OpenGL import GL  # noqa: E402
from PySide6.QtCore import QElapsedTimer, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QSurfaceFormat  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sashimono.core.commands import AddClip, AddEffect, AddMedia, AddTrack  # noqa: E402
from sashimono.core.model import (  # noqa: E402
    AnimatedValue,
    Clip,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate  # noqa: E402
from sashimono.effects.definition import registry  # noqa: E402
from sashimono.engine.decode import probe_media  # noqa: E402
from sashimono.engine.gpu import (  # noqa: E402
    GLContextError,
    OffscreenGLContext,
    preferred_surface_format,
)
from sashimono.engine.render import FrameRenderer, PreviewCache, RenderQuality  # noqa: E402
from sashimono.engine.render.prefetch import BYTES_PER_FRAME_PIXEL  # noqa: E402

#: 見張りのタイマーの間隔（ミリ秒） 届くのがこれより遅れた分だけ、操作を受け付けられなかった
PROBE_MS = 5
#: 再生ヘッドを送る間隔（ミリ秒） 30fps でつまみを送ったときの画面の書き換え
ADVANCE_MS = 33


def _make_source(directory: Path, *, width: int, height: int, seconds: float, tag: str) -> Path:
    """測る用の素材を ffmpeg で作る 動きのある絵にして、圧縮で楽をさせない"""
    path = directory / f"{tag}-{width}x{height}.mp4"
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
    subprocess.run(command, check=True)
    return path


def _project(sources: list[Path], width: int, height: int, frames: int, effects: int) -> Project:
    """``sources`` を 1 枚ずつ別のトラックへ重ね、それぞれへエフェクトを ``effects`` 個積む"""
    project = Project.create(ProjectSettings(width=width, height=height, frame_rate=FrameRate(30)))
    blur = registry.get("blur")
    glow = registry.get("glow")
    assert blur is not None and glow is not None
    for index, source in enumerate(sources):
        media = probe_media(source)
        project = AddMedia(media).apply(project)
        track = Track(kind=TrackKind.VIDEO, name=f"V{index + 1}")
        project = AddTrack(track).apply(project)
        # 不透明度は 0.0 から 1.0 1 のままだと下の層を描く意味が無くなる
        clip = Clip(
            timeline_start=0, duration=frames, media_id=media.id, opacity=AnimatedValue(0.7)
        )
        project = AddClip(track.id, clip).apply(project)
        for number in range(effects):
            definition = blur if number % 2 == 0 else glow
            project = AddEffect(clip.id, definition.create()).apply(project)
    return project


def _describe(times: list[float]) -> str:
    if not times:
        return "（標本なし）"
    ordered = sorted(times)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return (
        f"中央 {statistics.median(ordered):7.2f}  95% {p95:7.2f}  最大 {ordered[-1]:7.2f} ms"
        f"  （{len(ordered)} 回）"
    )


def measure_steps(project: Project, divisor: int, budget: int, steps: int) -> None:
    """先読みの 1 コマが呼び出し元を止める長さ 1 回目は温まっていないので捨てる"""
    context = OffscreenGLContext()
    renderer = FrameRenderer(project, context=context, quality=RenderQuality(divisor))
    blocking: list[float] = []
    finished: list[float] = []
    try:
        with context:
            cache = PreviewCache(renderer, budget_bytes=budget)
            try:
                for number in range(steps + 1):
                    started = time.perf_counter()
                    if not cache.step(0):
                        break
                    returned = time.perf_counter()
                    GL.glFinish()
                    done = time.perf_counter()
                    if number:
                        blocking.append((returned - started) * 1000)
                        finished.append((done - started) * 1000)
            finally:
                cache.release()
    finally:
        renderer.close()
        context.release()
    print(f"    step が戻るまで        {_describe(blocking)}")
    print(f"    GPU の終わりまで        {_describe(finished)}")


class _Lateness:
    """5ms おきの見張りのタイマーが、どれだけ遅れて届いたか

    遅れた分だけ、その間は操作（クリック・ドラッグ・キー）を受け付けられなかった
    """

    def __init__(self) -> None:
        self.samples: list[float] = []
        self._clock = QElapsedTimer()
        self._clock.start()
        self._last = self._clock.nsecsElapsed()
        self._timer = QTimer()
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(PROBE_MS)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self.samples = []
        self._last = self._clock.nsecsElapsed()
        self._timer.start()

    def stop(self) -> list[float]:
        self._timer.stop()
        return self.samples

    def _tick(self) -> None:
        now = self._clock.nsecsElapsed()
        self.samples.append(max(0.0, (now - self._last) / 1e6 - PROBE_MS))
        self._last = now


@dataclass
class WidgetResult:
    """プレビュー窓で測った値"""

    #: 貯めている間の見張りの遅れ（ミリ秒）
    filling: list[float] = field(default_factory=list)
    #: 貯め終わるまでの秒数と、貯まった枚数
    fill_seconds: float = 0.0
    filled: int = 0
    #: 頭から再生して、貯めた所の端を越えるまでの 1 コマごとの描き終わり（ミリ秒）
    #: 端を越えた最初のコマは、画面の側のデコーダがそこまで読み進める
    playback: list[float] = field(default_factory=list)
    #: 再生ヘッドを 30fps で送っている間の見張りの遅れ（ミリ秒）
    scrubbing: list[float] = field(default_factory=list)
    #: 再生ヘッドを送ってから、その絵を描き終えるまで（ミリ秒）
    frame_times: list[float] = field(default_factory=list)
    #: 別のスレッドで貯めていたか（作れなければ画面のスレッドへ戻る）
    background: bool = False


def _wait(application: QApplication, seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        application.processEvents()


def _fill(application: QApplication, widget: object, limit: float) -> tuple[float, int]:
    """貯まる枚数が 0.5 秒増えなくなるまで回す 掛かった秒数と枚数を返す"""
    started = time.perf_counter()
    count, changed = 0, started
    while time.perf_counter() - started < limit:
        application.processEvents()
        now = time.perf_counter()
        current = len(widget.cached_frames)  # type: ignore[attr-defined]
        if current != count:
            count, changed = current, now
        elif count and now - changed > 0.5:
            # 1 枚目までは待つ 走り係はレンダラとシェーダを作ってから描くので、
            # 4K に効果を積むと 1 枚目まで 0.5 秒を超える
            break
    return changed - started, count


def measure_widget(
    application: QApplication,
    project: Project,
    divisor: int,
    budget: int,
    seconds: float,
    *,
    thread: bool,
) -> WidgetResult:
    """本物のプレビュー窓で、イベントループの遅れと 1 コマの描き直しを測る"""
    from sashimono.ui.preview import PreviewWidget

    result = WidgetResult()
    widget = PreviewWidget(project, prefetch_bytes=budget, prefetch_thread=thread)
    widget.set_quality(RenderQuality(divisor))
    # 画面へは出さない 本人の画面に窓を出したり、マウスを取り合ったりしない
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.resize(960, 540)
    widget.show()
    deadline = time.perf_counter() + 10
    while widget.renderer is None and time.perf_counter() < deadline:
        application.processEvents()
    if widget.renderer is None:
        widget.shutdown()
        raise GLContextError("プレビュー窓の GL を作れない")
    # 1 枚目を描かせてから測る デコーダを開く重さを混ぜない
    widget.repaint()
    _wait(application, 0.2)
    try:
        probe = _Lateness()
        probe.start()
        widget.set_frame(1)
        if budget:
            result.fill_seconds, result.filled = _fill(application, widget, seconds * 4)
        else:
            _wait(application, seconds)
        result.filling = probe.stop()
        result.background = widget.prefetch_in_background

        clock = QElapsedTimer()
        clock.start()

        def show(frame: int, into: list[float]) -> None:
            asked = clock.nsecsElapsed()
            widget.set_frame(frame)
            widget.repaint()
            # 画面へ出す命令が GPU で終わるまで待つ 待たないと投げた時間しか測れない
            widget.makeCurrent()
            GL.glFinish()
            widget.doneCurrent()
            into.append((clock.nsecsElapsed() - asked) / 1e6)

        # 再生 頭から、貯めた所の端を 20 コマ越えるまで 再生中は先読みを止める
        if budget:
            widget.set_playing(True)
            for frame in range(1, result.filled + 20):
                began = time.perf_counter()
                show(frame, result.playback)
                _wait(application, max(0.0, ADVANCE_MS / 1000 - (time.perf_counter() - began)))
            widget.set_playing(False)

        # 貯めた所の外から送る 当たると描き直しは 1ms で済み、
        # 画面のスレッドで描く重さが見えない
        position = [result.filled + 40]

        def advance() -> None:
            position[0] += 1
            show(position[0], result.frame_times)

        mover = QTimer()
        mover.setTimerType(Qt.TimerType.PreciseTimer)
        mover.setInterval(ADVANCE_MS)
        mover.timeout.connect(advance)
        probe.start()
        mover.start()
        _wait(application, seconds)
        mover.stop()
        result.scrubbing = probe.stop()
    finally:
        widget.shutdown()
        widget.deleteLater()
        application.processEvents()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--layers", type=int, default=3, help="重ねる素材の枚数")
    parser.add_argument("--effects", type=int, default=0, help="1 層あたりのエフェクト数")
    parser.add_argument("--divisor", type=int, default=1, help="プレビューの画質の分母")
    parser.add_argument("--budget-mb", type=int, default=1024, help="先読みに使うメモリ")
    parser.add_argument("--steps", type=int, default=60, help="1 で測る先読みのコマ数")
    parser.add_argument("--seconds", type=float, default=4.0, help="2 で送る間を測る秒数")
    arguments = parser.parse_args(argv)
    for name in ("width", "height", "layers", "divisor", "budget_mb", "steps"):
        if getattr(arguments, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} は 1 以上にしてください")
    if arguments.effects < 0 or arguments.seconds <= 0:
        parser.error("--effects は 0 以上、--seconds は 0 より大きくしてください")

    if shutil.which("ffmpeg") is None:
        print("ffmpeg が無いので測れない")
        return 0
    QSurfaceFormat.setDefaultFormat(preferred_surface_format())
    existing = QApplication.instance()
    application = existing if isinstance(existing, QApplication) else QApplication([])
    try:
        OffscreenGLContext().release()
    except GLContextError as error:
        print(f"OpenGL コンテキストを作れないので測れない: {error}")
        return 0

    budget = arguments.budget_mb * 1024 * 1024
    width, height = RenderQuality(arguments.divisor).apply(arguments.width, arguments.height)
    capacity = budget // (width * height * BYTES_PER_FRAME_PIXEL)
    # 貯める所・再生で越える端・その外で送る所・余り が入る長さ
    frames = max(arguments.steps + 2, capacity + 60 + int(arguments.seconds * 30) + 30)
    try:
        sources = [
            _make_source(
                _base,
                width=arguments.width,
                height=arguments.height,
                seconds=frames / 30 + 1,
                tag=f"s{index}",
            )
            for index in range(arguments.layers)
        ]
    except subprocess.CalledProcessError:
        print("ffmpeg で素材を作れないので測れない（libx264 が入っているか見る）")
        return 0
    project = _project(sources, arguments.width, arguments.height, frames, arguments.effects)
    print(
        f"素材 {arguments.width}x{arguments.height} testsrc2 を {arguments.layers} 枚重ね"
        f" / エフェクト {arguments.effects} 個ずつ / 画質 1/{arguments.divisor}"
        f" / 先読み {arguments.budget_mb} MB（{capacity} 枚）"
    )

    print("\n1 先読みの 1 コマ（PreviewCache.step）")
    measure_steps(project, arguments.divisor, budget, arguments.steps)

    print("\n2 プレビュー窓（見張りの遅れ＝操作を受け付けられなかった長さ）")
    conditions: list[tuple[str, int, bool]] = [
        ("先読みなし", 0, False),
        ("画面のスレッドで先読み", budget, False),
        ("別のスレッドで先読み", budget, True),
    ]
    for name, spent, thread in conditions:
        result = measure_widget(
            application, project, arguments.divisor, spent, arguments.seconds, thread=thread
        )
        if thread and not result.background:
            name += "（作れず画面のスレッドへ戻った）"
        print(f"  {name}")
        if spent:
            print(f"    貯める間の遅れ          {_describe(result.filling)}")
            print(f"    貯まるまで              {result.fill_seconds:.2f} 秒で {result.filled} 枚")
            inside = result.playback[: max(0, result.filled - 1)]
            edge = result.playback[max(0, result.filled - 1) :]
            print(f"    再生 貯めた所          {_describe(inside)}")
            print(f"    再生 端を越えた所      {_describe(edge)}")
        print(f"    送る間の遅れ            {_describe(result.scrubbing)}")
        print(f"    送って描き終わるまで    {_describe(result.frame_times)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
