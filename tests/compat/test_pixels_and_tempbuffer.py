"""画素の読み書き・仮想バッファへ描く・表を渡す drawpoly

どれもテレビ字幕（配布物）が使う物で、仕様書（AviUtl2 の lua.txt）に意味が
書かれている 壊れても「板が出ない」「板の色が違う」としか見えず、どこで
崩れたかは分からないので、1 つずつ値で確かめる
"""

from __future__ import annotations

import numpy as np
import pytest

from kumiki.compat.aviutl import raster
from kumiki.compat.aviutl.mapping import _spec_value
from kumiki.compat.aviutl.objapi import ObjectState
from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.compat.aviutl.runtime import LuaScriptRuntime
from kumiki.core.model import GeneratedSource
from kumiki.effects.spec import ColorSpec
from kumiki.engine.sources import render_source

YELLOW = (255, 212, 0, 255)


def _source(kind: str, params: dict[str, object], width: int, height: int) -> np.ndarray:
    """本物の描き方で図形を作る（大きさの扱いを確かめるため、偽物にしない）"""
    from kumiki.core.model import AnimatedValue

    wrapped = {
        name: AnimatedValue(float(value)) if isinstance(value, int | float) else value
        for name, value in params.items()
        if not isinstance(value, bool)
    } | {name: value for name, value in params.items() if isinstance(value, bool)}
    image = render_source(GeneratedSource(kind=kind, params=wrapped), width, height)  # type: ignore[arg-type]
    assert image is not None
    return image


@pytest.fixture
def runtime() -> LuaScriptRuntime:
    return LuaScriptRuntime(render_source=_source, instruction_limit=500_000)


def _state(width: int = 20, height: int = 10) -> ObjectState:
    image = np.zeros((height, width, 4), np.uint8)
    image[..., 0] = np.arange(width, dtype=np.uint8)[None, :]
    image[..., 3] = 255
    return ObjectState(image=image, screen_w=320, screen_h=180)


class TestTriangles:
    def test_two_halves_make_one_even_rectangle(self) -> None:
        """四角を 2 つの三角形に分けて別々に描いても、境目が濃くも薄くもならない

        テレビ字幕は板を三角形 2 つで、別々の呼び出しで描く 境目の画素を
        両方で塗ると、半透明の板にだけ対角線が濃く出る
        """
        buffer = np.zeros((20, 40, 4), np.uint8)
        texture = np.full((4, 4, 4), YELLOW, np.uint8)
        x1, y1, x2, y2 = -10, -5, 10, 5
        raster.draw_triangle(
            buffer,
            ((x1, y1), (x1, y2), (x2, y1)),
            texture=texture,
            uvs=((0, 0), (0, 1), (1, 0)),
            alpha=0.5,
        )
        raster.draw_triangle(
            buffer,
            ((x1, y2), (x2, y2), (x2, y1)),
            texture=texture,
            uvs=((0, 1), (1, 1), (1, 0)),
            alpha=0.5,
        )
        alpha = buffer[..., 3]
        assert int((alpha > 0).sum()) == 20 * 10, "隙間か、はみ出しがある"
        assert set(alpha[alpha > 0].tolist()) == {128}, "境目が二重に塗られている"

    def test_the_winding_does_not_matter(self) -> None:
        # 頂点の並びが時計回りでも反時計回りでも同じ画素を塗る
        first = np.zeros((10, 10, 4), np.uint8)
        second = np.zeros((10, 10, 4), np.uint8)
        texture = np.full((1, 1, 4), YELLOW, np.uint8)
        uvs = ((0.0, 0.0), (0.0, 0.0), (0.0, 0.0))
        raster.draw_triangle(first, ((-5, -5), (5, -5), (-5, 5)), texture=texture, uvs=uvs)
        raster.draw_triangle(second, ((-5, -5), (-5, 5), (5, -5)), texture=texture, uvs=uvs)
        assert np.array_equal(first, second)

    def test_the_picture_is_mapped_by_uv(self) -> None:
        """u, v は 0〜1（仕様書どおり正規化） 画素で読むと、絵の左上の 1 画素しか貼れない"""
        buffer = np.zeros((10, 20, 4), np.uint8)
        texture = np.zeros((1, 2, 4), np.uint8)
        texture[0, 0] = (255, 0, 0, 255)
        texture[0, 1] = (0, 0, 255, 255)
        quad = [(-10, -5), (10, -5), (10, 5), (-10, 5)]
        uv = [(0, 0), (1, 0), (1, 1), (0, 1)]
        for a, b, c in ((0, 1, 2), (0, 2, 3)):
            raster.draw_triangle(
                buffer, (quad[a], quad[b], quad[c]), texture=texture, uvs=(uv[a], uv[b], uv[c])
            )
        assert buffer[5, 2].tolist() == [255, 0, 0, 255]
        assert buffer[5, 17].tolist() == [0, 0, 255, 255]

    def test_vertex_colors_are_premultiplied(self) -> None:
        """頂点の色は乗算済みアルファ（仕様書） ストレートとして読むと半透明の色が暗く沈む"""
        buffer = np.zeros((10, 10, 4), np.uint8)
        half_red = (0.5, 0.0, 0.0, 0.5)
        raster.draw_triangle(
            buffer, ((-5, -5), (5, -5), (-5, 5)), colors=(half_red, half_red, half_red)
        )
        assert buffer[1, 1].tolist() == [255, 0, 0, 128]


