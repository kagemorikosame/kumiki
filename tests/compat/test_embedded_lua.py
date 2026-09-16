"""テキスト欄に埋め込んだ Lua（``<?…?>``）

字幕に Lua のソースが映るか、書き出した文字が消えるかの二択で壊れる どちらも
見れば分かるが、配布物を開いて初めて気付くので、ここで押さえる
"""

from __future__ import annotations

import pytest

from kumiki.compat.aviutl.embedded import build_source, literal_text, split_embedded
from kumiki.compat.aviutl.objapi import ObjectState
from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.compat.aviutl.runtime import LuaScriptRuntime, blank_image


@pytest.fixture(scope="module")
def runtime() -> LuaScriptRuntime:
    return LuaScriptRuntime(instruction_limit=200_000, report=CompatibilityReport())


def _state(frame: int = 0) -> ObjectState:
    return ObjectState(image=blank_image(1, 1), frame=frame, framerate=30.0, totalframe=300)


class TestSplitting:
    def test_code_and_text_alternate(self) -> None:
        assert split_embedded("残り<?mes(3)?>秒") == [
            (False, "残り"),
            (True, "mes(3)"),
            (False, "秒"),
        ]

    def test_an_unclosed_block_stays_text(self) -> None:
        # 閉じ忘れを Lua として走らせると、本文の残り全部が構文エラーで消える
        assert split_embedded("a <? b") == [(False, "a <? b")]

    def test_the_fallback_hides_the_code(self) -> None:
        # 失敗したときにソースを出すと、字幕にプログラムが映る
        assert literal_text("残り<?mes(3)?>秒") == "残り秒"


class TestExpansion:
    def test_too_much_output_falls_back_to_the_text(self) -> None:
        # 書き出しは Lua のメモリ上限の外に溜まる 止めないと、配布ファイルの 1 行で
        # ソフトごとメモリを使い切れる
        report = CompatibilityReport()
        runtime = LuaScriptRuntime(instruction_limit=200_000, report=report)
        text = "前<?for i = 1, 100 do mes(string.rep('a', 10000)) end?>後"
        assert runtime.expand_text(text, _state()) == "前後"
        assert report.lines()

    def test_mes_writes_in_place(self, runtime: LuaScriptRuntime) -> None:
        assert runtime.expand_text("残り<?mes(10 - 4)?>秒", _state()) == "残り6秒"

    def test_obj_follows_the_frame(self, runtime: LuaScriptRuntime) -> None:
        # 時刻で数え上げる字幕が、描くフレームごとに変わること
        text = "<?mes(string.format('%.1f', obj.time))?>"
        assert runtime.expand_text(text, _state(frame=45)) == "1.5"

    def test_blocks_share_variables(self, runtime: LuaScriptRuntime) -> None:
        # AviUtl では前の <? ?> で作った変数を後ろで使える 別々に走らせると nil になる
        assert runtime.expand_text("<?n = 7?>値=<?mes(n)?>", _state()) == "値=7"

    def test_the_short_form_prints_the_value(self, runtime: LuaScriptRuntime) -> None:
        assert runtime.expand_text("<?=1+2?>", _state()) == "3"

    def test_text_with_brackets_survives(self, runtime: LuaScriptRuntime) -> None:
        # 本文に Lua の長い文字列の閉じ括弧が入っていても、途中で切れないこと
        assert runtime.expand_text("]] ]=] 前<?mes('x')?>\n後", _state()) == "]] ]=] 前x\n後"

    def test_a_broken_block_shows_only_the_text(self, runtime: LuaScriptRuntime) -> None:
        assert runtime.expand_text("前<?mes(?>後", _state()) == "前後"

    def test_mes_does_not_leak_into_scripts(self, runtime: LuaScriptRuntime) -> None:
        # 残ると、次のスクリプトの mes がテキストの書き出しを呼んで落ちる
        runtime.expand_text("<?mes(1)?>", _state())
        target = _state()
        runtime.run("obj.ox = (mes == nil) and 1 or 0", target)
        assert target.ox == 1.0

    def test_plain_text_is_untouched(self, runtime: LuaScriptRuntime) -> None:
        assert runtime.expand_text("ただの字幕", _state()) == "ただの字幕"

    def test_the_source_is_one_chunk(self) -> None:
        assert build_source("a<?x=1?>").count("function mes") == 1
