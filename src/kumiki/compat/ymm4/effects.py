"""YMM4 の映像エフェクトのうち、動きと加工のもの（33 種）をこちらのエフェクトへ写す

手元の配布テンプレート 2 本（123 本）を通して未対応として記録された 33 種を、
回数の多い順に並べてある 移す先は :mod:`kumiki.effects.motion` と
:mod:`kumiki.effects.stylize`

**YMM4 の Y は下が正、こちらは上が正** 位置と角度の Y 成分は符号を入れ替える
角度は Y を反転すると回る向きも反転するので、同じく符号を入れ替える

写し切れない設定（``CrashEffect`` の欠片の回転など）は、黙って捨てずに記録に残す
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.compat.ymm4.values import animated, colour, number
from kumiki.core.model import AnimatedValue, Effect
from kumiki.effects.definition import registry

__all__ = ["CenterPoint", "center_point", "map_effect", "mapped_names"]

#: イージングの名前 シェーダの番号と同じ並び（:data:`kumiki.effects.motion.EASING_KINDS`）
_EASINGS = {
    "Linear": "linear",
    "Sine": "sine",
    "Quad": "quad",
    "Cubic": "cubic",
    "Quart": "quart",
    "Quint": "quint",
    "Expo": "expo",
    "Circ": "circ",
    "Back": "back",
    "Elastic": "elastic",
    "Bounce": "bounce",
}
_EASING_MODES = {"In": "in", "Out": "out", "InOut": "inout"}


class _Reader:
    """1 つのエフェクトの値を読む アイテムの長さと中間点を覚えておく"""

    def __init__(
        self,
        entry: dict[str, Any],
        length: int,
        keyframes: Any,
        report: CompatibilityReport,
        name: str,
    ) -> None:
        self.entry = entry
        self.length = length
        self.keyframes = keyframes
        self.report = report
        self.name = name

    def track(self, key: str, default: float = 0.0, *, flip: bool = False) -> AnimatedValue:
        return animated(
            self.entry.get(key),
            default,
            length=self.length,
            keyframes=self.keyframes,
            scale=-1.0 if flip else 1.0,
        )

    def plain(self, key: str, default: float = 0.0) -> float:
        return number(self.entry.get(key), default)

    def flag(self, key: str, default: bool = False) -> bool:
        value = self.entry.get(key)
        return bool(value) if isinstance(value, bool) else default

    def choice(self, key: str, table: dict[str, str], default: str) -> str:
        """選択肢を読む 知らない名前は既定にして記録する"""
        raw = str(self.entry.get(key) or "")
        if not raw:
            return default
        found = table.get(raw)
        if found is None:
            self.report.note_missing(f"YMM4 の {self.name} の {key}: {raw}")
            return default
        return found

    def easing(self) -> dict[str, str]:
        return {
            "easing": self.choice("EasingType", _EASINGS, "linear"),
            "easing_mode": self.choice("EasingMode", _EASING_MODES, "in"),
        }

    def unused(self, *keys: str, default: float = 0.0) -> None:
        """写せない設定が既定から動いていれば記録する 動いていなければ見た目は変わらない

        既定はキーごとに違いうるので呼ぶ側が渡す 100 を一律に既定と見なすと、
        既定が 0 の設定に 100 が入っていても記録から漏れる
        """
        for key in keys:
            value = self.entry.get(key)
            if value is None:
                continue
            if isinstance(value, dict):
                values = value.get("Values")
                moved = isinstance(values, list) and any(
                    isinstance(item, dict) and number(item.get("Value"), default) != default
                    for item in values
                )
            elif isinstance(value, bool):
                moved = value != bool(default)
            else:
                moved = number(value, default) != default
            if moved:
                self.report.note_missing(f"YMM4 の {self.name} の {key}（写せない設定）")

    def still(self, key: str, default: float = 0.0) -> float:
        """動かせない設定を 1 つの数で読む 動いていれば記録して、先頭の値を使う"""
        value = self.track(key, default)
        if value.is_animated:
            self.report.note_missing(f"YMM4 の {self.name} の {key}（動きは写せない）")
            return value.keyframes[0].value
        return value.static


#: いま写している最中の記録 写し方ごとに記録を引き回さずに済むよう、
#: :func:`map_effect` が呼んでいる間だけ置く
_active_report: CompatibilityReport | None = None


def _create(kind: str, **params: Any) -> Effect | None:
    definition = registry.get(kind)
    if definition is None:
        # 写し方はあるのに移す先が登録されていない 黙って消すと対応済みに見える
        if _active_report is not None:
            _active_report.note_missing(f"YMM4 の映像エフェクトの移す先: {kind}")
        return None
    return definition.create(**params)


def _noise_displacement(r: _Reader) -> Effect | None:
    parameter = r.entry.get("NoiseParameter")
    inner = _Reader(
        parameter if isinstance(parameter, dict) else {}, r.length, r.keyframes, r.report, r.name
    )
    return _create(
        "noise_displacement",
        amount_x=r.track("X"),
        amount_y=r.track("Y", flip=True),
        noise=r.choice(
            "NoiseType", {"Block": "block", "Perlin": "perlin", "Random": "random"}, "perlin"
        ),
        strength=inner.track("Strength", 100.0),
        threshold=inner.track("Threshold"),
        levels=inner.track("Levels", 256.0),
        octaves=int(inner.still("Octaves", 5.0)),
        offset_x=inner.track("X"),
        offset_y=inner.track("Y", flip=True),
        offset_z=inner.track("Z"),
        speed_x=inner.track("SpeedX"),
        speed_y=inner.track("SpeedY", flip=True),
        speed_z=inner.track("SpeedZ"),
        scale_x=inner.track("ScaleX", 100.0),
        scale_y=inner.track("ScaleY", 100.0),
        scale_z=inner.track("ScaleZ", 100.0),
    )


def _random_move(r: _Reader) -> Effect | None:
    return _create(
        "random_move",
        range_x=r.track("X"),
        range_y=r.track("Y", flip=True),
        range_z=r.track("Z"),
        interval=r.track("Span"),
    )


def _morphology(r: _Reader) -> Effect | None:
    return _create(
        "morphology",
        mode=r.choice("Mode", {"Dilate": "dilate", "Erode": "erode"}, "dilate"),
        width=r.track("Width", 1.0),
        height=r.track("Height", 1.0),
    )


def _crop_by_angle(r: _Reader) -> Effect | None:
    return _create(
        "crop_angle",
        center_x=r.track("X"),
        center_y=r.track("Y", flip=True),
        angle=r.track("Angle", flip=True),
        width=r.track("Width", 400.0),
        blur=r.track("Blur"),
    )


def _spiral(r: _Reader) -> Effect | None:
    return _create("spiral", angle=r.track("Angle"), outer=r.flag("IsRotateOuter"))


def _round_corner(r: _Reader) -> Effect | None:
    return _create("round_corner", radius=r.track("Radius"), blur=r.track("Blur", 1.0))


def _edge_trimming(r: _Reader) -> Effect | None:
    return _create("edge_trim", thickness=r.track("Thickness"))


def _exposure(r: _Reader) -> Effect | None:
    return _create("exposure", amount=r.track("Value", 100.0))


def _halftone_border(r: _Reader) -> Effect | None:
    return _create(
        "halftone_border",
        blur=r.track("Blur"),
        grid=r.choice("Layout", {"Rhombus": "rhombus", "Square": "square"}, "rhombus"),
        spacing=r.track("Distance", 5.0),
        dot_size=r.track("Size", 100.0),
        strength=r.track("Strength", 100.0),
    )


def _stripe_glitch(r: _Reader) -> Effect | None:
    r.unused("Repeat")
    return _create(
        "stripe_glitch",
        count=r.track("StripeCount", 10.0),
        max_width=r.track("StripeMaxWidth", 10.0),
        max_shift=r.track("StripeMaxShift", 100.0),
        color_shift=r.track("ColorMaxShift"),
        rate=r.track("PlaybackRate", 30.0),
        hard=r.flag("IsHardBorderMode"),
        width_attenuation=r.track("StripeMaxWidthAttenuation"),
        shift_attenuation=r.track("StripeMaxShiftAttenuation"),
    )


def _long_shadow(r: _Reader) -> Effect | None:
    return _create(
        "long_shadow",
        angle=r.track("Angle", flip=True),
        length_=r.track("Length"),
        opacity=r.track("Opacity", 100.0),
        attenuation=r.track("Attenuation"),
        shadow_type=r.choice("ShadowType", {"Solid": "solid", "Gradient": "gradient"}, "solid"),
        color1=colour(r.entry.get("Color1"), (0.0, 0.0, 0.0, 1.0)),
        color2=colour(r.entry.get("Color2"), (0.0, 0.0, 0.0, 0.0)),
    )


def _random_zoom(r: _Reader) -> Effect | None:
    return _create(
        "random_zoom",
        zoom=r.track("Zoom", 100.0),
        zoom_x=r.track("ZoomX", 100.0),
        zoom_y=r.track("ZoomY", 100.0),
        interval=r.track("Span"),
    )


def _in_out(r: _Reader) -> dict[str, Any]:
    return {
        "effect_in": r.flag("IsInEffect", True),
        "effect_out": r.flag("IsOutEffect"),
        "effect_time": r.plain("EffectTimeSeconds", 0.5),
        **r.easing(),
    }


def _inout_move(r: _Reader) -> Effect | None:
    direction = r.choice(
        "Value", {"Top": "top", "Bottom": "bottom", "Left": "left", "Right": "right"}, "top"
    )
    return _create("inout_move", direction=direction, **_in_out(r))


def _inout_zoom(r: _Reader) -> Effect | None:
    return _create(
        "inout_zoom",
        zoom=r.plain("Value", 100.0),
        zoom_x=r.plain("X", 100.0),
        zoom_y=r.plain("Y", 100.0),
        **_in_out(r),
    )


def _inout_jump(r: _Reader) -> Effect | None:
    return _create(
        "inout_jump",
        effect_in=r.flag("IsInEffect", True),
        reverse_in=r.flag("IsInReverse"),
        effect_out=r.flag("IsOutEffect"),
        reverse_out=r.flag("IsOutReverse"),
        effect_time=r.plain("EffectTimeSeconds", 2.0),
        height=r.plain("JumpHeight", 150.0),
        stretch=r.plain("Stretch"),
        period=r.plain("Period", 0.5),
        distortion=r.plain("Distortion"),
        interval=r.plain("Interval"),
        offset_x=r.plain("X"),
        offset_y=-r.plain("Y"),
    )


def _repeat_opacity(r: _Reader) -> Effect | None:
    return _create(
        "repeat_opacity", opacity=r.track("Opacity"), interval=r.track("Span", 1.0), **r.easing()
    )


def _skew(r: _Reader) -> Effect | None:
    r.choice("CenterPoint", {"Center": "center"}, "center")
    return _create(
        "skew",
        angle_x=r.track("AngleX"),
        angle_y=r.track("AngleY", flip=True),
        center_x=r.track("CenterX"),
        center_y=r.track("CenterY", flip=True),
    )


def _highlights_shadows(r: _Reader) -> Effect | None:
    return _create(
        "highlights_shadows", highlights=r.track("Highlights"), shadows=r.track("Shadows")
    )


def _repeat_rotate(r: _Reader) -> Effect | None:
    return _create(
        "repeat_rotate",
        angle_x=r.track("X"),
        angle_y=r.track("Y"),
        angle_z=r.track("Z"),
        three_d=r.flag("Is3D"),
        interval=r.track("Span", 1.0),
        centering=r.flag("IsCentering", True),
        **r.easing(),
    )


def _circular_duplicator(r: _Reader) -> Effect | None:
    return _create(
        "circular_duplicate",
        count=int(r.still("Count", 8.0)),
        radius=r.track("Radius", 100.0),
        circumference=r.track("CircumferenceRate", 100.0),
        synced=r.flag("IsSyncedAngle", True),
    )


def _mesh_deformation(r: _Reader) -> Effect | None:
    columns = int(r.plain("HorizontalCount", 2.0))
    rows = int(r.plain("VerticalCount", 2.0))
    points = r.entry.get("Points")
    if columns != 2 or rows != 2 or not isinstance(points, list) or len(points) != 4:
        # 四隅より細かい格子は、こちらの四隅の変形では表せない
        r.report.note_missing(f"YMM4 の MeshDeformationEffect の格子 {columns}x{rows}")
        return None
    # YMM4 は格子の点を行ごとに並べる（左上・右上・左下・右下） こちらは一周の順
    order = (0, 1, 3, 2)
    params: dict[str, Any] = {}
    for corner, index in enumerate(order):
        point = points[index] if isinstance(points[index], dict) else {}
        reader = _Reader(point, r.length, r.keyframes, r.report, r.name)
        params[f"point{corner}_x"] = reader.track("X")
        params[f"point{corner}_y"] = reader.track("Y", flip=True)
    return _create("mesh_deform", **params)


def _inout_getup(r: _Reader) -> Effect | None:
    base = r.choice(
        "Base", {"Bottom": "bottom", "Top": "top", "Left": "left", "Right": "right"}, "bottom"
    )
    return _create("inout_getup", base=base, three_d=r.flag("Is3D", True), **_in_out(r))


def _crash(r: _Reader) -> Effect | None:
    r.unused("X", "Y", "Z", "RandomRotate")
    return _create(
        "crash",
        start=r.plain("StartTime"),
        speed=r.plain("PlaybackRate", 100.0),
        size=r.plain("Size", 50.0),
        fly=r.plain("FlySpeed", 100.0),
        fall=r.plain("FallSpeed", 100.0),
        delay=r.plain("Delay", 100.0),
        impact=r.plain("Impact", 100.0),
        spread=r.plain("RandomVector", 100.0),
    )


def _random_rotate(r: _Reader) -> Effect | None:
    return _create(
        "random_rotate",
        angle_x=r.track("X"),
        angle_y=r.track("Y"),
        angle_z=r.track("Z"),
        three_d=r.flag("Is3D"),
        interval=r.track("Span"),
    )


def _repeat_move(r: _Reader) -> Effect | None:
    return _create(
        "repeat_move",
        move_x=r.track("X"),
        move_y=r.track("Y", flip=True),
        move_z=r.track("Z"),
        interval=r.track("Span", 1.0),
        centering=r.flag("IsCentering", True),
        **r.easing(),
    )


def _color_shift(r: _Reader) -> Effect | None:
    orders = {name: name for name in ("RGB", "RBG", "GRB", "GBR", "BRG", "BGR")}
    return _create(
        "color_shift",
        shift=r.track("Shift"),
        angle=r.track("Angle", flip=True),
        strength=r.track("Strength", 100.0),
        order=r.choice("Mode", orders, "RGB"),
    )


def _wave(r: _Reader) -> Effect | None:
    return _create(
        "wave",
        angle=r.track("Angle1", flip=True),
        direction=r.track("Angle2", flip=True),
        amplitude=r.track("Amplitude"),
        wavelength=r.track("WaveLength", 100.0),
        period=r.track("Period", 1.0),
    )


def _radial_blur(r: _Reader) -> Effect | None:
    return _create(
        "radial_blur",
        amount=r.track("Blur"),
        center_x=r.track("X"),
        center_y=r.track("Y", flip=True),
        hard=r.flag("IsHardBorderMode"),
    )


def _circular_blur(r: _Reader) -> Effect | None:
    return _create(
        "circular_blur",
        angle=r.track("Angle"),
        center_x=r.track("X"),
        center_y=r.track("Y", flip=True),
        hard=r.flag("IsHardBorderMode"),
    )


def _invert(r: _Reader) -> Effect | None:
    del r
    return _create("invert")


def _tint(r: _Reader) -> Effect | None:
    return _create("tint", color=colour(r.entry.get("Color"), (1.0, 1.0, 1.0, 1.0)))


def _edge_detection(r: _Reader) -> Effect | None:
    return _create(
        "edge_detect",
        strength=r.track("Strength", 50.0),
        radius=max(1.0, r.still("BlurRadius")),
        mode=r.choice("Mode", {"Sobel": "sobel", "Prewitt": "prewitt"}, "sobel"),
        overlay=r.flag("IsOverlayEdges"),
    )


#: 名前と写し方 回数の多い順（手元の配布物での記録の回数）
_MAPPERS: dict[str, Callable[[_Reader], Effect | None]] = {
    "NoiseDisplacementMapEffect": _noise_displacement,
    "RandomMoveEffect": _random_move,
    "MorphologyEffect": _morphology,
    "CropByAngleEffect": _crop_by_angle,
    "SpiralTransformEffect": _spiral,
    "RoundCornerEffect": _round_corner,
    "EdgeTrimmingEffect": _edge_trimming,
    "ExposureEffect": _exposure,
    "HalfToneBorderBlurEffect": _halftone_border,
    "StripeGlitchNoiseEffect": _stripe_glitch,
    "LongShadowEffect": _long_shadow,
    "RandomZoomEffect": _random_zoom,
    "InOutMoveFromOutsideFrameEffect": _inout_move,
    "InOutZoomEffect": _inout_zoom,
    "InOutJumpEffect": _inout_jump,
    "RepeatOpacityEffect": _repeat_opacity,
    "SkewEffect": _skew,
    "HightlightsAndShadowsEffect": _highlights_shadows,
    "RepeatRotateEffect": _repeat_rotate,
    "CircularDuplicatorEffect": _circular_duplicator,
    "MeshDeformationEffect": _mesh_deformation,
    "InOutGetUpEffect": _inout_getup,
    "CrashEffect": _crash,
    "RandomRotateEffect": _random_rotate,
    "RepeatMoveEffect": _repeat_move,
    "ColorShiftEffect": _color_shift,
    "WaveEffect": _wave,
    "RadialBlurEffect": _radial_blur,
    "CircularBlurEffect": _circular_blur,
    "InvertEffect": _invert,
    "TintEffect": _tint,
    "EdgeDetectionEffect": _edge_detection,
}


def mapped_names() -> frozenset[str]:
    """ここで写せる種類 ``CenterPointEffect`` を含む"""
    return frozenset({*_MAPPERS, "CenterPointEffect"})


def map_effect(
    name: str,
    entry: dict[str, Any],
    report: CompatibilityReport,
    *,
    length: int = 1,
    keyframes: Any = None,
) -> Effect | None:
    """1 つ写す 知らない名前や、写せない形なら ``None``（形の問題は記録に残る）"""
    global _active_report
    mapper = _MAPPERS.get(name)
    if mapper is None:
        return None
    _active_report = report
    try:
        return mapper(_Reader(entry, length, keyframes, report, name))
    finally:
        _active_report = None


class CenterPoint:
    """``CenterPointEffect`` が決めた中心 後ろに続く変形の支点になる"""

    def __init__(
        self, horizontal: str, vertical: str, x: AnimatedValue, y: AnimatedValue, keep: bool
    ) -> None:
        self.horizontal = horizontal
        self.vertical = vertical
        self.x = x
        self.y = y
        self.keep = keep

    def params(self) -> dict[str, Any]:
        return {
            "pivot_h": self.horizontal,
            "pivot_v": self.vertical,
            "anchor_x": self.x,
            "anchor_y": self.y,
        }


def center_point(
    entry: dict[str, Any], report: CompatibilityReport, *, length: int = 1, keyframes: Any = None
) -> tuple[CenterPoint, Effect | None]:
    """中心点を読む 位置を保たないなら、絵をずらす変形も返す

    YMM4 の中心点は、それより後ろの回転や拡大の支点を決める こちらのエフェクトは
    1 つずつ独立しているので、写す側（:func:`kumiki.compat.ymm4.decorations.map_video_effects`）
    が後ろの変形に支点を配る
    """
    r = _Reader(entry, length, keyframes, report, "CenterPointEffect")
    point = CenterPoint(
        horizontal=r.choice(
            "Horizontal", {"Left": "left", "Right": "right", "Center": "center"}, "center"
        ),
        vertical=r.choice(
            "Vertical", {"Top": "top", "Bottom": "bottom", "Center": "middle"}, "middle"
        ),
        x=r.track("X"),
        y=r.track("Y", flip=True),
        keep=r.flag("IsKeepPosition", True),
    )
    if point.keep:
        return point, None
    return point, _create("transform", move_to_pivot=True, **point.params())
