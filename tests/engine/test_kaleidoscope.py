"""万華鏡の効き方（AviUtl の 万華鏡）

値の意味は AviUtl2 に 長さ・繰り返し回数・固定サイズ・円形マスク を変えた見本を
描かせて測った（Issue #39） ここに並ぶ数はその実測から取ったもので、
落ちたときに疑うのはこちらの実装の側
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator

import numpy as np
import pytest

from kumiki.core.model import (
    Clip,
    Effect,
    GeneratedSource,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from kumiki.core.timebase import FrameRate
from kumiki.effects import registry
from kumiki.effects.sources import SHAPE
from kumiki.engine.gpu import GLContextError, OffscreenGLContext
from kumiki.engine.render import FrameRenderer

WIDTH, HEIGHT = 240, 240
#: 下地の四角の一辺 真ん中に置く 鏡の三角より大きくして、読む所を必ず白にする
SQUARE = 120


@pytest.fixture(scope="module")
def gl() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


@pytest.fixture
def draw(gl: OffscreenGLContext) -> Callable[..., np.ndarray]:
    """真ん中に白い四角を置いた絵へエフェクトを掛ける"""

    def run(*effects: Effect) -> np.ndarray:
        source: GeneratedSource = SHAPE.create(
            shape="rect", width=SQUARE, height=SQUARE, color=(1.0, 1.0, 1.0, 1.0)
        )
        project = Project.create(
            ProjectSettings(width=WIDTH, height=HEIGHT, frame_rate=FrameRate(30))
        )
        track = Track(
            kind=TrackKind.VIDEO,
            clips=(Clip(timeline_start=0, duration=30, source=source, effects=effects),),
        )
        project = project.with_timeline(
            project.timeline.__class__(rate=project.rate, tracks=(track,))
        )
        renderer = FrameRenderer(project, context=gl)
        try:
            return renderer.render(0)
        finally:
            renderer.close()

    return run


def _lit(image: np.ndarray) -> np.ndarray:
    return image[:, :, :3].max(axis=2) > 128


def _size(image: np.ndarray) -> tuple[int, int]:
    """光っている所の幅と高さ"""
    rows, columns = np.where(_lit(image))
    if not len(rows):
        return (0, 0)
    return (int(columns.max() - columns.min() + 1), int(rows.max() - rows.min() + 1))


def _kaleidoscope(**params: float | bool) -> Effect:
    return registry.require("kaleidoscope").create(**params)


class TestKaleidoscope:
    def test_the_area_is_a_hexagon_one_step_wider_than_the_repeats(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        """繰り返し 2・長さ 20 は、外接円の半径 60 の六角形を覆う

        AviUtl2 の実測で、繰り返し 2・長さ 100 の端が 300（横）と 260（縦）、
        繰り返し 3・長さ 200 の模様の端が半径 800 の辺に乗った
        繰り返しをそのまま段数にすると、ひと回り小さい範囲になる
        """
        width, height = _size(draw(_kaleidoscope(span=20.0, repeats=2.0)))
        assert width == pytest.approx(120, abs=3)
        assert height == pytest.approx(2 * 60 * math.cos(math.pi / 6), abs=3)

    def test_the_span_scales_the_area(self, draw: Callable[..., np.ndarray]) -> None:
        # 長さを倍にすると範囲も倍 長さを無視すると 2 つが同じ大きさになる
        small, _ = _size(draw(_kaleidoscope(span=10.0, repeats=2.0)))
        large, _ = _size(draw(_kaleidoscope(span=20.0, repeats=2.0)))
        assert large == pytest.approx(small * 2, abs=3)

    def test_the_fixed_size_is_the_width_of_the_area(self, draw: Callable[..., np.ndarray]) -> None:
        # 実測 繰り返し 1・長さ 100・固定サイズ 200 で、模様がちょうど半分になった
        width, _ = _size(draw(_kaleidoscope(span=40.0, repeats=1.0, fixed_size=80.0)))
        assert width == pytest.approx(80, abs=3)

    def test_the_circle_mask_fits_inside_the_hexagon(self, draw: Callable[..., np.ndarray]) -> None:
        # 円の半径は六角形の内接円 外接円にすると横の端が 300 まで出て、実測の 259 と合わない
        width, height = _size(draw(_kaleidoscope(span=20.0, repeats=2.0, circle_mask=True)))
        radius = 60 * math.cos(math.pi / 6)
        assert width == pytest.approx(2 * radius, abs=3)
        assert height == pytest.approx(2 * radius, abs=3)

    def test_the_wedge_below_the_centre_is_read(self, draw: Callable[..., np.ndarray]) -> None:
        """鏡に映すのは中心から**下**へ開いた三角

        読む中心を四角の上端の 2px 手前へずらす 下の三角を読めば四角の中なので
        全部白、上の三角を読むと大半が四角の外で透明になる
        AviUtl2 で田の字を映すと、上を読む向きでは差が 8.0、下なら 3.3 だった
        """
        image = draw(
            _kaleidoscope(span=20.0, repeats=1.0, center_y=SQUARE / 2 - 2, clip_outside=True)
        )
        centre = image[HEIGHT // 2 - 10 : HEIGHT // 2 + 10, WIDTH // 2 - 10 : WIDTH // 2 + 10]
        assert _lit(centre).mean() > 0.9

    def test_nothing_outside_the_area_is_drawn(self, draw: Callable[..., np.ndarray]) -> None:
        # 範囲の外は元の絵も残らない（実測 長さ 50 で、はみ出た田の字が消えた）
        width, _ = _size(draw(_kaleidoscope(span=5.0, repeats=1.0)))
        assert width < SQUARE / 2
