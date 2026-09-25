"""YMM4 のブラシと、ブラシで塗るエフェクトを :mod:`sashimono.effects.paint` へ写す

ブラシは図形の色（``ShapeParameter.Brush``）と「前景を塗りつぶし」（``FillForegroundEffect``）
で使われる 単色だけでなく、線形・円形のグラデーション、ストライプ、水玉、格子がある
「グラデーション」エフェクト（``GradientEffect``）も同じ塗りで受ける

値の意味は YMM4 本体に描かせた試験テンプレート（``tools/ymm4_probes.py``）で確かめた

- 線形のブラシは角度 0 で左から右 グラデーションエフェクトは角度 0 で上から下
  （同じ「角度」でも 90 度ずれている）
- 位置（X / Y、中心）は下が正 模様の座標も下を正で持つので、そのまま渡す
- 合成モードは sRGB のまま計算する（:mod:`sashimono.effects.paint` が合わせてある）
"""

from __future__ import annotations

import colorsys
import math
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.ymm4.values import animated, colour, number
from sashimono.core.model import AnimatedValue, Effect, ParamValue
from sashimono.effects.definition import registry
from sashimono.effects.paint import MAX_STOPS

__all__ = [
    "BLEND_NAMES",
    "brush_effect",
    "fill_foreground",
    "gradient_effect",
    "is_solid",
    "noise_mask",
]

#: YMM4 の合成モードの名前と、塗りのエフェクトの合成 実物の名前だけを並べる
#: （``Lighten`` ``Darken`` のような名前は YMM4 が読み込みで断る）
BLEND_NAMES: dict[str, str] = {
    "Normal": "normal",
    "Add": "add",
    "LinearDodge": "add",
    "Subtract": "subtract",
    "Multiply": "multiply",
    "Screen": "screen",
    "Overlay": "overlay",
    "SoftLight": "soft_light",
    "HardLight": "hard_light",
    "ColorDodge": "color_dodge",
    "ColorBurn": "color_burn",
    "Lighter": "lighten",
    "Darker": "darken",
    "LighterColor": "lighter_color",
    "DarkerColor": "darker_color",
    "Difference": "difference",
    "Exclusion": "exclusion",
    "LinearBurn": "linear_burn",
    "LinearLight": "linear_light",
    "VividLight": "vivid_light",
    "PinLight": "pin_light",
    "HardMix": "hard_mix",
    "Division": "division",
    "Hue": "hue",
    "Saturation": "saturation",
    "Color": "color",
    "Luminosity": "luminosity",
}

_EXTEND = {"Clamp": "clamp", "Wrap": "wrap", "Mirror": "mirror"}


class _Values:
    """1 つの辞書から数を読む アニメーションはアイテムの長さで読む"""

    def __init__(self, entry: dict[str, Any], length: int, keyframes: Any) -> None:
        self.entry = entry
        self.length = length
        self.keyframes = keyframes

    def track(self, key: str, default: float = 0.0, *, scale: float = 1.0) -> AnimatedValue:
        return animated(
            self.entry.get(key), default, length=self.length, keyframes=self.keyframes, scale=scale
        )


def _mapped(value: AnimatedValue, change: Callable[[float], float]) -> AnimatedValue:
    return AnimatedValue(
        change(value.static),
        tuple(replace(frame, value=change(frame.value)) for frame in value.keyframes),
    )


def is_solid(brush: Any) -> bool:
    """単色のブラシか ブラシが無い（古い形）ときも単色として扱う"""
    if not isinstance(brush, dict):
        return True
    return "SolidColorBrush" in str(brush.get("Type") or "SolidColorBrush")


def _plugin(brush: dict[str, Any]) -> str:
    return str(brush.get("Type") or "").partition(",")[0].rpartition(".")[2]


def _stops(raw: Any, report: CompatibilityReport, owner: str) -> dict[str, ParamValue]:
    """色の並びを、位置の順に並べて上限までに収める

    配布物の中には位置の順に並んでいないものがある（エディタで点を動かした順）
    上限を超えた分は、両端を残して間を等間隔に間引く
    """
    entries = raw if isinstance(raw, list) else []
    stops = [
        (
            min(max(number(stop.get("Offset"), 0.0), 0.0), 1.0),
            colour(stop.get("Color"), (1.0, 1.0, 1.0, 1.0)),
        )
        for stop in entries
        if isinstance(stop, dict)
    ]
    stops.sort(key=lambda stop: stop[0])
    if not stops:
        stops = [(0.0, (1.0, 1.0, 1.0, 1.0)), (1.0, (0.0, 0.0, 0.0, 1.0))]
    if len(stops) > MAX_STOPS:
        report.note_missing(
            f"YMM4 の {owner} の色の数（{len(stops)} 色を {MAX_STOPS} 色に間引いた）"
        )
        step = (len(stops) - 1) / (MAX_STOPS - 1)
        stops = [stops[round(index * step)] for index in range(MAX_STOPS)]
    params: dict[str, ParamValue] = {"stops": len(stops)}
    for index, (offset, rgba) in enumerate(stops):
        params[f"color{index}"] = rgba
        params[f"offset{index}"] = AnimatedValue(offset)
    return params


