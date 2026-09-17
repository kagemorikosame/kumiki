"""絵の色を模様で塗るエフェクト 単色・線形と円形のグラデーション・ストライプ・水玉・格子

YMM4 の「前景を塗りつぶし」「グラデーション」エフェクトと、図形のブラシを 1 つで受ける
値の意味は YMM4 本体に描かせた試験テンプレート（``tools/ymm4_probes.py``）の絵から
読み取った

- 座標は絵の中心が原点、**Y は下が正**（YMM4 と同じ） 角度は画面上で時計回り
- 線形は角度 0 で左から右 ``size`` がグラデーションの長さ（画素）で、中心で 0.5
- 模様の色の補間と合成モードの計算は sRGB のまま行う YMM4（Direct2D）がそうしている
  リニアで混ぜると、乗算や中間色が暗く沈んで配布物と違う色になる
"""

from __future__ import annotations

from kumiki.effects.blending import BLEND_FUNCTIONS, BLEND_MODES
from kumiki.effects.builtin import PRELUDE
from kumiki.effects.definition import EffectDefinition, registry
from kumiki.effects.spec import CheckSpec, ColorSpec, SelectSpec, TrackSpec, ValueSpec

__all__ = ["BLEND_MODES", "MAX_STOPS", "PATTERNS", "register_paint_effects"]

#: グラデーションの色の数の上限 配布物の多くは 2〜5 色 金属風の文字で 10 を超えるものが
#: あるが、上限を超えた分は間引いて写す（:mod:`kumiki.compat.ymm4.brushes`）
MAX_STOPS = 8

#: 模様の種類 シェーダの番号と同じ並び
PATTERNS = (
    ("solid", "単色"),
    ("linear", "線形グラデーション"),
    ("radial", "円形グラデーション"),
    ("convex", "山形グラデーション"),
    ("stripe", "ストライプ"),
    ("dot", "水玉"),
    ("grid", "格子"),
)


_EXTEND = (("clamp", "端の色"), ("wrap", "繰り返し"), ("mirror", "折り返し"))

_STOP_UNIFORMS = "\n".join(
    f"uniform vec4 color{index};\nuniform float offset{index};" for index in range(MAX_STOPS)
)

