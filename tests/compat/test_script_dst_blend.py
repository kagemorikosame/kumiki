"""AviUtl1 の描き先の指定 ``obj.setoption("dst", …)`` と仮想バッファへの合成モード（#190）

AviUtl1 のスクリプトは描き先を ``obj.setoption("dst", "tmp", 幅, 高さ)`` で仮想バッファへ
向ける（AviUtl2 の ``"drawtarget", "tempbuffer"`` と同じ物） 知らない名前として捨てると
仮想バッファへ描くつもりの ``obj.draw`` が画面へ出て、sigma の 内側シャドー は影の絵を
画面へ直に描いて 20px ずれ、縁取りα は写す相手の仮想バッファが無いまま何も描かない

sigma は仮想バッファへ ``alpha_add`` と ``alpha_sub`` で重ねて、角を削ったり縁だけを
残したりする 通常の重ね方で描くと、削るつもりの所に黒い絵が重なる

あわせて ``obj.effect("反転", "透明度反転", 1)`` をその場で掛ける sigma は透明な所と
不透明な所を入れ替えて、内側の影や縁の形を作る
"""

from __future__ import annotations

import numpy as np
import pytest

from sashimono.compat.aviutl import raster
from sashimono.compat.aviutl.objapi import EffectRequest, ObjectState
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.aviutl.runtime import LuaScriptRuntime


def _white(width: int = 6, height: int = 4) -> np.ndarray:
    return np.full((height, width, 4), 255, np.uint8)


def _run(
    source: str,
    *,
    image: np.ndarray | None = None,
    report: CompatibilityReport | None = None,
    baker: object = None,
) -> ObjectState:
    state = ObjectState(image=_white() if image is None else image, screen_w=320, screen_h=180)
    runtime = LuaScriptRuntime(report=report or CompatibilityReport(), apply_effects=baker)
    result = runtime.run(source, state)
    assert not result.failed, result.message
    return state


class TestDst:
    def test_dst_tmp_draws_into_the_temp_buffer(self) -> None:
        # 画面へ描くと描画の記録が増え、仮想バッファは空のまま 読み戻すと透明な絵になる
        report = CompatibilityReport()
        state = _run('obj.setoption("dst", "tmp", 10, 8) obj.draw()', report=report)
        assert state.draws == []
        buffer = state.buffers["tmp"]
        assert buffer.shape[:2] == (8, 10)
        assert buffer[2:6, 2:8, 3].min() == 255
        assert not any("dst" in line for line in report.missing)

    def test_dst_tmp_without_a_size_keeps_the_buffer(self) -> None:
        # 大きさを省くと初期化しない sigma は obj.copybuffer("tmp", "obj") で絵を写してから
        # 大きさ無しで描き先を向ける 作り直すと写した絵が消える
        state = _run(
            'obj.copybuffer("tmp", "obj") obj.setoption("dst", "tmp")'
            ' obj.setoption("blend", "alpha_sub") obj.draw() obj.setoption("blend", 0)'
        )
        assert state.buffers["tmp"].shape[:2] == (4, 6)
        assert state.buffers["tmp"][..., 3].max() == 0

    def test_dst_frm_goes_back_to_the_screen(self) -> None:
        state = _run('obj.setoption("dst", "tmp", 10, 8) obj.setoption("dst", "frm") obj.draw()')
        assert len(state.draws) == 1
        assert state.buffers["tmp"][..., 3].max() == 0

    def test_an_unknown_target_is_recorded(self) -> None:
        report = CompatibilityReport()
        state = _run('obj.setoption("dst", "xyz") obj.draw()', report=report)
        assert len(state.draws) == 1
        assert any('obj.setoption("dst")' in line for line in report.missing)


def _pixel(rgb: tuple[int, int, int], alpha: int) -> np.ndarray:
    return np.array([[[*rgb, alpha]]], np.uint8)


