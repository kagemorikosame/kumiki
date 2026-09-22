"""文字装飾 AviUtl2 と YMM4 で共通に使う語彙

どちらのソフトも「文字そのもの」と「その飾り」を分けて持っている 名前は違うが
言っていることは同じで、縁取りと影の 2 つに落ちる

============================  ==========================================
AviUtl2 の ``文字装飾``       YMM4 の ``Decorations``
============================  ==========================================
``標準文字``                  （装飾なし）
``影付き文字``                ``ShadowDecoration``
``影付き文字（薄）``          ``ShadowDecoration``（薄いもの）
``縁取り文字``                ``BorderDecoration``
``縁取り文字（細）``          ``BorderDecoration``（細いもの）
``縁取り文字（太）``          ``BorderDecoration``（太いもの）
============================  ==========================================

**太さと影のずれは文字サイズに対する割合で持つ** AviUtl も YMM4 も、装飾の
見た目はフォントサイズに追従する 固定の画素数で持つと、サイズを変えたときだけ
縁が細くなったり太くなったりする

ここは GUI にも Qt にも依存しない 実際に描くのは
:mod:`sashimono.engine.sources`
"""

from __future__ import annotations

from dataclasses import dataclass

from sashimono.core.model import AnimatedValue, ParamValue

__all__ = [
    "DECORATIONS",
    "PLAIN",
    "TextDecoration",
    "decoration_params",
    "find_decoration",
]


@dataclass(frozen=True, slots=True)
class TextDecoration:
    """文字の飾り 1 つ分

    値はすべて**文字サイズに対する割合** ``border`` が 0.05 なら、サイズ 100 の
    文字に太さ 5 px の縁が付く
    """

    #: 元のソフトでの呼び名
    name: str
    #: 縁取りの太さ
    border: float = 0.0
    #: 影のずれ 右下へこの割合だけずらす
    shadow: float = 0.0
    #: 影のぼかし
    shadow_blur: float = 0.0
    #: 影の濃さ（0..1）
    shadow_opacity: float = 1.0


#: 装飾なし
PLAIN = TextDecoration(name="標準文字")

#: AviUtl2 の ``文字装飾`` の値
#:
#: 割合は手元の配布エイリアス（``%PROGRAMDATA%\\aviutl2\\Alias``）を
#: AviUtl2 で表示させた見た目に合わせて決めた 元の実装の数式は公開されて
#: いないので、これは**近似**であることを承知の上で使う
DECORATIONS: dict[str, TextDecoration] = {
    "標準文字": PLAIN,
    "影付き文字": TextDecoration("影付き文字", shadow=0.06),
    "影付き文字（薄）": TextDecoration("影付き文字（薄）", shadow=0.06, shadow_opacity=0.5),
    "縁取り文字": TextDecoration("縁取り文字", border=0.05),
    "縁取り文字（細）": TextDecoration("縁取り文字（細）", border=0.03),
    "縁取り文字（太）": TextDecoration("縁取り文字（太）", border=0.09),
}


def find_decoration(name: str) -> TextDecoration | None:
    """名前から引く 全角と半角の括弧の違いは吸収する

    エイリアスを手で書き換える人がいるので、``縁取り文字(太)`` と書かれていても
    同じものとして扱う
    """
    cleaned = name.strip()
    if not cleaned:
        return PLAIN
    found = DECORATIONS.get(cleaned)
    if found is not None:
        return found
    normalised = cleaned.replace("(", "（").replace(")", "）")
    return DECORATIONS.get(normalised)


def decoration_params(
    decoration: TextDecoration,
    size: float,
    colour: tuple[float, float, float, float],
) -> dict[str, ParamValue]:
    """装飾を、テキストオブジェクトのパラメータへ

    ``colour`` は AviUtl2 の ``影・縁色`` にあたる 縁取りと影で同じ色を使うのは
    元のソフトと同じ挙動で、片方だけ別の色にはできない
    """
    params: dict[str, ParamValue] = {}
    if decoration.border > 0.0:
        params["border_width"] = AnimatedValue(size * decoration.border)
        params["border_color"] = colour
    if decoration.shadow > 0.0:
        offset = size * decoration.shadow
        params["shadow_x"] = AnimatedValue(offset)
        params["shadow_y"] = AnimatedValue(-offset)
        params["shadow_blur"] = AnimatedValue(size * decoration.shadow_blur)
        params["shadow_color"] = (*colour[:3], colour[3] * decoration.shadow_opacity)
    return params