class TestPastingAPicture:
    def test_it_is_centred(self) -> None:
        buffer = np.zeros((10, 10, 4), np.uint8)
        image = np.full((2, 4, 4), 255, np.uint8)
        raster.draw_image(buffer, image)
        rows, columns = np.nonzero(buffer[..., 3])
        box = (int(rows.min()), int(rows.max()), int(columns.min()), int(columns.max()))
        assert box == (4, 5, 3, 6)

    def test_it_is_clipped_at_the_edge(self) -> None:
        # はみ出した分は捨てる 落とすと、画面の端に置いた字幕で描画ごと止まる
        buffer = np.zeros((4, 4, 4), np.uint8)
        raster.draw_image(buffer, np.full((2, 2, 4), 255, np.uint8), x=2)
        assert int(buffer[..., 3].astype(bool).sum()) == 2 * 1


class TestPixelData:
    def test_it_goes_there_and_back(self, runtime: LuaScriptRuntime) -> None:
        """読んで戻すだけなら絵は変わらない（DLL が何もしなかった場合）"""
        state = _state()
        before = state.image.copy()
        runtime.run(
            'local d, w, h = obj.getpixeldata("object")\nobj.putpixeldata("object", d, w, h)',
            state,
        )
        assert np.array_equal(state.image, before)

    def test_the_size_comes_back(self, runtime: LuaScriptRuntime) -> None:
        state = _state(20, 10)
        runtime.run('local d, w, h = obj.getpixeldata("object")\nobj.ox = w * 100 + h', state)
        assert state.ox == 2010

    def test_bgra_swaps_red_and_blue(self, runtime: LuaScriptRuntime) -> None:
        """``bgra`` で読んで ``rgba`` で書くと赤と青が入れ替わる（並びを変えて渡している）"""
        state = _state()
        state.image[..., :3] = (10, 20, 30)
        runtime.run(
            'local d, w, h = obj.getpixeldata("object", "bgra")\n'
            'obj.putpixeldata("object", d, w, h, "rgba")',
            state,
        )
        assert state.image[0, 0, :3].tolist() == [30, 20, 10]

    def test_a_different_size_is_refused(self, runtime: LuaScriptRuntime) -> None:
        """受けたときと違う大きさで書かない 別の並びとして読むことになり絵が崩れる"""
        report = CompatibilityReport()
        runtime = LuaScriptRuntime(render_source=_source, report=report)
        state = _state()
        before = state.image.copy()
        runtime.run(
            'local d, w, h = obj.getpixeldata("object")\nobj.putpixeldata("object", d, w + 1, h)',
            state,
        )
        assert np.array_equal(state.image, before)
        assert any("違う大きさ" in line for line in report.lines())

    def test_the_framebuffer_is_not_visible(self) -> None:
        # スクリプトの中から、それまでに描いた画面は見えない 黙って空を返さない
        report = CompatibilityReport()
        runtime = LuaScriptRuntime(render_source=_source, report=report)
        runtime.run('local d = obj.getpixeldata("framebuffer")', _state())
        assert any("framebuffer" in line for line in report.lines())


class TestTheTempBuffer:
    def test_a_size_makes_a_clear_buffer(self, runtime: LuaScriptRuntime) -> None:
        """大きさを渡すと、その大きさの透明な物で作り直す（仕様書）"""
        state = _state()
        state.buffers["tmp"] = np.full((3, 3, 4), 255, np.uint8)
        runtime.run('obj.setoption("drawtarget", "tempbuffer", 40, 30)', state)
        assert state.buffers["tmp"].shape == (30, 40, 4)
        assert not state.buffers["tmp"].any()

    def test_draw_goes_to_the_buffer_not_the_screen(self, runtime: LuaScriptRuntime) -> None:
        """描く先を仮想バッファにしている間は、画面へ描かない"""
        state = _state(4, 2)
        runtime.run(
            'obj.setoption("drawtarget", "tempbuffer", 10, 10)\nobj.draw()\n'
            'obj.setoption("drawtarget", "framebuffer")',
            state,
        )
        assert state.draws == []
        assert int(state.buffers["tmp"][..., 3].astype(bool).sum()) == 4 * 2

    def test_the_objects_position_is_not_used(self, runtime: LuaScriptRuntime) -> None:
        """仮想バッファへは引数の座標のまま（オブジェクトの位置は反映しない 仕様書）"""
        state = _state(2, 2)
        state.ox = 100
        runtime.run('obj.setoption("drawtarget", "tempbuffer", 10, 10)\nobj.draw()', state)
        _rows, columns = np.nonzero(state.buffers["tmp"][..., 3])
        assert (columns.min(), columns.max()) == (4, 5)

    def test_a_rotated_draw_is_recorded(self) -> None:
        # 回したり拡げたりして仮想バッファへ描くのはまだ写していない 黙って等倍で描かない
        report = CompatibilityReport()
        runtime = LuaScriptRuntime(render_source=_source, report=report)
        state = _state()
        runtime.run(
            'obj.setoption("drawtarget", "tempbuffer", 10, 10)\nobj.draw(0, 0, 0, 2)', state
        )
        assert not state.buffers["tmp"].any()
        assert any("回転か拡大" in line for line in report.lines())

    def test_loading_it_makes_it_the_object(self, runtime: LuaScriptRuntime) -> None:
        state = _state(4, 2)
        runtime.run(
            'obj.setoption("drawtarget", "tempbuffer", 10, 8)\nobj.draw()\n'
            'obj.setoption("drawtarget", "framebuffer")\nobj.load("tempbuffer")',
            state,
        )
        assert state.image.shape == (8, 10, 4)