_PAINT = (
    PRELUDE
    + _STOP_UNIFORMS
    + """
uniform int pattern;
uniform int extend;
uniform int stops;
uniform int blend;
uniform float opacity;
uniform float center_x;
uniform float center_y;
uniform float angle;
uniform float size;
uniform float offset;
uniform float radius_x;
uniform float radius_y;
uniform float width_a;
uniform float width_b;
uniform float dot_radius;
uniform float span;
uniform float aspect;
uniform float thickness;
uniform float cell_width;
uniform float cell_height;
uniform float zoom;
uniform bool inverted;
uniform bool pattern_only;
uniform bool relative;

vec3 to_srgb(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
}
vec3 to_linear(vec3 c) {
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c));
}

vec4 stop_color(int i) {
    if (i == 0) return color0; if (i == 1) return color1; if (i == 2) return color2;
    if (i == 3) return color3; if (i == 4) return color4; if (i == 5) return color5;
    if (i == 6) return color6; return color7;
}
float stop_offset(int i) {
    if (i == 0) return offset0; if (i == 1) return offset1; if (i == 2) return offset2;
    if (i == 3) return offset3; if (i == 4) return offset4; if (i == 5) return offset5;
    if (i == 6) return offset6; return offset7;
}

// 色は sRGB で補間する（YMM4 と同じ） uniform の色はリニアで届くので先に戻す
vec4 ramp(float t) {
    int count = clamp(stops, 1, 8);
    vec4 first = stop_color(0);
    if (count == 1 || t <= stop_offset(0)) return vec4(to_srgb(first.rgb), first.a);
    for (int i = 1; i < count; ++i) {
        float right = stop_offset(i);
        if (t <= right) {
            float left = stop_offset(i - 1);
            float k = right > left ? (t - left) / (right - left) : 1.0;
            vec4 a = stop_color(i - 1);
            vec4 b = stop_color(i);
            return mix(vec4(to_srgb(a.rgb), a.a), vec4(to_srgb(b.rgb), b.a), clamp(k, 0.0, 1.0));
        }
    }
    vec4 last = stop_color(count - 1);
    return vec4(to_srgb(last.rgb), last.a);
}

float extended(float t) {
    if (extend == 1) return fract(t);
    if (extend == 2) return 1.0 - abs(fract(t * 0.5) * 2.0 - 1.0);
    return clamp(t, 0.0, 1.0);
}

vec2 rotated(vec2 p, float degrees) {
    float r = radians(degrees);
    return mat2(cos(r), sin(r), -sin(r), cos(r)) * p;
}

// 模様の色（sRGB、ストレートアルファ） p は絵の中心からの画素で Y は下が正
vec4 pattern_color(vec2 p) {
    float z = max(zoom, 0.0001) * 0.01;
    vec2 q = p - vec2(center_x, center_y);
    if (pattern == 0) return ramp(0.0);
    if (pattern == 1 || pattern == 3) {
        vec2 direction = vec2(cos(radians(angle)), sin(radians(angle)));
        // 割合で指定したときは絵の幅が 100（YMM4 の CoordinateMode が Relative）
        float unit = relative ? object_size().x * 0.01 : 1.0;
        float along = dot(q, direction) - offset * unit;
        float length_ = max(size * unit, 1.0);
        // 山形は中心で終わりの色、長さの半分で始まりの色に戻る
        if (pattern == 3) return ramp(extended(1.0 - abs(along) / (length_ * 0.5)));
        return ramp(extended(along / length_ + 0.5));
    }
    if (pattern == 2) {
        vec2 r = rotated(q, -angle);
        vec2 radius = max(vec2(radius_x, radius_y) * z, vec2(1.0));
        radius.x *= 1.0 - max(aspect, 0.0) * 0.01;
        radius.y *= 1.0 + min(aspect, 0.0) * 0.01;
        return ramp(extended(length(r / radius)));
    }
    if (pattern == 4) {
        vec2 direction = vec2(cos(radians(angle)), sin(radians(angle)));
        float a = max(width_a * z, 0.0);
        float b = max(width_b * z, 0.0);
        float period = max(a + b, 1.0);
        // 1 本目の帯は絵の中心にまたがる（YMM4 の絵で中心の画素が帯の真ん中だった）
        float m = mod(dot(p, direction) - offset * z + a * 0.5, period);
        return m < a ? ramp(0.0) : ramp(1.0);
    }
    vec2 r = rotated(q, -angle);
    if (pattern == 5) {
        // 縦横比は模様ごと縮める 正なら横、負なら縦（間隔も水玉の形も同じ割合）
        vec2 squash = vec2(1.0 - max(aspect, 0.0) * 0.01, 1.0 + min(aspect, 0.0) * 0.01);
        squash = max(squash, vec2(0.01));
        vec2 cell = vec2(max(span * z, 1.0)) * squash;
        // 水玉は中心から半マスずれた所に並ぶ 反転の設定は YMM4 の絵でも変わらなかった
        vec2 local = mod(r, cell) - cell * 0.5;
        bool inside = length(local / squash) <= dot_radius * z;
        return inside ? ramp(0.0) : ramp(1.0);
    }
    vec2 cell = max(vec2(cell_width, cell_height) * z, vec2(1.0));
    // 格子の線は中心を通り、線の太さの真ん中が中心に来る
    vec2 local = mod(r + thickness * z * 0.5, cell);
    bool line = min(local.x, local.y) < thickness * z;
    if (inverted) line = !line;
    return line ? ramp(0.0) : ramp(1.0);
}

"""
    + BLEND_FUNCTIONS
    + """
vec3 blended(vec3 b, vec3 s) { return blend_colors(blend, b, s); }

void main() {
    vec4 base = texture(u_texture, v_uv);
    vec2 pixel = v_uv * u_size - object_center();
    pixel.y = -pixel.y;
    vec4 paint = pattern_color(pixel);
    float amount = clamp(opacity * 0.01, 0.0, 1.0) * paint.a;
    if (pattern_only) {
        // 模様だけで塗る 元の絵の色は使わず、形（不透明度）だけを借りる
        frag_color = vec4(to_linear(paint.rgb), base.a * amount);
        return;
    }
    vec3 under = to_srgb(base.rgb);
    vec3 rgb = mix(under, blended(under, paint.rgb), amount);
    frag_color = vec4(to_linear(rgb), base.a);
}
"""
)


