"""Lua ランタイムと ``obj`` API。

配布スクリプトは他人が書いたコードで、読み込むだけで走る。サンドボックスと
実行時間の上限は「動くこと」と同じくらい大事なので、そこも見る。
"""

from __future__ import annotations

import numpy as np
import pytest

from novaedit.compat.aviutl.objapi import ObjectState
from novaedit.compat.aviutl.report import CompatibilityReport
from novaedit.compat.aviutl.runtime import LuaScriptRuntime, blank_image, lua_available


@pytest.fixture(scope="module")
def runtime() -> LuaScriptRuntime:
    """使い回すランタイム。作るのに数ミリ秒かかる。"""
    return LuaScriptRuntime(instruction_limit=200_000)


def state(width: int = 64, height: int = 64, **fields: object) -> ObjectState:
    return ObjectState(image=blank_image(width, height), **fields)  # type: ignore[arg-type]


class TestAvailability:
    def test_lua_is_there_by_default(self) -> None:
        # P5 の前提。追加導入なしで動くこと。
        assert lua_available() is True

    def test_it_is_lua_5_1(self, runtime: LuaScriptRuntime) -> None:
        # AviUtl 本体が 5.1。配布スクリプトは unpack や setfenv を普通に使う。
        target = state()
        runtime.run("obj.ox = (_VERSION == 'Lua 5.1') and 1 or 0", target)
        assert target.ox == 1.0

    def test_five_one_only_functions_exist(self, runtime: LuaScriptRuntime) -> None:
        target = state()
        runtime.run("obj.ox = (unpack ~= nil and setfenv ~= nil) and 1 or 0", target)
        assert target.ox == 1.0


class TestFields:
    def test_writable_fields_round_trip(self, runtime: LuaScriptRuntime) -> None:
        target = state()
        runtime.run("obj.ox=1 obj.oy=2 obj.zoom=3 obj.alpha=0.5 obj.rz=90 obj.aspect=0.25", target)
        assert (target.ox, target.oy, target.zoom) == (1.0, 2.0, 3.0)
        assert (target.alpha, target.rz, target.aspect) == (0.5, 90.0, 0.25)

    def test_size_is_read_from_the_image(self, runtime: LuaScriptRuntime) -> None:
        target = state(width=120, height=80)
        runtime.run("obj.ox = obj.w  obj.oy = obj.h", target)
        assert (target.ox, target.oy) == (120.0, 80.0)

    def test_time_comes_from_the_frame_and_rate(self, runtime: LuaScriptRuntime) -> None:
        target = state(frame=15, totalframe=60, framerate=30.0)
        runtime.run("obj.ox = obj.time  obj.oy = obj.totaltime  obj.oz = obj.frame", target)
        assert (target.ox, target.oy, target.oz) == (0.5, 2.0, 15.0)

    def test_track_values_are_visible(self, runtime: LuaScriptRuntime) -> None:
        target = state()
        target.track = [10.0, 20.0, 30.0, 40.0]
        runtime.run("obj.ox = obj.track0 + obj.track3", target)
        assert target.ox == 50.0

    def test_check0_is_a_number(self, runtime: LuaScriptRuntime) -> None:
        # AviUtl では 0 か 1。真偽値で返すと ``obj.check0 == 1`` が偽になる。
        target = state()
        target.check0 = True
        runtime.run("obj.ox = obj.check0", target)
        assert target.ox == 1.0

    def test_named_parameters_are_visible(self, runtime: LuaScriptRuntime) -> None:
        # 名前付きの値は、大域変数としても obj 越しにも読める。配布スクリプトは
        # 前者を使うが、どちらで書かれていても動くようにしてある。
        target = state()
        target.values["amount"] = 7.0
        runtime.run("obj.ox = amount  obj.oy = obj.amount", target)
        assert (target.ox, target.oy) == (7.0, 7.0)

    def test_writing_a_read_only_field_is_recorded(self) -> None:
        report = CompatibilityReport()
        runtime = LuaScriptRuntime(report=report, instruction_limit=100_000)
        runtime.run("obj.w = 999", state())
        assert any("obj.w" in line for line in report.lines())


