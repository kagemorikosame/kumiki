"""YMM4 の JSON に出てくる値の読み方

YMM4 は .NET のシリアライザで書き出しているので、型の名前が ``$type`` に入る

.. code-block:: json

    {"$type": "YukkuriMovieMaker.Project.Items.TextItem, YukkuriMovieMaker"}

**振り分けにはクラス名だけを使う** 名前空間もアセンブリ名も版で変わる（実物には
``Version=4.32.0.2, Culture=neutral, PublicKeyToken=null`` まで入っていた） 丸ごと
突き合わせると、YMM4 が更新されただけで全部読めなくなる

数値は素の数でも「アニメーション」でも書かれる 後者はこの形

.. code-block:: json

    {"Values": [{"Value": -90.0}, {"Value": 0.0}], "Span": 0.0, "AnimationType": "Expo_Out"}

**``Values`` にフレーム番号は入っていない** YMM4 はアイテムの長さと「中間点」で
位置が決まる仕組みで、値の並びはその区切りに 1 対 1 で対応する

.. code-block:: text

    KeyFrames.Frames = [60, 240]、Length = 300
    → 区切りは 0, 60, 240, 300 の 4 点 → Values も 4 個

だから :func:`animated` はフレーム位置を**外から**受け取る ここを取り違えると、
300 フレームかけて動くはずのものが 2 フレームで終わる
"""

from __future__ import annotations

from typing import Any

from sashimono.core.model import AnimatedValue, Interpolation, Keyframe

__all__ = [
    "INTERPOLATIONS",
    "animated",
    "colour",
    "frame_positions",
    "interpolation_of",
    "number",
    "type_name",
]

#: YMM4 の移動方法と、こちらの補間方法
#:
#: 日本語の名前（YMM4 の UI がそのまま出る）と、英語のイージング名が混ざる
#: 英語のほうは ``Expo_Out`` ``Sine_In`` ``Quart_InOut`` のように
#: ``<曲線>_<向き>`` の形なので、向きだけを見れば足りる
INTERPOLATIONS: dict[str, Interpolation] = {
    "なし": Interpolation.HOLD,
    "瞬間移動": Interpolation.HOLD,
    "直線移動": Interpolation.LINEAR,
    "曲線移動": Interpolation.BEZIER,
    "加速": Interpolation.EASE_IN,
    "減速": Interpolation.EASE_OUT,
    "加減速": Interpolation.EASE_IN_OUT,
}

#: 英語のイージング名の末尾と、こちらの補間方法
_EASING_SUFFIXES: tuple[tuple[str, Interpolation], ...] = (
    ("_InOut", Interpolation.EASE_IN_OUT),
    ("_Out", Interpolation.EASE_OUT),
    ("_In", Interpolation.EASE_IN),
)


def type_name(value: Any) -> str:
    """``$type`` からクラス名だけを取り出す

    ``"名前空間.クラス名, アセンブリ, Version=…"`` の形なので、最初のカンマの前を
    取って最後の ``.`` から後ろを見る ``$type`` が無ければ空文字
    """
    if not isinstance(value, dict):
        return ""
    raw = value.get("$type")
    if not isinstance(raw, str):
        return ""
    return raw.partition(",")[0].strip().rpartition(".")[2]


def number(value: Any, default: float = 0.0) -> float:
    """素の数として読む アニメーションなら最初の値"""
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
            first = values[0]
            return number(first.get("Value") if isinstance(first, dict) else first, default)
    return default


def interpolation_of(name: str) -> Interpolation:
    """移動方法の名前を補間方法へ 知らない名前は直線にする

    動きの形は違っても、始点と終点は合う 知らないものを止めてしまうと、
    そこだけ動かないアニメーションになって原因が分かりにくい
    """
    found = INTERPOLATIONS.get(name)
    if found is not None:
        return found
    for suffix, interpolation in _EASING_SUFFIXES:
        if name.endswith(suffix):
            return interpolation
    return Interpolation.LINEAR


def frame_positions(keyframes: Any, length: int, count: int) -> list[int]:
    """値の並びに対応するフレーム位置

    YMM4 の「中間点」（``KeyFrames.Frames``）がアイテムを区切り、その境目が
    そのまま値の位置になる 中間点が無ければ、先頭と末尾に均等に割る
    """
    middle: list[int] = []
    if isinstance(keyframes, dict):
        raw = keyframes.get("Frames")
        if isinstance(raw, list):
            middle = [int(number(frame)) for frame in raw]

    positions = [0, *middle, max(1, length)]
    if len(positions) == count:
        return positions

    # 中間点の数と値の数が食い違うファイルもありうる 等間隔に割り振って、
    # 少なくとも始点と終点は合わせる
    span = max(1, length)
    return [round(span * index / max(1, count - 1)) for index in range(count)]


def animated(
    value: Any,
    default: float = 0.0,
    *,
    length: int = 1,
    keyframes: Any = None,
    scale: float = 1.0,
) -> AnimatedValue:
    """アニメーションを :class:`AnimatedValue` へ

    ``length`` と ``keyframes`` はアイテムの長さと中間点 値が 2 つ以上あるときに
    だけ使う ``scale`` は単位をそろえるための倍率（YMM4 の百分率をこちらの
    0..1 にするなど）
    """
    if not isinstance(value, dict):
        return AnimatedValue(number(value, default) * scale)

    values = value.get("Values")
    if not isinstance(values, list) or not values:
        return AnimatedValue(number(value, default) * scale)

    numbers = [
        number(item.get("Value") if isinstance(item, dict) else item, default) * scale
        for item in values
    ]
    if len(numbers) == 1:
        return AnimatedValue(numbers[0])

    style = str(value.get("AnimationType") or "")
    interpolation = interpolation_of(style)
    control = (0.42, 0.0, 0.58, 1.0) if interpolation is Interpolation.BEZIER else None

    positions = frame_positions(keyframes, length, len(numbers))
    built: list[Keyframe] = []
    for frame, amount in zip(positions, numbers, strict=True):
        # 同じフレームに 2 つは置けない 中間点が端に重なると起きる
        if built and built[-1].frame >= frame:
            continue
        built.append(
            Keyframe(
                frame=frame,
                value=amount,
                interpolation=interpolation,
                control_points=control,
            )
        )

    if len(built) < 2:
        return AnimatedValue(numbers[0])
    return AnimatedValue(static=built[0].value, keyframes=tuple(built))


def colour(
    value: Any, default: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
) -> tuple[float, float, float, float]:
    """``#AARRGGBB`` / ``#RRGGBB`` を 0..1 の組へ

    YMM4 はアルファを**先頭**に置く 後ろだと思って読むと、不透明のつもりの色が
    透明になる
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


def brush_colour(
    brush: Any, default: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
) -> tuple[float, float, float, float]:
    """ブラシから色を取り出す

    YMM4 のブラシは差し替え式（単色・格子・ノイズ…）で、こういう形をしている

    .. code-block:: json

        {"Type": "…SolidColorBrushPlugin, …",
         "Parameter": {"$type": "…SolidColorBrushParameter, …", "Color": "#FFFFFFFF"}}

    単色以外は色 1 つで表せないので、既定のままにする
    """
    if not isinstance(brush, dict):
        return default
    parameter = brush.get("Parameter")
    if not isinstance(parameter, dict):
        return default
    if "Color" not in parameter:
        return default
    return colour(parameter.get("Color"), default)
