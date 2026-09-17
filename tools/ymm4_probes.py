"""YMM4 の設定の意味を確かめるための試験テンプレートを作る

配布物の値の並びだけでは、角度の向きや大きさの基準（画素か割合か）が決まらない
値を 1 つずつ変えたテンプレートを作り、``tools/ymm4_compare.py`` で YMM4 本体に
描かせて、並んだ絵から意味を読み取る

ひな形（エフェクトや図形の JSON の形）は、手元の配布テンプレート
（``tests/fixtures/ymm4``）から取り出す 配布物そのものは使わず、形だけを借りる

使い方::

    .venv\\Scripts\\python.exe tools\\ymm4_probes.py .work\\probes\\probes.ymmt
    .venv\\Scripts\\python.exe tools\\ymm4_compare.py --work .work\\probe \
        build .work\\probes\\probes.ymmt
"""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from ymm4_compare import base_shape  # noqa: E402

from kumiki.compat.ymm4.template import load_template  # noqa: E402
from kumiki.compat.ymm4.values import type_name  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ymm4"
EFFECTS = "YukkuriMovieMaker.Project.Effects"
BRUSHES = "YukkuriMovieMaker.Brush"


def still(value: float) -> dict[str, Any]:
    return {"Values": [{"Value": value}], "Span": 0.0, "AnimationType": "なし"}


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def collect_samples() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """配布テンプレートから、種類ごとに 1 つずつ JSON の形を取り出す"""
    shapes: dict[str, dict[str, Any]] = {}
    brushes: dict[str, dict[str, Any]] = {}
    for path in sorted(FIXTURES.rglob("*.ymmt")):
        for template in load_template(path):
            for item in template.items:
                for node in _walk(item):
                    if "$type" in node:
                        shapes.setdefault(type_name(node), copy.deepcopy(node))
                    kind = str(node.get("Type") or "")
                    if "Brush" in kind and isinstance(node.get("Parameter"), dict):
                        brushes.setdefault(
                            kind.partition(",")[0].rpartition(".")[2], copy.deepcopy(node)
                        )
    return shapes, brushes


def set_values(node: dict[str, Any], **values: Any) -> dict[str, Any]:
    """数はアニメーションの形、そのほかはそのまま入れる"""
    for key, value in values.items():
        current = node.get(key)
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and isinstance(current, dict)
        ):
            node[key] = still(float(value))
        else:
            node[key] = value
    return node


def solid(color: str) -> dict[str, Any]:
    return {
        "Type": "YukkuriMovieMaker.Plugin.Brush.SolidColorBrushPlugin, YukkuriMovieMaker",
        "Parameter": {
            "$type": "YukkuriMovieMaker.Plugin.Brush.SolidColorBrushParameter, YukkuriMovieMaker",
            "Color": color,
        },
    }


