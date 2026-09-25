"""実物の測り 第 4 弾（#216 #195）で測った値の意味

YMM4 は ``tools/ymm4_probes.py`` の 9 回目、AviUtl2 は ``tools/aviutl_filter_probes.py --fourth``
測った数をここへ定数で持ち、描き方がその数を出すかを見る
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import numpy as np
import pytest

from sashimono.core.model import (
    Clip,
    Effect,
    GeneratedSource,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.effects import registry
from sashimono.effects.sources import PREVIOUS_OBJECT, SHAPE
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer

WIDTH, HEIGHT = 400, 400


@pytest.fixture(scope="module")
def gl() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


def _render(gl: OffscreenGLContext, *clips: Clip, frame: int = 0) -> np.ndarray:
    """1 本ずつ別のトラックに置いたクリップを下から重ねて描く（400x400・30fps）"""
    project = Project.create(ProjectSettings(width=WIDTH, height=HEIGHT, frame_rate=FrameRate(30)))
    tracks = tuple(Track(kind=TrackKind.VIDEO, clips=(clip,)) for clip in clips)
    project = project.with_timeline(project.timeline.__class__(rate=project.rate, tracks=tracks))
    renderer = FrameRenderer(project, context=gl)
    try:
        return np.asarray(renderer.render(frame))[..., :3]
    finally:
        renderer.close()


def _square(size: int = 100, grey: float = 1.0) -> GeneratedSource:
    return SHAPE.create(shape="rect", width=size, height=size, color=(grey, grey, grey, 1.0))


def _placed(x: float = 0.0, y: float = 0.0) -> Effect:
    """クリップが最初から持つ配置の欄（印付き）"""
    return replace(registry.require("transform").create(pos_x=x, pos_y=y), fixed=True)


def _rows(image: np.ndarray) -> tuple[int, int] | None:
    rows = np.nonzero((image.max(axis=2) >= 128).any(axis=1))[0]
    if rows.size == 0:
        return None
    return int(rows[0]), int(rows[-1]) + 1


class TestEnterFromOutsideVertically:
    """YMM4 の画面外から登場の上下（#216 の 2） 直線 2 秒で入る四角

    YMM4 は 640x360 の四角を上と下から入れると、残り 0.25 で 270（1080 x 0.25）ずれた
    Y 200 に置いた四角も同じだけずれた 絵の端が画面の端を越えるだけ（1080 - 180 - 180 = 720）
    ずらすと、半分のコマで四角が 180 早く見えてくる
    """

    @pytest.mark.parametrize(("direction", "expected"), [("top", (0, 50)), ("bottom", (350, 400))])
    def test_it_starts_a_screen_height_away(
        self, gl: OffscreenGLContext, direction: str, expected: tuple[int, int]
    ) -> None:
        # 400 の画面の真ん中（行 150〜250）に 100 の四角 60 フレームの登場の 30 フレーム目は
        # 画面の高さの半分（200）ずれ、四角の半分が画面の端で切れる
        enter = registry.require("inout_move").create(
            direction=direction, effect_in=True, effect_time=2.0, easing="linear"
        )
        clip = Clip(timeline_start=0, duration=60, source=_square(), effects=(enter,))
        rows = _rows(_render(gl, clip, frame=30))
        assert rows is not None
        assert rows == pytest.approx(expected, abs=2)


class TestPreviousObjectRest:
    """AviUtl2 の直前オブジェクトの残り（#195 の探り po08 po09）"""

    def test_the_copy_drops_the_add_blend(self, gl: OffscreenGLContext) -> None:
        # 灰 128 の下地の上で、灰 100 の四角を加算にすると 228 になる その写しは 100 だった
        # 合成方法を写すと 228 になる
        ground = Clip(timeline_start=0, duration=30, source=_square(400, 128 / 255))
        below = Clip(
            timeline_start=0,
            duration=30,
            source=_square(100, 100 / 255),
            effects=(_placed(-100.0),),
            blend_mode="add",
        )
        copy = Clip(
            timeline_start=0,
            duration=30,
            source=PREVIOUS_OBJECT.create(),
            effects=(_placed(100.0),),
        )
        image = _render(gl, ground, below, copy)
        assert int(image[200, 100, 0]) == pytest.approx(228, abs=2)
        assert int(image[200, 300, 0]) == pytest.approx(100, abs=2)

    def test_the_copy_is_not_clipped(self, gl: OffscreenGLContext) -> None:
        # 小さな四角（30）で切った白い四角（100）を写すと、写しは切る前の 100 のままだった
        # 切り抜いた相手の小さな四角は、切った側でも写しでも見えなかった
        small = Clip(timeline_start=0, duration=30, source=_square(30), effects=(_placed(-100.0),))
        clipped = Clip(
            timeline_start=0,
            duration=30,
            source=_square(100),
            effects=(_placed(-100.0),),
            clip_to_below=True,
        )
        copy = Clip(
            timeline_start=0,
            duration=30,
            source=PREVIOUS_OBJECT.create(),
            effects=(_placed(100.0),),
        )
        image = _render(gl, small, clipped, copy)
        left = image[:, :200]
        right = image[:, 200:]
        assert int((left.max(axis=2) >= 128).sum()) == 30 * 30
        assert int((right.max(axis=2) >= 128).sum()) == 100 * 100
