r"""4K の素材を、控えあり／なしで描いて速さを比べる

    .venv\Scripts\python.exe tools\bench_proxy.py
    .venv\Scripts\python.exe tools\bench_proxy.py --seconds 5 --height 540

済んだと言える条件「4K の素材でプレビューが 60fps に収まる」を測る側
1 フレーム 16.6ms が予算 測らずに直さない、の測る側

ffmpeg が無いか GPU が使えない環境では、何も測らずに終わる（終了コード 0）
"""

from __future__ import annotations

import argparse
import atexit
import io
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 開発者本人の設定やキャッシュに触らない
_base = Path(tempfile.mkdtemp(prefix="kumiki-proxy-bench-"))
# 終わったら捨てる 4K の素材と控えを置くので、回すたびに残すと GB 単位で溜まる
atexit.register(shutil.rmtree, _base, True)
os.environ["APPDATA"] = str(_base / "roaming")
os.environ["LOCALAPPDATA"] = str(_base / "local")

from kumiki.core.commands import AddClip, AddMedia, AddTrack  # noqa: E402
from kumiki.core.model import Clip, Project, ProjectSettings, Track, TrackKind  # noqa: E402
from kumiki.core.timebase import FrameRate  # noqa: E402
from kumiki.engine.cache.proxy import (  # noqa: E402
    PROXY_HEIGHT,
    ProxyStore,
    create_proxy,
    proxy_codecs,
)
from kumiki.engine.cache.store import CacheStore  # noqa: E402
from kumiki.engine.decode import probe_media  # noqa: E402
from kumiki.engine.gpu import GLContextError, OffscreenGLContext  # noqa: E402
from kumiki.engine.render import FrameRenderer, RenderQuality  # noqa: E402

#: 60fps の 1 コマ（ミリ秒） 4K のプレビューの目標
BUDGET_MS = 1000 / 60


def _make_source(directory: Path, *, width: int, height: int, seconds: float) -> Path | None:
    """測る用の素材を ffmpeg で作る 動きのある絵にして、圧縮で楽をさせない"""
    path = directory / f"{width}x{height}.mp4"
    if path.exists():
        return path
    if shutil.which("ffmpeg") is None:
        return None
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


def _project(source: Path, width: int, height: int, frames: int) -> Project:
    media = probe_media(source)
    project = Project.create(ProjectSettings(width=width, height=height, frame_rate=FrameRate(30)))
    project = AddMedia(media).apply(project)
    track = Track(kind=TrackKind.VIDEO, name="V1")
    project = AddTrack(track).apply(project)
    clip = Clip(timeline_start=0, duration=frames, media_id=media.id)
    return AddClip(track.id, clip).apply(project)


def _measure(
    project: Project,
    context: OffscreenGLContext,
    frames: int,
    proxies: ProxyStore | None,
    divisor: int = 1,
) -> list[float]:
    """1 フレームずつ描いて、かかった時間（ミリ秒）を返す"""
    renderer = FrameRenderer(
        project, context=context, quality=RenderQuality(divisor), proxies=proxies
    )
    try:
        # 最初の 1 枚はデコーダを開く分を含む 測る対象から外す
        renderer.render(0)
        times: list[float] = []
        for frame in range(1, frames):
            start = time.perf_counter()
            renderer.render(frame)
            times.append((time.perf_counter() - start) * 1000)
        return times
    finally:
        renderer.close()


def _report(label: str, times: list[float]) -> float:
    ordered = sorted(times)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    median = statistics.median(ordered)
    mark = "○" if p95 <= BUDGET_MS else "×"
    print(f"  {mark} {label}: 中央 {median:6.1f}ms  95% {p95:6.1f}ms")
    return p95


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=3840)
    parser.add_argument("--height", type=int, default=2160)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--proxy-height", type=int, default=PROXY_HEIGHT)
    args = parser.parse_args()

    if not math.isfinite(args.seconds):
        # nan と inf は int() が投げる 例外で終わると、測れない環境の
        # 素通り（終了コード 0）と区別が付かない
        print(f"--seconds が数ではない: {args.seconds}")
        return 1

    frames = int(args.seconds * 30)
    if frames < 2:
        # 1 枚目は開く分を含むので測らない 2 枚目が無いと何も測れない
        # 素材を作る前に見る 負の秒数では ffmpeg が先に失敗して、
        # 「この環境では測れない」と区別が付かなくなる
        print(f"--seconds が短すぎる 2 フレーム以上になる長さを指定する（{frames} フレーム）")
        return 1

    if not proxy_codecs():
        print("控えを作れるコーデックが無い 測らずに終わる")
        return 0

    directory = _base / "素材"
    directory.mkdir(parents=True, exist_ok=True)
    source = _make_source(directory, width=args.width, height=args.height, seconds=args.seconds)
    if source is None:
        print("ffmpeg が無い 測らずに終わる")
        return 0

    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        print(f"OpenGL コンテキストを作れない 測らずに終わる: {exc}")
        return 0

    project = _project(source, args.width, args.height, frames)
    store = ProxyStore(CacheStore(_base / "cache"), height=args.proxy_height)
    media = project.media[0]

    print(f"{args.width}x{args.height} を {frames} フレーム 予算 {BUDGET_MS:.1f}ms")
    started = time.perf_counter()
    made = create_proxy(source, store.prepare(media), height=args.proxy_height)
    building = time.perf_counter() - started
    if made is None:
        print("控えを作れなかった")
        context.release()
        return 1
    print(f"  控えを作るのに {building:.1f} 秒（{args.seconds:.0f} 秒の素材）")

    # 控えだけでは足りない デコードは軽くなるが、合成は画面の大きさのまま
    # 画面の側も落とす RenderQuality と組で測る
    combinations = (
        ("元の素材 + 等倍", None, 1),
        ("元の素材 + 1/2", None, 2),
        (f"控え {args.proxy_height}p + 等倍", store, 1),
        (f"控え {args.proxy_height}p + 1/2", store, 2),
        (f"控え {args.proxy_height}p + 1/4", store, 4),
    )
    try:
        results = [
            (label, _report(label, _measure(project, context, frames, proxies, divisor)))
            for label, proxies, divisor in combinations
        ]
    finally:
        context.release()

    best = min(results, key=lambda item: item[1])
    if best[1] > BUDGET_MS:
        print(f"  どの組でも予算に入らない 一番速いのは {best[0]}（{best[1]:.1f}ms）")
        return 1
    print(f"  予算に入る一番重い組: {_cheapest(results)}")
    return 0


def _cheapest(results: list[tuple[str, float]]) -> str:
    """予算に収まるもののうち、**一番きれいな**（＝一番遅い）組"""
    within = [item for item in results if item[1] <= BUDGET_MS]
    label, value = max(within, key=lambda item: item[1])
    return f"{label}（{value:.1f}ms）"


if __name__ == "__main__":
    raise SystemExit(main())