def _stop_parameters() -> tuple[ColorSpec | TrackSpec, ...]:
    specs: list[ColorSpec | TrackSpec] = []
    defaults = ((1.0, 1.0, 1.0, 1.0), (0.0, 0.0, 0.0, 1.0))
    for index in range(MAX_STOPS):
        colour = defaults[min(index, 1)]
        specs.append(ColorSpec(f"color{index}", f"色 {index + 1}", colour))
        specs.append(
            TrackSpec(f"offset{index}", f"位置 {index + 1}", 0, 1, min(index, 1), step=0.001)
        )
    return tuple(specs)


def register_paint_effects() -> None:
    registry.register(
        EffectDefinition(
            kind="brush_fill",
            label="模様で塗る",
            category="色",
            parameters=(
                SelectSpec("pattern", "模様", PATTERNS, "linear"),
                SelectSpec("blend", "合成", BLEND_MODES, "normal"),
                TrackSpec("opacity", "濃さ", 0, 100, 100, unit="%"),
                CheckSpec("pattern_only", "模様だけで塗る", False),
                ValueSpec("stops", "色の数", 2, minimum=1, maximum=MAX_STOPS),
                *_stop_parameters(),
                SelectSpec("extend", "端の扱い", _EXTEND, "clamp"),
                TrackSpec("center_x", "中心 X", -8000, 8000, 0, step=1, unit="px"),
                TrackSpec("center_y", "中心 Y（下が正）", -8000, 8000, 0, step=1, unit="px"),
                TrackSpec("angle", "角度", -3600, 3600, 0, unit="度"),
                TrackSpec("size", "長さ", 1, 20000, 400, step=1, unit="px"),
                TrackSpec("offset", "ずらし", -20000, 20000, 0, step=1, unit="px"),
                TrackSpec("radius_x", "半径 X", 1, 20000, 300, step=1, unit="px"),
                TrackSpec("radius_y", "半径 Y", 1, 20000, 300, step=1, unit="px"),
                TrackSpec("width_a", "帯の幅 1", 0, 4000, 40, step=1, unit="px"),
                TrackSpec("width_b", "帯の幅 2", 0, 4000, 40, step=1, unit="px"),
                TrackSpec("dot_radius", "水玉の半径", 0, 2000, 10, step=1, unit="px"),
                TrackSpec("span", "水玉の間隔", 1, 4000, 40, step=1, unit="px"),
                TrackSpec("aspect", "縦横比", -100, 100, 0, unit="%"),
                TrackSpec("thickness", "線の太さ", 0, 2000, 2, step=1, unit="px"),
                TrackSpec("cell_width", "格子の幅", 1, 4000, 40, step=1, unit="px"),
                TrackSpec("cell_height", "格子の高さ", 1, 4000, 40, step=1, unit="px"),
                TrackSpec("zoom", "拡大率", 1, 10000, 100, unit="%"),
                CheckSpec("inverted", "反転", False),
                CheckSpec("relative", "長さを絵の幅の割合で決める", False),
            ),
            fragment_shader=_PAINT,
        )
    )
