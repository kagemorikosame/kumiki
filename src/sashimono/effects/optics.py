"""光と歪みと繰り返しのエフェクト YMM4 の残りの映像エフェクトを受ける

どれも YMM4 本体に描かせた試験（``tools/ymm4_probes.py`` の 5 回目）の絵から値の意味を
読み取った 読み取れなかった所はコメントに書いてある

座標はほかのエフェクトと同じく画素で Y は上が正 YMM4 の向き（Y 下向き、角度は画面で
時計回り）は写す側（:mod:`sashimono.compat.ymm4.effects`）で直す
"""

from __future__ import annotations

from sashimono.effects.blending import BLEND_FUNCTIONS, BLEND_MODES
from sashimono.effects.builtin import PRELUDE
from sashimono.effects.definition import EffectDefinition, registry
from sashimono.effects.motion import _IN_OUT, _WAVE, _easing, _in_out_specs
from sashimono.effects.spec import CheckSpec, ColorSpec, SelectSpec, TrackSpec, ValueSpec

__all__ = ["register_optics_effects"]

_SRGB = """
vec3 to_srgb(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
}
vec3 to_linear(vec3 c) {
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c));
}
mat2 turn(float degrees) {
    float r = radians(degrees);
    return mat2(cos(r), sin(r), -sin(r), cos(r));
}
"""


def _shader(body: str) -> str:
    return PRELUDE + _SRGB + body


