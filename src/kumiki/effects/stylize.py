"""見た目を加工するエフェクト 形を削る・色を変える・ぼかす・グリッチ

YMM4 の配布テンプレートに出てくるものを、同じ効き方になるように作ったもの
（:mod:`kumiki.compat.ymm4.effects` が写す） 値の意味は配布物に入っていた値の
並びから読み取っている（:mod:`kumiki.effects.motion` の説明と同じ事情）

シェーダはリニア空間・ストレートアルファで受け取り、同じ形で返す
"""

from __future__ import annotations

from kumiki.effects.builtin import PRELUDE
from kumiki.effects.definition import EffectDefinition, registry
from kumiki.effects.spec import CheckSpec, ColorSpec, SelectSpec, TrackSpec

__all__ = ["register_stylize_effects"]


def _shader(body: str) -> str:
    return PRELUDE + body


#: リニアと sRGB の行き来 反転は見た目（sRGB）で行う リニアのまま 1 から引くと、
#: 中間の灰色が反転しても灰色にならず、白っぽく浮く
_SRGB = """
vec3 to_srgb(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
}
vec3 to_linear(vec3 c) {
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c));
}
"""


_MORPHOLOGY = _shader("""
uniform int mode;
uniform float width;
uniform float height;

void main() {
    // 横と縦に分けて調べる 膨張は周りで一番濃い画素、収縮は一番薄い画素を採る
    bool horizontal = u_pass == 0;
    float radius = horizontal ? width : height;
    vec2 direction = horizontal ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
    vec4 chosen = texture(u_texture, v_uv);
    int taps = int(min(radius, 64.0));
    for (int i = -taps; i <= taps; ++i) {
        vec4 c = texture(u_texture, v_uv + direction * float(i) / u_size);
        if (mode == 0 ? c.a > chosen.a : c.a < chosen.a) chosen = c;
    }
    frag_color = chosen;
}
""")

_CROP_ANGLE = _shader("""
uniform float center_x;
uniform float center_y;
uniform float angle;
uniform float width;
uniform float blur;

void main() {
    // 中心を通り angle の向きに伸びる帯（幅 width）だけを残す
    vec2 pixel = v_uv * u_size;
    vec2 normal = vec2(cos(radians(angle)), sin(radians(angle)));
    float distance = abs(dot(pixel - object_center() - vec2(center_x, center_y), normal));
    float edge = max(blur, 0.5);
    float keep = 1.0 - smoothstep(width * 0.5 - edge, width * 0.5 + edge, distance);
    vec4 color = texture(u_texture, v_uv);
    frag_color = vec4(color.rgb, color.a * keep);
}
""")

_ROUND_CORNER = _shader("""
uniform float radius;
uniform float blur;

void main() {
    // 絵が置かれた範囲を角丸の四角で切り抜く
    vec2 half_size = object_size() * 0.5;
    vec2 point = abs(v_uv * u_size - object_center());
    float r = clamp(radius, 0.0, min(half_size.x, half_size.y));
    vec2 q = point - (half_size - vec2(r));
    float distance = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
    float keep = 1.0 - smoothstep(-max(blur, 0.5), max(blur, 0.5), distance);
    vec4 color = texture(u_texture, v_uv);
    frag_color = vec4(color.rgb, color.a * keep);
}
""")

_EDGE_TRIM = _shader("""
uniform float thickness;

void main() {
    // 輪郭を内側へ削る 周りに透明な画素があれば、その分だけ薄くする
    vec4 color = texture(u_texture, v_uv);
    if (thickness < 0.5 || color.a <= 0.0) { frag_color = color; return; }
    float coverage = color.a;
    int steps = int(min(thickness, 32.0));
    for (int y = -steps; y <= steps; ++y) {
        for (int x = -steps; x <= steps; ++x) {
            vec2 offset = vec2(float(x), float(y));
            if (length(offset) > thickness) continue;
            coverage = min(coverage, texture(u_texture, v_uv + offset / u_size).a);
        }
    }
    frag_color = vec4(color.rgb, coverage);
}
""")

