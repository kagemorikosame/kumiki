"""合成モードの追加分と、奥行きのある配置

合成モードは数値で押さえる 混ぜ方を取り違えても「少し色が違う」にしか
見えず、配布物の見た目と並べるまで気付けない 奥行きは、四隅がどこへ来るかで
押さえる
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pytest

from sashimono.engine.gpu import (
    BlendMode,
    Compositor,
    GLContextError,
    OffscreenGLContext,
    Placement,
    Texture,
    Transform,
)
from sashimono.engine.gpu.projection import (
    CAMERA_DISTANCE,
    Corners,
    homography,
    project,
    rotate,
)


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


def _corners(transform: Transform, *sizes: int) -> Corners:
    corners = transform.corners(sizes[0], sizes[1], sizes[2], sizes[3])
    assert corners is not None
    return corners


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
        # 混ぜる式は符号化した値のまま計算する（AviUtl と YMM4 に合わせた）
        assert abs(_mix(gl_context, 200, 60, BlendMode.SUBTRACT) - (200 - 60)) <= 1

    def test_the_modes_mix_in_srgb(self, gl_context: OffscreenGLContext) -> None:
        # リニアで混ぜると、乗算やスクリーンの中間が配布物と違う明るさになる
        assert abs(_mix(gl_context, 200, 100, BlendMode.MULTIPLY) - round(200 * 100 / 255)) <= 2
        screen = 255 - (255 - 200) * (255 - 100) / 255
        assert abs(_mix(gl_context, 200, 100, BlendMode.SCREEN) - round(screen)) <= 2
        assert abs(_mix(gl_context, 200, 100, BlendMode.ADD) - 255) <= 1

    @pytest.mark.parametrize("encoded", [False, True], ids=["linear", "srgb"])
    def test_replacing_keeps_the_picture_colour(
        self, gl_context: OffscreenGLContext, encoded: bool
    ) -> None:
        """置き換えは、上の絵の色をそのまま置く sRGB で重ねるキャンバスでも暗くならない

        sRGB のキャンバスへリニアの値のまま混ぜると、灰色 128 が 55 ほどまで沈む
        （#191 のレビュー 事前乗算で読むように変えたときに符号化が抜けた）
        """
        with gl_context:
            compositor = Compositor(8, 8, encoded=encoded)
            base = Texture.from_array(_solid(8, 8, 30))
            top = Texture.from_array(_solid(8, 8, 128))
            compositor.begin()
            compositor.draw(base)
            compositor.draw(top, blend=BlendMode.REPLACE)
            result = compositor.read()
            compositor.release()
            base.release()
            top.release()
        assert abs(int(result[4, 4, 0]) - 128) <= 1

    def test_overlay_follows_the_backdrop(self, gl_context: OffscreenGLContext) -> None:
        # 暗い下地では乗算、明るい下地ではスクリーンになる 反対にすると
        # コントラストを強めるはずが弱める
        assert abs(_mix(gl_context, 40, 180, BlendMode.OVERLAY) - round(2 * 40 * 180 / 255)) <= 2
        screen = 255 - 2 * (255 - 230) * (255 - 180) / 255
        assert abs(_mix(gl_context, 230, 180, BlendMode.OVERLAY) - round(screen)) <= 2

    def test_every_mode_has_a_way_to_draw(self, gl_context: OffscreenGLContext) -> None:
        # 一覧にあるのに描き方が無いと、選んでも通常と同じに見える
        # 比較(明)(暗) はどちらが上かで通常と重なるので、上下を入れ替えた組も見る
        pairs = ((100, 150), (150, 100))
        normal = [_mix(gl_context, below, above, BlendMode.NORMAL) for below, above in pairs]
        for mode in BlendMode.ALL:
            # 輝度は上の明るさを下の色へ移す 灰色どうしでは上の色そのものになり、通常と重なる
            if mode in (BlendMode.NORMAL, BlendMode.LUMINOSITY):
                continue
            mixed = [_mix(gl_context, below, above, mode) for below, above in pairs]
            assert mixed != normal, f"{mode} が通常と同じ"

    def test_extended_modes_mix_in_srgb(self, gl_context: OffscreenGLContext) -> None:
        # YMM4 の合成は sRGB のまま混ぜる 差の絶対値なら符号化した値の差がそのまま出る
        assert abs(_mix(gl_context, 200, 50, BlendMode.DIFFERENCE) - 150) <= 1
        # ハードミックスは足して 1 を超えるかどうかで白か黒
        assert _mix(gl_context, 200, 100, BlendMode.HARD_MIX) >= 254
        assert _mix(gl_context, 100, 100, BlendMode.HARD_MIX) <= 1


class TestProjection:
    def test_nothing_moves_when_flat(self) -> None:
        # 平らな板の四隅が動くと、今までの見た目が変わる
        corners = _corners(Transform(), 100, 50, 400, 200)
        assert corners == ((150.0, 75.0), (250.0, 75.0), (250.0, 125.0), (150.0, 125.0))

    def test_going_deeper_makes_it_smaller(self) -> None:
        # 奥へ置いたものが小さくならないと、奥行きのあるスクリプトが平らに見える
        near = _corners(Transform(), 100, 100, 400, 400)
        far = _corners(Transform(z=CAMERA_DISTANCE), 100, 100, 400, 400)
        assert far[1][0] - far[0][0] == pytest.approx((near[1][0] - near[0][0]) / 2)

    def test_tilting_backwards_narrows_the_top(self) -> None:
        # X 軸の正で上端が奥へ倒れる 逆だと、起き上がる動きが倒れる動きになる
        corners = _corners(Transform(rotation_x=45), 200, 200, 800, 800)
        top = corners[1][0] - corners[0][0]
        bottom = corners[2][0] - corners[3][0]
        assert top < bottom

    def test_quarter_turn_about_y_is_edge_on(self) -> None:
        # 真横を向いた板は幅が無い このとき射影変換は解けず、描かない
        corners = _corners(Transform(rotation_y=90), 200, 200, 800, 800)
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

    def test_points_behind_the_camera_are_not_projected(self) -> None:
        # カメラを越えた点を丸めて写すと、板が巨大な四角形になって画面を覆う
        assert project((10.0, 0.0, -CAMERA_DISTANCE * 2), 100, 100) is None
        assert project((10.0, 0.0, -CAMERA_DISTANCE), 100, 100) is None
        near = project((10.0, 0.0, -CAMERA_DISTANCE + 2), 100, 100)
        assert near is not None
        assert np.isfinite(near[0])

    def test_a_board_crossing_the_camera_is_not_drawn(self) -> None:
        # 1 つの隅でもカメラを越えたら、残りの隅だけで形を作らない
        assert Transform(z=-CAMERA_DISTANCE).corners(100, 100, 400, 400) is None
        crossing = Transform(z=-CAMERA_DISTANCE + 10, rotation_x=80)
        assert crossing.corners(400, 400, 400, 400) is None

    def test_an_unsolvable_homography_is_none(self) -> None:
        # 解けない四隅で例外を投げると、描画が止まって画面が固まる
        flat = ((0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0))
        assert homography(flat, flat) is None


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


class TestContentBox:
    """合成途中の絵のどこに中身があるか（場面切り替えの場面の絵の範囲に使う）"""

    def test_the_box_is_where_the_picture_is_in_image_rows(
        self, gl_context: OffscreenGLContext
    ) -> None:
        # GL の行は下から数える 上下を取り違えると、場面の図形の上端と下端が入れ替わり、
        # 中心点の「下端」で回す場面切り替えが図形の上端で回る
        with gl_context:
            compositor = Compositor(40, 20)
            patch = Texture.from_array(_solid(10, 4, 255))
            compositor.begin((0.0, 0.0, 0.0, 0.0))
            compositor.draw(patch, placement=Placement(5.0, 2.0, 10.0, 4.0))
            box = compositor.content_box()
            compositor.begin((0.0, 0.0, 0.0, 0.0))
            empty = compositor.content_box()
            compositor.release()
            patch.release()
        assert box == (5, 2, 15, 6)
        assert empty is None

    def test_a_faint_picture_still_has_a_box(self, gl_context: OffscreenGLContext) -> None:
        """8 ビットへ丸めると 0 になるほど薄い絵でも、範囲を持つ

        キャンバスを 8 ビットで読んでから測ると、薄い場面が空に見え、回転やタイルが
        画面全体を基準にしてしまう
        """
        with gl_context:
            compositor = Compositor(40, 20)
            patch = Texture.from_array(_solid(10, 4, 255))
            compositor.begin((0.0, 0.0, 0.0, 0.0))
            compositor.draw(patch, placement=Placement(20.0, 10.0, 10.0, 4.0), opacity=0.001)
            faint = compositor.content_box()
            compositor.release()
            patch.release()
        assert faint == (20, 10, 30, 14)
