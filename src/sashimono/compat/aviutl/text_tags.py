"""AviUtl2 のテキストの制御文字を読む

合成フォント（``comfont.aux2``）は本文を ``<@書体名>…<@>`` へ書き換える これを読まないと、
書体を組み替えた字幕がタグの文字のまま画面に出る（Issue #108）

読むのは ``aviutl2.txt`` の「テキスト」の節にある次の物

- ``<@書体名[,文字装飾[スタイル]]>`` と ``<@>`` 書体・文字装飾・スタイルの切り替えと戻す
- ``<@+スタイル>`` ``<@-スタイル>`` 太字（B）と斜体（I）を足す・外す
- ``<#色[,影・縁色]>`` と ``<#>`` 文字色と影・縁色の切り替えと、設定欄の色へ戻す
- ``<s大きさ[,書体名][,スタイル][,縁取りの太さ]>`` と ``<s>`` 大きさ（と書体・スタイル・縁の太さ）
- ``<gw字間>`` ``<gh行間>`` と ``<gw>`` ``<gh>`` 字間と行間
- ``<tw横>`` ``<th縦>`` ``<tr角度>`` と ``<tw>`` ``<th>`` ``<tr>`` 1 文字ずつの変形
- ``<//コメント//>`` と ``</>`` 字を出さない

どれも AviUtl2 v2.1.6a に描かせて測った振る舞いに合わせた（``tools/aviutl_tag_probes.py``
の見本を 2026-09-24 と 2026-09-25 に書き出した #108 #134）

- 切り替えは改行をまたいで続く（``<@メイリオ>`` の後の行もメイリオ）
- ``<@>`` は入れ子を 1 つ戻るのではなく、設定欄の書体・文字装飾・スタイルへ戻る
  ``<s>`` で指定した書体も戻すが、大きさは残す（見本 tag45 tag46）
- ``<s*2>`` ``<s+20>`` は、その時点の大きさに対して掛ける・足す
- ``<s>`` は大きさと、``<s>`` で指定した書体・スタイル・縁の太さを戻す 書体は ``<s>`` の
  前の書体へ戻り、``<@メイリオ>`` の後なら設定欄ではなくメイリオ（見本 tag24 tag25）
- 書体名の無い ``<@,3>`` は何も変えない（見本 tag26 tag27 tag29 文字装飾もスタイルも効かない）
- ``<gw>`` ``<gh>`` は設定欄の字間・行間を置き換える（足さない 見本 tag47 tag48）
  字間はその字の前に、行間はその行の終わりの値で次の行との間に入る
- ``<#red>`` のような色の名前は、AviUtl2 の既定のプリセット色（``default.palette``）

測っていない・写せない所は読まずに文字のまま残す（``<r>`` ``<w>`` ``<c>`` の時間で変わる表示、
``<p>`` の座標、ふりがな ``<!>``、絵文字 ``<&>``、プリセット ``<$>``） 黙って捨てると、描けて
いないことに気付けない 取り消し線（``S``）は読んで捨て、互換性の記録
（:class:`CompatibilityReport`）に数える

GUI に依存しない 書体の引き当てと組版は描く側（``engine.sources``）の仕事
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from sashimono.compat.aviutl.report import CompatibilityReport, global_report

__all__ = [
    "DECORATION_NAMES",
    "DROPPED_STRIKE",
    "PRESET_COLORS",
    "TaggedLine",
    "TextRun",
    "TextStyle",
    "parse_tags",
]

#: 読まずに捨てた所の記録の名前 互換性の一覧に並ぶ
DROPPED_STRIKE = "テキストの制御文字の取り消し線（S は読まない）"

#: ``<@書体,番号>`` の番号と文字装飾の名前（``aviutl2.txt`` の並び）
#: 見本 tag42 で 1〜6 が影・薄い影・縁・細い縁・太い縁・角の縁の順に描かれた
DECORATION_NAMES: tuple[str, ...] = (
    "標準文字",
    "影付き文字",
    "影付き文字（薄）",
    "縁取り文字",
    "縁取り文字（細）",
    "縁取り文字（太）",
    "縁取り文字（角）",
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
    """制御文字で変わる見た目 ``None`` は設定欄の値のまま

    ``decoration`` は :data:`DECORATION_NAMES` の番号 ``edge_width`` は ``<s>`` の縁取りの太さ
    （AviUtl2 の書き方の数のまま 見本 tag44 で片側に半分ずつ付いた）
    ``scale_x`` ``scale_y`` は倍 ``turn`` は時計回りの度
    """

    font: str | None = None
    size: float | None = None
    color: str | None = None
    edge: str | None = None
    decoration: int | None = None
    bold: bool | None = None
    italic: bool | None = None
    edge_width: float | None = None
    letter_gap: float | None = None
    line_gap: float | None = None
    scale_x: float | None = None
    scale_y: float | None = None
    turn: float | None = None


@dataclass(frozen=True)
class TextRun:
    """同じ見た目で続く文字"""

    text: str
    style: TextStyle


@dataclass
class TaggedLine:
    """1 行ぶんの文字 ``end`` は行の終わりの見た目

    文字の無い行（空の行、制御文字だけの行）の高さは、その行の終わりの大きさと書体で決まる
    （``H\\n<s200>\\nH`` の真ん中の行は大きさ 200 の行の高さだった） 次の行との間の行間も
    行の終わりの ``<gh>`` で決まる（見本 tag33）
    """

    runs: list[TextRun] = field(default_factory=list)
    end: TextStyle = field(default_factory=TextStyle)

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)


@dataclass(frozen=True)
class _Layers:
    """制御文字の効き目を、戻し方の違う層に分けて持つ

    ``<@>`` と ``<s>`` はそれぞれ自分の層だけを戻す 1 つの見た目に畳んでしまうと、
    ``<@メイリオ>…<s50,ＭＳ ゴシック>…<s>`` の ``<s>`` がどの書体へ戻るかを決められない
    """

    font: str | None = None
    size_font: str | None = None
    size: float | None = None
    color: str | None = None
    edge: str | None = None
    decoration: int | None = None
    bold: bool | None = None
    italic: bool | None = None
    size_bold: bool = False
    size_italic: bool = False
    edge_width: float | None = None
    letter_gap: float | None = None
    line_gap: float | None = None
    scale_x: float | None = None
    scale_y: float | None = None
    turn: float | None = None

    def style(self) -> TextStyle:
        return TextStyle(
            font=self.size_font if self.size_font is not None else self.font,
            size=self.size,
            color=self.color,
            edge=self.edge,
            decoration=self.decoration,
            bold=True if self.size_bold else self.bold,
            italic=True if self.size_italic else self.italic,
            edge_width=self.edge_width,
            letter_gap=self.letter_gap,
            line_gap=self.line_gap,
            scale_x=self.scale_x,
            scale_y=self.scale_y,
            turn=self.turn,
        )


_NUMBER = r"[+\-]?\d+(?:\.\d+)?"
_FONT = re.compile(r"<@([^<>,+\-][^<>,]*)?(?:,([^<>]*))?>")
_FONT_STYLE = re.compile(r"<@([+\-])([A-Za-z]+)>")
_COLOR = re.compile(r"<#([^<>,]*)(?:,([^<>,]*))?>")
_SIZE = re.compile(r"<s([+\-*]?\d+(?:\.\d+)?)?(?:,([^<>,]*)(?:,([^<>,]*)(?:,([^<>,]*))?)?)?>")
_GAP = re.compile(rf"<g([wh])({_NUMBER})?>")
_SHAPE = re.compile(r"<t([wh])(\d+(?:\.\d+)?)?>")
_TURN = re.compile(rf"<tr({_NUMBER})?>")
_BLOCK = "</>"
_COMMENT_START, _COMMENT_END = "<//", "//>"
_HEX = re.compile(r"[0-9a-fA-F]{6}")
_DECORATION = re.compile(r"\s*(\d+)?\s*([A-Za-z]*)\s*")


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


def _styles(letters: str, report: CompatibilityReport) -> tuple[bool, bool] | None:
    """スタイルの文字を太字と斜体へ 読めない文字があれば ``None`` 取り消し線は数えて捨てる"""
    upper = letters.upper()
    if any(letter not in "BIS" for letter in upper):
        return None
    if "S" in upper:
        report.note_missing(DROPPED_STRIKE)
    return "B" in upper, "I" in upper


def _font_tag(match: re.Match[str], layers: _Layers, report: CompatibilityReport) -> _Layers | None:
    if match.group(0) == "<@>":
        # 設定欄の書体・装飾・スタイルへ戻す <s> で指定した書体も戻すが大きさは残す（tag45 tag46）
        return replace(layers, font=None, size_font=None, decoration=None, bold=None, italic=None)
    name = (match.group(1) or "").strip()
    if not name:
        # 書体名の無い <@,3> は AviUtl2 で何も変えなかった（tag26 tag27 tag29）
        return layers
    changed = replace(layers, font=name, size_font=None)
    extra = match.group(2)
    if extra is None or not extra.strip():
        return changed
    parts = _DECORATION.fullmatch(extra)
    if parts is None:
        return None
    number, letters = parts.group(1), parts.group(2)
    if number is not None:
        index = int(number)
        if index >= len(DECORATION_NAMES):
            return None
        changed = replace(changed, decoration=index)
    if not letters:
        # 番号だけならスタイルは今のまま
        return changed
    styles = _styles(letters, report)
    if styles is None:
        return None
    # スタイルを書いたときは、書いた物がその字のスタイルの全部（aviutl2.txt の <@メイリオ,6BI>）
    # 書かなかった方まで前の <@+B> を残すと、斜体だけを指定した字が太字にもなる（#183 のレビュー）
    return replace(changed, bold=styles[0], italic=styles[1])


def _style_tag(
    match: re.Match[str], layers: _Layers, report: CompatibilityReport
) -> _Layers | None:
    """``<@+B>`` ``<@-B>`` 見本 tag28 で足した太字と斜体が次の字から効き、外すと戻った"""
    styles = _styles(match.group(2), report)
    if styles is None:
        return None
    on = match.group(1) == "+"
    bold, italic = styles
    changed = layers
    if bold:
        changed = replace(changed, bold=on)
    if italic:
        changed = replace(changed, italic=on)
    return changed


def _size_tag(
    match: re.Match[str], layers: _Layers, base_size: float, report: CompatibilityReport
) -> _Layers | None:
    if match.group(0) == "<s>":
        return replace(
            layers,
            size=None,
            size_font=None,
            size_bold=False,
            size_italic=False,
            edge_width=None,
        )
    value, font, letters, edge = match.group(1), match.group(2), match.group(3), match.group(4)
    changed = layers
    if value:
        current = base_size if layers.size is None else layers.size
        changed = replace(changed, size=max(1.0, _sized(current, value)))
    if font is not None and font.strip():
        changed = replace(changed, size_font=font.strip())
    if letters is not None and letters.strip():
        styles = _styles(letters.strip(), report)
        if styles is None:
            return None
        changed = replace(changed, size_bold=styles[0], size_italic=styles[1])
    if edge is not None and edge.strip():
        try:
            width = float(edge)
        except ValueError:
            return None
        if not math.isfinite(width):
            # inf の縁は描けず、nan は max で黙って 0 になる 読めない指定として文字のまま残す
            return None
        changed = replace(changed, edge_width=max(0.0, width))
    return changed


def _color_tag(match: re.Match[str], layers: _Layers) -> _Layers | None:
    if match.group(0) == "<#>":
        return replace(layers, color=None, edge=None)
    first, second = match.group(1) or "", match.group(2)
    color = _colour(first) if first.strip() else layers.color
    edge = layers.edge if second is None else _colour(second)
    if (first.strip() and color is None) or (second is not None and edge is None):
        return None
    return replace(layers, color=color, edge=edge)


def _gap_tag(match: re.Match[str], layers: _Layers) -> _Layers:
    value = float(match.group(2)) if match.group(2) is not None else None
    if match.group(1) == "w":
        return replace(layers, letter_gap=value)
    return replace(layers, line_gap=value)


def _shape_tag(match: re.Match[str], layers: _Layers) -> _Layers:
    value = float(match.group(2)) if match.group(2) is not None else None
    if match.group(1) == "w":
        return replace(layers, scale_x=value)
    return replace(layers, scale_y=value)


def _turn_tag(match: re.Match[str], layers: _Layers) -> _Layers:
    raw = match.group(1)
    if raw is None:
        return replace(layers, turn=None)
    if raw[0] in "+-":
        # 符号付きは今の角度に足す（aviutl2.txt の <tr+45>）
        return replace(layers, turn=(layers.turn or 0.0) + float(raw))
    return replace(layers, turn=float(raw))


def _apply(
    text: str, index: int, layers: _Layers, base_size: float, report: CompatibilityReport
) -> tuple[_Layers, int] | None:
    """``index`` から始まる制御文字 1 つを読む 読めなければ ``None``（文字のまま残す）"""
    if text.startswith(_BLOCK, index):
        # 文字列の区切り 字は出ない（見本 tag37） ふりがなの <!> は読まないので文字のまま残る
        return layers, index + len(_BLOCK)
    if text.startswith(_COMMENT_START, index):
        end = text.find(_COMMENT_END, index + len(_COMMENT_START))
        if end < 0:
            return None
        # コメントは改行を含められる 字も改行も出ない（見本 tag37）
        return layers, end + len(_COMMENT_END)
    readers: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], _Layers | None]], ...] = (
        (_FONT_STYLE, lambda m: _style_tag(m, layers, report)),
        (_FONT, lambda m: _font_tag(m, layers, report)),
        (_COLOR, lambda m: _color_tag(m, layers)),
        (_SIZE, lambda m: _size_tag(m, layers, base_size, report)),
        (_GAP, lambda m: _gap_tag(m, layers)),
        (_SHAPE, lambda m: _shape_tag(m, layers)),
        (_TURN, lambda m: _turn_tag(m, layers)),
    )
    for pattern, read in readers:
        match = pattern.match(text, index)
        if match is None:
            continue
        changed = read(match)
        return None if changed is None else (changed, match.end())
    return None


def parse_tags(
    text: str, base_size: float, report: CompatibilityReport | None = None
) -> list[TaggedLine]:
    """本文を、行ごとの同じ見た目の並びへ ``base_size`` は設定欄の大きさ（相対の大きさの元）

    ``report`` は読まずに捨てた所を数える先 省略するとアプリ全体の記録
    """
    log = report if report is not None else global_report
    lines = [TaggedLine()]
    layers = _Layers()
    pending: list[str] = []

    def flush() -> None:
        if pending:
            lines[-1].runs.append(TextRun("".join(pending), layers.style()))
            pending.clear()

    index = 0
    while index < len(text):
        character = text[index]
        if character == "\n":
            flush()
            lines[-1].end = layers.style()
            lines.append(TaggedLine())
            index += 1
            continue
        if character == "<":
            read = _apply(text, index, layers, base_size, log)
            if read is not None:
                flush()
                layers, index = read
                continue
        pending.append(character)
        index += 1
    flush()
    lines[-1].end = layers.style()
    return lines