class TestDrawing:
    def test_without_an_explicit_draw_the_state_is_drawn_once(
        self, runtime: LuaScriptRuntime
    ) -> None:
        result = runtime.run("obj.ox = 40", state())
        assert len(result.draws) == 1
        assert result.draws[0].x == 40.0

    def test_explicit_draws_replace_the_automatic_one(self, runtime: LuaScriptRuntime) -> None:
        # 残像や複製を作るスクリプトはこの仕組みで動いている。
        result = runtime.run("for i=1,4 do obj.draw(i*10, 0, 0, 1, 0.25) end", state())
        assert [call.x for call in result.draws] == [10.0, 20.0, 30.0, 40.0]
        assert all(call.alpha == 0.25 for call in result.draws)

    def test_draw_without_arguments_uses_the_current_values(
        self, runtime: LuaScriptRuntime
    ) -> None:
        result = runtime.run("obj.ox = 12 obj.rz = 30 obj.draw()", state())
        assert (result.draws[0].x, result.draws[0].rz) == (12.0, 30.0)

    def test_drawpoly_is_recorded_as_missing(self) -> None:
        report = CompatibilityReport()
        runtime = LuaScriptRuntime(report=report, instruction_limit=100_000)
        runtime.run("obj.drawpoly(0,0,0, 1,0,0, 1,1,0, 0,1,0)", state())
        assert any("drawpoly" in line for line in report.lines())


class TestEffects:
    def test_a_known_filter_is_queued(self, runtime: LuaScriptRuntime) -> None:
        target = state()
        result = runtime.run('obj.effect("ぼかし", "範囲", 20)', target)
        assert [request.kind for request in result.draws[0].effects] == ["blur"]
        assert result.draws[0].effects[0].params == {"範囲": 20.0}

    def test_an_unknown_filter_is_recorded(self) -> None:
        report = CompatibilityReport()
        runtime = LuaScriptRuntime(report=report, instruction_limit=100_000)
        runtime.run('obj.effect("まだ無いフィルタ")', state())
        assert any("まだ無いフィルタ" in line for line in report.lines())


class TestHelpers:
    def test_rgb_and_hsv(self, runtime: LuaScriptRuntime) -> None:
        target = state()
        runtime.run("obj.ox = RGB(255,128,0)  obj.oy = HSV(0,255,255)", target)
        assert target.ox == 0xFF8000
        assert target.oy == 0xFF0000

    def test_bit_operations(self, runtime: LuaScriptRuntime) -> None:
        target = state()
        runtime.run("obj.ox = OR(5,2) obj.oy = AND(6,3) obj.oz = XOR(5,3)", target)
        assert (target.ox, target.oy, target.oz) == (7.0, 2.0, 6.0)

    def test_shift(self, runtime: LuaScriptRuntime) -> None:
        target = state()
        runtime.run("obj.ox = SHIFT(1, 4)  obj.oy = SHIFT(16, -2)", target)
        assert (target.ox, target.oy) == (16.0, 4.0)

    def test_rand_is_stable_within_a_frame(self, runtime: LuaScriptRuntime) -> None:
        # 毎回ばらつくと、1 フレーム描き直すたびに絵が変わる。
        first = state(frame=7)
        runtime.run("obj.ox = obj.rand(0, 1000)", first)
        second = state(frame=7)
        runtime.run("obj.ox = obj.rand(0, 1000)", second)
        assert first.ox == second.ox

    def test_rand_changes_between_frames(self, runtime: LuaScriptRuntime) -> None:
        values = set()
        for frame in range(8):
            target = state(frame=frame)
            runtime.run("obj.ox = obj.rand(0, 100000)", target)
            values.add(target.ox)
        assert len(values) > 1

    def test_interpolation_passes_through_the_points(self, runtime: LuaScriptRuntime) -> None:
        target = state()
        runtime.run("obj.ox = obj.interpolation(0.0, 10, 20, 30)", target)
        assert target.ox == 10.0
        runtime.run("obj.oy = obj.interpolation(1.0, 10, 20, 30)", target)
        assert target.oy == 30.0


