"""YMM4 の JSON に出てくる値の読み方。

YMM4 は .NET のシリアライザで書き出しているので、型の名前が ``$type`` に入る。

.. code-block:: json

    {"$type": "YukkuriMovieMaker.Project.Items.TextItem, YukkuriMovieMaker"}

**振り分けにはクラス名だけを使う。** 名前空間もアセンブリ名も版で変わるが、
クラス名は残る。丸ごと突き合わせると、YMM4 が更新されただけで全部読めなくなる。

数値は素の数でも「アニメーション」でも書かれる。後者はこの形。

.. code-block:: json

    {"Values": [{"Value": 0, "Frame": 0}, {"Value": 100, "Frame": 30}]}

どちらで来ても :class:`~novaedit.core.model.AnimatedValue` にする。
"""

from __future__ import annotations

from typing import Any

from novaedit.core.model import AnimatedValue, Interpolation, Keyframe

__all__ = [
    "INTERPOLATIONS",
    "animated",
    "colour",
    "number",
    "type_name",
]

#: YMM4 の移動方法と、こちらの補間方法。
#:
#: 名前は日本語で書かれている（YMM4 の UI がそのまま出る）。載っていないものは
#: 直線として扱う。動きの形は違っても、始点と終点は合う。
INTERPOLATIONS: dict[str, Interpolation] = {
    "なし": Interpolation.HOLD,
    "瞬間移動": Interpolation.HOLD,
    "直線移動": Interpolation.LINEAR,
    "曲線移動": Interpolation.BEZIER,
    "加速": Interpolation.EASE_IN,
    "減速": Interpolation.EASE_OUT,
    "加減速": Interpolation.EASE_IN_OUT,
}


def type_name(value: Any) -> str:
    """``$type`` からクラス名だけを取り出す。

    ``"名前空間.クラス名, アセンブリ"`` の形なので、カンマの前を取って最後の
    ``.`` から後ろを見る。``$type`` が無ければ空文字。
    """
    if not isinstance(value, dict):
        return ""
    raw = value.get("$type")
    if not isinstance(raw, str):
        return ""
    return raw.partition(",")[0].strip().rpartition(".")[2]


def number(value: Any, default: float = 0.0) -> float:
    """素の数として読む。アニメーションなら最初の値。"""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    if isinstance(value, dict):
        values = value.get("Values")
        if isinstance(values, list) and values:
            return number(values[0].get("Value") if isinstance(values[0], dict) else None, default)
    return default


def animated(value: Any, default: float = 0.0) -> AnimatedValue:
    """アニメーションを :class:`AnimatedValue` へ。

    キーフレームが 1 つだけなら、動かない値として持つ。1 点のキーフレームは
    静止と同じで、残しておくとグラフエディタに意味のない点が並ぶ。
    """
    if not isinstance(value, dict):
        return AnimatedValue(number(value, default))

    values = value.get("Values")
    if not isinstance(values, list) or not values:
        return AnimatedValue(number(value, default))
    if len(values) == 1:
        first = values[0]
        return AnimatedValue(
            number(first.get("Value") if isinstance(first, dict) else first, default)
        )

    keyframes: list[Keyframe] = []
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            continue
        # YMM4 は区間ごとに長さを持ち、絶対フレームを持たないことがある。
        # その場合は並び順をそのまま使う（等間隔）。
        frame = int(number(item.get("Frame"), float(index)))
        style = str(item.get("Type") or value.get("AnimationType") or "")
        interpolation = INTERPOLATIONS.get(style, Interpolation.LINEAR)
        control = (0.42, 0.0, 0.58, 1.0) if interpolation is Interpolation.BEZIER else None
        keyframes.append(
            Keyframe(
                frame=frame,
                value=number(item.get("Value"), default),
                interpolation=interpolation,
                control_points=control,
            )
        )

    keyframes.sort(key=lambda k: k.frame)
    unique: list[Keyframe] = []
    for keyframe in keyframes:
        # 同じフレームに 2 つ置けない。後から来たものを採る（YMM4 の見た目と同じ）。
        if unique and unique[-1].frame == keyframe.frame:
            unique[-1] = keyframe
            continue
        unique.append(keyframe)
    if len(unique) == 1:
        return AnimatedValue(unique[0].value)
    return AnimatedValue(static=unique[0].value, keyframes=tuple(unique))


def colour(
    value: Any, default: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
) -> tuple[float, float, float, float]:
    """``#AARRGGBB`` / ``#RRGGBB`` を 0..1 の組へ。

    YMM4 はアルファを**先頭**に置く。後ろだと思って読むと、不透明のつもりの色が
    透明になる。
    """
    if not isinstance(value, str):
        return default
    text = value.strip().lstrip("#")
    if len(text) not in (6, 8):
        return default
    try:
        channels = [int(text[i : i + 2], 16) / 255.0 for i in range(0, len(text), 2)]
    except ValueError:
        return default
    if len(channels) == 4:
        alpha, red, green, blue = channels
        return (red, green, blue, alpha)
    red, green, blue = channels
    return (red, green, blue, 1.0)