_BEVEL_LIGHT = _shader(
    BLEND_FUNCTIONS
    + """
uniform int lighting;
uniform float azimuth;
uniform float elevation;
uniform float constant;
uniform float exponent;
uniform vec4 color;
uniform int blend;
uniform float surface_scale;
uniform int profile;
uniform float thickness;
uniform float blur;
uniform bool inverted;

float profile_height(float t) {
    t = clamp(t, 0.0, 1.0);
    float h = t;
    if (profile == 1) h = sqrt(max(1.0 - (1.0 - t) * (1.0 - t), 0.0));
    if (profile == 2) h = 1.0 - sqrt(max(1.0 - t * t, 0.0));
    if (profile == 3) h = step(0.5, t);
    return inverted ? -h : h;
}

float height_at(vec2 uv) {
    return profile_height(texture(u_texture, uv).a);
}

float shade(vec3 normal, vec3 light, float k) {
    if (lighting == 0) {
        vec3 halfway = normalize(light + vec3(0.0, 0.0, 1.0));
        return k * pow(max(dot(normal, halfway), 0.0), max(exponent, 0.01));
    }
    return k * max(dot(normal, light), 0.0);
}

// YMM4 は光の色を sRGB の値へ足す（加算） 平らな面にも光が当たって明るくなる
vec3 lit_color(vec3 under, float amount) {
    if (blend == 0) return mix(under, color.rgb, clamp(amount * color.a, 0.0, 1.0));
    return blend_colors(blend, under, clamp(color.rgb * amount * color.a, 0.0, 1.0));
}

// 画質を落とした合成で、書き出しの画素を 1 つずつ思い描いて光を平均するか
// 書き出しの帯は画面の 1 画素ずつ刻んだ段で、いちばん外の段は傾きが半分になる 合成の
// 1 画素には、急な段・半分の段・平らな面が太さの端数に応じて入り交じる 合成の画素で
// 刻み直すと、端数（6 の縁を 1/4 で 1.5 画素など）で段の数と傾きが書き出しと合わず、
// 縮めた書き出しと光り方がずれる（#182） 光は傾きに比例しないので、傾きを平均してから
// 光を当てても合わない 覆う書き出しの画素ごとに光を当てて色で平均する
// ぼかしは段を均すので、書き出しの画素を思い描けない その時は合成の画素で刻む
// 太さの上限 46 は、輪を 1 画素ずつ刻める 48 から帯の外の 2 画素を引いた数
// 書き出しの画素で 1 に満たない太さ（キーフレームの 0 や負の値も）は、書き出しが 1 へ
// 切り上げて別の式で描くので思い描けない 通すと輪の数が 0 になり 0 で割る
// 画質の分母が 8 を超えると、覆う書き出しの画素を 8 × 8 までしか数えられず平均が偏る
bool fine_rings() {
    return u_pixel_scale < 0.999 && u_pixel_scale >= 0.125 && blur <= 0.0
        && thickness <= 46.0 && thickness >= u_pixel_scale;
}

// 縁の距離を刻む輪の幅
// 書き出しの画素を思い描く時は、縁から帯の外の平らな所まで（太さ + 2 画素）の距離が要る
// 1 画素ずつ刻むので、書き出しの輪と同じく合成の画素の升目に揃う
float ring_width() {
    if (fine_rings()) return ceil(thickness) + 2.0;
    return clamp(thickness, 1.0, 128.0);
}

// 書き出しの pass 0 が、縁から distance 画素（画素の中心）の所へ置く高さ
// 輪は太さ（上限 128）を 48 までの数で刻み、縁の外に掛かった最初の輪の半径を太さで割る
float export_height(float distance, float width) {
    if (distance <= 0.0) return profile_height(0.0);
    float rings = ceil(min(width, 48.0));
    float ring = floor(distance * rings / width) + 1.0;
    return profile_height(min(ring / rings, 1.0));
}

// 合成の 1 画素が覆う書き出しの画素に光を当てて、色で平均する
// 縁からの距離と向きは合成の輪から取る 直線の縁ならどの画素も書き出しと同じ段に入る
vec3 fine_lit(vec3 under, vec3 light, float k, float width) {
    vec2 step_ = 1.0 / u_size;
    float here = texture(u_texture, v_uv).a * width - 0.5;
    vec2 slope = vec2(
        texture(u_texture, v_uv + vec2(step_.x, 0.0)).a
            - texture(u_texture, v_uv - vec2(step_.x, 0.0)).a,
        texture(u_texture, v_uv + vec2(0.0, step_.y)).a
            - texture(u_texture, v_uv - vec2(0.0, step_.y)).a
    );
    vec2 inward = length(slope) > 1e-6 ? normalize(slope) : vec2(0.0);
    float scale = 1.0 / u_pixel_scale;
    int count = int(clamp(floor(scale + 0.5), 1.0, 8.0));
    float full = min(thickness * scale, 128.0);
    float strength = surface_scale * scale;
    vec3 sum = vec3(0.0);
    float taken = 0.0;
    for (int y = 0; y < count; ++y) {
        for (int x = 0; x < count; ++x) {
            vec2 offset = (vec2(float(x), float(y)) + 0.5) / float(count) - 0.5;
            float distance = (here + dot(offset, inward)) * scale;
            // 縁の外に掛かる書き出しの画素は、書き出しでも光らない（形の外）
            if (distance <= 0.0) continue;
            float g = (export_height(distance + 1.0, full) - export_height(distance - 1.0, full))
                * 0.5;
            vec3 normal = normalize(vec3(-inward * g * strength, 1.0));
            sum += lit_color(under, shade(normal, light, k));
            taken += 1.0;
        }
    }
    if (taken <= 0.0) return lit_color(under, shade(vec3(0.0, 0.0, 1.0), light, k));
    return sum / taken;
}

void main() {
    if (u_pass == 0) {
        // 縁からの距離を太さで割った値（0 が縁、1 が太さ以上の内側）を作る
        float inside = texture(u_texture, v_uv).a;
        if (inside < 0.5) { frag_color = vec4(1.0, 1.0, 1.0, 0.0); return; }
        float width = ring_width();
        float nearest = width;
        int rings = int(ceil(min(width, 48.0)));
        for (int ring = 1; ring <= rings; ++ring) {
            float r = width * float(ring) / float(rings);
            if (r >= nearest) break;
            for (int k = 0; k < 24; ++k) {
                float a = PI * 2.0 * float(k) / 24.0;
                if (texture(u_texture, v_uv + vec2(cos(a), sin(a)) * r / u_size).a < 0.5) {
                    nearest = min(nearest, r);
                }
            }
        }
        frag_color = vec4(1.0, 1.0, 1.0, nearest / width);
        return;
    }
    if (u_pass == 1 || u_pass == 2) {
        vec2 direction = u_pass == 1 ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
        frag_color = blur1d(u_texture, v_uv, direction, blur);
        return;
    }
    vec4 base = texture(u_source, v_uv);
    if (base.a <= 0.0001) { frag_color = base; return; }
    // 方位は画面で右が 0、時計回り（YMM4 に描かせた絵で、-85 は上から当たった）
    float a = radians(azimuth);
    float e = radians(elevation);
    vec3 light = normalize(vec3(cos(e) * cos(a), -cos(e) * sin(a), sin(e)));
    float k = max(constant, 0.0) * 0.01;
    vec3 under = to_srgb(base.rgb);
    float width = ring_width();
    if (fine_rings()) {
        frag_color = vec4(to_linear(fine_lit(under, light, k, width)), base.a);
        return;
    }
    vec2 step_ = 1.0 / u_size;
    float gx = (height_at(v_uv + vec2(step_.x, 0.0)) - height_at(v_uv - vec2(step_.x, 0.0))) * 0.5;
    float gy = (height_at(v_uv + vec2(0.0, step_.y)) - height_at(v_uv - vec2(0.0, step_.y))) * 0.5;
    // 1 画素に満たない太さ（ぼかした縁を画質を落とした合成で描く時）は、1 画素の帯として
    // 描いてから、帯の画素に占める縁の割合だけ平らな面の光と混ぜる 書き出しを縮めると、
    // 細い縁の急な面と内側の平らな面が 1 画素の中で平均される 1 画素に切り上げたままだと、
    // 面が緩く帯が太い別の光り方になる 傾きは太さで割って、書き出しの急な面に合わせる
    // ぼかしが段を均すので、端数の太さは太さのまま刻んでも縮めた書き出しと大きくは違わない
    float share = clamp(thickness, 0.0001, 1.0);
    vec3 normal = normalize(vec3(-gx * surface_scale, -gy * surface_scale, share));
    vec3 lit = lit_color(under, shade(normal, light, k));
    // 混ぜるのは色にしてから 光の量で混ぜると、強く当てて白く飽和する所が縁の割合より濃く出る
    if (share < 1.0) {
        lit = mix(lit_color(under, shade(vec3(0.0, 0.0, 1.0), light, k)), lit, share);
    }
    frag_color = vec4(to_linear(lit), base.a);
}
"""
)

