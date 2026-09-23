"""合成フォント（AviUtl2 の汎用プラグイン ``comfont.aux2``）まわり

配布エイリアス「合成フォントテキスト」は 3 つが揃って初めて絵になる

1. ``obj.getfont()`` が大きさと字間を返す
2. ``obj.module("compositefont")`` が汎用プラグインの登録した表を返す
3. テキスト欄の中の ``obj.mes`` が本文を書き出す

どれが欠けてもオブジェクトは真っ黒のまま出る 実物のプラグインは
リポジトリに入れられないので、**実物を使う試験は無ければ飛ばし**、
仕組みの方は偽のプラグインと偽の登録で確かめる
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sashimono.compat.aviutl import native, plugin
from sashimono.compat.aviutl.objapi import ObjApi, ObjectState
from sashimono.compat.aviutl.runtime import LuaScriptRuntime, blank_image
from sashimono.core.model import AnimatedValue
from sashimono.engine.render.scripts import text_font

#: 実物の置き場 利用者の AviUtl2 にしか無い
_REAL_PLUGIN = Path(os.environ.get("PROGRAMDATA", "")) / "aviutl2" / "Plugin" / "comfont.aux2"


@pytest.fixture(autouse=True)
def allowed() -> Iterator[None]:
    """試験のたびに設定を戻す 切ったままにすると、後の試験が読めなくなる"""
    native.set_enabled(True)
    plugin.forget()
    yield
    native.set_enabled(True)
    plugin.forget()


def _state() -> ObjectState:
    return ObjectState(image=blank_image(4, 4))


class TestGetFont:
    """``obj.getfont`` AviUtl2 の lua.txt が定める並びで返す"""

    def test_returns_what_setfont_was_given(self) -> None:
        """往復で値が変わると、書体を取って組み直すスクリプトが別の絵を出す"""
        api = ObjApi(_state())
        api.lua_setfont("Yu Mincho", 72, 3, 0x112233, 0x445566, True, False, 4, 8)
        assert api.lua_getfont() == ("Yu Mincho", 72.0, 3, 0x112233, 0x445566, True, False, 4, 8)

    def test_name_is_empty_when_no_font_was_chosen(self) -> None:
        """既定の書体名を返すと、合成フォントが AviUtl2 と違う組み方をする"""
        name, size, *_ = ObjApi(_state()).lua_getfont()
        assert name == ""
        assert size == 48.0

    def test_short_calls_are_filled_to_nine_values(self) -> None:
        """短い組を返すと、受け側の ``local a,…,h = obj.getfont()`` に nil が並ぶ"""
        api = ObjApi(_state())
        api.lua_setfont("Meiryo", 30)
        values = api.lua_getfont()
        assert len(values) == 9
        assert values[:2] == ("Meiryo", 30.0)
        assert values[7:] == (0.0, 0.0)

    def test_lua_can_call_it(self) -> None:
        """Lua から呼べないと、配布エイリアスの 1 行目で落ちて真っ黒になる"""
        runtime = LuaScriptRuntime()
        state = _state()
        state.font = text_font(
            {"size": AnimatedValue(64.0), "letter_spacing": AnimatedValue(3.0)}, 0
        )
        text = runtime.expand_text(
            "<?local f,s,_,_,_,_,_,sp = obj.getfont() obj.mes(s..'/'..sp..'/'..f..'.')?>", state
        )
        assert text == "64/3/."


class TestTextFont:
    """テキストオブジェクトの設定欄を ``obj.getfont`` の並びへ"""

    def test_size_and_spacing_come_from_the_object(self) -> None:
        """設定欄を読まないと、大きさも字間もいじれない字幕になる"""
        font = text_font(
            {
                "size": AnimatedValue(64.0),
                "letter_spacing": AnimatedValue(2.5),
                "line_spacing": AnimatedValue(6.0),
                "color": (1.0, 0.0, 0.5, 1.0),
                "bold": 1,
            },
            0,
        )
        name, size, style, color, _, bold, italic, letter, line = font["given"]
        assert (name, size, style) == ("", 64.0, 0)
        assert color == 0xFF0080
        assert (bold, italic) == (True, False)
        assert (letter, line) == (2.5, 6.0)

    def test_font_name_stays_empty_when_unset(self) -> None:
        """空でない名前を作ると、合成フォントが書体を引きに行って別の組みになる"""
        assert text_font({}, 0)["given"][0] == ""


class TestEmbeddedMes:
    """テキスト欄の中の ``obj.mes``"""

    def test_writes_the_body_instead_of_drawing(self) -> None:
        """絵にしてしまうと 1x1 の作業用の絵へ描いて捨て、本文が空＝真っ黒になる"""
        runtime = LuaScriptRuntime()
        assert runtime.expand_text("<?obj.mes('あいう')?>", _state()) == "あいう"

    def test_scripts_outside_the_text_box_still_draw(self) -> None:
        """書き出しへ回し続けると、``obj.mes`` で字を描くスクリプトが何も描かなくなる"""
        drawn: list[str] = []

        def record(kind: str, params: dict[str, Any], width: int, height: int) -> Any:
            del kind
            drawn.append(str(params["text"]))
            return blank_image(width, height)

        api = ObjApi(_state(), render_source=record)
        api.lua_mes("かきく")
        assert drawn == ["かきく"]


class TestPluginModules:
    """汎用プラグイン（``.aux2``）の読み込み"""

    def test_empty_folder_gives_no_modules(self, tmp_path: Path) -> None:
        """置き場が空で落ちると、AviUtl2 の入っていない機械で互換層ごと使えなくなる"""
        assert plugin.script_modules(roots=(tmp_path,)) == {}

    def test_missing_folder_gives_no_modules(self, tmp_path: Path) -> None:
        """無いフォルダで落ちると、AviUtl2 を入れていない人が起動できない"""
        assert plugin.script_modules(roots=(tmp_path / "無い",)) == {}

    def test_broken_file_is_skipped(self, tmp_path: Path) -> None:
        """1 つの壊れたプラグインで残りを諦めると、無関係な物のせいで合成フォントが消える"""
        (tmp_path / "こわれ.aux2").write_bytes("MZ これは DLL ではない".encode())
        assert plugin.script_modules(roots=(tmp_path,)) == {}

    def test_switched_off_gives_no_modules(self, tmp_path: Path) -> None:
        """切っても読むなら、設定に意味が無い"""
        native.set_enabled(False)
        assert plugin.script_modules(roots=(tmp_path,)) == {}

    def test_obj_module_prefers_the_registered_one(self, monkeypatch: Any) -> None:
        """名前で登録されたモジュールは、ファイルとして存在しない
        先に見ないと「見つかりません」で終わり、エイリアスが真っ黒になる
        """

        class _Fake:
            path = Path("偽.aux2")
            names = ("api_version",)

            def call(self, name: str, args: Any) -> list[Any]:
                del name, args
                return [10]

        monkeypatch.setattr(plugin, "script_modules", lambda: {"compositefont": _Fake()})
        runtime = LuaScriptRuntime()
        text = runtime.expand_text(
            "<?obj.mes(obj.module('compositefont').api_version())?>", _state()
        )
        assert text == "10"


@pytest.mark.skipif(not _REAL_PLUGIN.is_file(), reason="comfont.aux2 が入っていない")
class TestRealPlugin:
    """実物の ``comfont.aux2`` 形式の推測ではなく、配布されている物で確かめる"""

    def test_registers_the_composite_font_module(self) -> None:
        modules = plugin.script_modules(roots=(_REAL_PLUGIN.parent,))
        assert "compositefont" in modules
        assert "decorate_layout" in modules["compositefont"].names

    def test_api_version_can_be_called(self) -> None:
        """呼べないと、エイリアスが ``api_version()`` の行で落ちる"""
        module = plugin.script_modules(roots=(_REAL_PLUGIN.parent,))["compositefont"]
        assert int(module.call("api_version", [])[0]) >= 9

    def test_layout_survives_a_font_name(self) -> None:
        """``edit`` が空のままだと、書体名を渡した時点で中から落ちて
        Sashimono ごと消える（捕まえる手段は無い）
        """
        module = plugin.script_modules(roots=(_REAL_PLUGIN.parent,))["compositefont"]
        results = module.call("decorate_layout", ["あいう", "default", 64.0, 0.0, "Yu Gothic UI"])
        assert results and isinstance(results[0], str)
