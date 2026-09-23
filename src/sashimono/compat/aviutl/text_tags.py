"""AviUtl2 のテキストの制御文字（書体・色・大きさ）を読む

合成フォント（``comfont.aux2``）は本文を ``<@書体名>…<@>`` へ書き換える これを読まないと、
書体を組み替えた字幕がタグの文字のまま画面に出る（Issue #108）

読むのは ``aviutl2.txt`` の「テキスト」の節にある次の 3 つ

- ``<@書体名[,文字装飾]>`` と ``<@>`` 書体の切り替えと、設定欄の書体へ戻す
- ``<#色[,影・縁色]>`` と ``<#>`` 文字色と影・縁色の切り替えと、設定欄の色へ戻す
- ``<s大きさ[,書体名][,スタイル][,縁取りの太さ]>`` と ``<s>`` 大きさ（と書体）の切り替え

どれも AviUtl2 v2.1.6a に描かせて測った振る舞いに合わせた（``tools/aviutl_tag_probes.py``
の見本を 2026-09-24 に書き出した）

- 切り替えは改行をまたいで続く（``<@メイリオ>`` の後の行もメイリオ）
- ``<@>`` は入れ子を 1 つ戻るのではなく、設定欄の書体へ戻る
- ``<s*2>`` ``<s+20>`` は、その時点の大きさに対して掛ける・足す
- ``<s>`` は大きさと、``<s>`` で指定した書体を戻す
- ``<#red>`` のような色の名前は、AviUtl2 の既定のプリセット色（``default.palette``）

測っていない所は読まずに文字のまま残す（``<@+B>`` のようなスタイルの足し引き、``<r>`` ``<p>``
などほかの制御文字） 黙って捨てると、描けていないことに気付けない
文字装飾の番号（``<@メイリオ,3>`` の ``3``）と ``<s>`` のスタイル・縁取りの太さは、書体と
大きさだけを読んで捨てる 捨てたことは互換性の記録（:class:`CompatibilityReport`）に種類ごとに
数える 数えないと、縁取りの付くはずの字が素の字で出ていることに本人が気付けない

GUI に依存しない 書体の引き当てと組版は描く側（``engine.sources``）の仕事
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from sashimono.compat.aviutl.report import CompatibilityReport, global_report

__all__ = [
    "DROPPED_DECORATION",
    "DROPPED_SIZE_STYLE",
    "PRESET_COLORS",
    "TaggedLine",
    "TextRun",
    "TextStyle",
    "parse_tags",
]

#: 読まずに捨てた所の記録の名前 互換性の一覧に種類ごとに並ぶ
DROPPED_DECORATION = "テキストの制御文字 <@書体,文字装飾>（文字装飾・スタイルは読まない）"
DROPPED_SIZE_STYLE = (
    "テキストの制御文字 <s大きさ,書体,スタイル,縁取り>（スタイル・縁取りの太さは読まない）"
)

#: 色の名前 AviUtl2 v2.1.6a の既定のプリセット色（``ProgramData\\aviutl2\\Default\\default.palette``
#: で名前の付いた 8 色） ``<#red>`` が ff0000 で描かれることは書き出して確かめた
#: パレットは本人が書き換えられるが、書き換えた物までは追わない
PRESET_COLORS: dict[str, str] = {
    "white": "ffffff",
    "red": "ff0000",
    "yellow": "ffff00",
    "green": "00ff00",
    "aqua": "00ffff",
    "blue": "0000ff",
    "magenta": "ff00ff",
    "black": "000000",
}


@dataclass(frozen=True)
class TextStyle:
    """制御文字で変わる見た目 ``None`` は設定欄の値のまま"""

    font: str | None = None
    size: float | None = None
    color: str | None = None
    edge: str | None = None


@dataclass(frozen=True)
class TextRun:
    """同じ見た目で続く文字"""

    text: str
    style: TextStyle


@dataclass
class TaggedLine:
    """1 行ぶんの文字 ``end`` は行の終わりの見た目

    文字の無い行（空の行、制御文字だけの行）の高さは、その行の終わりの大きさと書体で決まる
    （``H\\n<s200>\\nH`` の真ん中の行は大きさ 200 の行の高さだった）
    """

    runs: list[TextRun] = field(default_factory=list)
    end: TextStyle = field(default_factory=TextStyle)

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)


_FONT = re.compile(r"<@([^<>,+\-][^<>,]*)?(?:,([^<>]*))?>")
_COLOR = re.compile(r"<#([^<>,]*)(?:,([^<>,]*))?>")
_SIZE = re.compile(r"<s([+\-*]?\d+(?:\.\d+)?)?(?:,([^<>,]*)(?:,([^<>]*))?)?>")
_HEX = re.compile(r"[0-9a-fA-F]{6}")


def _colour(value: str) -> str | None:
    """色の指定を 16 進 6 桁へ 読めなければ ``None``"""
    value = value.strip()
    if _HEX.fullmatch(value):
        return value.lower()
    return PRESET_COLORS.get(value.lower())


def _sized(current: float, value: str) -> float:
    """``<s>`` の大きさ ``+`` ``-`` ``*`` はその時点の大きさに対して効く（測った）"""
    if value[0] == "*":
        return current * float(value[1:])
    if value[0] in "+-":
        return current + float(value)
    return float(value)


def _apply(
    match: re.Match[str], style: TextStyle, base_size: float, report: CompatibilityReport
) -> TextStyle | None:
    """制御文字 1 つを見た目へ効かせる 読めない中身なら ``None``（文字のまま残す）"""
    whole = match.group(0)
    if whole.startswith("<@"):
        if whole == "<@>":
            return replace(style, font=None)
        if (match.group(2) or "").strip():
            report.note_missing(DROPPED_DECORATION)
        name = (match.group(1) or "").strip()
        # ``<@,3>`` のように書体名が空なら書体は変えない 文字装飾は読まない
        return replace(style, font=name) if name else style
    if whole.startswith("<#"):
        if whole == "<#>":
            return replace(style, color=None, edge=None)
        first, second = match.group(1) or "", match.group(2)
        color = _colour(first) if first.strip() else style.color
        edge = style.edge if second is None else _colour(second)
        if (first.strip() and color is None) or (second is not None and edge is None):
            return None
        return replace(style, color=color, edge=edge)
    if whole == "<s>":
        return replace(style, size=None, font=None)
    value, font = match.group(1), match.group(2)
    if (match.group(3) or "").strip(", "):
        report.note_missing(DROPPED_SIZE_STYLE)
    size = style.size
    if value:
        size = max(1.0, _sized(base_size if size is None else size, value))
    new = replace(style, size=size)
    if font is not None and font.strip():
        new = replace(new, font=font.strip())
    return new


def parse_tags(
    text: str, base_size: float, report: CompatibilityReport | None = None
) -> list[TaggedLine]:
    """本文を、行ごとの同じ見た目の並びへ ``base_size`` は設定欄の大きさ（相対の大きさの元）

    ``report`` は読まずに捨てた所を数える先 省略するとアプリ全体の記録
    """
    log = report if report is not None else global_report
    lines = [TaggedLine()]
    style = TextStyle()
    pending: list[str] = []

    def flush() -> None:
        if pending:
            lines[-1].runs.append(TextRun("".join(pending), style))
            pending.clear()

    index = 0
    while index < len(text):
        character = text[index]
        if character == "\n":
            flush()
            lines[-1].end = style
            lines.append(TaggedLine())
            index += 1
            continue
        if character == "<":
            consumed = False
            for pattern in (_FONT, _COLOR, _SIZE):
                match = pattern.match(text, index)
                if match is None:
                    continue
                changed = _apply(match, style, base_size, log)
                if changed is not None:
                    flush()
                    style = changed
                    index = match.end()
                    consumed = True
                break
            if consumed:
                continue
        pending.append(character)
        index += 1
    flush()
    lines[-1].end = style
    return lines