_LENS_BLUR = _shader(
    """
uniform float radius;
uniform float brightness;
uniform float edge_strength;

void main() {
    // 丸い絞りのぼかし 縁ほど重くして、玉ボケの輪が明るく中が抜けて見えるようにする
    vec4 centre = texture(u_texture, v_uv);
    if (radius < 0.5) { frag_color = centre; return; }
    int rings = int(clamp(ceil(radius / 3.0), 2.0, 10.0));
    vec3 sum = centre.rgb * centre.a;
    float alpha = centre.a;
    float total = 1.0;
    for (int ring = 1; ring <= rings; ++ring) {
        float r = radius * float(ring) / float(rings);
        int count = 6 * ring;
        float weight = 1.0 + max(edge_strength, 0.0) * pow(float(ring) / float(rings), 2.0);
        for (int k = 0; k < count; ++k) {
            float a = PI * 2.0 * (float(k) + 0.5 * float(ring % 2)) / float(count);
            vec4 c = texture(u_texture, v_uv + vec2(cos(a), sin(a)) * r / u_size);
            sum += c.rgb * c.a * weight;
            alpha += c.a * weight;
            total += weight;
        }
    }
    vec3 rgb = sum / total * max(brightness, 0.0) * 0.01;
    float a = alpha / total;
    frag_color = a > 0.0001 ? vec4(rgb / a, a) : vec4(0.0);
}
"""
)

_FISH_EYE = _shader(
    """
uniform int projection;
uniform float angle;
uniform float zoom;

void main() {
    // 絵の対角の半分を 1 として、魚眼の像の高さから元の（普通のレンズの）高さへ戻す
    // 四隅は動かず、辺の中ほどが外へ膨らむ（YMM4 の絵もそうだった）
    vec2 centre = object_center();
    vec2 p = (v_uv * u_size - centre) / max(zoom * 0.01, 0.01);
    float corner = length(object_size()) * 0.5;
    float rho = length(p) / corner;
    if (rho < 1e-5) { frag_color = sample_pixel(centre); return; }
    if (rho > 1.0) { frag_color = vec4(0.0); return; }
    float half_angle = radians(clamp(angle, 1.0, 179.0)) * 0.5;
    float theta;
    if (projection == 1) theta = rho * half_angle;
    else if (projection == 2) theta = 2.0 * atan(rho * tan(half_angle * 0.5));
    else if (projection == 3) theta = 2.0 * asin(clamp(rho * sin(half_angle * 0.5), -1.0, 1.0));
    else theta = asin(clamp(rho * sin(half_angle), -1.0, 1.0));
    float source = tan(min(theta, radians(89.0))) / tan(half_angle) * corner;
    frag_color = sample_pixel(centre + normalize(p) * source);
}
"""
)

_RIPPLE = _shader(
    """
uniform float center_x;
uniform float center_y;
uniform float amplitude;
uniform float wavelength;
uniform float period;

void main() {
    // 中心から広がる波で、半径の向きにずらす 時間で外へ進む
    vec2 centre = object_center() + vec2(center_x, center_y);
    vec2 p = v_uv * u_size - centre;
    float r = length(p);
    if (r < 1e-4) { frag_color = sample_pixel(centre); return; }
    float phase = r / max(wavelength, 1.0) - u_time / max(period, 0.01);
    float shift = amplitude * sin(2.0 * PI * phase);
    // はみ出した所は絵の端の画素を読む YMM4 の絵は波打っても外形が四角のままだった
    vec2 low = object_center() - object_size() * 0.5 + 0.5;
    vec2 high = object_center() + object_size() * 0.5 - 0.5;
    vec2 source = clamp(centre + p / r * (r - shift), low, high);
    vec2 here = v_uv * u_size;
    if (any(lessThan(here, low - 0.5)) || any(greaterThan(here, high + 0.5))) {
        frag_color = vec4(0.0);
        return;
    }
    frag_color = sample_pixel(source);
}
"""
)

