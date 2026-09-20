"""写しを撒くエフェクトの効き方（AviUtl の ランダム配置）

値の意味は AviUtl2 に見本を描かせて測った ここに並ぶ数はその実測から取ったもので、
落ちたときに疑うのはこちらの実装の側
"""

from __future__ import annotations

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
#: 下地の四角の一辺 真ん中に置く
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

    def run(*effects: Effect, width: int = SQUARE, height: int = SQUARE) -> np.ndarray:
        source: GeneratedSource = SHAPE.create(
            shape="rect", width=width, height=height, color=(1.0, 1.0, 1.0, 1.0)
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
    return image[:, :, :3].max(axis=2) > 24


def _extent(image: np.ndarray) -> tuple[int, int, int, int]:
    """光っている所の 左・右・上・下（画面の座標 下が大きい）"""
    rows, columns = np.where(_lit(image))
    if not len(rows):
        return (0, 0, 0, 0)
    return (int(columns.min()), int(columns.max()), int(rows.min()), int(rows.max()))


class TestScatter:
    """ランダム配置 数のぶん写しを撒く 位置は乱数なので広がりと量で確かめる"""

    def _scatter(self, **params: float | bool) -> Effect:
        return registry.require("scatter").create(**params)

    def test_more_copies_cover_more(self, draw: Callable[..., np.ndarray]) -> None:
        few = int(_lit(draw(self._scatter(count=2, span=200.0))).sum())
        many = int(_lit(draw(self._scatter(count=12, span=200.0))).sum())
        assert many > few

    def test_a_wider_span_spreads_further(self, draw: Callable[..., np.ndarray]) -> None:
        # 範囲を無視していると、どちらも同じ広がりになる
        narrow = _extent(draw(self._scatter(count=8, span=40.0)))
        wide = _extent(draw(self._scatter(count=8, span=200.0)))
        assert (wide[1] - wide[0]) > (narrow[1] - narrow[0]) + 20
        assert (wide[3] - wide[2]) > (narrow[3] - narrow[2]) + 20

    def test_the_copies_spread_in_both_directions(self, draw: Callable[..., np.ndarray]) -> None:
        """縦と横のどちらにも散らばる

        乱数の 2 つの数が揃っていると斜めの線の上にしか並ばず、
        縦の広がりが足りなくなる（実測で 4 分の 3 しか出なかった）
        """
        image = draw(self._scatter(count=16, span=160.0), width=20, height=20)
        left, right, top, bottom = _extent(image)
        assert (right - left) > 60
        assert (bottom - top) > 60

    def test_one_copy_leaves_the_picture_whole(self, draw: Callable[..., np.ndarray]) -> None:
        # 1 つなら写しは 1 枚 光る量が元とほぼ同じになる（位置はずれる）
        plain = int(_lit(draw()).sum())
        single = int(_lit(draw(self._scatter(count=1, span=0.0))).sum())
        assert single == pytest.approx(plain, rel=0.05)
