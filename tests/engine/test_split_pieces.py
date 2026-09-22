"""分けたマスの位置を動かすエフェクトの効き方（AviUtl の 個別オブジェクト の拡大と回転）

値の意味は AviUtl2 に オブジェクト分割 と 座標の拡大縮小・回転(個別オブジェクト) を
積んだ見本を描かせて測った（Issue #42） ここに並ぶ数はその実測から取ったもので、
落ちたときに疑うのはこちらの実装の側
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

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
from sashimono.effects.sources import SHAPE
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer

WIDTH, HEIGHT = 320, 320
#: 下地は横長の帯 分けたマスが回ったかどうかを、縦横の長さで見分ける
BAND_WIDTH, BAND_HEIGHT = 120, 40


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
    """真ん中に白い帯を置いた絵へエフェクトを掛ける"""

    def run(*effects: Effect) -> np.ndarray:
        source: GeneratedSource = SHAPE.create(
            shape="rect", width=BAND_WIDTH, height=BAND_HEIGHT, color=(1.0, 1.0, 1.0, 1.0)
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


def _extent(image: np.ndarray) -> tuple[int, int, int, int]:
    """光っている所の 左・右・上・下（画面の座標 下が大きい 中心を 0 にする）"""
    rows, columns = np.where(_lit(image))
    return (
        int(columns.min()) - WIDTH // 2,
        int(columns.max()) + 1 - WIDTH // 2,
        int(rows.min()) - HEIGHT // 2,
        int(rows.max()) + 1 - HEIGHT // 2,
    )


def _pieces(**params: float) -> Effect:
    return registry.require("split_pieces").create(**params)


class TestSplitPieces:
    def test_turning_moves_the_pieces_but_keeps_them_upright(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        """2x1 を 90 度回すと、左半分が真上へ動き、中身は横長のまま

        AviUtl2 の実測で、田田田 を左右に分けて 90 度回すと左半分が上、
        右半分が下へ行き、文字は立ったままだった マスの真ん中を軸に
        中身を回す読み方では、縦長の 2 枚が横に並ぶ
        """
        left, right, top, bottom = _extent(draw(_pieces(columns=2.0, rows=1.0, angle=90.0)))
        assert right - left == pytest.approx(BAND_WIDTH / 2, abs=2)
        assert bottom - top == pytest.approx(BAND_WIDTH / 2 + BAND_HEIGHT, abs=2)

    def test_a_positive_angle_turns_clockwise(self, draw: Callable[..., np.ndarray]) -> None:
        # 軸を右半分の真ん中に置くと、右半分は動かず左半分だけが回る
        # 時計回りなら左半分は真上へ行く 下へ行くなら回る向きが逆
        left, right, top, bottom = _extent(
            draw(_pieces(columns=2.0, rows=1.0, angle=90.0, center_x=BAND_WIDTH / 4))
        )
        assert (left, right) == (0, BAND_WIDTH // 2)
        assert (top, bottom) == (-80, 20)

    def test_scaling_spreads_the_pieces_without_scaling_them(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        # 3x1 を 200% にすると、マスの間隔だけが倍になって隙間が空く
        # 中身まで拡大すると、光る量が倍になり隙間もできない
        plain = draw()
        spread = draw(_pieces(columns=3.0, rows=1.0, scale=200.0))
        left, right, _, _ = _extent(spread)
        assert (left, right) == (-100, 100)
        assert int(_lit(spread).sum()) == pytest.approx(int(_lit(plain).sum()), rel=0.02)

    def test_shrinking_packs_the_pieces_towards_the_centre(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        # 実測 3x3 を 50% にすると、田田田 が 504 から 320 の幅に詰まった
        left, right, top, bottom = _extent(draw(_pieces(columns=3.0, rows=3.0, scale=50.0)))
        assert (left, right) == (-40, 40)
        assert (top, bottom) == pytest.approx((-40 / 3, 40 / 3), abs=1)

    def test_a_single_piece_does_not_move(self, draw: Callable[..., np.ndarray]) -> None:
        # 分けていなければ何も起きない（AviUtl2 でも元の絵のままだった）
        plain = draw()
        moved = draw(_pieces(scale=50.0, angle=30.0))
        assert int(np.abs(plain.astype(np.int16) - moved.astype(np.int16)).max()) <= 2

    def test_the_centre_shifts_the_pieces(self, draw: Callable[..., np.ndarray]) -> None:
        # 実測 中心X 100 で 50% にすると、絵は 100 x (1 - 0.5) = 50 だけずれた
        base = _extent(draw(_pieces(columns=3.0, rows=1.0, scale=50.0)))
        shifted = _extent(draw(_pieces(columns=3.0, rows=1.0, scale=50.0, center_x=40.0)))
        assert shifted[0] - base[0] == 20
        assert shifted[1] - base[1] == 20