_POLAR = _shader(
    """
uniform float core;
uniform float twist;

void main() {
    // 絵を円に巻く 横は真上から反時計回りの角度、縦は中心の穴の縁から外への距離
    // YMM4 に左が赤・右が青の横のグラデーションを巻かせると、真上の継ぎ目の左が赤、右が青に
    // なった（絵の左端が真上で、右へ進むほど反時計回り #198） 時計回りに巻くと左右が裏返る
    // ねじれも同じ向きに裏返す（配布物のねじれはどれも 0 で、向きは測っていない）
    vec2 centre = object_center();
    vec2 size = object_size();
    vec2 p = v_uv * u_size - centre;
    float r = length(p);
    float inner = max(core, 0.0);
    if (r < inner || r > inner + size.y) { frag_color = vec4(0.0); return; }
    float depth = (r - inner) / size.y;
    float a = -(atan(p.x, p.y) + radians(twist) * depth);
    float along = fract(a / (2.0 * PI));
    vec2 corner = centre - size * 0.5;
    frag_color = sample_pixel(corner + vec2(along * size.x, size.y * (1.0 - depth)));
}
"""
)

_STRETCH = _shader(
    """
uniform float center_x;
uniform float center_y;
uniform float angle;
uniform float stretch;
uniform float range;
uniform bool centering;

void main() {
    // 中心を通る線で絵を切り、両側を離す 間は線の近く（幅 range）の画素を引き伸ばして埋める
    // 角度 0 で上下に離れる（YMM4 の絵）
    vec2 normal = turn(-angle) * vec2(0.0, -1.0);
    vec2 centre = object_center() + vec2(center_x, center_y);
    vec2 p = v_uv * u_size - centre;
    float length_ = max(stretch, 0.0);
    // 中央揃えでなければ、片側だけが離れる（真ん中を半分ずらして同じ式で扱う）
    if (!centering) p += normal * length_ * 0.5;
    float s = dot(p, normal);
    float band = max(range, 0.0) * 0.5;
    float reach = band + length_ * 0.5;
    float source_s;
    if (s > reach) source_s = s - length_ * 0.5;
    else if (s < -reach) source_s = s + length_ * 0.5;
    else source_s = reach > 1e-4 ? s / reach * band : 0.0;
    frag_color = sample_pixel(centre + p + normal * (source_s - s));
}
"""
)

_REEL_SPIN = _shader(
    """
uniform float rotation;
uniform float direction;

void main() {
    // 絵の範囲の中で、向きに沿ってずらして折り返す（リールが回るように） 100% で 1 周
    vec2 size = object_size();
    vec2 corner = object_center() - size * 0.5;
    vec2 d = turn(-direction) * vec2(1.0, 0.0);
    vec2 extent = abs(d) * size;
    vec2 p = v_uv * u_size - corner;
    if (p.x < 0.0 || p.y < 0.0 || p.x > size.x || p.y > size.y) { frag_color = vec4(0.0); return; }
    vec2 source = mod(p - d * length(extent) * rotation * 0.01, size);
    frag_color = sample_pixel(corner + source);
}
"""
)

_TILE = _shader(
    """
uniform float count_x;
uniform float count_y;

void main() {
    // 絵を横に count_x 個、縦に count_y 個、隙間なく並べる 真ん中は元の位置
    vec2 counts = max(floor(vec2(count_x, count_y) + 0.5), vec2(1.0));
    vec2 size = object_size();
    vec2 centre = object_center();
    vec2 p = v_uv * u_size - centre;
    vec2 index = floor(p / size + counts * 0.5);
    if (any(lessThan(index, vec2(0.0))) || any(greaterThanEqual(index, counts))) {
        frag_color = vec4(0.0);
        return;
    }
    vec2 offset = (index - (counts - 1.0) * 0.5) * size;
    frag_color = sample_pixel(centre + p - offset);
}
"""
)

_INOUT_WIPE = _shader(
    """
uniform int pattern;
uniform float tolerance;
uniform float angle;
uniform bool reverse_in;
uniform bool reverse_out;
"""
    + _IN_OUT
    + """
// YMM4 に付いている切り替え画像を式にしたもの 絵を覆う正方形に合わせる（縦横比を保つ）
float wipe_value(vec2 p) {
    float half_side = max(object_size().x, object_size().y) * 0.5;
    vec2 u = turn(angle) * (p / half_side);
    u.y = -u.y;
    if (pattern == 0) return 1.0 - (u.x + 1.0) * 0.5;
    if (pattern == 1) return 1.0 - (u.y + 1.0) * 0.5;
    if (pattern == 2) return min(length(u) / sqrt(2.0), 1.0);
    if (pattern == 3) return min((abs(u.x) + abs(u.y)) * 0.5, 1.0);
    return fract(atan(u.x, -u.y) / (2.0 * PI) + 1.0);
}

float reveal(float value, float amount) {
    float soft = max(tolerance * 0.01, 1.0 / 256.0);
    return clamp((amount * (1.0 + soft) - value) / soft, 0.0, 1.0);
}

void main() {
    vec4 color = texture(u_texture, v_uv);
    vec2 p = v_uv * u_size - object_center();
    float span = max(effect_time, 0.0001);
    float keep = 1.0;
    if (effect_in) {
        float amount = ease(u_time / span, easing, easing_mode);
        if (pattern == 5) keep = min(keep, amount);
        else {
            float value = wipe_value(p);
            keep = min(keep, reveal(reverse_in ? 1.0 - value : value, amount));
        }
    }
    if (effect_out) {
        // 退場は登場を時間で裏返す 終わりから数えた残りで登場と同じ曲線を引く
        // （motion の hidden_amount と同じ決まり YMM4 の木製看板テロップで測った #177）
        float amount = ease((u_duration - u_time) / span, easing, easing_mode);
        if (pattern == 5) keep = min(keep, amount);
        else {
            float value = wipe_value(p);
            keep = min(keep, reveal(reverse_out ? 1.0 - value : value, amount));
        }
    }
    frag_color = vec4(color.rgb, color.a * keep);
}
"""
)

