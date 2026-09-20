"""絵を分けて別々に動かすエフェクトの効き方（AviUtl の分身まわり）

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


class TestSplitZoom:
    """個別オブジェクトの拡大 碁盤の目に切って、1 マスずつ縮める"""

    def _zoom(self, **params: float) -> Effect:
        return registry.require("split_zoom").create(**params)

    def test_one_cell_means_the_whole_picture_shrinks(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        # 分割していない（1x1）ときは、絵ぜんたいが 1 マス
        before = _extent(draw())
        after = _extent(draw(self._zoom(zoom=50.0, columns=1, rows=1)))
        assert (after[1] - after[0]) == pytest.approx((before[1] - before[0]) * 0.5, abs=4)

    def test_each_cell_shrinks_in_place(self, draw: Callable[..., np.ndarray]) -> None:
        """マスごとに縮むので、**穴が開いて光る所が減る**

        絵ぜんたいを縮める実装だと、光る所は減るが外の広がりも一緒に縮む
        マスごとなら外の広がりは残り（端のマスが端に残る）、中に隙間ができる
        """
        plain = draw()
        split = draw(self._zoom(zoom=50.0, columns=3, rows=3))
        assert int(_lit(split).sum()) < int(_lit(plain).sum()) * 0.4, "縮んでいない"
        # 3x3 に切ると端のマスは端に残る 外の広がりはほとんど変わらない
        assert _extent(split)[0] - _extent(plain)[0] < (SQUARE // 3) // 2 + 4

    def test_more_cells_leave_more_gaps(self, draw: Callable[..., np.ndarray]) -> None:
        # マスの数を無視していると、どちらも同じ絵になる
        coarse = draw(self._zoom(zoom=50.0, columns=2, rows=2))
        fine = draw(self._zoom(zoom=50.0, columns=6, rows=6))
        assert _extent(coarse) != _extent(fine) or int(_lit(coarse).sum()) != int(_lit(fine).sum())

    def test_the_centre_shifts_by_what_is_left(self, draw: Callable[..., np.ndarray]) -> None:
        """中心をずらすと、絵は ``ずらし x (1 - 拡大率)`` だけ動く

        AviUtl2 で 中心X=100・拡大率 50 を描かせると絵が 50 ずれた
        ずらしをそのまま位置の移動として使うと、倍の量だけ動く
        """
        middle = _extent(draw(self._zoom(zoom=50.0, columns=1, rows=1)))
        moved = _extent(draw(self._zoom(zoom=50.0, columns=1, rows=1, center_x=40.0)))
        assert moved[0] - middle[0] == pytest.approx(20, abs=4)

    def test_full_zoom_changes_nothing(self, draw: Callable[..., np.ndarray]) -> None:
        # 拡大率 100 は元のまま ここが変わるならマスの割り出しがずれている
        plain = draw()
        same = draw(self._zoom(zoom=100.0, columns=4, rows=4))
        assert int(np.abs(plain.astype(np.int16) - same.astype(np.int16)).max()) <= 2


class TestSplitRotate:
    def test_each_cell_turns(self, draw: Callable[..., np.ndarray]) -> None:
        # 1 マスずつ回すので、四角の角が削れて光る所が減る
        plain = draw()
        turned = draw(registry.require("split_rotate").create(angle=30.0, columns=3, rows=3))
        assert int(_lit(turned).sum()) < int(_lit(plain).sum())

    def test_no_angle_changes_nothing(self, draw: Callable[..., np.ndarray]) -> None:
        plain = draw()
        same = draw(registry.require("split_rotate").create(angle=0.0, columns=3, rows=3))
        assert int(np.abs(plain.astype(np.int16) - same.astype(np.int16)).max()) <= 2


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
