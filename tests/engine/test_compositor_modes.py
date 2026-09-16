"""合成モードの追加分と、奥行きのある配置

合成モードは数値で押さえる 混ぜ方を取り違えても「少し色が違う」にしか
見えず、配布物の見た目と並べるまで気付けない 奥行きは、四隅がどこへ来るかで
押さえる
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pytest

from kumiki.engine.gpu import (
    BlendMode,
    Compositor,
    GLContextError,
    OffscreenGLContext,
    Placement,
    Texture,
    Transform,
)
from kumiki.engine.gpu.projection import CAMERA_DISTANCE, homography, project, rotate


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


def _solid(width: int, height: int, value: int) -> np.ndarray:
    image = np.full((height, width, 4), value, dtype=np.uint8)
    image[..., 3] = 255
    return image


def _mix(gl_context: OffscreenGLContext, below: int, above: int, mode: str) -> int:
    with gl_context:
        compositor = Compositor(8, 8)
        base = Texture.from_array(_solid(8, 8, below))
        top = Texture.from_array(_solid(8, 8, above))
        compositor.begin()
        compositor.draw(base)
        compositor.draw(top, blend=mode)
        result = compositor.read()
        compositor.release()
        base.release()
        top.release()
    return int(result[4, 4, 0])


def _linear(value: int) -> float:
    c = value / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _encoded(value: float) -> int:
    c = min(max(value, 0.0), 1.0)
    return round((c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055) * 255)


class TestShaderBlends:
    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            (BlendMode.LIGHTEN, max),
            (BlendMode.DARKEN, min),
        ],
    )
    def test_compare_keeps_one_side(
        self, gl_context: OffscreenGLContext, mode: str, expected: object
    ) -> None:
        # 取り違えると、比較(明) で暗い方が残るような反対の絵になる
        assert _mix(gl_context, 60, 200, mode) == expected(60, 200)  # type: ignore[operator]
        assert _mix(gl_context, 200, 60, mode) == expected(60, 200)  # type: ignore[operator]

    def test_subtract_never_goes_negative(self, gl_context: OffscreenGLContext) -> None:
        # 負の値が残ると、次に加算したときに下の絵が暗く沈む
        assert _mix(gl_context, 60, 200, BlendMode.SUBTRACT) == 0
        expected = _encoded(_linear(200) - _linear(60))
        assert abs(_mix(gl_context, 200, 60, BlendMode.SUBTRACT) - expected) <= 1

    def test_overlay_follows_the_backdrop(self, gl_context: OffscreenGLContext) -> None:
        # 暗い下地では乗算、明るい下地ではスクリーンになる 反対にすると
        # コントラストを強めるはずが弱める
        dark, light = _linear(40), _linear(230)
        above = _linear(180)
        assert abs(_mix(gl_context, 40, 180, BlendMode.OVERLAY) - _encoded(2 * dark * above)) <= 1
        screen = 1 - 2 * (1 - light) * (1 - above)
        assert abs(_mix(gl_context, 230, 180, BlendMode.OVERLAY) - _encoded(screen)) <= 1

    def test_every_mode_has_a_way_to_draw(self, gl_context: OffscreenGLContext) -> None:
        # 一覧にあるのに描き方が無いと、選んでも通常と同じに見える
        for mode in BlendMode.ALL:
            _mix(gl_context, 100, 150, mode)


class TestProjection:
    def test_nothing_moves_when_flat(self) -> None:
        # 平らな板の四隅が動くと、今までの見た目が変わる
        corners = Transform().corners(100, 50, 400, 200)
        assert corners == ((150.0, 75.0), (250.0, 75.0), (250.0, 125.0), (150.0, 125.0))

    def test_going_deeper_makes_it_smaller(self) -> None:
        # 奥へ置いたものが小さくならないと、奥行きのあるスクリプトが平らに見える
        near = Transform().corners(100, 100, 400, 400)
        far = Transform(z=CAMERA_DISTANCE).corners(100, 100, 400, 400)
        assert far[1][0] - far[0][0] == pytest.approx((near[1][0] - near[0][0]) / 2)

    def test_tilting_backwards_narrows_the_top(self) -> None:
        # X 軸の正で上端が奥へ倒れる 逆だと、起き上がる動きが倒れる動きになる
        corners = Transform(rotation_x=45).corners(200, 200, 800, 800)
        top = corners[1][0] - corners[0][0]
        bottom = corners[2][0] - corners[3][0]
        assert top < bottom

    def test_quarter_turn_about_y_is_edge_on(self) -> None:
        # 真横を向いた板は幅が無い このとき射影変換は解けず、描かない
        corners = Transform(rotation_y=90).corners(200, 200, 800, 800)
        assert corners[0][0] == pytest.approx(corners[1][0])
        source = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
        assert homography(source, corners) is None

    def test_rotation_directions(self) -> None:
        # 右の点を Z で回すと下へ（画面上で時計回り）
        x, y, _ = rotate((1.0, 0.0, 0.0), 0, 0, 90)
        assert (round(x, 6), round(y, 6)) == (0.0, 1.0)
        # 右端を Y で回すと奥へ
        _, _, z = rotate((1.0, 0.0, 0.0), 0, 90, 0)
        assert z == pytest.approx(1.0)

    def test_the_camera_never_divides_by_zero(self) -> None:
        # カメラより手前の点で割り算が発散すると、画面が 1 つの色で塗り潰される
        x, _ = project((10.0, 0.0, -CAMERA_DISTANCE * 2), 100, 100)
        assert np.isfinite(x)


class TestMappedDraw:
    def test_a_trapezoid_is_filled_in_perspective(self, gl_context: OffscreenGLContext) -> None:
        # 上が狭い台形へ貼る 上の行は狭く、下の行は広く塗られる 塗られないと、
        # 傾けた板や drawpoly がまったく映らない
        with gl_context:
            compositor = Compositor(64, 64)
            texture = Texture.from_array(_solid(16, 16, 255))
            compositor.begin()
            drawn = compositor.draw_mapped(
                texture.handle,
                source=Placement(0, 0, 16, 16),
                anchor=Placement(0, 0, 16, 16),
                corners=((24.0, 8.0), (40.0, 8.0), (60.0, 56.0), (4.0, 56.0)),
            )
            result = compositor.read()
            compositor.release()
            texture.release()

        assert drawn
        top = int((result[10, :, 0] > 128).sum())
        bottom = int((result[54, :, 0] > 128).sum())
        assert 0 < top < bottom
        assert result[2, 32, 0] < 16

    def test_blend_modes_work_on_a_quad_too(self, gl_context: OffscreenGLContext) -> None:
        # 四角形の描画だけ合成モードを無視すると、傾けた板だけ通常で重なる
        with gl_context:
            compositor = Compositor(16, 16)
            base = Texture.from_array(_solid(16, 16, 200))
            top = Texture.from_array(_solid(16, 16, 60))
            compositor.begin()
            compositor.draw(base)
            full = Placement(0, 0, 16, 16)
            compositor.draw_mapped(
                top.handle,
                source=full,
                anchor=full,
                corners=((0.0, 0.0), (16.0, 0.0), (16.0, 16.0), (0.0, 16.0)),
                blend=BlendMode.LIGHTEN,
            )
            result = compositor.read()
            compositor.release()
            base.release()
            top.release()
        assert int(result[8, 8, 0]) == 200
