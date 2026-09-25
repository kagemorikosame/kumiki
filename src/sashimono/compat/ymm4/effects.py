"""YMM4 の映像エフェクトのうち、動きと加工のもの（33 種）をこちらのエフェクトへ写す

手元の配布テンプレート 2 本（123 本）を通して未対応として記録された 33 種を、
回数の多い順に並べてある 移す先は :mod:`sashimono.effects.motion` と
:mod:`sashimono.effects.stylize`

**YMM4 の Y は下が正、こちらは上が正** 位置と角度の Y 成分は符号を入れ替える
角度は Y を反転すると回る向きも反転するので、同じく符号を入れ替える

写し切れない設定（``CrashEffect`` の欠片の回転など）は、黙って捨てずに記録に残す
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.ymm4.brushes import BLEND_NAMES, is_solid
from sashimono.compat.ymm4.values import animated, brush_colour, colour, number
from sashimono.core.model import AnimatedValue, Effect
from sashimono.effects.definition import registry
from sashimono.effects.motion import MESH_MAX_POINTS

__all__ = ["CenterPoint", "center_point", "map_effect", "mapped_names"]

#: イージングの名前 シェーダの番号と同じ並び（:data:`sashimono.effects.motion.EASING_KINDS`）
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
    "Jump": "jump",
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
            report=self.report,
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

    def count(self, key: str, default: int, *, animated: bool = True) -> int:
        """個数を読む NaN や無限大は記録して既定へ戻す

        ``int()`` へそのまま渡すと例外になり、同じアイテムの後ろのエフェクトまで
        読めなくなる
        """
        value = self.still(key, float(default)) if animated else self.plain(key, float(default))
        if not math.isfinite(value):
            self.report.note_missing(f"YMM4 の {self.name} の {key}（数でない値: {value}）")
            return default
        return int(value)

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


#: 歪めるノイズに無い種類と、代わりに使う種類 面ごとに値が変わるものはブロックで近づける
_NOISE_STAND_INS = {
    "Fractal": "perlin",
    "Curl": "perlin",
    "Simplex": "perlin",
}


def _noise_displacement(r: _Reader) -> Effect | None:
    parameter = r.entry.get("NoiseParameter")
    inner = _Reader(
        parameter if isinstance(parameter, dict) else {}, r.length, r.keyframes, r.report, r.name
    )
    # 移動量は新しい形では Transform の XScale と YScale に入る 古い形は X と Y
    transform = r.entry.get("Transform")
    moves = (
        _Reader(transform, r.length, r.keyframes, r.report, r.name)
        if isinstance(transform, dict)
        else None
    )
    kind = str(r.entry.get("NoiseType") or "Perlin")
    if kind in _NOISE_STAND_INS:
        r.report.note_missing(
            f"YMM4 の NoiseDisplacementMapEffect の NoiseType: {kind}（近い種類で代用）"
        )
    return _create(
        "noise_displacement",
        amount_x=moves.track("XScale") if moves else r.track("X"),
        amount_y=moves.track("YScale", flip=True) if moves else r.track("Y", flip=True),
        noise=_NOISE_STAND_INS.get(kind)
        or r.choice(
            "NoiseType",
            {
                "Block": "block",
                "Perlin": "perlin",
                "Random": "random",
                "Voronoi": "voronoi",
                "Cellular": "cellular",
            },
            "perlin",
        ),
        strength=inner.track("Strength", 100.0),
        threshold=inner.track("Threshold"),
        levels=inner.track("Levels", 256.0),
        octaves=inner.count("Octaves", 5),
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
        # YMM4 の角度は残す帯の伸びる向き（画面で時計回り） こちらの ``angle`` は帯の
        # 幅を測る向き（法線 反時計回り）なので 90 度回す 回さないと角度 0 のドッグタグ風
        # テロップが横長の札ではなく縦長の細い板に切られ、45 度の切り欠きは反対の角へ付く
        angle=_shifted_angle(r.track("Angle"), 90.0, negate=True),
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
    return _create(
        "stripe_glitch",
        repeat=r.track("Repeat", 1.0),
        count=r.track("StripeCount", 10.0),
        max_width=r.track("StripeMaxWidth", 10.0),
        max_shift=r.track("StripeMaxShift", 100.0),
        color_shift=r.track("ColorMaxShift"),
        rate=r.track("PlaybackRate", 30.0),
        hard=r.flag("IsHardBorderMode"),
        width_attenuation=r.track("StripeMaxWidthAttenuation"),
        shift_attenuation=r.track("StripeMaxShiftAttenuation"),
    )


def _shifted_angle(value: AnimatedValue, base: float, *, negate: bool = False) -> AnimatedValue:
    """角度の基準と向きをそろえる ``base - value``（``negate``）か ``base + value``"""

    def change(angle: float) -> float:
        return base - angle if negate else base + angle

    return AnimatedValue(
        change(value.static),
        tuple(replace(frame, value=change(frame.value)) for frame in value.keyframes),
    )


def _shadow(r: _Reader) -> Effect | None:
    """影 X / Y は下が正 拡大と回転は絵の中心を支点にする（YMM4 の絵で確かめた）"""
    brush = r.entry.get("Brush")
    if not is_solid(brush):
        r.report.note_missing("YMM4 の影のブラシ（単色以外は先頭の色で塗った）")
    # IsRotateAtCenter は試験の絵で違いが出なかった
    return _create(
        "shadow",
        offset_x=r.track("X"),
        offset_y=r.track("Y", flip=True),
        blur=r.track("Blur"),
        opacity=r.track("Opacity", 100.0),
        color=brush_colour(brush, (0.0, 0.0, 0.0, 1.0)),
        zoom=r.track("Zoom", 100.0),
        angle=r.track("Angle"),
    )


def _inner_shadow(r: _Reader) -> Effect | None:
    """内側の影 X / Y は下が正"""
    brush = r.entry.get("Brush")
    if not is_solid(brush):
        r.report.note_missing("YMM4 の内側の影のブラシ（単色以外は先頭の色で塗った）")
    return _create(
        "inner_shadow",
        offset_x=r.track("X"),
        offset_y=r.track("Y", flip=True),
        blur=r.track("Blur"),
        opacity=r.track("Opacity", 100.0),
        color=brush_colour(brush, (0.0, 0.0, 0.0, 1.0)),
        blend=r.choice("BlendMode", BLEND_NAMES, "normal"),
    )


def _inner_halftone(r: _Reader) -> Effect | None:
    """網点の内側の影 色はブラシでなく ``Color`` に直に入る"""
    return _create(
        "inner_halftone",
        offset_x=r.track("X"),
        offset_y=r.track("Y", flip=True),
        blur=r.track("Blur"),
        opacity=r.track("Opacity", 100.0),
        color=colour(r.entry.get("Color"), (0.0, 0.0, 0.0, 1.0)),
        blend=r.choice("BlendMode", BLEND_NAMES, "normal"),
        grid=r.choice("Layout", {"Rhombus": "rhombus", "Square": "square"}, "rhombus"),
        spacing=r.track("Distance", 10.0),
        dot_size=r.track("Size", 100.0),
        strength=r.track("Strength", 100.0),
    )


def _inner_outline(r: _Reader) -> Effect | None:
    brush = r.entry.get("Brush")
    if not is_solid(brush):
        r.report.note_missing("YMM4 の内側の縁取りのブラシ（単色以外は先頭の色で塗った）")
    # Quality と Smoothness は縁の滑らかさの計算の細かさ 絵はほとんど変わらない
    return _create(
        "inner_outline",
        thickness=r.track("Thickness", 4.0),
        blur=r.track("Blur"),
        opacity=r.track("Opacity", 100.0),
        color=brush_colour(brush, (1.0, 1.0, 1.0, 1.0)),
        blend=r.choice("Blend", BLEND_NAMES, "normal"),
        outline_only=r.flag("IsOutlineOnly"),
        angular=r.flag("IsAngular"),
    )


#: マスクに使える図形 プラグイン名の先頭と、切り抜きの図形
_MASK_SHAPES = {
    "Background": "background",
    "Circle": "ellipse",
    "Quadrilateral": "rect",
    "Rectangle": "rect",
    "Fan": "fan",
    "Triangle": "triangle",
}


def _mask(r: _Reader) -> Effect | None:
    """図形で切り抜く YMM4 の位置は下が正、角度は時計回り（YMM4 の絵で確かめた）"""
    plugin = str(r.entry.get("ShapeType2") or "").partition(",")[0].rpartition(".")[2]
    shape = next((kind for key, kind in _MASK_SHAPES.items() if plugin.startswith(key)), None)
    if shape is None:
        r.report.note_missing(f"YMM4 のマスクの図形: {plugin or '種類不明'}")
        return None
    raw = r.entry.get("ShapeParameter")
    parameter = _Reader(
        raw if isinstance(raw, dict) else {}, r.length, r.keyframes, r.report, r.name
    )
    if str(parameter.entry.get("SizeMode") or "") in ("Size", "SizeAspect"):
        size = parameter.track("Size", 100.0)
        aspect = parameter.plain("AspectRate") / 100.0
        width = _scaled(size, 1.0 - max(0.0, aspect))
        height = _scaled(size, 1.0 + min(0.0, aspect))
    else:
        width = parameter.track("Width", 100.0)
        height = parameter.track("Height", 100.0)
    return _create(
        "shape_mask",
        shape=shape,
        width=width,
        height=height,
        corner=parameter.track("Round"),
        span=parameter.track("CenterAngle", 360.0),
        center_x=r.track("X"),
        center_y=r.track("Y", flip=True),
        rotation=r.track("Angle"),
        blur=r.track("Blur"),
        invert=r.flag("InvertMask"),
    )


def _scaled(value: AnimatedValue, factor: float) -> AnimatedValue:
    return AnimatedValue(
        value.static * factor,
        tuple(replace(frame, value=frame.value * factor) for frame in value.keyframes),
    )


def _copy_and_reverse(r: _Reader) -> Effect | None:
    return _create(
        "copy_reverse",
        position=r.choice(
            "Position",
            {"Right": "right", "Left": "left", "Bottom": "bottom", "Top": "top"},
            "right",
        ),
        distance=r.track("Distance"),
        flip_horizontal=r.flag("IsLeftRightReversed"),
        flip_vertical=r.flag("IsTopBottomReversed"),
        centering=r.flag("IsCentering", True),
    )


def _fill_background(r: _Reader) -> Effect | None:
    brush = r.entry.get("Brush")
    if not is_solid(brush):
        r.report.note_missing("YMM4 の背景の塗りのブラシ（単色以外は先頭の色で塗った）")
    if str(r.entry.get("BlendMode") or "Normal") != "Normal":
        r.report.note_missing(f"YMM4 の背景の塗りの合成モード: {r.entry.get('BlendMode')}")
    return _create(
        "fill_background",
        color=brush_colour(brush, (1.0, 1.0, 1.0, 1.0)),
        opacity=r.track("Opacity", 100.0),
        corner=r.track("Round"),
        margin_top=r.track("Top", 10.0),
        margin_bottom=r.track("Bottom", 10.0),
        margin_left=r.track("Left", 10.0),
        margin_right=r.track("Right", 10.0),
        background_only=r.flag("IsBackgroundOnly"),
    )


def _binarization(r: _Reader) -> Effect | None:
    return _create(
        "binarize",
        threshold=r.track("Threshold", 50.0),
        invert=r.flag("IsInverted"),
        keep_color=r.flag("KeepColor"),
    )


def _chroma_key(r: _Reader) -> Effect | None:
    return _create(
        "color_key",
        key_color=colour(r.entry.get("Color"), (0.0, 0.0, 0.0, 1.0)),
        tolerance=r.track("Tolerance", 10.0),
        feather=r.flag("Feather", True),
        invert=r.flag("IsInvert"),
    )


def _linear_transfer(r: _Reader) -> Effect | None:
    return _create(
        "linear_transfer",
        red_slope=r.track("RedSlope", 100.0),
        red_intercept=r.track("RedYIntercept"),
        green_slope=r.track("GreenSlope", 100.0),
        green_intercept=r.track("GreenYIntercept"),
        blue_slope=r.track("BlueSlope", 100.0),
        blue_intercept=r.track("BlueYIntercept"),
        alpha_slope=r.track("AlphaSlope", 100.0),
        alpha_intercept=r.track("AlphaYIntercept"),
    )


def _border_blur(r: _Reader) -> Effect | None:
    return _create("border_blur", blur=r.track("Blur", 10.0))


def _bloom(r: _Reader) -> Effect | None:
    # 大きさを固定する設定（IsFixedSizeEnabled）は、試験の絵で違いが見えなかった
    return _create(
        "glow",
        threshold=_scaled(r.track("Threshold", 50.0), 0.01),
        intensity=r.track("Strength", 100.0),
        # YMM4 の光は Blur の長さでほぼ消える こちらのぼかしは範囲の半分が標準偏差なので縮める
        radius=_scaled(r.track("Blur", 30.0), 0.67),
        tinted=r.flag("IsColorizationEnabled"),
        tint=colour(r.entry.get("Color"), (1.0, 1.0, 1.0, 1.0)),
    )


def _sharpen(r: _Reader) -> Effect | None:
    return _create("sharpen", strength=_scaled(r.track("Sharpness", 10.0), 10.0))


def _long_shadow(r: _Reader) -> Effect | None:
    return _create(
        "long_shadow",
        # YMM4 は上を 0 とした時計回り こちらは右を 0 とした反時計回り（Y が上）
        angle=_shifted_angle(r.track("Angle"), 90.0, negate=True),
        length_=r.track("Length"),
        opacity=r.track("Opacity", 100.0),
        attenuation=r.track("Attenuation"),
        shadow_type=r.choice(
            "ShadowType", {"Solid": "solid", "Gradient": "gradient", "Image": "image"}, "solid"
        ),
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


def _inout_fade(r: _Reader) -> Effect | None:
    return _create("inout_fade", opacity=r.plain("Value"), **_in_out(r))


def _inout_rotate(r: _Reader) -> Effect | None:
    # YMM4 の X と Y は Sashimono の傾きと向きが逆 X が正だと上の辺が手前へ来る
    return _create(
        "inout_rotate",
        angle_x=-r.plain("ValueX"),
        angle_y=-r.plain("ValueY"),
        angle_z=r.plain("ValueZ"),
        three_d=r.flag("Is3D"),
        **_in_out(r),
    )


def _inout_offset(r: _Reader) -> Effect | None:
    # Value3 は手前へ出す量 YMM4 に 0・300・-300 を描かせると、300 の四角が 428 と 230 の
    # 大きさから始まって元の大きさへ戻った（奥行き 1000 ほどの遠近 #198）
    return _create(
        "inout_offset",
        offset_x=r.plain("Value"),
        offset_y=-r.plain("Value2"),
        offset_z=r.plain("Value3"),
        **_in_out(r),
    )


def _inout_skew(r: _Reader) -> Effect | None:
    r.choice("CenterPoint", {"Center": "center"}, "center")
    return _create(
        "inout_skew", angle_x=r.plain("AngleX"), angle_y=-r.plain("AngleY"), **_in_out(r)
    )


def _inout_blur(r: _Reader) -> Effect | None:
    return _create("inout_blur", radius=r.plain("Value"), **_in_out(r))


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
        angle_x=r.track("X", flip=True),
        angle_y=r.track("Y", flip=True),
        angle_z=r.track("Z"),
        three_d=r.flag("Is3D"),
        interval=r.track("Span", 1.0),
        centering=r.flag("IsCentering", True),
        **r.easing(),
    )


def _circular_duplicator(r: _Reader) -> Effect | None:
    return _create(
        "circular_duplicate",
        count=r.count("Count", 8),
        radius=r.track("Radius", 100.0),
        circumference=r.track("CircumferenceRate", 100.0),
        synced=r.flag("IsSyncedAngle", True),
    )


def _mesh_deformation(r: _Reader) -> Effect | None:
    columns = r.count("HorizontalCount", 2, animated=False)
    rows = r.count("VerticalCount", 2, animated=False)
    points = r.entry.get("Points")
    fits = 2 <= columns <= MESH_MAX_POINTS and 2 <= rows <= MESH_MAX_POINTS
    shaped = (
        [point for point in points if isinstance(point, dict)] if isinstance(points, list) else []
    )
    if not fits or len(points if isinstance(points, list) else []) != columns * rows:
        # 点数と点の数が合わない値で作ると、シェーダが配列の外を読む
        r.report.note_missing(f"YMM4 の MeshDeformationEffect の格子 {columns}x{rows}")
        return None
    if len(shaped) != columns * rows:
        # 点が辞書でない物を空の辞書へ置き換えると、その点だけ動かない格子が
        # 黙って通り、記録にも残らないので直しようが無くなる
        r.report.note_missing(f"YMM4 の MeshDeformationEffect の点の書き方 {columns}x{rows}")
        return None
    readers = [_Reader(point, r.length, r.keyframes, r.report, r.name) for point in shaped]
    if columns == 2 and rows == 2:
        return _mesh_corners(readers)
    # YMM4 の Points は左上から行ごと こちらの格子も同じ並びなので、並べ替えない
    # 並べ替えを挟むと、読むときと描くときで順が食い違って絵が対角に折れる
    offsets: list[float] = []
    for reader in readers:
        # 点ごとのアニメーションは格子では持てない 1 つの平らな値の並びだから
        offsets += [reader.still("X"), -reader.still("Y")]
    return _create("mesh_deform", grid=(float(columns), float(rows), *offsets))


def _mesh_corners(readers: list[_Reader]) -> Effect | None:
    """2x2 は四隅のスライダへ写す こちらは点ごとのアニメーションも運べる

    YMM4 の並びは左上・右上・左下・右下、スライダは一周の順（左上・右上・右下・左下）
    """
    order = (0, 1, 3, 2)
    params: dict[str, Any] = {}
    for corner, index in enumerate(order):
        params[f"point{corner}_x"] = readers[index].track("X")
        params[f"point{corner}_y"] = readers[index].track("Y", flip=True)
    return _create("mesh_deform", **params)


def _inout_getup(r: _Reader) -> Effect | None:
    base = r.choice(
        "Base", {"Bottom": "bottom", "Top": "top", "Left": "left", "Right": "right"}, "bottom"
    )
    return _create("inout_getup", base=base, three_d=r.flag("Is3D", True), **_in_out(r))


def _crash(r: _Reader) -> Effect | None:
    r.unused("X", "Y", "Z")
    return _create(
        "crash",
        spin=r.plain("RandomRotate"),
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
        angle_x=r.track("X", flip=True),
        angle_y=r.track("Y", flip=True),
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
def _nested(r: _Reader, *keys: str) -> _Reader:
    """入れ子の設定を読む係 無ければ空の辞書を読む"""
    node: Any = r.entry
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return _Reader(node if isinstance(node, dict) else {}, r.length, r.keyframes, r.report, r.name)


def _reflection(r: _Reader) -> Effect | None:
    """反射と押し出し 縁からの距離で高さを作り、光を当てて明るさを足す"""
    lighting = r.choice(
        "LightingMode", {"DistantSpecular": "specular", "DistantDiffuse": "diffuse"}, "specular"
    )
    r.choice("HeightmapMode", {"Bevel": "bevel"}, "bevel")
    source = _nested(r, "Lighting", "LightSource")
    highlight = _nested(r, "Lighting", "Highlight")
    heightmap = _nested(r, "Heightmap")
    return _create(
        "bevel_light",
        lighting=lighting,
        azimuth=source.track("Azimuth"),
        elevation=source.track("Elevation"),
        constant=highlight.track("Constant", 50.0),
        exponent=highlight.track("Exponent", 1.0),
        color=colour(highlight.entry.get("Color"), (1.0, 1.0, 1.0, 1.0)),
        blend=highlight.choice("Blend", BLEND_NAMES, "add"),
        surface_scale=_nested(r, "Lighting").track("SurfaceScale", 10.0),
        profile=heightmap.choice(
            "BevelMode",
            {
                "Straight": "straight",
                "Round": "round",
                "InvertedRound": "inverted_round",
                "Step": "step",
            },
            "straight",
        ),
        thickness=heightmap.track("Thickness", 10.0),
        blur=r.track("Blur"),
        inverted=r.flag("IsInvert"),
    )


def _lens_blur(r: _Reader) -> Effect | None:
    # Quality は丸を作る点の数 絵はほとんど変わらない
    return _create(
        "lens_blur",
        radius=r.track("BlurRadius", 10.0),
        brightness=r.track("Brightness", 100.0),
        edge_strength=r.track("EdgeStrength", 2.0),
    )


def _fish_eye(r: _Reader) -> Effect | None:
    return _create(
        "fish_eye",
        projection=r.choice(
            "Projection",
            {
                "Orthographic": "orthographic",
                "Equidistant": "equidistant",
                "Stereographic": "stereographic",
                "Equisolid": "equisolid",
                "EquisolidAngle": "equisolid",
            },
            "orthographic",
        ),
        angle=r.track("Angle", 90.0),
        zoom=r.track("Zoom", 100.0),
    )


def _ripple(r: _Reader) -> Effect | None:
    return _create(
        "ripple",
        center_x=r.track("X"),
        center_y=r.track("Y", flip=True),
        amplitude=r.track("Amplitude", 20.0),
        wavelength=r.track("WaveLength", 100.0),
        period=r.track("Period", 2.0),
    )


def _polar(r: _Reader) -> Effect | None:
    return _create("polar", core=r.track("CoreWidth"), twist=r.track("TwistAngle"))


def _stretch(r: _Reader) -> Effect | None:
    return _create(
        "stretch",
        center_x=r.track("X"),
        center_y=r.track("Y", flip=True),
        angle=r.track("Angle"),
        stretch=r.track("StretchLength", 100.0),
        range=r.track("Range"),
        centering=r.flag("IsCentering", True),
    )


def _reel_spin(r: _Reader) -> Effect | None:
    # 回すときのぶれ（Blur）は動く速さから決まる こちらは速さを持たないので写さない
    r.unused("Blur")
    return _create("reel_spin", rotation=r.track("Rotation"), direction=r.track("Direction"))


def _tiling(r: _Reader) -> Effect | None:
    return _create("tile", count_x=r.track("X", 1.0), count_y=r.track("Y", 1.0))


#: YMM4 に付いている切り替え画像の名前と、同じ形を作る式
_WIPE_IMAGES = {
    "ワイプ横": "horizontal",
    "ワイプ縦": "vertical",
    "円": "circle",
    "四角": "square",
    "時計回り": "clockwise",
}


def _inout_wipe(r: _Reader) -> Effect | None:
    """画像で切り替える登場と退場 付属の 5 枚は式で作る

    ほかの画像は読み込まない（配布物のパスは作者の PC の場所で、開けないことが多い）
    YMM4 も画像が開けないときはフェードになった
    """
    name = str(r.entry.get("File") or "").replace("\\", "/").rpartition("/")[2]
    stem = name.rpartition(".")[0] or name
    pattern = _WIPE_IMAGES.get(stem)
    if pattern is None:
        r.report.note_missing(f"YMM4 の切り替え画像: {stem or '指定なし'}（フェードで代用）")
        pattern = "fade"
    return _create(
        "inout_wipe",
        pattern=pattern,
        tolerance=r.track("Tolerance", 3.0),
        angle=r.track("Angle"),
        reverse_in=r.flag("IsReversedInEffect"),
        reverse_out=r.flag("IsReversedOutEffect"),
        **_in_out(r),
    )


def _directional_key(r: _Reader) -> Effect | None:
    # 色の塊の数や散らばりの見積もりは、2 色の間の位置で抜く形に畳んだ
    return _create(
        "directional_key",
        background=colour(r.entry.get("BackgroundColor"), (0.0, 0.0, 0.0, 1.0)),
        foreground=colour(r.entry.get("ForegroundColor"), (1.0, 1.0, 1.0, 1.0)),
        softness=r.track("EdgeSoftness", 3.0),
        threshold=r.track("NoiseThreshold", 0.02),
        output_foreground=r.flag("OutputForeground", True),
    )


def _repeat_zoom(r: _Reader) -> Effect | None:
    return _create(
        "repeat_zoom",
        zoom=r.track("Zoom", 100.0),
        zoom_x=r.track("ZoomX", 100.0),
        zoom_y=r.track("ZoomY", 100.0),
        interval=r.track("Span", 1.0),
        centering=r.flag("IsCentering"),
        **r.easing(),
    )


def _jump(r: _Reader) -> Effect | None:
    r.unused("X", "Y")
    return _create(
        "jump",
        height=r.track("JumpHeight", 50.0),
        stretch=r.track("Stretch"),
        period=r.track("Period", 0.5),
        distortion=r.track("Distortion"),
        interval=r.track("Interval"),
    )


def _particles(r: _Reader) -> Effect | None:
    # 奥行き（Z・遠近・仰角）と渦の流れは平面の動きに畳んだ
    r.unused("Z", "EmitElevation", "ElevationSpreadAngle", "CurlStrength")
    return _create(
        "particles",
        rate=r.track("Rate", 50.0),
        lifetime=r.track("Lifetime", 2.0),
        preroll=r.track("Preroll"),
        size=r.track("Size", 100.0),
        end_scale=r.track("EndScale", 100.0),
        emitter_x=r.track("X"),
        emitter_y=r.track("Y"),
        emit_range=r.track("EmitRange"),
        emit_angle=r.track("EmitAngle", 90.0),
        spread=r.track("SpreadAngle"),
        speed=r.track("Speed", 100.0),
        gravity=r.track("Gravity"),
        wind_angle=r.track("WindAngle"),
        wind_speed=r.track("WindSpeed"),
        turbulence=r.track("Turbulence"),
        rotation=r.track("Rotation"),
        fade=r.track("Fade"),
        randomness=r.track("Randomness", 50.0),
    )


def _after_image(r: _Reader) -> Effect | None:
    return _create(
        "after_image",
        strength=r.track("Strength", 50.0),
        mode=r.choice("Mode", {"Front": "front", "Back": "back"}, "front"),
    )


def _no_visible_change(r: _Reader) -> Effect | None:
    """YMM4 に描かせても絵が変わらなかったもの 何も足さない

    立体（ThreeDimensional）は配布物の設定でも、値を振った試験でも絵が同じだった
    奥行きのあるカメラの中でだけ効くと見ている
    """
    del r
    return None


def _draw_lazy(r: _Reader) -> Effect | None:
    """位置と拡大をこの場所で当てる印 並べ替えはアイテムを読む所（template）で行う"""
    del r
    return None


_MAPPERS: dict[str, Callable[[_Reader], Effect | None]] = {
    "ReflectionAndExtrusionEffect": _reflection,
    "LensBlurEffect": _lens_blur,
    "FishEyeLensEffect": _fish_eye,
    "RippleEffect": _ripple,
    "PolarTransformEffect": _polar,
    "StretchEffect": _stretch,
    "ReelSpinEffect": _reel_spin,
    "TilingEffect": _tiling,
    "InOutTransitionEffect": _inout_wipe,
    "DirectionalColorKeyEffect": _directional_key,
    "RepeatZoomEffect": _repeat_zoom,
    "JumpEffect": _jump,
    "ParticleOutputEffect": _particles,
    "AfterImageEffect": _after_image,
    "ThreeDimensionalEffect": _no_visible_change,
    "DrawLazyEffectEffect": _draw_lazy,
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
    "InOutFadeEffect": _inout_fade,
    "InOutRotateEffect": _inout_rotate,
    "InOutMoveEffect": _inout_offset,
    "InOutSkewEffect": _inout_skew,
    "InOutGaussianBlurEffect": _inout_blur,
    "InOutJumpEffect": _inout_jump,
    "RepeatOpacityEffect": _repeat_opacity,
    "SkewEffect": _skew,
    "HightlightsAndShadowsEffect": _highlights_shadows,
    "RepeatRotateEffect": _repeat_rotate,
    "ShadowEffect": _shadow,
    "InnerShadowEffect": _inner_shadow,
    "InnerHalfToneShadowEffect": _inner_halftone,
    "InnerOutlineEffect": _inner_outline,
    "MaskEffect": _mask,
    "CopyAndReverseEffect": _copy_and_reverse,
    "FillBackgroundEffect": _fill_background,
    "BinarizationEffect": _binarization,
    "ChromaKeyEffect": _chroma_key,
    "LinearTransferEffect": _linear_transfer,
    "BorderBlurEffect": _border_blur,
    "BloomEffect": _bloom,
    "SharpenEffect": _sharpen,
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
    1 つずつ独立しているので、写す側（:func:`sashimono.compat.ymm4.decorations.map_video_effects`）
    が後ろの変形に支点を配る
    """
    r = _Reader(entry, length, keyframes, report, "CenterPointEffect")
    # 任意（Custom）は絵の原点から X と Y だけずらした点 素材や図形では原点が絵の中央なので
    # 中央からずらした点と同じになる 場面切り替えの場面は原点（画面の中央）と中身の中央が
    # 離れていて、中央から取るとページめくり風その2 の後の場面が 200 画素ずれて回った
    # 原点（Origin）はアイテムの置き場所 絵の置き場の中央（画面の中央の基準）にあたる
    point = CenterPoint(
        horizontal=r.choice(
            "Horizontal",
            {
                "Left": "left",
                "Right": "right",
                "Center": "center",
                "Custom": "origin",
                "Origin": "screen",
            },
            "center",
        ),
        vertical=r.choice(
            "Vertical",
            {
                "Top": "top",
                "Bottom": "bottom",
                "Center": "middle",
                "Custom": "origin",
                "Origin": "screen",
            },
            "middle",
        ),
        x=r.track("X"),
        y=r.track("Y", flip=True),
        keep=r.flag("IsKeepPosition", True),
    )
    if point.keep:
        return point, None
    return point, _create("transform", move_to_pivot=True, **point.params())