_EXPOSURE = _shader("""
uniform float amount;

void main() {
    // 100% で元のまま 0% で真っ暗、200% で光の量が 2 倍
    vec4 color = texture(u_texture, v_uv);
    frag_color = vec4(color.rgb * max(amount, 0.0) / 100.0, color.a);
}
""")

_HALFTONE_BORDER = _shader("""
uniform float blur;
uniform int grid;
uniform float spacing;
uniform float dot_size;
uniform float strength;

void main() {
    // 輪郭のぼけた部分を、網点（ドットの大きさ）で表す
    vec4 color = texture(u_texture, v_uv);
    if (blur < 0.5) { frag_color = color; return; }

    float soft = 0.0;
    float total = 0.0;
    for (int ring = 0; ring <= 4; ++ring) {
        float r = blur * float(ring) / 4.0;
        for (int k = 0; k < 8; ++k) {
            float a = PI * 0.25 * float(k);
            soft += texture(u_texture, v_uv + vec2(cos(a), sin(a)) * r / u_size).a;
            total += 1.0;
        }
    }
    soft /= total;

    vec2 pixel = v_uv * u_size;
    float pitch = max(spacing, 1.0) * 4.0;
    if (grid == 0) {
        // 菱形 格子を 45 度回す
        pixel = mat2(0.7071, 0.7071, -0.7071, 0.7071) * pixel;
    }
    vec2 cell = fract(pixel / pitch) - 0.5;
    float reach = soft * 0.7071 * dot_size / 100.0;
    float dotted = 1.0 - smoothstep(reach - 0.05, reach + 0.05, length(cell));
    float alpha = mix(soft, dotted, clamp(strength / 100.0, 0.0, 1.0) * step(soft, 0.999));
    frag_color = vec4(color.a > 0.0 ? color.rgb : texture(u_source, v_uv).rgb, alpha);
}
""")

_STRIPE_GLITCH = _shader("""
uniform float count;
uniform float max_width;
uniform float max_shift;
uniform float color_shift;
uniform float rate;
uniform bool hard;
uniform float width_attenuation;
uniform float shift_attenuation;

void main() {
    // 横の帯をいくつか選び、左右へずらす 帯と量は rate 回/秒で選び直す
    vec2 pixel = v_uv * u_size;
    float tick = floor(u_time * max(rate, 0.0));
    float height = object_size().y;
    float shift = 0.0;
    int stripes = int(clamp(count, 0.0, 64.0));
    for (int i = 0; i < stripes; ++i) {
        float salt = float(i) * 3.7 + tick * 11.3;
        float fade_width = 1.0 / (1.0 + float(i) * width_attenuation / 100.0);
        float fade_shift = 1.0 / (1.0 + float(i) * shift_attenuation / 100.0);
        float centre = u_object.y + hash(vec2(salt, 1.0)) * height;
        float half_height = hash(vec2(salt, 2.0)) * height * max_width / 100.0 * 0.5 * fade_width;
        float inside = hard
            ? step(abs(pixel.y - centre), half_height)
            : 1.0 - smoothstep(half_height * 0.7, half_height, abs(pixel.y - centre));
        shift += inside * (hash(vec2(salt, 3.0)) * 2.0 - 1.0) * max_shift * fade_shift;
    }
    vec2 source = pixel - vec2(shift, 0.0);
    vec4 red = sample_pixel(source + vec2(color_shift, 0.0));
    vec4 middle = sample_pixel(source);
    vec4 blue = sample_pixel(source - vec2(color_shift, 0.0));
    frag_color = vec4(red.r, middle.g, blue.b, max(max(red.a, middle.a), blue.a));
}
""")

