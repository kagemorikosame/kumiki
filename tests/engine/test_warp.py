"""絵を歪めるエフェクトの効き方（AviUtl の変形まわり）

値の意味は AviUtl2 に 1 つずつ動かした見本を描かせて測った ここに並ぶ数は
その実測から取ったもので、落ちたときに疑うのはこちらの実装の側
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

WIDTH, HEIGHT = 200, 200
#: 下地の四角の一辺 真ん中に置く
SQUARE = 60


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


def _extent(image: np.ndarray) -> tuple[int, int, int, int]:
    """光っている所の 左・右・上・下（画面の座標 下が大きい）"""
    lit = image[:, :, :3].max(axis=2) > 24
    rows, columns = np.where(lit)
    if not len(rows):
        return (0, 0, 0, 0)
    return (int(columns.min()), int(columns.max()), int(rows.min()), int(rows.max()))


class TestExpandArea:
    """領域拡張 入れ物を広げると、絵は真ん中に残るので**半分ずれる**"""

    def test_expanding_the_top_pushes_the_picture_down(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        # AviUtl2 に 上=200 を描かせると、絵は下へ 100（半分）動いた
        # ずれの向きを逆にすると、配布物の飾り枠が上下逆の位置に出る
        before = _extent(draw())
        after = _extent(draw(registry.require("expand_area").create(top=40.0)))
        assert after[2] - before[2] == pytest.approx(20, abs=2)
        assert after[0] == pytest.approx(before[0], abs=2)

    def test_expanding_the_right_pushes_the_picture_left(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        before = _extent(draw())
        after = _extent(draw(registry.require("expand_area").create(right=60.0)))
        assert before[0] - after[0] == pytest.approx(30, abs=2)

    def test_expanding_evenly_does_not_move_the_picture(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        # 四方を同じだけ広げれば真ん中は動かない ここがずれるなら符号が片側だけ違う
        before = _extent(draw())
        after = _extent(
            draw(
                registry.require("expand_area").create(top=40.0, bottom=40.0, left=40.0, right=40.0)
            )
        )
        assert after == before


class TestMirror:
    def _mirror(self, **params: float | str) -> Effect:
        return registry.require("mirror").create(**params)

    def test_the_reflection_appears_below(self, draw: Callable[..., np.ndarray]) -> None:
        before = _extent(draw())
        after = _extent(draw(self._mirror(side="bottom")))
        assert after[3] > before[3] + 20, "下に鏡像が出ていない"
        assert after[2] == before[2], "上へはみ出している"

    def test_the_reflection_can_go_to_the_right(self, draw: Callable[..., np.ndarray]) -> None:
        before = _extent(draw())
        after = _extent(draw(self._mirror(side="right")))
        assert after[1] > before[1] + 20
        assert after[3] == before[3]

    def test_the_gap_pushes_the_reflection_twice_as_far(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        """境目調整 を足すと鏡像は**その倍**離れる

        折り返す線が動くので、鏡像はその倍動く AviUtl2 で 40 を入れると
        80px 離れた 1 倍で写すと、境目の空きが半分になる
        """
        # 小さめの四角で試す 大きいと鏡像が絵の下端で切れて、
        # 離れた量が測れない（境目調整 0 でも既に端まで届いてしまう）
        close = _extent(draw(self._mirror(side="bottom", gap=0.0), width=30, height=30))
        far = _extent(draw(self._mirror(side="bottom", gap=10.0), width=30, height=30))
        assert far[3] - close[3] == pytest.approx(20, abs=3)

    def test_full_transparency_hides_the_reflection(self, draw: Callable[..., np.ndarray]) -> None:
        before = _extent(draw())
        after = _extent(draw(self._mirror(side="bottom", opacity=100.0)))
        assert after == before

    def test_the_falloff_thins_the_far_end(self, draw: Callable[..., np.ndarray]) -> None:
        # 減衰 を上げると、線から遠い方が先に消える
        hard = draw(self._mirror(side="bottom", falloff=0.0))
        soft = draw(self._mirror(side="bottom", falloff=100.0))
        assert _extent(soft)[3] < _extent(hard)[3]


class TestDisplacementMap:
    def _displace(self, **params: float | str) -> Effect:
        base: dict[str, float | str] = {"size": 120.0, "blur": 5.0}
        base.update(params)
        return registry.require("displacement_map").create(**base)

    def test_it_moves_what_is_inside_the_map(self, draw: Callable[..., np.ndarray]) -> None:
        # マップの中だけ動く 外まで動かすと、ただの移動になる
        before = _extent(draw())
        after = _extent(draw(self._displace(move_x=30.0)))
        assert after != before

    def test_an_empty_map_changes_nothing(self, draw: Callable[..., np.ndarray]) -> None:
        # ずらす量が 0 なら絵は変わらない ここが変わるならマップの中心がずれている
        plain = draw()
        still = draw(self._displace(move_x=0.0, move_y=0.0))
        assert int(np.abs(plain.astype(np.int16) - still.astype(np.int16)).max()) <= 2

    def test_a_map_smaller_than_the_picture_leaves_the_edges(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        """マップより外は動かない

        大きさを無視して全体を動かすと、四角がまるごと移動してしまう
        """
        after = draw(self._displace(size=20.0, move_x=40.0))
        before = draw()
        # 四角の左端は マップ（半径 10）の外なので、そのまま残る
        assert _extent(after)[0] == pytest.approx(_extent(before)[0], abs=2)