_DIRECTIONAL_KEY = _shader(
    """
uniform vec4 background;
uniform vec4 foreground;
uniform float softness;
uniform float threshold;
uniform bool output_foreground;

void main() {
    // 背景の色から前景の色へ向かう線の上で、どこまで前景に寄っているかを不透明度にする
    vec4 base = texture(u_texture, v_uv);
    vec3 c = to_srgb(base.rgb);
    vec3 bg = to_srgb(background.rgb);
    vec3 fg = to_srgb(foreground.rgb);
    vec3 axis = fg - bg;
    float t = dot(c - bg, axis) / max(dot(axis, axis), 1e-5);
    float soft = max(softness * 0.01, 0.01);
    float alpha = smoothstep(threshold, threshold + soft, t);
    vec3 rgb = c;
    if (output_foreground && alpha > 0.001) {
        // 背景の色が混ざった分を取り除く
        rgb = clamp((c - (1.0 - alpha) * bg) / alpha, 0.0, 1.0);
    }
    frag_color = vec4(to_linear(rgb), base.a * alpha);
}
"""
)

_REPEAT_ZOOM = _shader(
    """
uniform float zoom;
uniform float zoom_x;
uniform float zoom_y;
uniform float interval;
uniform bool centering;
uniform int easing;
uniform int easing_mode;
"""
    + _WAVE
    + """
void main() {
    // 等倍と「拡大率 × 縦横の割合」の間を往復する 中央揃えなら等倍を挟んで振れる
    float k = repeat_wave() - (centering ? 0.5 : 0.0);
    vec2 peak = vec2(zoom_x, zoom_y) * 0.01 * zoom * 0.01;
    vec2 scale = max(1.0 + (peak - 1.0) * k, vec2(0.0001));
    vec2 centre = object_center();
    frag_color = sample_pixel(centre + (v_uv * u_size - centre) / scale);
}
"""
)

_JUMP = _shader(
    """
uniform float height;
uniform float stretch;
uniform float period;
uniform float distortion;
uniform float interval;

void main() {
    // 1 回の跳びは period 秒の半周期の正弦 着いたら interval 秒休む（休む間は少し潰れる）
    float cycle = max(period, 0.01) + max(interval, 0.0);
    float phase = mod(u_time, cycle);
    float lift = 0.0;
    vec2 scale = vec2(1.0);
    if (phase < period) {
        lift = height * sin(PI * phase / max(period, 0.01));
        scale = vec2(1.0 - 0.01 * stretch, 1.0 + 0.0067 * stretch);
    } else if (interval > 0.0) {
        float squash = distortion * sin(PI * (phase - period) / interval);
        scale = vec2(1.0 + 0.01 * squash, 1.0 - 0.0067 * squash);
    }
    // 潰れと伸びは足元を支点にする
    vec2 foot = vec2(object_center().x, u_object.y);
    vec2 p = v_uv * u_size - vec2(0.0, lift) - foot;
    frag_color = sample_pixel(foot + p / max(scale, vec2(0.0001)));
}
"""
)

#: 1 画素で調べる粒の数の上限 上限を超えた古い粒は描かない（読み込む所で記録に残す）
#: 重さは居る粒の数に比例し、上限は GPU が止まるほど重い設定を防ぐ栓 1080p の書き出しで
#: 1500 粒（配布物の雪）が 1 枚 35ms、4096 粒で 95ms ほど（#199 で測った）
#: 前の 512 では配布物の雨（800 粒）と雪（1500 粒）の古い粒が黙って消えていた
MAX_PARTICLES = 4096