_LONG_SHADOW = _shader("""
uniform float angle;
uniform float length_;
uniform float opacity;
uniform float attenuation;
uniform int shadow_type;
uniform vec4 color1;
uniform vec4 color2;

void main() {
    // 絵を angle の向きへ length_ 画素ぶん引き伸ばした影を、絵の後ろに敷く
    vec4 base = texture(u_texture, v_uv);
    vec2 direction = vec2(cos(radians(angle)), sin(radians(angle)));
    int taps = int(clamp(length_, 0.0, 512.0));
    float coverage = 0.0;
    float where = 0.0;
    vec3 image = vec3(0.0);
    for (int i = 1; i <= taps; ++i) {
        float t = float(i) / max(float(taps), 1.0);
        vec4 found = texture(u_texture, v_uv - direction * float(i) / u_size);
        float a = found.a * (1.0 - clamp(attenuation / 100.0, 0.0, 1.0) * t);
        if (a > coverage) { coverage = a; where = t; image = found.rgb; }
    }
    // 画像: 絵そのものの色で伸ばす（YMM4 の ShadowType が Image のとき）
    vec4 tint = shadow_type == 0 ? color1
        : shadow_type == 2 ? vec4(image, 1.0)
        : mix(color1, color2, where);
    vec4 shadow = vec4(tint.rgb, tint.a * coverage * clamp(opacity / 100.0, 0.0, 1.0));
    frag_color = over(base, shadow);
}
""")

_INNER_SHADOW = _shader(
    _SRGB
    + """
uniform float offset_x;
uniform float offset_y;
uniform float blur;
uniform float opacity;
uniform vec4 color;
uniform int blend;

void main() {
    if (u_pass == 0) {
        // 影は絵の内側で、ずらした絵に隠れない所に落ちる まず横にぼかす
        // ずらす向きは影が落ちる向き（Y は上が正）
        vec2 shift = -vec2(offset_x, offset_y) / u_size;
        float covered = blur1d(u_texture, v_uv + shift, vec2(1.0, 0.0), blur).a;
        frag_color = vec4(1.0, 1.0, 1.0, 1.0 - covered);
        return;
    }
    float shadow = blur1d(u_texture, v_uv, vec2(0.0, 1.0), blur).a;
    vec4 base = texture(u_source, v_uv);
    // 合成は sRGB で行う（YMM4 と同じ） 0 通常 1 乗算 2 加算 3 スクリーン 4 オーバーレイ
    vec3 under = to_srgb(base.rgb);
    vec3 over_ = to_srgb(color.rgb);
    vec3 mixed = over_;
    if (blend == 1) mixed = under * over_;
    if (blend == 2) mixed = min(under + over_, 1.0);
    if (blend == 3) mixed = under + over_ - under * over_;
    vec3 lifted = 1.0 - 2.0 * (1.0 - under) * (1.0 - over_);
    if (blend == 4) mixed = mix(2.0 * under * over_, lifted, step(0.5, under));
    float amount = clamp(shadow * color.a * opacity * 0.01, 0.0, 1.0);
    frag_color = vec4(to_linear(mix(under, mixed, amount)), base.a);
}
"""
)

_SHAPE_MASK = _shader("""
uniform int shape;
uniform float width;
uniform float height;
uniform float corner;
uniform float span;
uniform float center_x;
uniform float center_y;
uniform float rotation;
uniform float blur;
uniform bool invert;

float sd_box(vec2 p, vec2 half_size, float radius) {
    vec2 q = abs(p) - (half_size - radius);
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - radius;
}

// 頂点を上に向けた正三角形 外接円の半径 r
float sd_triangle(vec2 p, float r) {
    const float k = sqrt(3.0);
    p.y = -p.y - r * 0.25;
    float half_side = r * k * 0.5;
    p.x = abs(p.x) - half_side;
    p.y = p.y + half_side / k;
    if (p.x + k * p.y > 0.0) p = vec2(p.x - k * p.y, -k * p.x - p.y) / 2.0;
    p.x -= clamp(p.x, -2.0 * half_side, 0.0);
    return -length(p) * sign(p.y);
}

void main() {
    // 絵の中心からの画素 Y は下が正 図形は時計回りに rotation 度回っている
    vec2 p = v_uv * u_size - object_center();
    p.y = -p.y;
    p -= vec2(center_x, center_y);
    float r = radians(-rotation);
    p = mat2(cos(r), sin(r), -sin(r), cos(r)) * p;
    vec2 half_size = max(vec2(width, height) * 0.5, vec2(0.5));

    float d;
    if (shape == 0) {
        d = -1.0e6;
    } else if (shape == 1) {
        d = (length(p / half_size) - 1.0) * min(half_size.x, half_size.y);
    } else if (shape == 2) {
        d = sd_box(p, half_size, clamp(corner, 0.0, min(half_size.x, half_size.y)));
    } else if (shape == 3) {
        // 扇 上を 0 として反時計回りに span 度ぶん（YMM4 の CenterAngle）
        d = (length(p / half_size) - 1.0) * min(half_size.x, half_size.y);
        float theta = degrees(atan(p.x, -p.y));
        if (theta > 0.0) theta -= 360.0;
        if (theta < -clamp(span, 0.0, 360.0)) d = max(d, 1.0e6);
    } else {
        d = sd_triangle(p, min(half_size.x, half_size.y));
    }

    float edge = max(blur, 0.75);
    // ぼかしは縁の前後に広げる 幅は YMM4 の絵に近づけて 2 倍にした
    float inside = 1.0 - smoothstep(-edge, edge, d);
    if (invert) inside = 1.0 - inside;
    vec4 color = texture(u_texture, v_uv);
    frag_color = vec4(color.rgb, color.a * inside);
}
""")

