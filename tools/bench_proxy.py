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

from OpenGL import GL  # noqa: E402

from kumiki.core.commands import AddClip, AddMedia, AddTrack  # noqa: E402
from kumiki.core.model import (  # noqa: E402
    AnimatedValue,
    Clip,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from kumiki.core.timebase import FrameRate  # noqa: E402
from kumiki.effects.definition import registry  # noqa: E402
from kumiki.engine.cache.proxy import (  # noqa: E402
    PROXY_HEIGHT,
    ProxyStore,
    create_proxy,
    proxy_codecs,
)
from kumiki.engine.cache.store import CacheStore  # noqa: E402
from kumiki.engine.decode import probe_media  # noqa: E402
from kumiki.engine.gpu import (  # noqa: E402
    Framebuffer,
    GLContextError,
    OffscreenGLContext,
)
from kumiki.engine.render import FrameRenderer, RenderQuality  # noqa: E402

#: 60fps の 1 コマ（ミリ秒） 4K のプレビューの目標
BUDGET_MS = 1000 / 60

#: 画面へ出す先の大きさ プレビューの枠は画面の実寸で、素材の大きさではない
PREVIEW_WIDTH, PREVIEW_HEIGHT = 1920, 1080


def _make_source(directory: Path, *, width: int, height: int, seconds: float) -> Path | None:
    """測る用の素材を ffmpeg で作る 動きのある絵にして、圧縮で楽をさせない"""
    path = directory / f"{width}x{height}.mp4"
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


def _project(
    source: Path,
    width: int,
    height: int,
    frames: int,
    *,
    layers: int = 1,
    effects: tuple[str, ...] = (),
) -> Project:
    """測る対象のプロジェクト

    重ねる枚数と積むエフェクトを変えられる 1 枚だけの絵は一番軽い場合で、
    実際のタイムラインは何枚も重なる そこを測らないと、軽い場合だけを見て
    「余裕がある」と決めることになる
    """
    project = Project.create(ProjectSettings(width=width, height=height, frame_rate=FrameRate(30)))
    for index in range(max(1, layers)):
        # 層ごとに別の素材として登録する レンダラはデコーダを
        # （素材, ストリーム）で使い回すので、同じ素材を重ねると
        # デコードが 1 回で済んでしまい、重ねた分の重さが出ない
        # （同じファイルでも probe のたびに別の素材として扱われる）
        media = probe_media(source)
        project = AddMedia(media).apply(project)
        track = Track(kind=TrackKind.VIDEO, name=f"V{index + 1}")
        project = AddTrack(track).apply(project)
        clip = Clip(
            timeline_start=0,
            duration=frames,
            media_id=media.id,
            # 重ねた下の絵も描かせる 不透明のままだと、上の 1 枚で隠れた分を
            # 飛ばす作りになったときに重ねた意味が無くなる
            opacity=AnimatedValue(70.0),
            effects=tuple(registry.require(name).create() for name in effects),
        )
        project = AddClip(track.id, clip).apply(project)
    return project


def _measure(
    project: Project,
    context: OffscreenGLContext,
    frames: int,
    proxies: ProxyStore | None,
    divisor: int = 1,
) -> list[float]:
    """1 フレームずつ描いて、かかった時間（ミリ秒）を返す

    測るのは**プレビューが通るのと同じ 2 つ** 合成（:meth:`compose`）と、
    その結果を画面へ出す所（:meth:`Compositor.present`）
    :meth:`render` は最後に GPU から CPU へ読み戻すので、プレビューには
    無い時間まで数えることになる

    出す先は**自前で用意した 1920x1080 の描画先** 画面の実寸に近い値で、
    素材の大きさではない オフスクリーンの既定の描画先（0 番）へ出すと、
    その大きさが環境任せになり、転送の重さを測ったことにならない

    GL の命令は投げただけでは終わっていない 1 枚ごとに ``glFinish`` で
    終わりを待つ 待たないと、投げるのに掛かった時間を測るだけになる
    """
    renderer = FrameRenderer(
        project, context=context, quality=RenderQuality(divisor), proxies=proxies
    )
    try:
        with context:
            screen = Framebuffer(PREVIEW_WIDTH, PREVIEW_HEIGHT, internal_format=GL.GL_RGBA8)
            viewport = (0, 0, PREVIEW_WIDTH, PREVIEW_HEIGHT)
            try:
                # 最初の 1 枚はデコーダを開く分と、シェーダを組む分を含む 外す
                renderer.compose(0)
                renderer.compositor.present(screen.handle, viewport)
                GL.glFinish()
                times: list[float] = []
                for frame in range(1, frames):
                    start = time.perf_counter()
                    renderer.compose(frame)
                    renderer.compositor.present(screen.handle, viewport)
                    GL.glFinish()
                    times.append((time.perf_counter() - start) * 1000)
            finally:
                screen.release()
        return times
    finally:
        renderer.close()


def _detail(height: int, proxy_height: int, proxies: ProxyStore | None, divisor: int) -> int:
    """その組で画面に残る、縦の画素の細かさ

    読む元と描く先の**小さい方**で決まる 控えを使えば元がそこまで落ち、
    画質を下げれば描く先がそこまで落ちる 細かい方がきれい
    """
    source = proxy_height if proxies is not None else height
    return min(source, max(1, height // divisor))


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
    parser.add_argument("--layers", type=int, default=1, help="重ねる枚数")
    parser.add_argument(
        "--effects", default="", help="積むエフェクト（コンマ区切り 例: blur,glow）"
    )
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
    if shutil.which("ffmpeg") is None:
        print("ffmpeg が無い 測らずに終わる")
        return 0
    source = _make_source(directory, width=args.width, height=args.height, seconds=args.seconds)
    if source is None:
        # 作れないのは環境の都合ではなく、指定か ffmpeg の側の問題
        # 素通り（終了コード 0）にすると、測れていないのに通ったように見える
        print(f"測る素材を作れなかった {args.width}x{args.height} {args.seconds} 秒")
        return 1

    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        print(f"OpenGL コンテキストを作れない 測らずに終わる: {exc}")
        return 0

    effects = tuple(name for name in args.effects.split(",") if name)
    try:
        project = _project(
            source, args.width, args.height, frames, layers=args.layers, effects=effects
        )
    except KeyError as exc:
        print(f"知らないエフェクト: {exc}")
        context.release()
        return 1
    store = ProxyStore(CacheStore(_base / "cache"), height=args.proxy_height)
    media = project.media[0]

    piled = f" {args.layers} 枚重ね" if args.layers > 1 else ""
    stacked = f" + {'/'.join(effects)}" if effects else ""
    print(f"{args.width}x{args.height}{piled}{stacked} を {frames} フレーム 予算 {BUDGET_MS:.1f}ms")
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
    # きれいな順は**残る画素の細かさ**から出す 並べた順を手で決めると、
    # 「控え 540p の等倍」を「元の素材の 1/2」より上に置くような取り違えが起きる
    # （前者に残るのは 540 本、後者は 1080 本）
    combinations = sorted(
        (
            ("元の素材 + 等倍", None, 1),
            ("元の素材 + 1/2", None, 2),
            (f"控え {args.proxy_height}p + 等倍", store, 1),
            (f"控え {args.proxy_height}p + 1/2", store, 2),
            (f"控え {args.proxy_height}p + 1/4", store, 4),
        ),
        key=lambda item: -_detail(args.height, args.proxy_height, item[1], item[2]),
    )
    try:
        results = [
            (label, divisor, _report(label, _measure(project, context, frames, proxies, divisor)))
            for label, proxies, divisor in combinations
        ]
    finally:
        context.release()

    within = [item for item in results if item[2] <= BUDGET_MS]
    if not within:
        fastest = min(results, key=lambda item: item[2])
        print(f"  どの組でも予算に入らない 一番速いのは {fastest[0]}（{fastest[2]:.1f}ms）")
        return 1
    # きれいな順は**並べた順**で決める 掛かった時間から推し量ると、
    # ばらついたときに粗い組を「一番きれい」と呼ぶことになる
    label, _, value = within[0]
    print(f"  予算に入る一番きれいな組: {label}（{value:.1f}ms）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