_PARTICLES = _shader(
    f"const int MAX_PARTICLES = {MAX_PARTICLES};\n"
    + """
uniform float rate;
uniform float lifetime;
uniform float preroll;
uniform float size;
uniform float end_scale;
uniform float emitter_x;
uniform float emitter_y;
uniform float emit_range;
uniform float emit_angle;
uniform float spread;
uniform float speed;
uniform float gravity;
uniform float wind_angle;
uniform float wind_speed;
uniform float turbulence;
uniform float rotation;
uniform float fade;
uniform float randomness;

float rand(float index, float salt) { return hash(vec2(index * 0.1371 + 3.7, salt)); }

void main() {
    // 絵を粒として放つ 放ち口は絵の中心から (emitter_x, emitter_y)（画面の Y 下向き）で、
    // 横に emit_range の幅でばらつく 重力は下、風は角度の向き 絵は粒ごとに中心へ置き直す
    float now = u_time + max(preroll, 0.0);
    float per_second = max(rate, 0.0);
    float life = max(lifetime, 0.01);
    if (per_second <= 0.0) { frag_color = vec4(0.0); return; }
    float newest = floor(now * per_second);
    float count = min(ceil(life * per_second), float(MAX_PARTICLES));
    vec2 centre = object_center();
    vec2 pixel = v_uv * u_size;
    vec2 wind = vec2(cos(radians(wind_angle)), sin(radians(wind_angle))) * wind_speed;
    // 粒の中心から絵の中身の一番遠い角まで（等倍） どう回しても粒の絵はこの円の中に収まる
    // u_object ではなく u_content で測る 前の変形で広げた絵は u_object の外まである
    vec2 corner = max(abs(u_content.xy - centre), abs(u_content.zw - centre));
    float reach = length(corner) + 1.0;
    vec4 result = vec4(0.0);
    for (int k = 0; k < MAX_PARTICLES; ++k) {
        if (float(k) >= count) break;
        float index = newest - float(k);
        // 番号が負の粒はまだ生まれていない（先に進めておく時間が 0 のとき、頭から密集する）
        if (index < 0.0) continue;
        float age = now - index / per_second;
        if (age < 0.0 || age > life) continue;
        float jitter = randomness * 0.01;
        float a = radians(emit_angle + (rand(index, 1.0) - 0.5) * spread);
        vec2 start = vec2(emitter_x + (rand(index, 2.0) * 2.0 - 1.0) * emit_range, emitter_y);
        float v = speed * (1.0 + (rand(index, 3.0) - 0.5) * jitter);
        vec2 moved = start + vec2(cos(a), sin(a)) * v * age + wind * age
                   + vec2(0.0, 0.5 * gravity * age * age)
                   + vec2(sin(age * 3.0 + index), cos(age * 2.3 + index)) * turbulence;
        // 画面の Y 下向きから、このバッファの Y 上向きへ
        vec2 place = centre + vec2(moved.x, -moved.y);
        float t = age / life;
        float scale = mix(size, size * end_scale * 0.01, t) * 0.01;
        if (scale <= 0.0001) continue;
        // 粒の絵が届かない画素は回す前に外す 絵を読む（4 点を混ぜる）所が一番重く、
        // 粒の数だけ全画素で読むと上限を上げたぶんだけ書き出しが重くなる
        vec2 offset = pixel - place;
        if (dot(offset, offset) > reach * reach * scale * scale) continue;
        float fade_from = 1.0 - clamp(fade * 0.01, 0.0, 1.0);
        float alpha = t > fade_from ? 1.0 - (t - fade_from) / max(1.0 - fade_from, 1e-4) : 1.0;
        vec2 local = offset / scale;
        float spin = radians(rotation * (rand(index, 4.0) * 2.0 - 1.0) * jitter + rotation);
        local = mat2(cos(spin), -sin(spin), sin(spin), cos(spin)) * local;
        vec4 c = sample_pixel(centre + local);
        if (c.a <= 0.0) continue;
        result = over(vec4(c.rgb, c.a * alpha), result);
    }
    frag_color = result;
}
"""
)

_FLIP = _shader(
    """
uniform bool horizontal;
uniform bool vertical;

void main() {
    // 絵の中心を軸に裏返す
    vec2 centre = object_center();
    vec2 p = v_uv * u_size - centre;
    if (horizontal) p.x = -p.x;
    if (vertical) p.y = -p.y;
    frag_color = sample_pixel(centre + p);
}
"""
)

_PASS_THROUGH = (
    PRELUDE
    + """
void main() { frag_color = texture(u_texture, v_uv); }
"""
)