def _two_colours(first: Any, second: Any) -> dict[str, ParamValue]:
    return {
        "stops": 2,
        "color0": colour(first, (1.0, 1.0, 1.0, 1.0)),
        "offset0": AnimatedValue(0.0),
        "color1": colour(second, (0.0, 0.0, 0.0, 1.0)),
        "offset1": AnimatedValue(1.0),
    }


def _rainbow() -> dict[str, ParamValue]:
    """色相を一周する虹色 配布物の虹色ブラシは彩度と明るさがどれも 100"""
    params: dict[str, ParamValue] = {"stops": MAX_STOPS}
    for index in range(MAX_STOPS):
        hue = index / (MAX_STOPS - 1)
        red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, 1.0, 1.0)
        params[f"color{index}"] = (red, green, blue, 1.0)
        params[f"offset{index}"] = AnimatedValue(hue)
    return params


def _pattern_params(
    brush: Any, length: int, keyframes: Any, report: CompatibilityReport
) -> dict[str, ParamValue] | None:
    """ブラシを模様の設定へ 写せない種類なら ``None``（記録に残す）"""
    if not isinstance(brush, dict):
        return None
    parameter = brush.get("Parameter")
    parameter = parameter if isinstance(parameter, dict) else {}
    values = _Values(parameter, length, keyframes)
    plugin = _plugin(brush)

    if "SolidColorBrush" in plugin or not plugin:
        return {
            "pattern": "solid",
            "stops": 1,
            "color0": colour(parameter.get("Color"), (1.0, 1.0, 1.0, 1.0)),
        }
    if plugin.startswith("LinearGradientBrush"):
        return {
            "pattern": "linear",
            **_stops(parameter.get("Stops"), report, "線形グラデーションのブラシ"),
            "size": values.track("Size", 400.0),
            "offset": values.track("Offset"),
            "angle": values.track("Angle"),
            "extend": _EXTEND.get(str(parameter.get("ExtendMode") or ""), "clamp"),
            "relative": str(parameter.get("CoordinateMode") or "") == "Relative",
        }
    if plugin.startswith("RadiulGradientBrush") or plugin.startswith("RadialGradientBrush"):
        # 焦点（Origin）は中心からのずれとして足す 試験では中心を動かしたときと同じ絵だった
        center_x = number(parameter.get("CenterX"), 0.0) + number(parameter.get("OriginX"), 0.0)
        center_y = number(parameter.get("CenterY"), 0.0) + number(parameter.get("OriginY"), 0.0)
        return {
            "pattern": "radial",
            **_stops(parameter.get("Stops"), report, "円形グラデーションのブラシ"),
            "center_x": AnimatedValue(center_x),
            "center_y": AnimatedValue(center_y),
            "radius_x": values.track("RadiusX", 300.0),
            "radius_y": values.track("RadiusY", 300.0),
            "zoom": values.track("Zoom", 100.0),
            "angle": values.track("Angle"),
            "aspect": values.track("Aspect"),
            "extend": _EXTEND.get(str(parameter.get("ExtendMode") or ""), "clamp"),
        }
    if plugin.startswith("StripeBrush"):
        return {
            "pattern": "stripe",
            **_two_colours(parameter.get("Color1"), parameter.get("Color2")),
            "width_a": values.track("Width1", 40.0),
            "width_b": values.track("Width2", 40.0),
            "offset": values.track("Offset"),
            "zoom": values.track("Zoom", 100.0),
            "angle": values.track("Angle"),
        }
    if plugin.startswith("DotBrush"):
        return {
            "pattern": "dot",
            **_two_colours(parameter.get("Foreground"), parameter.get("Background")),
            "dot_radius": values.track("Radius", 10.0),
            "span": values.track("Span", 40.0),
            "zoom": values.track("Zoom", 100.0),
            "center_x": values.track("X"),
            "center_y": values.track("Y"),
            "angle": values.track("Angle"),
            "aspect": values.track("Aspect"),
            "inverted": parameter.get("IsInverted") is True,
        }
    if plugin.startswith("GridLineBrush"):
        return {
            "pattern": "grid",
            **_two_colours(parameter.get("StrokeColor"), parameter.get("BackgroundColor")),
            "thickness": values.track("Thickness", 2.0),
            "cell_width": values.track("Width", 40.0),
            "cell_height": values.track("Height", 40.0),
            "zoom": values.track("Zoom", 100.0),
            "center_x": values.track("X"),
            "center_y": values.track("Y"),
            "angle": values.track("Angle"),
            "inverted": parameter.get("IsInverted") is True,
        }
    if plugin.startswith("RainbowLinearGradientBrush"):
        # 割合の指定は絵の幅が 100（YMM4 に 400 と 800 の幅で描かせて確かめた）
        return {
            "pattern": "linear",
            **_rainbow(),
            "size": values.track("Width", 200.0),
            "offset": values.track("Offset"),
            "angle": values.track("Angle"),
            "extend": _EXTEND.get(str(parameter.get("ExtendMode") or ""), "wrap"),
            "relative": str(parameter.get("CoordinateMode") or "") == "Relative",
        }
    if plugin.startswith("NoiseBrush"):
        return _noise(parameter, length, keyframes, report)
    report.note_missing(f"YMM4 のブラシ: {plugin}")
    return None