class TestAlphaBlends:
    def test_alpha_add_sums_the_opacity(self) -> None:
        # 通常の重ね方では 半分 + 半分 が 3/4 になる sigma は透明度を反転した絵を alpha_add で
        # 重ねて隙間の無い 1 枚にするので、縁が半透明のまま残る
        buffer = _pixel((255, 0, 0), 128)
        raster.draw_image(buffer, _pixel((0, 0, 255), 127), blend="alpha_add")
        red, green, blue, alpha = (int(v) for v in buffer[0, 0])
        assert alpha == 255
        # 色は不透明度で重みを付けた平均
        assert abs(red - 128) <= 1 and green == 0 and abs(blue - 127) <= 1

    def test_alpha_sub_removes_opacity_and_keeps_the_colour(self) -> None:
        # 通常の重ね方だと削る絵の色（黒など）が重なって、角が黒く残る
        buffer = _pixel((255, 0, 0), 255)
        raster.draw_image(buffer, _pixel((0, 0, 0), 200), blend="alpha_sub")
        assert tuple(int(v) for v in buffer[0, 0]) == (255, 0, 0, 55)

    def test_alpha_sub_does_not_go_below_zero(self) -> None:
        buffer = _pixel((255, 0, 0), 50)
        raster.draw_image(buffer, _pixel((0, 0, 0), 200), blend="alpha_sub")
        assert int(buffer[0, 0, 3]) == 0

    def test_alpha_max_takes_the_larger_opacity(self) -> None:
        buffer = _pixel((255, 0, 0), 200)
        raster.draw_image(buffer, _pixel((0, 0, 255), 100), blend="alpha_max")
        assert int(buffer[0, 0, 3]) == 200

    def test_alpha_add2_lays_the_colour_over(self) -> None:
        # 色は通常の重ね方、不透明度は足す 不透明な絵を重ねた所は上の色になる
        buffer = _pixel((255, 0, 0), 128)
        raster.draw_image(buffer, _pixel((0, 0, 255), 255), blend="alpha_add2")
        assert tuple(int(v) for v in buffer[0, 0]) == (0, 0, 255, 255)

    def test_the_blend_applies_to_polygons_too(self) -> None:
        # sigma の角丸は drawpoly で角の形を alpha_sub で削る
        buffer = np.full((4, 4, 4), 255, np.uint8)
        raster.draw_triangle(
            buffer,
            ((-2.0, -2.0), (2.0, -2.0), (2.0, 2.0)),
            colors=((0.0, 0.0, 0.0, 1.0),) * 3,
            blend="alpha_sub",
        )
        assert int(buffer[0, 3, 3]) == 0
        assert tuple(int(v) for v in buffer[3, 0]) == (255, 255, 255, 255)

    def test_the_script_blend_reaches_the_temp_buffer(self) -> None:
        # 絵ごと alpha_sub で重ねれば、同じ形の所が透明になる
        state = _run(
            'obj.setoption("dst", "tmp", 6, 4) obj.draw()'
            ' obj.setoption("blend", "alpha_sub") obj.draw() obj.setoption("blend", 0)'
        )
        assert state.buffers["tmp"][..., 3].max() == 0

    @pytest.mark.parametrize("mode", ["2", '"sub"'])
    def test_other_modes_into_the_temp_buffer_are_recorded(self, mode: str) -> None:
        # 仮想バッファへの 減算 などはまだ写していない 黙って通常で描くと形が違っても気づけない
        report = CompatibilityReport()
        _run(
            f'obj.setoption("dst", "tmp", 6, 4) obj.setoption("blend", {mode}) obj.draw()',
            report=report,
        )
        assert any("blend" in line for line in report.missing)

    def test_numbered_zero_is_normal(self) -> None:
        # 旧形式の数 0 は通常 記録に残すと sigma が 1 回描くたびに未対応として数えられる
        report = CompatibilityReport()
        _run(
            'obj.setoption("dst", "tmp", 6, 4) obj.setoption("blend", 0) obj.draw()',
            report=report,
        )
        assert not any("blend" in line for line in report.missing)


class TestInvertAlpha:
    def test_the_opacity_is_flipped_at_once(self) -> None:
        # sigma は 領域拡張 の直後に透明度を反転して、広げた所を不透明にする 描くときまで
        # 待つと、その間の obj.copybuffer が反転する前の絵を写す
        image = _white()
        image[0, 0, 3] = 0
        state = _run('obj.effect("反転", "透明度反転", 1)', image=image)
        assert state.effects == []
        assert int(state.image[0, 0, 3]) == 255
        assert state.image[1:, :, 3].max() == 0

    def test_effects_stacked_before_are_applied_first(self) -> None:
        # 縁取りα は 縁取り を積んだ後で反転する 縁取りの前の絵を反転すると縁が消える
        calls: list[tuple[str, ...]] = []

        def bake(image: np.ndarray, effects: tuple[EffectRequest, ...]) -> np.ndarray:
            calls.append(tuple(effect.kind for effect in effects))
            return np.pad(image, ((1, 1), (1, 1), (0, 0)))

        state = _run(
            'obj.effect("ぼかし", "範囲", 1) obj.effect("反転", "透明度反転", 1)', baker=bake
        )
        assert calls == [("blur",)]
        assert state.image.shape[:2] == (6, 8)
        assert int(state.image[0, 0, 3]) == 255
        assert int(state.image[2, 2, 3]) == 0

    def test_without_a_baker_it_waits_for_the_draw(self) -> None:
        # 先に積んだ効果を掛けられなければ、順を守るため反転も積んで描くときへ回す
        report = CompatibilityReport()
        state = _run(
            'obj.effect("ぼかし", "範囲", 1) obj.effect("反転", "透明度反転", 1)', report=report
        )
        assert [effect.kind for effect in state.effects] == ["blur", "flip"]
        assert int(state.image[0, 0, 3]) == 255

    def test_mirroring_with_it_is_done_too(self) -> None:
        image = _white()
        image[0, 0] = (255, 0, 0, 128)
        state = _run('obj.effect("反転", "透明度反転", 1, "左右反転", 1)', image=image)
        assert tuple(int(v) for v in state.image[0, 5]) == (255, 0, 0, 127)

    def test_without_the_alpha_flag_it_stays_a_filter(self) -> None:
        # 左右反転だけなら今までどおり描くときに GPU で掛ける
        state = _run('obj.effect("反転", "左右反転", 1)')
        assert [effect.kind for effect in state.effects] == ["flip"]
