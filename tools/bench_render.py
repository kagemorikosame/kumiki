r"""1 フレームの合成の速さを測る

    .venv\Scripts\python.exe tools\bench_render.py
    .venv\Scripts\python.exe tools\bench_render.py --width 3840 --height 2160 --depth 6

性能目標「1080p のプレビューが 30fps」（1 フレーム 33.3ms 以内）と、
深く入れ子にしたシーンや大量のクリップでどこまで持つかを見る 測らずに直さない、の測る側

GPU が無い環境では作れないので、何も測らずに終わる（終了コード 0）
"""

from __future__ import annotations

import argparse
import io
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 開発者本人の設定やキャッシュに触らない
_base = Path(tempfile.mkdtemp(prefix="kumiki-bench-"))
os.environ["APPDATA"] = str(_base / "roaming")
os.environ["LOCALAPPDATA"] = str(_base / "local")

from kumiki.core.commands import (  # noqa: E402
    AddClip,
    AddEffect,
    AddScene,
    AddTrack,
    Command,
    InScene,
    insert_scene,
    new_scene,
)
from kumiki.core.model import (  # noqa: E402
    AnimatedValue,
    Clip,
    GeneratedSource,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from kumiki.core.timebase import FrameRate  # noqa: E402
from kumiki.effects.definition import registry  # noqa: E402
from kumiki.effects.sources import TEXT  # noqa: E402
from kumiki.engine.gpu import GLContextError, OffscreenGLContext  # noqa: E402
from kumiki.engine.render import FrameRenderer  # noqa: E402

#: 30fps の 1 コマ（ミリ秒） プレビューの目標
BUDGET_MS = 1000 / 30


def _shape(colour: tuple[float, float, float, float], size: float) -> GeneratedSource:
    return GeneratedSource(
        kind="shape",
        params={
            "shape": "rect",
            "color": colour,
            "width": AnimatedValue(size),
            "height": AnimatedValue(size),
        },
    )


def _apply(project: Project, commands: list[Command]) -> Project:
    for command in commands:
        project = command.apply(project)
    return project


def nested_project(settings: ProjectSettings, depth: int, length: int) -> Project:
    """シーンを ``depth`` 段の入れ子にしたプロジェクト 一番奥に図形を 1 つ置く"""
    project = Project.create(settings)
    scenes = []
    for level in range(depth):
        scene = new_scene(project, f"階層{level + 1}")
        project = AddScene(scene).apply(project)
        track = Track(TrackKind.VIDEO, f"S{level + 1}")
        project = InScene(scene.id, AddTrack(track)).apply(project)
        scenes.append((scene.id, track.id))

    innermost = Clip(timeline_start=0, duration=length, source=_shape((1.0, 1.0, 1.0, 1.0), 200))
    scene_id, track_id = scenes[-1]
    project = InScene(scene_id, AddClip(track_id, innermost)).apply(project)
    # 奥から手前へ、1 段ずつ親のシーンへ置いていく
    for (outer_id, outer_track), (inner_id, _) in zip(
        reversed(scenes[:-1]), reversed(scenes[1:]), strict=True
    ):
        placed = Clip(timeline_start=0, duration=length, scene_id=inner_id)
        project = InScene(outer_id, AddClip(outer_track, placed)).apply(project)
    return _apply(project, insert_scene(project, scenes[0][0], at_frame=0, duration=length))


def busy_project(settings: ProjectSettings, tracks: int, effects: int, length: int) -> Project:
    """トラックを ``tracks`` 本重ね、それぞれのクリップへエフェクトを ``effects`` 個積む"""
    project = Project.create(settings)
    blur = registry.get("blur")
    glow = registry.get("glow")
    assert blur is not None and glow is not None
    for index in range(tracks):
        track = Track(TrackKind.VIDEO, f"V{index + 1}")
        project = AddTrack(track).apply(project)
        clip = Clip(
            timeline_start=0,
            duration=length,
            source=_shape((0.2 + index * 0.05, 0.4, 0.9, 1.0), 200 + index * 20),
        )
        project = AddClip(track.id, clip).apply(project)
        for number in range(effects):
            definition = blur if number % 2 == 0 else glow
            project = AddEffect(clip.id, definition.create()).apply(project)
    return project


def text_project(settings: ProjectSettings, count: int, length: int) -> Project:
    """テキストを ``count`` 本重ねたプロジェクト 文字は毎フレーム描き直される"""
    project = Project.create(settings)
    for index in range(count):
        track = Track(TrackKind.VIDEO, f"T{index + 1}")
        project = AddTrack(track).apply(project)
        clip = Clip(
            timeline_start=0,
            duration=length,
            source=TEXT.create(text=f"字幕 {index + 1}", size=48, pos_y=index * 30 - 200),
        )
        project = AddClip(track.id, clip).apply(project)
    return project


def measure(project: Project, context: OffscreenGLContext, frames: int) -> list[float]:
    """1 フレームを合成する時間（ミリ秒） 1 回目は温まっていないので捨てる"""
    renderer = FrameRenderer(project, context=context)
    times: list[float] = []
    try:
        for frame in range(frames + 1):
            started = time.perf_counter()
            with context:
                # 読み出しまで測る GPU は非同期なので、compose だけだと命令を積んだ時間になる
                renderer.render(frame % max(project.duration, 1))
            elapsed = (time.perf_counter() - started) * 1000
            if frame:
                times.append(elapsed)
    finally:
        renderer.close()
    return times


def report(name: str, times: list[float]) -> bool:
    worst = max(times)
    p95 = statistics.quantiles(times, n=20)[18] if len(times) >= 20 else worst
    ok = p95 <= BUDGET_MS
    print(
        f"{name:<24} 中央 {statistics.median(times):7.2f} ms  95% {p95:7.2f} ms  "
        f"最悪 {worst:7.2f} ms  {'OK' if ok else '予算超え'}"
    )
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--depth", type=int, default=6, help="シーンの入れ子の深さ")
    parser.add_argument("--tracks", type=int, default=20, help="重ねるトラックの数")
    parser.add_argument("--effects", type=int, default=2, help="1 クリップのエフェクト数")
    parser.add_argument("--texts", type=int, default=20, help="重ねるテキストの数")
    parser.add_argument("--frames", type=int, default=20, help="測るフレーム数")
    arguments = parser.parse_args(argv)

    settings = ProjectSettings(
        width=arguments.width, height=arguments.height, frame_rate=FrameRate(30)
    )
    length = max(arguments.frames + 1, 30)
    try:
        context = OffscreenGLContext()
    except GLContextError as error:
        print(f"OpenGL コンテキストを作れないので測れない: {error}")
        return 0

    print(f"画面 {arguments.width}x{arguments.height} / 目標 {BUDGET_MS:.1f} ms")
    passed = True
    try:
        for depth in range(1, arguments.depth + 1):
            project = nested_project(settings, depth, length)
            passed &= report(f"シーン {depth} 段", measure(project, context, arguments.frames))
        project = busy_project(settings, arguments.tracks, arguments.effects, length)
        passed &= report(
            f"{arguments.tracks} 本 × エフェクト {arguments.effects}",
            measure(project, context, arguments.frames),
        )
        project = text_project(settings, arguments.texts, length)
        passed &= report(
            f"テキスト {arguments.texts} 本", measure(project, context, arguments.frames)
        )
    finally:
        context.release()
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
