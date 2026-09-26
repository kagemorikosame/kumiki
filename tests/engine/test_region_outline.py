"""範囲の枠（:func:`region_frame`）が、実際に範囲を掛けた所と重なること

枠は描く側（範囲のシェーダと、後ろの配置）を前向きに辿り直した物 GPU で本当に描いて、
部分フィルタで色を反転した所と枠を画素で比べる 食い違うと、つまんだ所と効く所が合わない
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sashimono.core.commands import AddClip, AddMedia, AddTrack
from sashimono.core.commands.fixed import TRANSFORM_EFFECT_KIND, with_fixed_items
from sashimono.core.model import (
    FILTER_KIND,
    AnimatedValue,
    Clip,
    Effect,
    GeneratedSource,
    MediaItem,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.effects import registry
from sashimono.effects.region import PARTIAL_FILTER
from sashimono.engine.decode import probe_media
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer
from sashimono.engine.render.outline import Point
from sashimono.engine.render.region_outline import region_frame
from sashimono.ui.preview_handles import inside

SETTINGS = ProjectSettings(width=320, height=180, frame_rate=FrameRate(30))


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


@pytest.fixture
def white(tmp_path: Path) -> MediaItem:
    """64 × 32 の真っ白な画像"""
    from PySide6.QtGui import QImage

    data = np.ascontiguousarray(np.full((32, 64, 4), 255, dtype=np.uint8))
    image = QImage(data.tobytes(), 64, 32, 64 * 4, QImage.Format.Format_RGBA8888)
    path = tmp_path / "白.png"
    assert image.save(str(path))
    return probe_media(path)


def _region(**values: float) -> Effect:
    return registry.require(PARTIAL_FILTER).create(
        **{name: AnimatedValue(value) for name, value in values.items()}
    )


def _project(*clips: Clip, media: MediaItem | None = None) -> Project:
    project = Project.create(SETTINGS)
    if media is not None:
        project = AddMedia(media).apply(project)
    for index, clip in enumerate(clips):
        track = Track(kind=TrackKind.VIDEO, name=f"V{index + 1}")
        project = AddClip(track.id, clip).apply(AddTrack(track).apply(project))
    return project


def _dark(image: np.ndarray) -> np.ndarray:
    """反転して暗くなった画素（白い絵の上の範囲）"""
    return image[:, :, :3].max(axis=2) < 80


def _compare(image: np.ndarray, corners: tuple[Point, ...], background: np.ndarray) -> None:
    dark = _dark(image) & background
    ys, xs = np.nonzero(dark)
    assert len(xs) > 50, "範囲が描かれていない"
    # 暗い画素は枠の中にある（縁の 1 画素ぶんは丸めの差として外す）
    hits = sum(inside(corners, (x + 0.5, y + 0.5)) for x, y in zip(xs, ys, strict=True))
    assert hits / len(xs) > 0.9
    # 枠の真ん中と暗い所の真ん中が合う
    centre = (sum(x for x, _ in corners) / 4.0, sum(y for _, y in corners) / 4.0)
    assert centre == pytest.approx((xs.mean() + 0.5, ys.mean() + 0.5), abs=1.5)


def test_a_turned_region_on_a_scaled_picture(
    gl_context: OffscreenGLContext, white: MediaItem
) -> None:
    # 範囲を回し、後ろの配置で絵ごと 3 倍にして回す 枠が片方だけを辿ると重ならない
    effects = (
        _region(center_x=8.0, center_y=4.0, region_width=24.0, region_height=12.0, rotation=20.0),
        registry.require("invert").create(),
    )
    clip = with_fixed_items(
        Clip(timeline_start=0, duration=10, media_id=white.id, native_size=True, effects=effects),
        picture=True,
    )
    clip = replace(
        clip,
        effects=tuple(
            e.with_param("scale", AnimatedValue(300.0)).with_param("rotation", AnimatedValue(15.0))
            if e.fixed and e.kind == TRANSFORM_EFFECT_KIND
            else e
            for e in clip.effects
        ),
    )
    project = _project(clip, media=white)
    image = _render(project, gl_context)
    frame = region_frame(project, clip, clip.effects[0].id, 0)
    assert frame is not None
    # 反転を切って描いた白い所だけを見る 絵の外（黒い下地）は反転しなくても暗い
    plain = replace(
        clip, effects=tuple(replace(e, enabled=False) for e in effects) + clip.effects[2:]
    )
    background = ~_dark(_render(_project(plain, media=white), gl_context))
    _compare(image, frame.corners, background)


def _render(project: Project, context: OffscreenGLContext) -> np.ndarray:
    renderer = FrameRenderer(project, context=context)
    try:
        return renderer.render(0)
    finally:
        renderer.close()


def test_a_filter_clip_region(gl_context: OffscreenGLContext, white: MediaItem) -> None:
    # フィルタのクリップは画面の中央が原点 下の絵（画面いっぱいの白）を範囲だけ反転する
    below = Clip(timeline_start=0, duration=10, media_id=white.id)
    filter_clip = Clip(
        timeline_start=0,
        duration=10,
        source=GeneratedSource(kind=FILTER_KIND),
        effects=(
            _region(center_x=-50.0, center_y=30.0, region_width=60.0, region_height=30.0),
            registry.require("invert").create(),
        ),
    )
    project = _project(below, filter_clip, media=white)
    image = _render(project, gl_context)
    frame = region_frame(project, filter_clip, filter_clip.effects[0].id, 0)
    assert frame is not None
    assert frame.corners[0] == pytest.approx((80.0, 45.0))
    # 下の白い絵は画面に収めて置くので、上下に 10 画素ずつ黒い帯が残る（それは数えない）
    background = np.zeros(image.shape[:2], dtype=bool)
    background[10:170] = True
    _compare(image, frame.corners, background)
