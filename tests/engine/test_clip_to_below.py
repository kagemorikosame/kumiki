"""すぐ下のクリップの形で切り抜く（YMM4 の「上のオブジェクトでクリッピング」）

吹き出しの形だけに模様を見せる配布テンプレートが使っている 切り抜けないと、模様の
背景が画面全体を覆う
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import numpy as np
import pytest

from sashimono.core.io import project_from_dict, project_to_dict
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    GeneratedSource,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer

SETTINGS = ProjectSettings(width=64, height=64, frame_rate=FrameRate(30))


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


def _shape(color: tuple[float, float, float, float], size: float | None) -> GeneratedSource:
    if size is None:
        return GeneratedSource(kind="shape", params={"shape": "background", "color": color})
    return GeneratedSource(
        kind="shape",
        params={
            "shape": "rect",
            "color": color,
            "width": AnimatedValue(size),
            "height": AnimatedValue(size),
        },
    )


def _project(*, clipped: bool) -> Project:
    base = Project.create(SETTINGS)
    square = Clip(timeline_start=0, duration=10, source=_shape((0.0, 0.0, 1.0, 1.0), 20.0))
    cover = Clip(
        timeline_start=0,
        duration=10,
        source=_shape((1.0, 0.0, 0.0, 1.0), None),
        clip_to_below=clipped,
    )
    tracks = (Track(TrackKind.VIDEO, "V1", (square,)), Track(TrackKind.VIDEO, "V2", (cover,)))
    return base.with_timeline(replace(base.timeline, tracks=tracks))


def _render(project: Project, context: OffscreenGLContext) -> np.ndarray:
    renderer = FrameRenderer(project, context=context)
    try:
        return renderer.render(0)
    finally:
        renderer.close()


def test_the_cover_shows_only_inside_the_shape_below(gl_context: OffscreenGLContext) -> None:
    image = _render(_project(clipped=True), gl_context)
    # 形の中は上の赤、外は下地の黒のまま（覆われない）
    assert image[32, 32, 0] > 200 and image[32, 32, 2] < 50
    assert image[2, 2, 0] < 30


def test_without_clipping_the_cover_fills_the_screen(gl_context: OffscreenGLContext) -> None:
    image = _render(_project(clipped=False), gl_context)
    assert image[2, 2, 0] > 200


def test_the_setting_is_saved() -> None:
    project = _project(clipped=True)
    loaded = project_from_dict(project_to_dict(project))
    assert loaded.timeline.tracks[1].clips[0].clip_to_below is True
