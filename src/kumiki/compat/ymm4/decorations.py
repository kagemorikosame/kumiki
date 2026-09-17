"""YMM4 の文字装飾と映像エフェクトを、こちらの持ち物へ写す

**実物を見て分かったこと** 配布テンプレートの ``Decorations`` は空で、飾りは
次の 2 か所に入っていた

* ``Style`` / ``StyleColor`` — テキストアイテム自身が持つ文字装飾 AviUtl2 の
  ``文字装飾`` と同じもので、:mod:`kumiki.compat.decoration` の語彙に載る
* ``VideoEffects`` — 積まれた映像エフェクトの列 手元の 2 本では
  ``OutlineEffect``（縁取り）が 152 回と圧倒的に多く、これが YMM4 の縁取りの
  実体だった

だから ``Decorations`` だけを見ていると、**縁取りが 1 つも出ない** ここでは
3 つとも読む

こちらのテキストオブジェクトが自前で持てる飾りは縁取りと影の 1 つずつなので、
縁取りが複数あるときは**一番太いもの**をテキストに載せ、残りは縁取りエフェクト
として外側に積む 重ね順は YMM4 と同じ「内側から外側へ」になる
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.compat.decoration import decoration_params, find_decoration
from kumiki.compat.ymm4.brushes import fill_foreground, gradient_effect
from kumiki.compat.ymm4.effects import CenterPoint, center_point, map_effect, mapped_names
from kumiki.compat.ymm4.values import (
    animated,
    brush_colour,
    colour,
    number,
    type_name,
)
from kumiki.core.model import AnimatedValue, Effect, ParamValue
from kumiki.effects.definition import registry

__all__ = ["DecorationResult", "map_decorations", "map_video_effects"]

#: YMM4 の ``Style``（テキストの文字装飾）と、AviUtl2 での呼び名
#:
#: 中身は同じものなので、:mod:`kumiki.compat.decoration` の表に寄せて
#: 太さの決め方を 1 か所にまとめる
_STYLES: dict[str, str] = {
    "Normal": "標準文字",
    "Shadow": "影付き文字",
    "ThinShadow": "影付き文字（薄）",
    "Border": "縁取り文字",
    "ThinBorder": "縁取り文字（細）",
    "ThickBorder": "縁取り文字（太）",
    "Outline": "縁取り文字",
}


@dataclass(slots=True)
class DecorationResult:
    """装飾を分けた結果"""

    #: テキストオブジェクトへ直接載せる設定
    params: dict[str, ParamValue] = field(default_factory=dict)
    #: 外側に積むエフェクト 内側から外側の順
    effects: list[Effect] = field(default_factory=list)
    #: 最後に効いている中心点 アイテムの位置・拡大・回転の支点にもなる
    pivot: CenterPoint | None = None


def map_decorations(
    decorations: Any,
    report: CompatibilityReport,
    *,
    size: float = 64.0,
    style: str = "",
    style_colour: Any = None,
) -> DecorationResult:
    """文字の飾りを読む

    ``decorations`` は ``Decorations`` の列（実物では空のことが多い）、
    ``style`` は ``Style``、``style_colour`` は ``StyleColor``
    """
    result = DecorationResult()

    tint = colour(style_colour, (0.0, 0.0, 0.0, 1.0))
    if style:
        name = _STYLES.get(style)
        if name is None:
            report.note_missing(f"YMM4 の文字装飾: {style}")
        else:
            decoration = find_decoration(name)
            if decoration is not None:
                result.params.update(decoration_params(decoration, size, tint))

    if not isinstance(decorations, list):
        return result

    borders: list[tuple[float, tuple[float, float, float, float]]] = []
    for entry in decorations:
        if not isinstance(entry, dict):
            continue
        name = type_name(entry)
        if name.endswith("BorderDecoration") or name in ("Border", "Outline"):
            borders.append(_border(entry))
        elif name.endswith("ShadowDecoration") or name == "Shadow":
            _shadow(entry, result, size)
        else:
            report.note_missing(f"YMM4 の装飾: {name or '種類不明'}")

    _place_borders(borders, result)
    return result


#: ``VideoEffects`` の種類と、こちらのエフェクト種別
#:
#: 手元の配布テンプレート 2 本に出てきた 50 種あまりのうち、同じ絵になるものだけ
#: 残りは記録に残して素通しにする 似た別のもので代用すると、直したつもりの
#: 無い違いが出る
_VIDEO_EFFECTS: dict[str, str] = {
    "GaussianBlurEffect": "blur",
    "BlurEffect": "blur",
    "DirectionalBlurEffect": "directional_blur",
    "UnidirectionalBlurEffect": "directional_blur",
    "ColorCorrectionEffect": "color",
    "MonocolorizationEffect": "monochrome",
    "MosaicEffect": "mosaic",
    "NoiseEffect": "noise",
    "CropEffect": "crop",
    "ZoomEffect": "zoom",
    "RotateEffect": "rotate",
    "DrawPositionEffect": "position",
    "OpacityEffect": "opacity",
    "LuminanceKeyEffect": "luminance_key",
}


def map_video_effects(
    effects: Any, report: CompatibilityReport, *, length: int = 1, keyframes: Any = None
) -> DecorationResult:
    """``VideoEffects`` の列を読む

    縁取り（``OutlineEffect``）はテキストの飾りとして扱えるので、
    :class:`DecorationResult` に分けて返す
    """
    result = DecorationResult()
    if not isinstance(effects, list):
        return result

    borders: list[tuple[float, tuple[float, float, float, float]]] = []
    pivot: CenterPoint | None = None
    for entry in effects:
        if not isinstance(entry, dict):
            continue
        if entry.get("IsEnabled") is False:
            continue

        name = type_name(entry)
        if name == "ShowOnlyPreviewEffect":
            # 掛かったアイテムごと書き出しから外す（アイテムを読む所で見る）
            continue
        if name == "CenterPointEffect":
            # 後ろに続く回転と拡大の支点になる 位置を保たないなら絵もずらす
            # 「位置を保つ」を切ったときのずらしは、アイテムを最後に置く変形
            # （:func:`kumiki.compat.ymm4.template._placement`）がまとめて行う 途中で
            # ずらすと、後ろの変形が元の範囲を支点にしたまま回り、支点が合わない
            pivot, _ = center_point(entry, report, length=length, keyframes=keyframes)
            continue
        if name == "OutlineEffect":
            borders.append(
                (
                    max(0.0, number(entry.get("StrokeThickness"), 4.0)),
                    brush_colour(entry.get("StrokeBrush")),
                )
            )
            continue

        if name == "FillForegroundEffect":
            result.effects.append(
                _or_skip(fill_foreground(entry, report, length=length, keyframes=keyframes))
            )
            continue
        if name == "GradientEffect":
            result.effects.append(
                _or_skip(gradient_effect(entry, report, length=length, keyframes=keyframes))
            )
            continue
        built = _video_effect(name, entry, length, keyframes)
        if built is None:
            built = map_effect(name, entry, report, length=length, keyframes=keyframes)
        if built is None:
            # 写し方を持っている種類で None なら、形の問題としてすでに記録してある
            if name not in mapped_names():
                report.note_missing(f"YMM4 の映像エフェクト: {name or '種類不明'}")
            continue
        if pivot is not None:
            if built.kind == "transform":
                built = with_pivot(built, pivot)
            elif built.kind in _PIVOTED_KINDS:
                # 支点を受け取れない回転と拡大 中央で回るので見た目が変わりうる
                report.note_missing(f"YMM4 の CenterPointEffect（{name} の支点）")
        result.effects.append(built)

    _place_borders(borders, result)
    result.pivot = pivot
    return result


#: 支点で見た目が変わるが、まだ支点を受け取れないエフェクト
_PIVOTED_KINDS = frozenset(
    {
        "random_rotate",
        "random_zoom",
        "repeat_rotate",
        "inout_zoom",
        "inout_getup",
        "spiral",
    }
)


def _or_skip(effect: Effect | None) -> Effect:
    """写せなかったブラシは素通しのエフェクトにする（記録は写す側が残してある）"""
    if effect is not None:
        return effect
    definition = registry.get("opacity")
    assert definition is not None  # 標準エフェクトは必ずある
    return definition.create(amount=100.0)


def with_pivot(effect: Effect, pivot: CenterPoint) -> Effect:
    """変形の支点を、前にあった中心点に合わせる"""
    definition = registry.get(effect.kind)
    if definition is None:  # pragma: no cover - 変形は標準エフェクト
        return effect
    params = dict(effect.params)
    for name, value in pivot.params().items():
        spec = definition.spec(name)
        if spec is not None:
            params[name] = spec.coerce(value)
    return replace(effect, params=params)


def _video_effect(name: str, entry: dict[str, Any], length: int, keyframes: Any) -> Effect | None:
    kind = _VIDEO_EFFECTS.get(name)
    if kind is None:
        return None

    def value(key: str, default: float = 0.0, scale: float = 1.0) -> AnimatedValue:
        return animated(entry.get(key), default, length=length, keyframes=keyframes, scale=scale)

    if kind == "blur":
        definition = registry.get("blur")
        return None if definition is None else definition.create(radius=value("Blur", 8.0))
    if kind == "mosaic":
        definition = registry.get("mosaic")
        return None if definition is None else definition.create(size=value("Size", 16.0))
    if kind == "noise":
        definition = registry.get("noise")
        return None if definition is None else definition.create(strength=value("Intensity", 20.0))
    if kind == "color":
        definition = registry.get("color")
        if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
            return None
        # YMM4 は 100 を「変化なし」にする百分率 こちらは 0 が変化なし
        return definition.create(
            brightness=AnimatedValue(number(entry.get("Lightness"), 100.0) - 100.0),
            contrast=AnimatedValue(number(entry.get("Contrast"), 100.0) - 100.0),
            saturation=AnimatedValue(number(entry.get("Saturation"), 100.0) - 100.0),
            hue=value("HueRotation"),
        )
    if kind == "directional_blur":
        definition = registry.get("directional_blur")
        if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
            return None
        return definition.create(radius=value("StandardDeviation", 16.0), angle=value("Angle"))
    if kind == "fill":
        definition = registry.get("fill")
        if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
            return None
        # 合成モードまでは写せない 塗る色と強さだけを合わせる
        return definition.create(
            color=brush_colour(entry.get("Brush"), (1.0, 1.0, 1.0, 1.0)),
            amount=value("Opacity", 100.0),
        )
    if kind == "opacity":
        definition = registry.get("opacity")
        if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
            return None
        return definition.create(amount=value("Opacity", 100.0))
    if kind == "luminance_key":
        definition = registry.get("luminance_key")
        if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
            return None
        # ``Mode`` が ``Dark`` なら暗いところを抜く ``IsInvert`` はその反転
        dark = str(entry.get("Mode") or "") == "Dark"
        return definition.create(
            threshold=value("Threshold", 50.0),
            smoothness=value("Smoothness", 10.0),
            invert=dark is bool(entry.get("IsInvert")),
        )
    if kind == "position":
        definition = registry.get("transform")
        if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
            return None
        # YMM4 の Y は下向き こちらは上向き
        return definition.create(pos_x=value("X"), pos_y=value("Y", scale=-1.0))
    if kind == "monochrome":
        definition = registry.get("color")
        if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
            return None
        # 単色化 色を抜くところまでは同じ絵になる 着色まではできない
        return definition.create(saturation=-100)
    if kind == "zoom":
        definition = registry.get("transform")
        if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
            return None
        # 拡大率は ``Zoom`` に縦横それぞれの ``ZoomX`` ``ZoomY`` が掛かる
        # どれも動きうるので、素の数で読むと登場アニメーションが止まる
        return definition.create(scale=value("Zoom", 100.0), scale_y=value("ZoomY", 100.0))
    if kind == "rotate":
        definition = registry.get("transform")
        if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
            return None
        # Z は平面の回転、X と Y は板を傾ける立体の回転 X と Y は Kumiki と向きが逆
        return definition.create(
            rotation=value("Z"),
            rotation_x=value("X", scale=-1.0),
            rotation_y=value("Y", scale=-1.0),
        )
    if kind == "crop":
        definition = registry.get("crop")
        if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
            return None
        return definition.create(
            top=value("Top"), bottom=value("Bottom"), left=value("Left"), right=value("Right")
        )
    return None


def _border(entry: dict[str, Any]) -> tuple[float, tuple[float, float, float, float]]:
    thickness = number(entry.get("Thickness"), number(entry.get("StrokeThickness"), 4.0))
    tint = brush_colour(entry.get("StrokeBrush"), colour(entry.get("Color"), (0.0, 0.0, 0.0, 1.0)))
    return max(0.0, thickness), tint


def _place_borders(
    borders: list[tuple[float, tuple[float, float, float, float]]],
    result: DecorationResult,
) -> None:
    """縁取りを、テキスト側 1 本とエフェクト側の残りに分ける

    一番太いものをテキストに持たせるのは、それが文字の形をいちばん強く決めるから
    細いほうをテキストに載せると、太いほうをエフェクトで足したときに二重の縁の
    間隔が変わる
    """
    usable = [item for item in borders if item[0] > 0.0]
    if not usable:
        return

    widest = max(usable, key=lambda item: item[0])
    result.params["border_width"] = AnimatedValue(widest[0])
    result.params["border_color"] = widest[1]

    definition = registry.get("border")
    if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
        return
    seen_widest = False
    for thickness, tint in usable:
        if not seen_widest and (thickness, tint) == widest:
            seen_widest = True
            continue
        result.effects.append(definition.create(width=thickness, color=tint))


def _shadow(entry: dict[str, Any], result: DecorationResult, size: float) -> None:
    if "shadow_x" in result.params:
        # 2 つ目以降の影は載せられない 黙って捨てず、エフェクトの影として積む
        definition = registry.get("shadow")
        if definition is not None:
            result.effects.append(
                definition.create(
                    offset_x=number(entry.get("X"), 0.0),
                    offset_y=number(entry.get("Y"), 0.0),
                    blur=number(entry.get("Blur"), 0.0),
                    color=colour(entry.get("Color"), (0.0, 0.0, 0.0, 1.0)),
                )
            )
        return

    default = size * 0.06
    result.params["shadow_x"] = AnimatedValue(number(entry.get("X"), default))
    result.params["shadow_y"] = AnimatedValue(-number(entry.get("Y"), default))
    result.params["shadow_blur"] = AnimatedValue(number(entry.get("Blur"), 0.0))
    result.params["shadow_color"] = colour(entry.get("Color"), (0.0, 0.0, 0.0, 1.0))