#: YMM4 のノイズの種類と、塗りのエフェクトのノイズ
_NOISE_KINDS = {
    "Perlin": "perlin",
    "Simplex": "perlin",
    "Random": "random",
    "Fractal": "fractal",
    "Curl": "curl",
    "Voronoi": "voronoi",
    "Cellular": "cellular",
    "Block": "block",
}


def _noise(
    parameter: dict[str, Any], length: int, keyframes: Any, report: CompatibilityReport
) -> dict[str, ParamValue]:
    """ノイズのブラシ 乱数の出方は YMM4 と違うが、粒の粗さ・濃さ・段階・動きを合わせる

    値の意味は YMM4 に描かせた試験（``tools/ymm4_probes.py`` の 3 回目）から読んだ
    大きさ（``Size``）は横と縦の大きさにそのまま掛かる
    """
    kind = str(parameter.get("NoiseType") or "Perlin")
    mapped = _NOISE_KINDS.get(kind)
    if mapped is None:
        report.note_missing(f"YMM4 のノイズのブラシの種類: {kind}")
        mapped = "perlin"
    raw = parameter.get("NoiseParameter")
    noise = _Values(raw if isinstance(raw, dict) else {}, length, keyframes)
    if isinstance(raw, dict) and number(raw.get("WarpStrength"), 0.0) != 0.0:
        report.note_missing("YMM4 のノイズのゆがみ（WarpStrength）")
    size = number(raw.get("Size"), 100.0) / 100.0 if isinstance(raw, dict) else 1.0
    if not math.isfinite(size):
        # 粒の大きさへ非有限が入ると、掛けた先の横縦の大きさまで NaN になり模様が消える
        report.note_missing("YMM4 のノイズのブラシの大きさ（数として読めない値）")
        size = 1.0
    # NaN や無限大を round へ渡すと例外になり、同じテンプレートのほかのアイテムまで読めない
    octaves = number(raw.get("Octaves"), 5.0) if isinstance(raw, dict) else 5.0
    if not math.isfinite(octaves):
        report.note_missing("YMM4 のノイズのブラシの重ね数（数として読めない値）")
        octaves = 5.0
    fractal = str(raw.get("FractalMode") or "Normal") if isinstance(raw, dict) else "Normal"
    return {
        "pattern": "noise",
        **_two_colours(parameter.get("Color1"), parameter.get("Color2")),
        "noise_kind": mapped,
        "noise_strength": noise.track("Strength", 100.0),
        "noise_threshold": noise.track("Threshold"),
        "noise_levels": noise.track("Levels", 256.0),
        "noise_octaves": max(1, min(8, round(octaves))),
        "turbulence": fractal == "Turbulence",
        "colorful": parameter.get("IsColor") is True,
        "noise_scale_x": noise.track("ScaleX", 100.0, scale=size),
        "noise_scale_y": noise.track("ScaleY", 100.0, scale=size),
        "noise_x": noise.track("X"),
        "noise_y": noise.track("Y"),
        "noise_z": noise.track("Z"),
        "speed_x": noise.track("SpeedX"),
        "speed_y": noise.track("SpeedY"),
        "speed_z": noise.track("SpeedZ"),
        "angle": noise.track("Angle"),
    }


def _create(params: dict[str, ParamValue]) -> Effect | None:
    definition = registry.get("brush_fill")
    return None if definition is None else definition.create(**params)


