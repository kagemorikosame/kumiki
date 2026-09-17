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


def build_second(
    samples: dict[str, dict[str, Any]], brushes: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """2 回目の試験 登場と退場（時間で変わる）、ノイズ、変形、光の当て方など

    登場は 2 秒（60 フレーム）にして、枠の中の 3 枚（入り・真ん中・終わり）で途中の
    進み具合が見えるようにする
    """
    probes: list[tuple[str, dict[str, Any]]] = []

    def base(width: float = 600.0, height: float = 300.0) -> dict[str, Any]:
        item = base_shape(0, 0, 60)
        item["ShapeParameter"]["Width"] = still(width)
        item["ShapeParameter"]["Height"] = still(height)
        item["ShapeParameter"]["Brush"] = solid("#FFFFFFFF")
        return item

    def textured() -> dict[str, Any]:
        item = base(800.0, 400.0)
        item["ShapeParameter"]["Brush"] = set_values(
            copy.deepcopy(brushes["StripeBrushPlugin"]["Parameter"])
            and copy.deepcopy(brushes["StripeBrushPlugin"]),
        )
        set_values(
            item["ShapeParameter"]["Brush"]["Parameter"],
            Color1="#FFFF4040",
            Width1=40,
            Color2="#FF4040FF",
            Width2=40,
            Offset=0,
            Zoom=100,
            Angle=0,
        )
        return item

    def effect(kind: str, **values: Any) -> dict[str, Any]:
        entry = copy.deepcopy(samples[kind])
        entry["IsEnabled"] = True
        return set_values(entry, **values)

    def add(name: str, *effects: dict[str, Any], item: dict[str, Any] | None = None) -> None:
        target = item if item is not None else base()
        target["VideoEffects"] = list(effects)
        probes.append((name, target))

    timing = {
        "IsInEffect": True,
        "IsOutEffect": False,
        "EffectTimeSeconds": 2.0,
        "EasingType": "Linear",
        "EasingMode": "In",
    }
    add("inout_fade", effect("InOutFadeEffect", Value=0.0, **timing))
    add(
        "inout_fade_out",
        effect(
            "InOutFadeEffect", Value=0.0, **{**timing, "IsInEffect": False, "IsOutEffect": True}
        ),
    )
    add(
        "inout_rotate_z180",
        effect("InOutRotateEffect", ValueX=0.0, ValueY=0.0, ValueZ=180.0, Is3D=False, **timing),
    )
    add(
        "inout_rotate_x90_3d",
        effect("InOutRotateEffect", ValueX=90.0, ValueY=0.0, ValueZ=0.0, Is3D=True, **timing),
    )
    add(
        "inout_rotate_y90_3d",
        effect("InOutRotateEffect", ValueX=0.0, ValueY=90.0, ValueZ=0.0, Is3D=True, **timing),
    )
    add("inout_move_x600", effect("InOutMoveEffect", Value=600.0, Value2=0.0, Value3=0.0, **timing))
    add(
        "inout_move_v2_90",
        effect("InOutMoveEffect", Value=600.0, Value2=90.0, Value3=0.0, **timing),
    )
    add(
        "inout_move_v3_300",
        effect("InOutMoveEffect", Value=600.0, Value2=0.0, Value3=300.0, **timing),
    )
    add(
        "inout_skew_x30",
        effect(
            "InOutSkewEffect",
            AngleX=30.0,
            AngleY=0.0,
            CenterPoint="Center",
            CenterX=0.0,
            CenterY=0.0,
            **timing,
        ),
    )
    add(
        "inout_skew_y30",
        effect(
            "InOutSkewEffect",
            AngleX=0.0,
            AngleY=30.0,
            CenterPoint="Center",
            CenterX=0.0,
            CenterY=0.0,
            **timing,
        ),
    )
    add("inout_blur20", effect("InOutGaussianBlurEffect", Value=20.0, **timing))
    add(
        "repeat_zoom",
        effect(
            "RepeatZoomEffect",
            Zoom=150.0,
            ZoomX=100.0,
            ZoomY=100.0,
            Span=2.0,
            EasingType="Linear",
            EasingMode="In",
            IsCentering=True,
        ),
    )
    add(
        "center_custom_rotate",
        effect(
            "CenterPointEffect",
            Horizontal="Custom",
            Vertical="Custom",
            X=150.0,
            Y=100.0,
            IsKeepPosition=True,
        ),
        effect("RotateEffect", X=0.0, Y=0.0, Z=45.0)
        if "RotateEffect" in samples
        else effect("CenterPointEffect"),
    )
    add(
        "center_origin_rotate",
        effect(
            "CenterPointEffect",
            Horizontal="Origin",
            Vertical="Origin",
            X=0.0,
            Y=0.0,
            IsKeepPosition=True,
        ),
        effect("RotateEffect", X=0.0, Y=0.0, Z=45.0)
        if "RotateEffect" in samples
        else effect("CenterPointEffect"),
    )
    add(
        "reel_spin",
        effect("ReelSpinEffect", Rotation=50.0, Direction=0.0, Blur=0.0, Tile=False),
        item=textured(),
    )
    add(
        "fish_eye",
        effect("FishEyeLensEffect", Projection="Orthographic", Angle=60.0, Zoom=100.0),
        item=textured(),
    )
    add(
        "ripple",
        effect("RippleEffect", X=0.0, Y=0.0, Amplitude=20.0, WaveLength=200.0, Period=1.0),
        item=textured(),
    )
    add(
        "stretch",
        effect(
            "StretchEffect",
            X=0.0,
            Y=0.0,
            Angle=0.0,
            StretchLength=300.0,
            Range=0.0,
            IsCentering=True,
        ),
    )
    add("polar", effect("PolarTransformEffect", CoreWidth=100.0, TwistAngle=0.0), item=textured())
    add(
        "inner_outline",
        effect(
            "InnerOutlineEffect",
            Thickness=20.0,
            Opacity=100.0,
            Blur=0.0,
            Quality=64.0,
            Smoothness=100.0,
            Blend="Normal",
            IsOutlineOnly=False,
            IsAngular=False,
            Brush=solid("#FFFF0000"),
        ),
    )
    add(
        "inner_halftone",
        effect(
            "InnerHalfToneShadowEffect",
            X=40.0,
            Y=30.0,
            Opacity=100.0,
            Blur=0.0,
            BlendMode="Normal",
            Layout="Rhombus",
            Distance=10.0,
            Size=100.0,
            Color="#FFFF0000",
            Strength=100.0,
        ),
    )
    add(
        "three_dimensional",
        effect(
            "ThreeDimensionalEffect",
            X=100.0,
            Y=100.0,
            Length=30.0,
            Opacity=100.0,
            Attenuation=0.0,
            ShadowType="Solid",
            Color1="#FFFF0000",
            Color2="#FF0000FF",
            IsAbsolutePoint=False,
        ),
    )
    add("reflection_bevel", effect("ReflectionAndExtrusionEffect", Blur=0.0, IsInvert=False))
    add("show_only_preview", effect("ShowOnlyPreviewEffect"))
    for noise in ("Perlin", "Voronoi", "Cellular", "Curl", "Random", "Block"):
        brush = copy.deepcopy(brushes["NoiseBrushPlugin"])
        brush["Parameter"]["NoiseType"] = noise
        brush["Parameter"]["Color1"] = "#FF000000"
        brush["Parameter"]["Color2"] = "#FFFFFFFF"
        item = base(800.0, 400.0)
        item["ShapeParameter"]["Brush"] = brush
        probes.append((f"noise_brush_{noise.lower()}", item))
    return [
        {"Name": f"probe2_{name}", "Path": ["probe2", name], "Items": [item]}
        for name, item in probes
    ]


def _noise_parameters() -> dict[str, dict[str, Any]]:
    """配布テンプレートに出てくるノイズの設定を、ノイズの種類ごとに 1 つずつ"""
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(FIXTURES.rglob("*.ymmt")):
        for template in load_template(path):
            for item in template.items:
                for node in _walk(item):
                    parameter = node.get("NoiseParameter")
                    kind = node.get("NoiseType")
                    if isinstance(parameter, dict) and isinstance(kind, str):
                        found.setdefault(kind, copy.deepcopy(parameter))
    return found


def build_third(
    samples: dict[str, dict[str, Any]], brushes: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """3 回目の試験 ノイズのブラシとノイズで歪める設定の意味、立体の回転の向き"""
    probes: list[tuple[str, dict[str, Any]]] = []
    noises = _noise_parameters()

    def base(width: float = 800.0, height: float = 400.0) -> dict[str, Any]:
        item = base_shape(0, 0, 60)
        item["ShapeParameter"]["Width"] = still(width)
        item["ShapeParameter"]["Height"] = still(height)
        item["ShapeParameter"]["Brush"] = solid("#FFFFFFFF")
        return item

    def parameter(kind: str, **values: Any) -> dict[str, Any]:
        source = noises.get(kind) or noises["Perlin"]
        node = copy.deepcopy(source)
        defaults: dict[str, Any] = {
            "Strength": 100.0,
            "Threshold": 0.0,
            "Levels": 256.0,
            "Octaves": 1.0,
            "Lacunarity": 2.0,
            "Gain": 0.5,
            "FractalMode": "Normal",
            "WarpStrength": 0.0,
            "WarpScale": 100.0,
            "X": 0.0,
            "Y": 0.0,
            "Z": 0.0,
            "SpeedX": 0.0,
            "SpeedY": 0.0,
            "SpeedZ": 0.0,
            "Size": 100.0,
            "ScaleX": 100.0,
            "ScaleY": 100.0,
            "ScaleZ": 100.0,
            "Angle": 0.0,
        }
        for key, value in {**defaults, **values}.items():
            if key in node or key in values:
                node[key] = still(float(value)) if isinstance(value, float) else value
        return node

    def noise_brush(
        kind: str,
        *,
        colors: tuple[str, str] = ("#FF000000", "#FFFFFFFF"),
        is_color: bool = False,
        **values: Any,
    ) -> dict[str, Any]:
        item = base()
        brush = copy.deepcopy(brushes["NoiseBrushPlugin"])
        brush["Parameter"]["NoiseType"] = kind
        brush["Parameter"]["NoiseParameter"] = parameter(kind, **values)
        brush["Parameter"]["Color1"], brush["Parameter"]["Color2"] = colors
        brush["Parameter"]["IsColor"] = is_color
        item["ShapeParameter"]["Brush"] = brush
        return item

    def textured() -> dict[str, Any]:
        item = base()
        stripe = copy.deepcopy(brushes["StripeBrushPlugin"])
        set_values(
            stripe["Parameter"],
            Color1="#FFFF4040",
            Width1=40,
            Color2="#FF4040FF",
            Width2=40,
            Offset=0,
            Zoom=100,
            Angle=0,
        )
        item["ShapeParameter"]["Brush"] = stripe
        return item

    def effect(kind: str, **values: Any) -> dict[str, Any]:
        entry = copy.deepcopy(samples[kind])
        entry["IsEnabled"] = True
        return set_values(entry, **values)

    def add(name: str, item: dict[str, Any], *effects: dict[str, Any]) -> None:
        if effects:
            item["VideoEffects"] = list(effects)
        probes.append((name, item))

    add("perlin", noise_brush("Perlin"))
    add("perlin_scale400", noise_brush("Perlin", ScaleX=400.0, ScaleY=400.0))
    add("perlin_scalex400", noise_brush("Perlin", ScaleX=400.0))
    add("perlin_size400", noise_brush("Perlin", Size=400.0))
    add("perlin_octaves5", noise_brush("Perlin", Octaves=5.0))
    add("perlin_levels4", noise_brush("Perlin", Levels=4.0))
    add("perlin_threshold50", noise_brush("Perlin", Threshold=50.0))
    add("perlin_strength50", noise_brush("Perlin", Strength=50.0))
    add("perlin_strength200", noise_brush("Perlin", Strength=200.0))
    add("perlin_turbulence", noise_brush("Perlin", Octaves=5.0, FractalMode="Turbulence"))
    add("perlin_x200", noise_brush("Perlin", X=200.0))
    add("perlin_z1", noise_brush("Perlin", Z=1.0))
    add("perlin_angle45", noise_brush("Perlin", ScaleX=400.0, Angle=45.0))
    add("perlin_speedx100", noise_brush("Perlin", SpeedX=100.0))
    add("perlin_speedz1", noise_brush("Perlin", SpeedZ=1.0))
    add("perlin_red_blue", noise_brush("Perlin", colors=("#FFFF0000", "#FF0000FF")))
    add("perlin_is_color", noise_brush("Perlin", is_color=True))
    for kind in ("Random", "Fractal", "Curl", "Voronoi", "Cellular", "Block"):
        add(kind.lower(), noise_brush(kind))
    add("voronoi_scale400", noise_brush("Voronoi", ScaleX=400.0, ScaleY=400.0))
    add("random_scale400", noise_brush("Random", ScaleX=400.0, ScaleY=400.0))

    def displaced(kind: str, x: float, y: float, **values: Any) -> dict[str, Any]:
        entry = effect("NoiseDisplacementMapEffect", NoiseType=kind)
        entry["NoiseParameter"] = parameter(kind, **values)
        transform = entry.get("Transform")
        if isinstance(transform, dict):
            transform["XScale"] = still(x)
            transform["YScale"] = still(y)
        return entry

    add("move_perlin_x100", textured(), displaced("Perlin", 100.0, 0.0))
    add("move_perlin_y100", textured(), displaced("Perlin", 0.0, 100.0))
    add(
        "move_perlin_x100_scale400",
        textured(),
        displaced("Perlin", 100.0, 0.0, ScaleX=400.0, ScaleY=400.0),
    )
    add("move_random_x100", textured(), displaced("Random", 100.0, 0.0))
    add("move_voronoi_x100", textured(), displaced("Voronoi", 100.0, 0.0))

    for axis, angle in (("X", 45.0), ("Y", 45.0), ("Z", 30.0)):
        values = {"X": 0.0, "Y": 0.0, "Z": 0.0, axis: angle}
        add(f"rotate_{axis.lower()}", base(600.0, 300.0), effect("RotateEffect", **values))
    add("reflection_sample", base(600.0, 300.0), effect("ReflectionAndExtrusionEffect"))
    add("three_dimensional_sample", base(600.0, 300.0), effect("ThreeDimensionalEffect"))
    return [
        {"Name": f"probe3_{name}", "Path": ["probe3", name], "Items": [item]}
        for name, item in probes
    ]


def _transition_parameters() -> dict[str, dict[str, Any]]:
    """配布テンプレートに出てくる切り替えの設定を、切り替えの種類ごとに 1 つずつ"""
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(FIXTURES.rglob("*.ymmt")):
        for template in load_template(path):
            for item in template.items:
                if type_name(item) != "TransitionItem":
                    continue
                kind = str(item.get("TransitionType") or "").partition(",")[0]
                found.setdefault(kind.rpartition(".")[2], copy.deepcopy(item))
    return found


def build_fourth(
    samples: dict[str, dict[str, Any]], brushes: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """4 回目の試験 場面切り替え（TransitionItem）の効き方

    レイヤー 0 に前の場面 A（赤い四角、左）と後の場面 B（青い丸、右）を 90 フレームずつ
    並べ、切り替えをレイヤー 1 の 60〜120 フレームに置く 切り替えの前後と真ん中を撮る
    """
    del brushes
    transitions = _transition_parameters()
    probes: list[tuple[str, list[dict[str, Any]]]] = []

    def scene(
        frame: int, layer: int, colour: str, x: float, *, ellipse: bool = False
    ) -> dict[str, Any]:
        item = base_shape(frame, layer, 90)
        item["ShapeParameter"]["Width"] = still(500.0)
        item["ShapeParameter"]["Height"] = still(300.0)
        item["ShapeParameter"]["Brush"] = solid(colour)
        item["X"] = still(x)
        if ellipse:
            circle = (
                copy.deepcopy(samples["CircleShapeParameter"])
                if "CircleShapeParameter" in samples
                else None
            )
            if circle is not None:
                item["ShapeType2"] = "YukkuriMovieMaker.Shape.CircleShapePlugin, YukkuriMovieMaker"
                circle["Size"] = still(400.0)
                circle["StrokeThickness"] = still(4000.0)
                circle["Brush"] = solid(colour)
                item["ShapeParameter"] = circle
        return item

    def rotate(values: list[float]) -> dict[str, Any]:
        entry = copy.deepcopy(samples["RotateEffect"])
        entry["IsEnabled"] = True
        entry["X"] = still(0.0)
        entry["Y"] = still(0.0)
        entry["Is3D"] = False
        entry["Z"] = {
            "Values": [{"Value": v} for v in values],
            "Span": 0.0,
            "AnimationType": "直線移動",
        }
        return entry

    def transition(
        kind: str,
        *,
        frame: int = 60,
        length: int = 60,
        layer: int = 1,
        before: list[dict[str, Any]] | None = None,
        after: list[dict[str, Any]] | None = None,
        **parameter: Any,
    ) -> dict[str, Any]:
        item = copy.deepcopy(transitions[kind])
        item["Frame"] = frame
        item["Length"] = length
        item["Layer"] = layer
        item["Group"] = 0
        item["IsLocked"] = False
        item["BeforeVideoEffects"] = before or []
        item["AfterVideoEffects"] = after or []
        item["VideoEffects"] = []
        item["TransitionParameter"].update(parameter)
        return item

    def add(name: str, *items: dict[str, Any], a_layer: int = 0) -> None:
        probes.append(
            (
                name,
                [
                    scene(0, a_layer, "#FFFF3030", -300.0),
                    scene(90, a_layer, "#FF3060FF", 300.0, ellipse=True),
                    *items,
                ],
            )
        )

    linear = {"EasingType": "Linear", "EasingMode": "In"}
    add("switch", transition("SwitchTransitionPlugin"))
    add("fade", transition("FadeTransitionPlugin", **linear))
    add("push_0", transition("PushTransitionPlugin", Angle=0.0, **linear))
    add("push_90", transition("PushTransitionPlugin", Angle=90.0, **linear))
    add("slide_before_0", transition("SlideTransitionPlugin", Target="Before", Angle=0.0, **linear))
    add("slide_after_0", transition("SlideTransitionPlugin", Target="After", Angle=0.0, **linear))
    add("none_before", transition("NoneTransitionPlugin", OverlayTarget="Before"))
    add("none_after", transition("NoneTransitionPlugin", OverlayTarget="After"))
    add(
        "switch_rotate",
        transition(
            "SwitchTransitionPlugin", before=[rotate([0.0, 90.0])], after=[rotate([-90.0, 0.0])]
        ),
    )
    add(
        "none_rotate",
        transition(
            "NoneTransitionPlugin",
            OverlayTarget="Before",
            before=[rotate([0.0, 90.0])],
            after=[rotate([-90.0, 0.0])],
        ),
    )
    add("switch_early", transition("SwitchTransitionPlugin", frame=30, length=90))
    add("fade_early", transition("FadeTransitionPlugin", frame=30, length=90, **linear))
    # 切り替えより上のレイヤーに置いた場面は変わるか（緑の小さな四角をずっと出す）
    marker = base_shape(0, 2, 180)
    marker["ShapeParameter"]["Width"] = still(200.0)
    marker["ShapeParameter"]["Height"] = still(200.0)
    marker["ShapeParameter"]["Brush"] = solid("#FF30C030")
    marker["Y"] = still(300.0)
    add("push_with_marker_above", transition("PushTransitionPlugin", Angle=0.0, **linear), marker)
    # 切り替えより上に場面を置いたとき
    add(
        "push_scenes_above",
        transition("PushTransitionPlugin", Angle=0.0, layer=0, **linear),
        a_layer=1,
    )
    return [
        {"Name": f"probe4_{name}", "Path": ["probe4", name], "Items": items}
        for name, items in probes
    ]


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".work" / "probes" / "probes.ymmt"
    samples, brushes = collect_samples()
    which = sys.argv[2] if len(sys.argv) > 2 else "first"
    builders = {
        "first": build,
        "second": build_second,
        "third": build_third,
        "fourth": build_fourth,
    }
    templates = builders[which](samples, brushes)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"ItemTemplates": templates}, ensure_ascii=False), encoding="utf-8"
    )
    print(f"{len(templates)} 本の試験テンプレートを {target} に書いた")
    return 0


if __name__ == "__main__":
    sys.exit(main())