class TestDrawpolyWithATable:
    def test_triangles_from_a_vertex_list(self, runtime: LuaScriptRuntime) -> None:
        """``obj.drawpoly({頂点の表}, 3, 透明度)`` テレビ字幕が板を描く形"""
        state = _state()
        state.image[...] = YELLOW
        runtime.run(
            'obj.setoption("drawtarget", "tempbuffer", 20, 10)\n'
            "obj.drawpoly({{-10,-5,0,0,0},{-10,5,0,0,1},{10,-5,0,1,0}}, 3, 1.0)\n"
            "obj.drawpoly({{-10,5,0,0,1},{10,5,0,1,1},{10,-5,0,1,0}}, 3, 1.0)",
            state,
        )
        buffer = state.buffers["tmp"]
        assert int(buffer[..., 3].astype(bool).sum()) == 200
        assert buffer[5, 10].tolist() == list(YELLOW)

    def test_a_quad_from_a_vertex_list_goes_to_the_screen(self, runtime: LuaScriptRuntime) -> None:
        """画面へ直に描く四角形は、今までの描画へ渡す（u, v は画素へ直す）"""
        state = _state(20, 10)
        runtime.run(
            "obj.drawpoly({{-10,-5,0,0,0},{10,-5,0,1,0},{10,5,0,1,1},{-10,5,0,0,1}})", state
        )
        assert len(state.draws) == 1
        assert state.draws[0].uv == ((0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0))

    def test_a_triangle_to_the_screen_is_recorded(self) -> None:
        # 画面へ描く側は三角形をまだ持っていない 黙って捨てない
        report = CompatibilityReport()
        runtime = LuaScriptRuntime(render_source=_source, report=report)
        runtime.run("obj.drawpoly({{0,0,0,0,0},{1,0,0,1,0},{0,1,0,0,1}}, 3)", _state())
        assert any("三角形" in line for line in report.lines())


class TestFigures:
    def test_a_figure_is_its_own_size(self, runtime: LuaScriptRuntime) -> None:
        """``obj.load("figure", …)`` の絵は図形の大きさちょうど（``obj.w`` が大きさ）

        画面の大きさのまま持つと、図形を 0〜1 で貼るスクリプト（テレビ字幕の板）が
        ほとんど透明な所を貼って、板が消える
        """
        state = _state()
        runtime.run('obj.load("figure", "四角形", 0xffd400)\nobj.ox = obj.w', state)
        assert state.ox == 100
        assert state.image[50, 50].tolist() == list(YELLOW)

    def test_a_given_size(self, runtime: LuaScriptRuntime) -> None:
        state = _state()
        runtime.run('obj.load("figure", "四角形", 0xffd400, 40)', state)
        assert state.image.shape[:2] == (40, 40)


class TestOffscreen:
    def test_with_nothing_stacked_the_picture_stays(self) -> None:
        """先に積んだ効果が無ければ絵は変わらない（配布物のテレビ字幕はこの形）"""
        report = CompatibilityReport()
        runtime = LuaScriptRuntime(render_source=_source, report=report)
        state = _state()
        before = state.image.copy()
        runtime.run('obj.effect("オフスクリーン描画")', state)
        assert np.array_equal(state.image, before)
        assert not any("オフスクリーン" in line for line in report.lines())

    def test_with_effects_stacked_it_is_recorded(self) -> None:
        # 積んだ効果の焼き込みはまだ写していない 黙って素通しにしない
        report = CompatibilityReport()
        runtime = LuaScriptRuntime(render_source=_source, report=report)
        runtime.run('obj.effect("ぼかし", "範囲", 5)\nobj.effect("オフスクリーン描画")', _state())
        assert any("焼き込み" in line for line in report.lines())


class TestScriptColours:
    def test_a_hex_colour_is_read(self) -> None:
        """エイリアスの色（``ffd400``）を色として読む

        そのまま渡すと色として読めず、既定の黒へ落ちる テレビ字幕の板が
        黒の上に黒で描かれて、何も出ていないように見えていた
        """
        spec = ColorSpec("color", "背景色", (0.0, 0.0, 0.0, 1.0))
        value = _spec_value(spec, "ffd400", (), CompatibilityReport(), "背景色")
        assert spec.coerce(value) == pytest.approx((1.0, 212 / 255, 0.0, 1.0))