def brush_effect(
    brush: Any,
    report: CompatibilityReport,
    *,
    length: int = 1,
    keyframes: Any = None,
    blend: str = "normal",
    opacity: AnimatedValue | None = None,
    pattern_only: bool = False,
    key_only: bool = False,
) -> Effect | None:
    """ブラシで塗るエフェクト 写せないブラシなら ``None``

    ``key_only`` は目印の色（マゼンタ）で塗った所だけを模様に替える
    （線の図形の塗りのように、絵の一部だけを模様にしたいとき）
    """
    params = _pattern_params(brush, length, keyframes, report)
    if params is None:
        return None
    params.update(
        {
            "blend": blend,
            "pattern_only": pattern_only,
            "key_only": key_only,
            "opacity": opacity or AnimatedValue(100.0),
        }
    )
    return _create(params)


def noise_mask(
    entry: dict[str, Any], report: CompatibilityReport, *, length: int = 1, keyframes: Any = None
) -> Effect | None:
    """「ノイズ」エフェクト（``NoiseEffect``） ノイズの値だけ不透明度か色を薄める

    値の作り方はノイズのブラシと同じ（強さを掛け、しきい値で持ち上げ、段階で刻む）
    YMM4 に描かせた白い四角（#177 の探り）は、乱数・強さ 100 で不透明度の平均 0.50、
    強さ 50 で 0.75、しきい値 50 で半分が消え、色を薄める方（``IsAlpha`` が偽）は灰 128 が
    64 になった どれも「1 - 値」を掛けると合う 強さ 200 は全部が消え、この読みと合わない
    （読みなら半分残る） 配布物は強さ 11〜164 で、200 を越える物は無い
    """
    params = _noise(entry, length, keyframes, report)
    if entry.get("IsColor") is True:
        # 色ごとのノイズは 1 つの値で薄める 色ずれまでは写さない
        report.note_missing("YMM4 のノイズの色ごとの値（1 つの値で薄めた）")
        params["colorful"] = False
    params["noise_mask"] = "alpha" if entry.get("IsAlpha") is not False else "color"
    return _create(params)


def _blend(name: Any, report: CompatibilityReport, owner: str) -> str:
    raw = str(name or "Normal")
    found = BLEND_NAMES.get(raw)
    if found is None:
        report.note_missing(f"YMM4 の {owner} の合成モード: {raw}")
        return "normal"
    return found


def fill_foreground(
    entry: dict[str, Any], report: CompatibilityReport, *, length: int = 1, keyframes: Any = None
) -> Effect | None:
    """「前景を塗りつぶし」 ブラシの模様を、合成モードと不透明度で重ねる"""
    values = _Values(entry, length, keyframes)
    return brush_effect(
        entry.get("Brush"),
        report,
        length=length,
        keyframes=keyframes,
        blend=_blend(entry.get("BlendMode"), report, "前景を塗りつぶし"),
        opacity=values.track("Opacity", 100.0),
        # ブラシだけで塗る（元の絵の色を捨てて形だけを使う）
        pattern_only=entry.get("IsBrushOnly") is True,
    )


def gradient_effect(
    entry: dict[str, Any], report: CompatibilityReport, *, length: int = 1, keyframes: Any = None
) -> Effect | None:
    """「グラデーション」エフェクト 角度 0 で上から下、中心は絵の中心、X / Y は下が正"""
    values = _Values(entry, length, keyframes)
    kind = str(entry.get("GradientType") or "Linear")
    size = values.track("Size", 400.0)
    params: dict[str, ParamValue] = {
        **_stops(entry.get("Stops"), report, "グラデーション"),
        "center_x": values.track("X"),
        "center_y": values.track("Y"),
        "extend": _EXTEND.get(str(entry.get("ExtendMode") or ""), "clamp"),
        "blend": _blend(entry.get("Blend"), report, "グラデーション"),
        "opacity": values.track("Opacity", 100.0),
    }
    if kind == "Round":
        # 中心から Size の半分で端の色に届く
        half = _mapped(size, lambda value: value * 0.5)
        params.update({"pattern": "radial", "radius_x": half, "radius_y": half})
    else:
        if kind not in ("Linear", "Convex"):
            report.note_missing(f"YMM4 のグラデーションの種類: {kind}")
        # グラデーションエフェクトの角度 0 は上から下 模様の角度 0 は左から右
        angle = _mapped(values.track("Rotation"), lambda value: value + 90.0)
        pattern = "convex" if kind == "Convex" else "linear"
        params.update({"pattern": pattern, "size": size, "angle": angle})
    return _create(params)
