"""テキスト欄に埋め込んだ Lua（``<?…?>``）

AviUtl のテキストオブジェクトは、本文の ``<?`` と ``?>`` の間を Lua として走らせ、
``mes(文字)`` で書き出したものをその場所の文字にする 時刻に合わせて数字を
数え上げる、といった字幕が作れる

ここでは本文を「文字の部分」と「Lua の部分」に分け、1 本の Lua に組み立てるだけ
走らせるのは :meth:`kumiki.compat.aviutl.runtime.LuaScriptRuntime.expand_text`
（サンドボックスと実行時間の上限をスクリプトと共有するため）
"""

from __future__ import annotations

import re

__all__ = ["EMIT", "build_source", "has_embedded", "literal_text", "split_embedded"]

#: 書き出しを受け取る関数の名前 スクリプトの変数と重ならない名前にする
EMIT = "__kumiki_emit"

_BLOCK = re.compile(r"<\?(.*?)\?>", re.DOTALL)


def has_embedded(text: str) -> bool:
    return _BLOCK.search(text) is not None


def split_embedded(text: str) -> list[tuple[bool, str]]:
    """``(Lua か, 中身)`` の並び 閉じていない ``<?`` は文字として残す"""
    pieces: list[tuple[bool, str]] = []
    position = 0
    for match in _BLOCK.finditer(text):
        if match.start() > position:
            pieces.append((False, text[position : match.start()]))
        pieces.append((True, match.group(1)))
        position = match.end()
    if position < len(text):
        pieces.append((False, text[position:]))
    return pieces


def literal_text(text: str) -> str:
    """Lua の部分を取り除いた文字だけ 実行に失敗したときに出す

    Lua のソースをそのまま画面に出すと、字幕にプログラムが映る 失敗は記録に
    残し、文字の部分だけを見せる
    """
    return "".join(body for is_code, body in split_embedded(text) if not is_code)


def build_source(text: str) -> str:
    """本文を、書き出しを順に呼ぶ 1 本の Lua にする

    ``<?=式?>`` は ``<?mes(式)?>`` と同じ扱い（AviUtl2 の配布物に見られる短い形）
    Lua の部分どうしは同じチャンクの中なので、前の部分で作った変数を後ろで使える
    """
    lines = [f"function mes(value) {EMIT}(tostring(value)) end"]
    for is_code, body in split_embedded(text):
        if not is_code:
            lines.append(f"{EMIT}({_long_string(body)})")
        elif body.startswith("="):
            lines.append(f"{EMIT}(tostring({body[1:]}))")
        else:
            lines.append(body)
    return "\n".join(lines)


def _long_string(body: str) -> str:
    """Lua の長い文字列 本文に出てこない閉じ括弧を選ぶ

    開き括弧の直後の改行は Lua が捨てるので、1 つ足しておく 足さないと、
    改行で始まる文字の部分から改行が 1 つ消える
    """
    level = 0
    while f"]{'=' * level}]" in body:
        level += 1
    equals = "=" * level
    return f"[{equals}[\n{body}]{equals}]"