_HIGHLIGHTS_SHADOWS = _shader("""
uniform float highlights;
uniform float shadows;

void main() {
    // 明るい部分と暗い部分を別々に持ち上げたり沈めたりする
    vec4 color = texture(u_texture, v_uv);
    float luma = dot(max(color.rgb, 0.0), LUMA);
    float bright = smoothstep(0.18, 1.0, luma);
    float dark = 1.0 - smoothstep(0.0, 0.18, luma);
    vec3 rgb = color.rgb * (1.0 + highlights / 100.0 * bright);
    rgb += (shadows / 100.0) * dark * 0.18;
    frag_color = vec4(max(rgb, 0.0), color.a);
}
""")

_COLOR_SHIFT = _shader("""
uniform float shift;
uniform float angle;
uniform float strength;
uniform int order;

void main() {
    // 3 つの色の成分を、angle の向きへ前・そのまま・後ろにずらす
    // order は、どの成分をどの位置に置くか（RGB・RBG・GRB・GBR・BRG・BGR）
    vec2 pixel = v_uv * u_size;
    vec2 offset = vec2(cos(radians(angle)), sin(radians(angle))) * shift * strength / 100.0;
    vec4 ahead = sample_pixel(pixel + offset);
    vec4 still = sample_pixel(pixel);
    vec4 behind = sample_pixel(pixel - offset);
    ivec3 slots[6] = ivec3[6](ivec3(0, 1, 2), ivec3(0, 2, 1), ivec3(1, 0, 2),
                              ivec3(1, 2, 0), ivec3(2, 0, 1), ivec3(2, 1, 0));
    ivec3 slot = slots[clamp(order, 0, 5)];
    vec4 picks[3] = vec4[3](ahead, still, behind);
    vec3 rgb = vec3(0.0);
    float alpha = 0.0;
    for (int channel = 0; channel < 3; ++channel) {
        vec4 chosen = picks[slot[channel]];
        rgb[channel] = chosen.rgb[channel] * chosen.a;
        alpha = max(alpha, chosen.a);
    }
    frag_color = alpha > 0.0001 ? vec4(rgb / alpha, alpha) : vec4(0.0);
}
""")

_RADIAL_BLUR = _shader("""
uniform float amount;
uniform float center_x;
uniform float center_y;
uniform bool hard;

void main() {
    // 中心から外へ向かって伸ばす
    vec2 centre = object_center() + vec2(center_x, center_y);
    vec2 pixel = v_uv * u_size;
    vec4 sum = vec4(0.0);
    float span = clamp(amount / 100.0, 0.0, 1.0) * 0.5;
    for (int i = 0; i < 48; ++i) {
        float scale = 1.0 - span * float(i) / 47.0;
        sum += premul(sample_pixel(centre + (pixel - centre) * scale));
    }
    vec4 result = unpremul(sum / 48.0);
    if (hard) result.a = texture(u_texture, v_uv).a;
    frag_color = result;
}
""")