def build(
    samples: dict[str, dict[str, Any]], brushes: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    probes: list[tuple[str, dict[str, Any]]] = []

    def shape(
        width: float = 1200.0, height: float = 600.0, color: str = "#FFFFFFFF"
    ) -> dict[str, Any]:
        item = base_shape(0, 0, 60)
        item["ShapeParameter"]["Width"] = still(width)
        item["ShapeParameter"]["Height"] = still(height)
        item["ShapeParameter"]["Brush"] = solid(color)
        return item

    def with_effect(name: str, effect: dict[str, Any], base: dict[str, Any] | None = None) -> None:
        item = base if base is not None else shape()
        item["VideoEffects"] = [effect]
        probes.append((name, item))

    def effect(kind: str, **values: Any) -> dict[str, Any]:
        found = samples.get(kind)
        if found is None:
            raise KeyError(kind)
        entry = copy.deepcopy(found)
        entry["IsEnabled"] = True
        return set_values(entry, **values)

    def brush(kind: str, **values: Any) -> dict[str, Any]:
        entry = copy.deepcopy(brushes[kind])
        set_values(entry["Parameter"], **values)
        return entry

    two_stops = [{"Offset": 0.0, "Color": "#FFFF0000"}, {"Offset": 1.0, "Color": "#FF0000FF"}]
    three_stops = [
        {"Offset": 0.0, "Color": "#FFFF0000"},
        {"Offset": 0.25, "Color": "#FF00FF00"},
        {"Offset": 1.0, "Color": "#800000FF"},
    ]

    # --- グラデーション（エフェクト） ---
    gradient = {
        "GradientType": "Linear",
        "ExtendMode": "Clamp",
        "Blend": "Normal",
        "X": 0,
        "Y": 0,
        "Rotation": 0,
        "Size": 400,
        "Opacity": 100,
        "Stops": two_stops,
    }
    for name, change in {
        "linear": {},
        "rot45": {"Rotation": 45},
        "rot90": {"Rotation": 90},
        "size800": {"Size": 800},
        "x300": {"X": 300},
        "y200": {"Y": 200},
        "round": {"GradientType": "Round"},
        "convex": {"GradientType": "Convex"},
        "wrap": {"ExtendMode": "Wrap", "Size": 200},
        "mirror": {"ExtendMode": "Mirror", "Size": 200},
        "multiply": {"Blend": "Multiply"},
        "opacity50": {"Opacity": 50},
        "stops3": {"Stops": three_stops},
    }.items():
        with_effect(f"gradient_{name}", effect("GradientEffect", **{**gradient, **change}))

    # --- 前景の塗りつぶし（ブラシ） ---
    fill = {"Opacity": 100, "BlendMode": "Normal", "IsBrushOnly": False}
    linear = {
        "Stops": two_stops,
        "CoordinateMode": "Pixel",
        "Size": 600,
        "Offset": 0,
        "Angle": 0,
        "ExtendMode": "Clamp",
    }
    for name, change in {
        "linear": {},
        "linear_angle45": {"Angle": 45},
        "linear_offset200": {"Offset": 200},
        "linear_wrap": {"ExtendMode": "Wrap", "Size": 200},
        "linear_relative": {"CoordinateMode": "Relative", "Size": 50},
        "linear_stops3": {"Stops": three_stops},
    }.items():
        with_effect(
            f"fill_{name}",
            effect(
                "FillForegroundEffect",
                **fill,
                Brush=brush("LinearGradientBrushPlugin", **{**linear, **change}),
            ),
        )
    radial = {
        "Stops": two_stops,
        "CenterX": 0,
        "CenterY": 0,
        "OriginX": 0,
        "OriginY": 0,
        "RadiusX": 300,
        "RadiusY": 300,
        "Zoom": 100,
        "Angle": 0,
        "Aspect": 0,
        "ExtendMode": "Clamp",
        "IsInverted": False,
    }
    for name, change in {
        "radial": {},
        "radial_center": {"CenterX": 200, "CenterY": 100},
        "radial_origin": {"OriginX": 150},
        "radial_aspect": {"Aspect": 50},
        "radial_zoom": {"Zoom": 200},
        "radial_inverted": {"IsInverted": True},
    }.items():
        with_effect(
            f"fill_{name}",
            effect(
                "FillForegroundEffect",
                **fill,
                Brush=brush("RadiulGradientBrushPlugin", **{**radial, **change}),
            ),
        )
    stripe = {
        "Color1": "#FFFF0000",
        "Width1": 40,
        "Color2": "#FF0000FF",
        "Width2": 80,
        "Offset": 0,
        "Zoom": 100,
        "Angle": 0,
    }
    for name, change in {
        "stripe": {},
        "stripe_angle45": {"Angle": 45},
        "stripe_offset20": {"Offset": 20},
        "stripe_zoom200": {"Zoom": 200},
    }.items():
        with_effect(
            f"fill_{name}",
            effect(
                "FillForegroundEffect",
                **fill,
                Brush=brush("StripeBrushPlugin", **{**stripe, **change}),
            ),
        )
    dot = {
        "Foreground": "#FFFF0000",
        "Background": "#FF0000FF",
        "Radius": 10,
        "Span": 40,
        "Zoom": 100,
        "X": 0,
        "Y": 0,
        "Angle": 0,
        "Aspect": 0,
        "IsInverted": False,
    }
    for name, change in {
        "dot": {},
        "dot_angle45": {"Angle": 45},
        "dot_xy": {"X": 10, "Y": 20},
        "dot_aspect": {"Aspect": 50},
        "dot_inverted": {"IsInverted": True},
    }.items():
        with_effect(
            f"fill_{name}",
            effect(
                "FillForegroundEffect", **fill, Brush=brush("DotBrushPlugin", **{**dot, **change})
            ),
        )
    grid = {
        "StrokeColor": "#FFFF0000",
        "BackgroundColor": "#FF0000FF",
        "Thickness": 4,
        "Width": 40,
        "Height": 60,
        "Zoom": 100,
        "X": 0,
        "Y": 0,
        "Angle": 0,
        "Aspect": 0,
        "IsInverted": False,
    }
    for name, change in {"grid": {}, "grid_angle30": {"Angle": 30}}.items():
        with_effect(
            f"fill_{name}",
            effect(
                "FillForegroundEffect",
                **fill,
                Brush=brush("GridLineBrushPlugin", **{**grid, **change}),
            ),
        )
    for mode in (
        "Multiply",
        "Overlay",
        "Screen",
        "Subtract",
        "Add",
        "LighterColor",
        "Darker",
        "SoftLight",
        "Difference",
        "ColorBurn",
        "PinLight",
        "VividLight",
        "LinearLight",
        "Division",
        "Lighter",
        "ColorDodge",
        "HardMix",
        "LinearDodge",
        "HardLight",
        "Hue",
    ):
        base = shape(color="#FF808080")
        with_effect(
            f"fill_blend_{mode}",
            effect(
                "FillForegroundEffect",
                Opacity=100,
                BlendMode=mode,
                IsBrushOnly=False,
                Brush=solid("#FFFF8040"),
            ),
            base,
        )
    with_effect(
        "fill_opacity50",
        effect(
            "FillForegroundEffect",
            Opacity=50,
            BlendMode="Normal",
            IsBrushOnly=False,
            Brush=solid("#FFFF0000"),
        ),
    )
    dotted = shape()
    dotted["ShapeParameter"]["Brush"] = brush("DotBrushPlugin", **dot)
    probes.append(("shape_brush_dot", dotted))

    # --- 影 ---
    red = solid("#FFFF0000")
    small = lambda: shape(600, 300)  # noqa: E731 - 影がはみ出すので小さめの下地
    for name, change in {
        "plain": {},
        "blur10": {"Blur": 10},
        "zoom120": {"Zoom": 120},
        "angle30": {"Angle": 30},
        "angle30_center": {"Angle": 30, "IsRotateAtCenter": True},
        "opacity50": {"Opacity": 50},
    }.items():
        values = {
            "X": 40,
            "Y": 30,
            "Opacity": 100,
            "Zoom": 100,
            "Angle": 0,
            "Blur": 0,
            "IsRotateAtCenter": False,
            "Brush": red,
        }
        with_effect(f"shadow_{name}", effect("ShadowEffect", **{**values, **change}), small())
    for name, change in {
        "plain": {},
        "blur10": {"Blur": 10},
        "multiply": {"BlendMode": "Multiply"},
    }.items():
        values = {"X": 40, "Y": 30, "Opacity": 100, "Blur": 0, "BlendMode": "Normal", "Brush": red}
        with_effect(
            f"inner_shadow_{name}", effect("InnerShadowEffect", **{**values, **change}), small()
        )
    for name, change in {
        "solid": {},
        "angle45": {"Angle": 45},
        "image": {"ShadowType": "Image"},
        "attenuation50": {"Attenuation": 50},
    }.items():
        values = {
            "Angle": 135,
            "Length": 60,
            "Opacity": 100,
            "Attenuation": 0,
            "ShadowType": "Solid",
            "Color1": "#FFFF0000",
            "Color2": "#FF0000FF",
        }
        with_effect(
            f"long_shadow_{name}", effect("LongShadowEffect", **{**values, **change}), small()
        )

    # --- マスク ---
    circle = copy.deepcopy(samples["MaskEffect"])
    for name, change in {
        "circle": {},
        "circle_invert": {"InvertMask": True},
        "circle_blur30": {"Blur": 30},
        "circle_x200": {"X": 200, "Y": 100},
    }.items():
        entry = copy.deepcopy(circle)
        entry["ShapeType2"] = "YukkuriMovieMaker.Shape.CircleShapePlugin, YukkuriMovieMaker"
        entry["ShapeParameter"] = copy.deepcopy(samples["CircleShapeParameter"])
        set_values(
            entry["ShapeParameter"],
            SizeMode="SizeAspect",
            Size=400,
            AspectRate=0,
            StrokeThickness=4000,
        )
        set_values(entry, X=0, Y=0, Opacity=100, Angle=0, Blur=0, InvertMask=False, IsEnabled=True)
        set_values(entry, **change)
        with_effect(f"mask_{name}", entry)
    fan = copy.deepcopy(circle)
    fan["ShapeType2"] = "YukkuriMovieMaker.Shape.FanShapePlugin, YukkuriMovieMaker"
    fan["ShapeParameter"] = set_values(
        copy.deepcopy(samples["FanShapeParameter"]),
        CenterAngle=90,
        CenterAngleBase=100,
        SizeMode="SizeAspect",
        Size=500,
        AspectRate=0,
        StrokeThickness=4000,
    )
    set_values(fan, X=0, Y=0, Opacity=100, Angle=0, Blur=0, InvertMask=False, IsEnabled=True)
    with_effect("mask_fan90", fan)

    # --- 反転コピー・背景の塗り ---
    for name, change in {
        "right": {},
        "bottom": {"Position": "Bottom"},
        "distance100": {"Distance": 100},
        "not_centering": {"IsCentering": False},
    }.items():
        values = {
            "Position": "Right",
            "Distance": 0,
            "IsTopBottomReversed": False,
            "IsLeftRightReversed": True,
            "IsCentering": True,
        }
        with_effect(
            f"copy_reverse_{name}",
            effect("CopyAndReverseEffect", **{**values, **change}),
            shape(400, 300),
        )
    for name, change in {
        "margins": {},
        "round40": {"Round": 40},
        "negative": {"Right": -50},
    }.items():
        values = {
            "Opacity": 100,
            "Round": 0,
            "BlendMode": "Normal",
            "IsBackgroundOnly": False,
            "Brush": red,
            "Top": 10,
            "Bottom": 20,
            "Left": 30,
            "Right": 40,
        }
        with_effect(
            f"fill_background_{name}",
            effect("FillBackgroundEffect", **{**values, **change}),
            shape(400, 300),
        )

    # --- 図形 ---
    def shaped(name: str, plugin: str, parameter: str, **values: Any) -> None:
        item = base_shape(0, 0, 60)
        item["ShapeType2"] = f"YukkuriMovieMaker.Shape.{plugin}, YukkuriMovieMaker"
        item["ShapeParameter"] = set_values(copy.deepcopy(samples[parameter]), **values)
        if "Brush" in item["ShapeParameter"]:
            item["ShapeParameter"]["Brush"] = solid("#FFFFFFFF")
        probes.append((f"shape_{name}", item))

    shaped(
        "circle_aspect50",
        "CircleShapePlugin",
        "CircleShapeParameter",
        SizeMode="SizeAspect",
        Size=400,
        AspectRate=50,
        StrokeThickness=4000,
    )
    shaped(
        "circle_aspect_minus50",
        "CircleShapePlugin",
        "CircleShapeParameter",
        SizeMode="SizeAspect",
        Size=400,
        AspectRate=-50,
        StrokeThickness=4000,
    )
    shaped(
        "circle_stroke20",
        "CircleShapePlugin",
        "CircleShapeParameter",
        SizeMode="SizeAspect",
        Size=400,
        AspectRate=0,
        StrokeThickness=20,
    )
    shaped(
        "triangle",
        "TriangleShapePlugin",
        "TriangleShapeParameter",
        SizeMode="SizeAspect",
        Size=400,
        AspectRate=0,
        Round=0,
        StrokeThickness=4000,
    )
    shaped(
        "hexagon",
        "HexagonShapePlugin",
        "HexagonShapeParameter",
        SizeMode="SizeAspect",
        Size=400,
        AspectRate=0,
        Round=0,
        StrokeThickness=4000,
    )
    shaped(
        "star",
        "StarShapePlugin",
        "StarShapeParameter",
        SizeMode="SizeAspect",
        Size=400,
        AspectRate=0,
        Round=0,
        InflationRatio=50,
        StrokeThickness=4000,
    )
    shaped(
        "arrow",
        "ArrowShapePlugin",
        "ArrowShapeParameter",
        SizeMode="SizeAspect",
        Size=400,
        AspectRate=0,
        Dent=0,
        BarLength=50,
        BarThickness=50,
        StrokeThickness=4000,
    )
    shaped(
        "fan90",
        "FanShapePlugin",
        "FanShapeParameter",
        SizeMode="SizeAspect",
        Size=400,
        AspectRate=0,
        CenterAngle=90,
        CenterAngleBase=100,
        StrokeThickness=4000,
    )
    shaped(
        "superformula",
        "SuperformulaShapePlugin",
        "SuperformulaShapeParameter",
        SizeMode="SizeAspect",
        Size=400,
        AspectRate=0,
        M=5,
        N=1,
        StrokeThickness=4000,
    )
    shaped(
        "concentration",
        "ConcentrationLineShapePlugin",
        "ConcentrationLineShapeParameter",
        Size=800,
        Density=50,
        Thickness=50,
        Length=50,
        CenterWidth=50,
        Speed=0,
        Stroke="#FFFFFFFF",
    )
    line = set_values(
        copy.deepcopy(samples["LineShapeParameter"]),
        LineType="Straight",
        DashStyle="Solid",
        Thickness=10,
        LengthRate=100,
        IsClosed=False,
    )
    line["Points"] = [{"X": -300.0, "Y": -100.0}, {"X": 0.0, "Y": 100.0}, {"X": 300.0, "Y": -100.0}]
    line["Brush"] = solid("#FFFFFFFF")
    item = base_shape(0, 0, 60)
    item["ShapeType2"] = "YukkuriMovieMaker.Shape.LineShapePlugin, YukkuriMovieMaker"
    item["ShapeParameter"] = line
    probes.append(("shape_line", item))
    closed = copy.deepcopy(item)
    closed["ShapeParameter"]["IsClosed"] = True
    closed["ShapeParameter"]["FillBrush"] = solid("#FFFF0000")
    probes.append(("shape_line_closed", closed))

    # --- 絵を加工するもの（明暗のある下地に掛ける） ---
    def gradient_base() -> dict[str, Any]:
        base = shape()
        base["ShapeParameter"]["Brush"] = brush(
            "LinearGradientBrushPlugin",
            Stops=[
                {"Offset": 0.0, "Color": "#FF000000"},
                {"Offset": 0.5, "Color": "#FFFF2020"},
                {"Offset": 1.0, "Color": "#FFFFFFFF"},
            ],
            CoordinateMode="Pixel",
            Size=1200,
            Offset=0,
            Angle=0,
            ExtendMode="Clamp",
        )
        return base

    simple: dict[str, Callable[[], dict[str, Any]]] = {
        "border_blur20": lambda: effect("BorderBlurEffect", Blur=20),
        "binarization50": lambda: effect(
            "BinarizationEffect", Threshold=50, IsInverted=False, KeepColor=False
        ),
        "binarization50_keep": lambda: effect(
            "BinarizationEffect", Threshold=50, IsInverted=False, KeepColor=True
        ),
        "sharpen": lambda: effect("SharpenEffect", Sharpness=9, Threshold=12),
        "chroma_key_black": lambda: effect(
            "ChromaKeyEffect", Color="#FF000000", Tolerance=30, Feather=True, IsInvert=False
        ),
        "linear_transfer": lambda: effect(
            "LinearTransferEffect",
            RedYIntercept=0,
            RedSlope=50,
            GreenYIntercept=30,
            GreenSlope=100,
            BlueYIntercept=0,
            BlueSlope=100,
            AlphaYIntercept=0,
            AlphaSlope=100,
        ),
        "bloom": lambda: effect(
            "BloomEffect",
            Strength=100,
            Threshold=50,
            Blur=30,
            IsFixedSizeEnabled=False,
            IsColorizationEnabled=False,
        ),
    }
    for name, make in simple.items():
        with_effect(name, make(), gradient_base())
    return [
        {"Name": f"probe_{name}", "Path": ["probe", name], "Items": [item]} for name, item in probes
    ]


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".work" / "probes" / "probes.ymmt"
    samples, brushes = collect_samples()
    templates = build(samples, brushes)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"ItemTemplates": templates}, ensure_ascii=False), encoding="utf-8"
    )
    print(f"{len(templates)} 本の試験テンプレートを {target} に書いた")
    return 0


if __name__ == "__main__":
    sys.exit(main())
