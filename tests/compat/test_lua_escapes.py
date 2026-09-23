"""Lua の文字の逃がし書きを、AviUtl1 の Lua（LuaJIT）と同じに戻す

``--dialog`` の初期値と ``.exa`` の ``param=`` は Lua の書き方のまま入っている
戻し方が 1 つでも違うと、スクリプトが受け取る文字が AviUtl1 と変わる

比べる相手は lupa に入っている LuaJIT 同じ文字を LuaJIT に読ませ、出来たバイト列を
そのまま突き合わせる（こちらが規則を覚え違えていても、表の側で気付ける）
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest

from sashimono.compat.aviutl.control import lua_string, lua_value, parse_control
from sashimono.compat.aviutl.objapi import ObjectState
from sashimono.compat.aviutl.runtime import LuaScriptRuntime, blank_image
from sashimono.effects.spec import TextSpec

#: LuaJIT がそのまま読める書き方 どれも引用符の中身（``\\`` は Lua の逆斜線 1 つ）
ESCAPES = (
    "a\\ab",
    "\\b\\f\\v",
    "\\r\\n\\t",
    "\\\\\\'\\\"",
    "\\065\\66C",
    "\\0\\9\\255",
    "\\1234",
    "\\x41\\xe3\\x81\\x82",
    "a\\z  \n  b",
    "a\\zb",
    "\\u{3042}\\u{41}",
    "行\\\n次",
    "行\\\r\n次",
    "あ\\255い",
)

#: LuaJIT が読み込みで止める書き方 引用符ごと書かれたまま返る決まり
BAD_ESCAPES = (
    "\\q",
    "\\256",
    "\\x4",
    "\\xg0",
    "\\u41",
    "\\u{}",
    "a\\",
    # 代用符号の範囲と U+10FFFF より先 LuaJIT は invalid escape sequence で止める
    "\\u{D800}",
    "\\u{DFFF}",
    "\\u{110000}",
)


def _luajit() -> Any:
    """比べる相手の LuaJIT AviUtl1 の Lua はこれ

    ソフトが走らせる埋め込みの Lua は 5.1 で、``\\x`` と ``\\z`` を知らず、``\\q`` を
    黙って ``q`` にする AviUtl1 の読み方とは違うので、比べる相手にはしない
    無い機械では LuaJIT との突き合わせだけを飛ばす
    """
    # 型の情報を持たない包みなので、名前で読み込む（runtime.py と同じやり方）
    try:
        luajit21 = import_module("lupa.luajit21")
    except ImportError:
        return None
    return luajit21.LuaRuntime(encoding=None, register_builtins=False)


LUAJIT = _luajit()


@pytest.mark.parametrize("body", ESCAPES)
def test_the_same_bytes_as_luajit(body: str) -> None:
    if LUAJIT is None:
        pytest.skip("LuaJIT が無い")
    literal = '"' + body + '"'
    expected = LUAJIT.execute(b"return " + literal.encode("utf-8"))
    assert lua_string(literal).encode("utf-8", "surrogateescape") == expected


@pytest.mark.parametrize("body", BAD_ESCAPES)
def test_what_luajit_rejects_is_left_as_written(body: str) -> None:
    # 推し量って直すと、AviUtl1 では読めない値が動く値として紛れる
    literal = '"' + body + '"'
    if LUAJIT is not None:
        with pytest.raises(Exception):  # noqa: B017 - lupa の例外の種類は版で違う
            LUAJIT.execute(b"return " + literal.encode("utf-8"))
    assert lua_string(literal) == literal


def test_the_old_shortcuts_are_gone() -> None:
    # 以前は \n と \t 以外を次の 1 文字にしていた \r が r、\065 が 065 になっていた
    assert lua_string('"\\r"') == "\r"
    assert lua_string('"\\065"') == "A"
    assert lua_string("'\\x41\\z   B'") == "AB"


def test_a_dialog_default_is_unescaped() -> None:
    (spec,) = parse_control('--dialog:本文,_1="\\065\\r";').parameters
    assert isinstance(spec, TextSpec)
    assert spec.default == "A\r"


#: 文字の中身をバイトの番号の並びにする Lua の書き方 LuaJIT とソフトの Lua の両方で走らせる
_BYTES_OF = (
    "local t = {} for i = 1, #s do t[#t + 1] = string.byte(s, i) end return table.concat(t, ',')"
)


@pytest.mark.parametrize("body", ["a\\255b", "\\xFF\\xFE", "\\xe3\\x81", "あ\\128"])
def test_a_high_byte_reaches_the_script_as_the_same_bytes(body: str) -> None:
    """ダイアログの値が、LuaJIT が持つのと同じバイト列でスクリプトに届く

    読めないバイトを持つ値をそのまま lupa へ渡すと UTF-8 へ直せずに例外になり、
    スクリプトが 1 行も走らなかった 大域変数と ``obj.名前`` の両方の入口を見る
    """
    (spec,) = parse_control(f'--dialog:本文,_1="{body}";').parameters
    value = lua_value(spec, spec.default_value())
    runtime = LuaScriptRuntime(instruction_limit=100_000)
    state = ObjectState(image=blank_image(8, 8))
    state.values["_1"] = value
    script = (
        "local function bytes(s) " + _BYTES_OF + " end "
        "obj.global_bytes = bytes(_1) obj.field_bytes = bytes(obj._1)"
    )
    result = runtime.run(script, state)
    assert not result.failed, result.message
    got = state.values["global_bytes"]
    assert got == state.values["field_bytes"]
    if LUAJIT is None:
        pytest.skip("LuaJIT が無い")
    expected = LUAJIT.execute(('local s = "' + body + '" ' + _BYTES_OF).encode("utf-8"))
    assert str(got).encode("ascii") == expected