_CIRCULAR_BLUR = _shader("""
uniform float angle;
uniform float center_x;
uniform float center_y;
uniform bool hard;

void main() {
    // 中心の周りに回す向きへ伸ばす
    vec2 centre = object_center() + vec2(center_x, center_y);
    vec2 point = v_uv * u_size - centre;
    vec4 sum = vec4(0.0);
    for (int i = 0; i < 48; ++i) {
        float a = radians(angle) * (float(i) / 47.0 - 0.5);
        float c = cos(a);
        float s = sin(a);
        sum += premul(sample_pixel(centre + mat2(c, s, -s, c) * point));
    }
    vec4 result = unpremul(sum / 48.0);
    if (hard) result.a = texture(u_texture, v_uv).a;
    frag_color = result;
}
""")

_INVERT = _shader(
    _SRGB
    + """
void main() {
    vec4 color = texture(u_texture, v_uv);
    frag_color = vec4(to_linear(1.0 - to_srgb(color.rgb)), color.a);
}
"""
)

_TINT = _shader("""
uniform vec4 color;

void main() {
    // 明るさを残したまま、色だけを指定の色にする
    vec4 base = texture(u_texture, v_uv);
    float luma = dot(max(base.rgb, 0.0), LUMA);
    float tint_luma = max(dot(color.rgb, LUMA), 0.0001);
    vec3 rgb = color.rgb * (luma / tint_luma);
    frag_color = vec4(mix(base.rgb, rgb, color.a), base.a);
}
""")

_EDGE_DETECT = _shader("""
uniform float strength;
uniform float radius;
uniform int mode;
uniform bool overlay;

float luma_at(vec2 offset) {
    vec4 c = texture(u_texture, v_uv + offset / u_size);
    return dot(max(c.rgb, 0.0), LUMA) * c.a;
}

void main() {
    // 明るさの変わり目を線にする ソーベルとプレウィットは重みだけが違う
    float d = max(radius, 1.0);
    float w = mode == 0 ? 2.0 : 1.0;
    float gx = -luma_at(vec2(-d, d)) - w * luma_at(vec2(-d, 0.0)) - luma_at(vec2(-d, -d))
             + luma_at(vec2(d, d)) + w * luma_at(vec2(d, 0.0)) + luma_at(vec2(d, -d));
    float gy = -luma_at(vec2(-d, -d)) - w * luma_at(vec2(0.0, -d)) - luma_at(vec2(d, -d))
             + luma_at(vec2(-d, d)) + w * luma_at(vec2(0.0, d)) + luma_at(vec2(d, d));
    float edge = clamp(length(vec2(gx, gy)) * strength / 25.0, 0.0, 1.0);
    vec4 base = texture(u_texture, v_uv);
    if (overlay) {
        frag_color = vec4(base.rgb + vec3(edge), base.a);
    } else {
        frag_color = vec4(vec3(edge), base.a);
    }
}
""")