class TestTextAndFigures:
    def test_setfont_and_mes_make_an_image(self, qt_application: object) -> None:
        del qt_application
        from novaedit.core.model import GeneratedSource
        from novaedit.engine.sources import render_source

        def draw(kind: str, params: dict[str, object], width: int, height: int) -> np.ndarray:
            from novaedit.core.model import AnimatedValue

            wrapped = {
                name: AnimatedValue(float(value)) if isinstance(value, int | float) else value
                for name, value in params.items()
            }
            image = render_source(GeneratedSource(kind=kind, params=wrapped), width, height)  # type: ignore[arg-type]
            assert image is not None
            return image

        runtime = LuaScriptRuntime(render_source=draw, instruction_limit=200_000)
        target = state(width=200, height=100)
        target.screen_w, target.screen_h = 200, 100
        runtime.run('obj.setfont("Yu Gothic UI", 40, 1, 0xffffff)  obj.mes("あ")', target)
        assert target.image.shape == (100, 200, 4)
        assert int((target.image[..., 3] > 0).sum()) > 0

    def test_load_figure_uses_the_aviutl_names(self, qt_application: object) -> None:
        del qt_application
        from novaedit.core.model import AnimatedValue, GeneratedSource
        from novaedit.engine.sources import render_source

        seen: dict[str, object] = {}

        def draw(kind: str, params: dict[str, object], width: int, height: int) -> np.ndarray:
            seen.update(params)
            wrapped = {
                name: AnimatedValue(float(value)) if isinstance(value, int | float) else value
                for name, value in params.items()
            }
            image = render_source(GeneratedSource(kind=kind, params=wrapped), width, height)  # type: ignore[arg-type]
            assert image is not None
            return image

        runtime = LuaScriptRuntime(render_source=draw, instruction_limit=200_000)
        runtime.run('obj.load("figure", "六角形", 0xff0000, 80)', state(200, 200))
        assert seen["shape"] == "hexagon"
        assert seen["color"] == (1.0, 0.0, 0.0, 1.0)


class TestSandbox:
    def test_file_access_is_closed(self, runtime: LuaScriptRuntime) -> None:
        result = runtime.run('local f = io.open("秘密.txt")', state())
        assert result.failed is True

    def test_process_access_is_closed(self, runtime: LuaScriptRuntime) -> None:
        assert runtime.run('os.execute("calc")', state()).failed is True

    def test_require_only_reaches_the_script_folders(self, runtime: LuaScriptRuntime) -> None:
        # require は塞がずに、スクリプトフォルダの中だけへ向けてある。配布物は
        # 共通処理を別ファイルへ切り出しており、塞ぐと 1 行目で落ちる。
        target = state()
        result = runtime.run('obj.ox = require("どこにも無い") == nil and 1 or 0', target)
        assert result.failed is False
        assert target.ox == 1.0

    def test_require_cannot_escape_the_folders(self, runtime: LuaScriptRuntime) -> None:
        target = state()
        runtime.run('obj.ox = require("../../秘密") == nil and 1 or 0', target)
        assert target.ox == 1.0

    def test_an_infinite_loop_is_cut_off(self, runtime: LuaScriptRuntime) -> None:
        # 掛けておかないと、編集画面が戻ってこなくなる。
        result = runtime.run("while true do end", state())
        assert result.failed is True
        assert "長すぎ" in result.message

    def test_a_failure_still_draws_the_original(self, runtime: LuaScriptRuntime) -> None:
        # 1 つのスクリプトの失敗でフレームが真っ黒になる方が困る。
        result = runtime.run("error('わざと')", state())
        assert result.failed is True
        assert len(result.draws) == 1

    def test_a_syntax_error_is_reported(self, runtime: LuaScriptRuntime) -> None:
        result = runtime.run("this is not lua", state())
        assert result.failed is True

    def test_failures_are_recorded(self) -> None:
        report = CompatibilityReport()
        runtime = LuaScriptRuntime(report=report, instruction_limit=100_000)
        runtime.run("error('だめ')", state(), script="ためし.anm")
        assert any("ためし.anm" in line for line in report.lines())


class TestSetup:
    def test_param_code_runs_before_the_script(self, runtime: LuaScriptRuntime) -> None:
        from novaedit.compat.aviutl.control import parse_control

        header = parse_control("--param:base=5;")
        target = state()
        runtime.run("obj.ox = base * 2", target, header=header)
        assert target.ox == 10.0