def register_optics_effects() -> None:
    """一覧へ登録する 何度呼んでも 1 回だけ"""
    if "bevel_light" in registry:
        return
    definitions = (
        EffectDefinition(
            kind="bevel_light",
            label="縁の反射",
            category="装飾",
            parameters=(
                SelectSpec(
                    "lighting",
                    "光の当て方",
                    (("specular", "照り返し"), ("diffuse", "拡散")),
                    "specular",
                ),
                TrackSpec("azimuth", "光の方位", -360, 360, -90, unit="度"),
                TrackSpec("elevation", "光の高さ", 0, 90, 0, unit="度"),
                TrackSpec("constant", "強さ", 0, 1000, 50, unit="%"),
                TrackSpec("exponent", "鋭さ", 0.01, 128, 1, step=0.01),
                ColorSpec("color", "光の色", (1.0, 1.0, 1.0, 1.0)),
                SelectSpec("blend", "合成", BLEND_MODES, "add"),
                # 見た目は倍率だが、シェーダは 1 画素あたりの高さの差に掛けて面の傾きを出す
                # 縁の太さを縮めた合成では 1 画素あたりの差が大きくなるので、倍率も同じだけ
                # 縮めないと、1/2 画質で面が 2 倍急になって光り方が変わる
                TrackSpec("surface_scale", "高さの倍率", 0, 100, 10, step=0.1, pixels=True),
                SelectSpec(
                    "profile",
                    "縁の形",
                    (
                        ("straight", "直線"),
                        ("round", "丸"),
                        ("inverted_round", "くぼんだ丸"),
                        ("step", "段"),
                    ),
                    "straight",
                ),
                TrackSpec("thickness", "縁の太さ", 1, 128, 10, step=0.1, unit="px"),
                TrackSpec("blur", "ぼかし", 0, 96, 0, unit="px"),
                CheckSpec("inverted", "へこませる", False),
            ),
            fragment_shader=_BEVEL_LIGHT,
            passes=4,
        ),
        EffectDefinition(
            kind="lens_blur",
            label="レンズぼかし",
            category="ぼかし",
            parameters=(
                TrackSpec("radius", "範囲", 0, 200, 10, unit="px"),
                TrackSpec("brightness", "明るさ", 0, 1000, 100, unit="%"),
                TrackSpec("edge_strength", "輪の強さ", 0, 20, 2, step=0.1),
            ),
            fragment_shader=_LENS_BLUR,
        ),
        EffectDefinition(
            kind="fish_eye",
            label="魚眼",
            category="変形",
            parameters=(
                SelectSpec(
                    "projection",
                    "射影",
                    (
                        ("orthographic", "正射影"),
                        ("equidistant", "等距離射影"),
                        ("stereographic", "立体射影"),
                        ("equisolid", "等立体角射影"),
                    ),
                    "orthographic",
                ),
                TrackSpec("angle", "画角", 1, 179, 90, unit="度"),
                TrackSpec("zoom", "拡大率", 1, 1000, 100, unit="%"),
            ),
            fragment_shader=_FISH_EYE,
        ),
        EffectDefinition(
            kind="ripple",
            label="波紋",
            category="変形",
            parameters=(
                TrackSpec("center_x", "中心 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("center_y", "中心 Y", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("amplitude", "振れ幅", -1000, 1000, 20, unit="px"),
                TrackSpec("wavelength", "波長", 1, 4000, 100, unit="px"),
                TrackSpec("period", "周期", 0.01, 60, 2, step=0.01, unit="秒"),
            ),
            fragment_shader=_RIPPLE,
        ),
        EffectDefinition(
            kind="polar",
            label="極座標",
            category="変形",
            parameters=(
                TrackSpec("core", "中心の穴", 0, 4000, 0, step=1, unit="px"),
                TrackSpec("twist", "ねじれ", -3600, 3600, 0, unit="度"),
            ),
            fragment_shader=_POLAR,
        ),
        EffectDefinition(
            kind="stretch",
            label="引き伸ばし",
            category="変形",
            parameters=(
                TrackSpec("center_x", "中心 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("center_y", "中心 Y", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("angle", "角度", -360, 360, 0, unit="度"),
                TrackSpec("stretch", "伸ばす長さ", 0, 10000, 100, step=1, unit="px"),
                TrackSpec("range", "伸ばす幅", 0, 4000, 0, step=1, unit="px"),
                CheckSpec("centering", "両側へ伸ばす", True),
            ),
            fragment_shader=_STRETCH,
        ),
        EffectDefinition(
            kind="reel_spin",
            label="リール回転",
            category="動き",
            parameters=(
                TrackSpec("rotation", "回転", -10000, 10000, 0, unit="%"),
                TrackSpec("direction", "向き", -360, 360, 0, unit="度"),
            ),
            fragment_shader=_REEL_SPIN,
        ),
        EffectDefinition(
            kind="tile",
            label="タイル",
            category="変形",
            parameters=(
                TrackSpec("count_x", "横の数", 1, 100, 2, step=1),
                TrackSpec("count_y", "縦の数", 1, 100, 2, step=1),
            ),
            fragment_shader=_TILE,
        ),
        EffectDefinition(
            kind="inout_wipe",
            label="ワイプで登場",
            category="登場・退場",
            parameters=(
                SelectSpec(
                    "pattern",
                    "形",
                    (
                        ("horizontal", "横"),
                        ("vertical", "縦"),
                        ("circle", "円"),
                        ("square", "四角"),
                        ("clockwise", "時計回り"),
                        ("fade", "フェード"),
                    ),
                    "horizontal",
                ),
                TrackSpec("tolerance", "境目のぼかし", 0, 100, 3, unit="%"),
                TrackSpec("angle", "角度", -360, 360, 0, unit="度"),
                CheckSpec("reverse_in", "登場を逆向きに", False),
                CheckSpec("reverse_out", "退場を逆向きに", False),
                *_in_out_specs(),
            ),
            fragment_shader=_INOUT_WIPE,
        ),
        EffectDefinition(
            kind="directional_key",
            label="2 色の間で抜く",
            category="色",
            keeps_content=True,
            parameters=(
                ColorSpec("background", "抜く色", (0.02, 0.02, 0.02, 1.0)),
                ColorSpec("foreground", "残す色", (0.8, 0.8, 0.8, 1.0)),
                TrackSpec("softness", "境目のぼかし", 0, 100, 3, unit="%"),
                TrackSpec("threshold", "しきい値", 0, 1, 0.02, step=0.001),
                CheckSpec("output_foreground", "混ざった背景の色を取り除く", True),
            ),
            fragment_shader=_DIRECTIONAL_KEY,
        ),
        EffectDefinition(
            kind="repeat_zoom",
            label="反復拡大",
            category="動き",
            parameters=(
                TrackSpec("zoom", "拡大率", 0, 1000, 150, unit="%"),
                TrackSpec("zoom_x", "横の割合", 0, 1000, 100, unit="%"),
                TrackSpec("zoom_y", "縦の割合", 0, 1000, 100, unit="%"),
                TrackSpec("interval", "周期", 0.01, 60, 1, step=0.01, unit="秒"),
                CheckSpec("centering", "等倍を挟んで往復", False),
                *_easing(),
            ),
            fragment_shader=_REPEAT_ZOOM,
        ),
        EffectDefinition(
            kind="jump",
            label="跳ねる",
            category="動き",
            parameters=(
                TrackSpec("height", "高さ", 0, 4000, 50, step=1, unit="px"),
                TrackSpec("stretch", "伸び縮み", 0, 100, 0, step=0.01),
                TrackSpec("period", "1 回の長さ", 0.01, 60, 0.5, step=0.01, unit="秒"),
                TrackSpec("distortion", "着地の潰れ", 0, 100, 0, step=0.01),
                TrackSpec("interval", "休み", 0, 60, 0, step=0.01, unit="秒"),
            ),
            fragment_shader=_JUMP,
        ),
        EffectDefinition(
            kind="particles",
            label="パーティクル",
            category="動き",
            parameters=(
                TrackSpec("rate", "1 秒の数", 0, 2000, 50, step=0.1),
                TrackSpec("lifetime", "寿命", 0.01, 120, 2, step=0.01, unit="秒"),
                TrackSpec("preroll", "先に進めておく時間", 0, 120, 0, step=0.1, unit="秒"),
                TrackSpec("size", "大きさ", 0, 1000, 100, unit="%"),
                TrackSpec("end_scale", "消えるときの大きさ", 0, 1000, 100, unit="%"),
                TrackSpec("emitter_x", "放つ位置 X", -10000, 10000, 0, step=1, unit="px"),
                TrackSpec("emitter_y", "放つ位置 Y（下が正）", -10000, 10000, 0, step=1, unit="px"),
                TrackSpec("emit_range", "放つ幅", 0, 10000, 0, step=1, unit="px"),
                TrackSpec("emit_angle", "放つ角度", -360, 360, 90, unit="度"),
                TrackSpec("spread", "広がり", 0, 360, 0, unit="度"),
                # 速さと重力は秒あたりの画面の画素 単位が PIXEL_UNITS に無いので明に書く
                # 書かないと、1/2 画質で粒が 2 倍遠くまで飛ぶ
                TrackSpec("speed", "速さ", 0, 10000, 100, unit="px/秒", pixels=True),
                TrackSpec("gravity", "重力", -100000, 100000, 0, unit="px/秒²", pixels=True),
                TrackSpec("wind_angle", "風の角度", -360, 360, 0, unit="度"),
                TrackSpec("wind_speed", "風の速さ", 0, 10000, 0, unit="px/秒", pixels=True),
                TrackSpec("turbulence", "揺らぎ", 0, 1000, 0, unit="px"),
                TrackSpec("rotation", "回転", -3600, 3600, 0, unit="度"),
                TrackSpec("fade", "消えていく割合", 0, 100, 0, unit="%"),
                TrackSpec("randomness", "ばらつき", 0, 100, 50, unit="%"),
            ),
            fragment_shader=_PARTICLES,
        ),
        EffectDefinition(
            kind="flip",
            label="反転",
            category="変形",
            parameters=(
                CheckSpec("horizontal", "左右", True),
                CheckSpec("vertical", "上下", False),
            ),
            fragment_shader=_FLIP,
            # どちらも裏返さなければ絵は変わらない クリップが最初から持つ左右反転は
            # この値で付く（:mod:`sashimono.core.commands.fixed`）
            idle_when=(("horizontal", False), ("vertical", False)),
        ),
        EffectDefinition(
            kind="after_image",
            label="残像",
            category="動き",
            parameters=(
                TrackSpec("strength", "残る強さ", 0, 99, 50, unit="%"),
                SelectSpec("mode", "重ね方", (("front", "手前"), ("back", "奥")), "front"),
                ValueSpec("samples", "残す枚数", 12, minimum=1, maximum=60),
            ),
            # 絵は変えない 前のフレームを重ねるのはレンダラ（sashimono.engine.render.renderer）
            fragment_shader=_PASS_THROUGH,
        ),
    )
    for definition in definitions:
        registry.register(definition)