def register_stylize_effects() -> None:
    """加工のエフェクトを一覧へ登録する 何度呼んでも 1 回だけ"""
    if "morphology" in registry:
        return

    definitions = (
        EffectDefinition(
            kind="morphology",
            label="膨張・収縮",
            category="形",
            parameters=(
                SelectSpec("mode", "方法", (("dilate", "膨張"), ("erode", "収縮")), "dilate"),
                TrackSpec("width", "横", 0, 64, 2, step=1, unit="px"),
                TrackSpec("height", "縦", 0, 64, 2, step=1, unit="px"),
            ),
            fragment_shader=_MORPHOLOGY,
            passes=2,
        ),
        EffectDefinition(
            kind="crop_angle",
            label="角度で切り抜き",
            category="形",
            parameters=(
                TrackSpec("center_x", "中心 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("center_y", "中心 Y", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("angle", "角度", -360, 360, 0, unit="度"),
                TrackSpec("width", "幅", 0, 8000, 400, step=1, unit="px"),
                TrackSpec("blur", "ぼかし", 0, 400, 0, unit="px"),
            ),
            fragment_shader=_CROP_ANGLE,
        ),
        EffectDefinition(
            kind="round_corner",
            label="角丸",
            category="形",
            parameters=(
                TrackSpec("radius", "半径", 0, 4000, 20, step=1, unit="px"),
                TrackSpec("blur", "ぼかし", 0, 100, 1, unit="px"),
            ),
            fragment_shader=_ROUND_CORNER,
        ),
        EffectDefinition(
            kind="edge_trim",
            label="輪郭を削る",
            category="形",
            parameters=(TrackSpec("thickness", "太さ", 0, 32, 2, step=1, unit="px"),),
            fragment_shader=_EDGE_TRIM,
        ),
        EffectDefinition(
            kind="exposure",
            label="露出",
            category="色",
            parameters=(TrackSpec("amount", "露出", 0, 1000, 100, unit="%"),),
            fragment_shader=_EXPOSURE,
        ),
        EffectDefinition(
            kind="halftone_border",
            label="網点の輪郭ぼかし",
            category="形",
            parameters=(
                TrackSpec("blur", "ぼかし", 0, 400, 20, unit="px"),
                SelectSpec("grid", "並び", (("rhombus", "菱形"), ("square", "正方形")), "rhombus"),
                TrackSpec("spacing", "間隔", 1, 100, 5, step=1, unit="px"),
                TrackSpec("dot_size", "点の大きさ", 0, 200, 100, unit="%"),
                TrackSpec("strength", "強さ", 0, 100, 100, unit="%"),
            ),
            fragment_shader=_HALFTONE_BORDER,
        ),
        EffectDefinition(
            kind="stripe_glitch",
            label="横ずれグリッチ",
            category="装飾",
            parameters=(
                TrackSpec("count", "本数", 0, 64, 10, step=1),
                TrackSpec("max_width", "帯の最大幅", 0, 100, 10, unit="%"),
                TrackSpec("max_shift", "最大のずれ", 0, 4000, 100, step=1, unit="px"),
                TrackSpec("color_shift", "色ずれ", 0, 400, 5, step=1, unit="px"),
                TrackSpec("rate", "切り替え", 0, 240, 30, step=1, unit="回/秒"),
                CheckSpec("hard", "境目をぼかさない", False),
                TrackSpec("width_attenuation", "幅の減衰", 0, 1000, 10, unit="%"),
                TrackSpec("shift_attenuation", "ずれの減衰", 0, 1000, 50, unit="%"),
            ),
            fragment_shader=_STRIPE_GLITCH,
        ),
        EffectDefinition(
            kind="long_shadow",
            label="長い影",
            category="装飾",
            parameters=(
                TrackSpec("angle", "角度", -360, 360, -45, unit="度"),
                TrackSpec("length_", "長さ", 0, 512, 50, step=1, unit="px"),
                TrackSpec("opacity", "濃さ", 0, 100, 100, unit="%"),
                TrackSpec("attenuation", "薄れ", 0, 100, 0, unit="%"),
                SelectSpec(
                    "shadow_type",
                    "塗り",
                    (("solid", "単色"), ("gradient", "グラデーション"), ("image", "絵の色")),
                    "solid",
                ),
                ColorSpec("color1", "色 1", (0.0, 0.0, 0.0, 1.0)),
                ColorSpec("color2", "色 2", (0.0, 0.0, 0.0, 0.0)),
            ),
            fragment_shader=_LONG_SHADOW,
        ),
        EffectDefinition(
            kind="highlights_shadows",
            label="ハイライトとシャドウ",
            category="色",
            parameters=(
                TrackSpec("highlights", "ハイライト", -100, 100, 0, unit="%"),
                TrackSpec("shadows", "シャドウ", -100, 100, 0, unit="%"),
            ),
            fragment_shader=_HIGHLIGHTS_SHADOWS,
        ),
        EffectDefinition(
            kind="color_shift",
            label="色ずれ",
            category="装飾",
            parameters=(
                TrackSpec("shift", "ずれ", 0, 400, 10, step=1, unit="px"),
                TrackSpec("angle", "角度", -360, 360, 0, unit="度"),
                TrackSpec("strength", "強さ", 0, 100, 100, unit="%"),
                SelectSpec(
                    "order",
                    "並び",
                    tuple((name, name) for name in ("RGB", "RBG", "GRB", "GBR", "BRG", "BGR")),
                    "RGB",
                ),
            ),
            fragment_shader=_COLOR_SHIFT,
        ),
        EffectDefinition(
            kind="radial_blur",
            label="放射ぼかし",
            category="ぼかし",
            parameters=(
                TrackSpec("amount", "強さ", 0, 100, 20, unit="%"),
                TrackSpec("center_x", "中心 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("center_y", "中心 Y", -4000, 4000, 0, step=1, unit="px"),
                CheckSpec("hard", "輪郭を保つ", False),
            ),
            fragment_shader=_RADIAL_BLUR,
        ),
        EffectDefinition(
            kind="circular_blur",
            label="回転ぼかし",
            category="ぼかし",
            parameters=(
                TrackSpec("angle", "角度", 0, 360, 10, unit="度"),
                TrackSpec("center_x", "中心 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("center_y", "中心 Y", -4000, 4000, 0, step=1, unit="px"),
                CheckSpec("hard", "輪郭を保つ", False),
            ),
            fragment_shader=_CIRCULAR_BLUR,
        ),
        EffectDefinition(
            kind="invert",
            label="色の反転",
            category="色",
            fragment_shader=_INVERT,
        ),
        EffectDefinition(
            kind="tint",
            label="色付け",
            category="色",
            parameters=(ColorSpec("color", "色", (1.0, 0.9, 0.7, 1.0)),),
            fragment_shader=_TINT,
        ),
        EffectDefinition(
            kind="inner_shadow",
            label="内側の影",
            category="装飾",
            parameters=(
                TrackSpec("offset_x", "X", -2000, 2000, 6, step=1, unit="px"),
                TrackSpec("offset_y", "Y", -2000, 2000, -6, step=1, unit="px"),
                TrackSpec("blur", "ぼかし", 0, 96, 0, unit="px"),
                TrackSpec("opacity", "濃さ", 0, 100, 100, unit="%"),
                ColorSpec("color", "色", (0.0, 0.0, 0.0, 1.0)),
                SelectSpec(
                    "blend",
                    "合成",
                    (
                        ("normal", "通常"),
                        ("multiply", "乗算"),
                        ("add", "加算"),
                        ("screen", "スクリーン"),
                        ("overlay", "オーバーレイ"),
                    ),
                    "normal",
                ),
            ),
            fragment_shader=_INNER_SHADOW,
            passes=2,
        ),
        EffectDefinition(
            kind="shape_mask",
            label="図形で切り抜く",
            category="形",
            parameters=(
                SelectSpec(
                    "shape",
                    "図形",
                    (
                        ("background", "全体"),
                        ("ellipse", "楕円"),
                        ("rect", "四角"),
                        ("fan", "扇"),
                        ("triangle", "三角"),
                    ),
                    "ellipse",
                ),
                TrackSpec("width", "幅", 0, 20000, 400, step=1, unit="px"),
                TrackSpec("height", "高さ", 0, 20000, 400, step=1, unit="px"),
                TrackSpec("corner", "角の丸み", 0, 10000, 0, step=1, unit="px"),
                TrackSpec("span", "扇の角度", 0, 360, 360, unit="度"),
                TrackSpec("center_x", "X", -20000, 20000, 0, step=1, unit="px"),
                TrackSpec("center_y", "Y（下が正）", -20000, 20000, 0, step=1, unit="px"),
                TrackSpec("rotation", "回転", -3600, 3600, 0, unit="度"),
                TrackSpec("blur", "ぼかし", 0, 1000, 0, unit="px"),
                CheckSpec("invert", "反転", False),
            ),
            fragment_shader=_SHAPE_MASK,
        ),
        EffectDefinition(
            kind="edge_detect",
            label="輪郭抽出",
            category="装飾",
            parameters=(
                TrackSpec("strength", "強さ", 0, 400, 50, unit="%"),
                TrackSpec("radius", "太さ", 1, 16, 1, step=1, unit="px"),
                SelectSpec(
                    "mode", "方法", (("sobel", "ソーベル"), ("prewitt", "プレウィット")), "sobel"
                ),
                CheckSpec("overlay", "元の絵に重ねる", False),
            ),
            fragment_shader=_EDGE_DETECT,
        ),
    )
    for definition in definitions:
        registry.register(definition)
