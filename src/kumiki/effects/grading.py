"""色を作り替えるエフェクト AviUtl の色まわりの効果に当たるもの

値の意味は **AviUtl2 に描かせた絵を測って**決めた（推測していない）
測り方は ``tools/aviutl_compare.py`` の見本を作り、黒から白の階調や小さな円に
効果を 1 つだけ掛けて、入りと出の対応を読む

シェーダはリニア空間・ストレートアルファで受け取り、同じ形で返す
色の曲げ方は **符号化した値（sRGB）で計算する** AviUtl も YMM4 も画面に出る値を
そのまま動かしており、リニアのまま掛けると中間の明るさが別物になる
"""

from __future__ import annotations

from kumiki.effects.builtin import PRELUDE
from kumiki.effects.definition import EffectDefinition, registry
from kumiki.effects.spec import CheckSpec, ColorSpec, TrackSpec

__all__ = ["register_grading_effects"]


def _shader(body: str) -> str:
    return PRELUDE + body


_SRGB = """
vec3 to_srgb(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
}
vec3 to_linear(vec3 c) {
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c));
}
"""

#: 明るさの重み **Rec.601** AviUtl の色まわりはこの重みで明るさを測っている
#: （グラデーションマップに赤を通すと 30% の位置の色が出る Rec.709 なら 21%）
_LUMA601 = """
const vec3 LUMA601 = vec3(0.299, 0.587, 0.114);
"""

#: ゲイン・ガンマ・リフト・オフセットの 4 つを 1 つの値へ掛ける
#:
#: AviUtl2 に 1 つずつ動かした見本を描かせて測った（値は 50 と -50）
#:
#: * ゲイン   ``×(1 + v/100)``      50 で 1.5 倍、-50 で 0.5 倍
#: * オフセット ``+ v/100``          50 で +127/255、-50 で -127/255
#: * リフト   黒を ``v/100`` まで持ち上げ、白はそのまま（間は引き伸ばす）
#: * ガンマ   ``^ 2^(-v/100)``      50 で 0.72 乗、-50 で 1.40 乗
#:
#: 順番は「ゲイン → オフセット → リフト → ガンマ」 1 つずつしか測っていないので、
#: 2 つ以上を同時に使ったときの順番までは実物で確かめていない
_CURVE = """
float apply_curve(float value, float gain, float gamma, float lift, float offset) {
    float out_ = value * (1.0 + gain * 0.01);
    out_ += offset * 0.01;
    float low = lift * 0.01;
    out_ = out_ * (1.0 - low) + low;
    out_ = pow(max(out_, 0.0), pow(2.0, -gamma * 0.01));
    return out_;
}
"""

#: 色相と彩度で色の近さを測るための行き来（特定色域変換だけが使う）
#: 拡張色調補正は YUV で計算する AviUtl がそちらだから
_HSV = """
vec3 to_hsv(vec3 c) {
    float high = max(c.r, max(c.g, c.b));
    float low = min(c.r, min(c.g, c.b));
    float span = high - low;
    float hue = 0.0;
    if (span > 1e-6) {
        if (high == c.r) {
            hue = mod((c.g - c.b) / span, 6.0);
        } else if (high == c.g) {
            hue = (c.b - c.r) / span + 2.0;
        } else {
            hue = (c.r - c.g) / span + 4.0;
        }
        hue /= 6.0;
    }
    return vec3(hue, high <= 1e-6 ? 0.0 : span / high, high);
}
vec3 from_hsv(vec3 c) {
    vec3 k = mod(c.x * 6.0 + vec3(5.0, 3.0, 1.0), 6.0);
    return c.z - c.z * c.y * max(min(min(k, 4.0 - k), 1.0), 0.0);
}
"""


_COLOR_GRADE = _shader(
    _SRGB
    + _LUMA601
    + _CURVE
    + """
uniform float luma_gain;
uniform float luma_gamma;
uniform float luma_lift;
uniform float luma_offset;
uniform float sat_gain;
uniform float sat_gamma;
uniform float sat_lift;
uniform float sat_offset;
uniform float hue_offset;
uniform float red_gain;
uniform float red_gamma;
uniform float red_lift;
uniform float red_offset;
uniform float green_gain;
uniform float green_gamma;
uniform float green_lift;
uniform float green_offset;
uniform float blue_gain;
uniform float blue_gamma;
uniform float blue_lift;
uniform float blue_offset;
uniform bool clamped;

void main() {
    vec4 base = texture(u_texture, v_uv);
    vec3 rgb = to_srgb(base.rgb);

    // AviUtl は YUV で持っている（``色空間`` の既定も YUV）
    // 明るさ・彩度・色相を HSV で動かすと、灰色の階調では合っていても
    // 色の付いた所だけが別物になる（実測で差が 90 を超えた）
    float y = dot(rgb, LUMA601);
    float u = 0.492 * (rgb.b - y);
    float v = 0.877 * (rgb.r - y);

    y = apply_curve(y, luma_gain, luma_gamma, luma_lift, luma_offset);

    // 彩度は色差の長さ 色相はその向き（回すだけで長さは変えない）
    float chroma = length(vec2(u, v));
    if (chroma > 1e-6) {
        float scaled = apply_curve(chroma, sat_gain, sat_gamma, sat_lift, sat_offset);
        float turn = radians(hue_offset);
        float cs = cos(turn);
        float sn = sin(turn);
        vec2 spun = vec2(u * cs - v * sn, u * sn + v * cs) * (scaled / chroma);
        u = spun.x;
        v = spun.y;
    }

    rgb = vec3(y + 1.140 * v, y - 0.395 * u - 0.581 * v, y + 2.032 * u);

    rgb.r = apply_curve(rgb.r, red_gain, red_gamma, red_lift, red_offset);
    rgb.g = apply_curve(rgb.g, green_gain, green_gamma, green_lift, green_offset);
    rgb.b = apply_curve(rgb.b, blue_gain, blue_gamma, blue_lift, blue_offset);

    // 飽和する を外すと、はみ出した値を切らずに全体を縮めて収める
    // 切ると、明るくしたときに色が抜けて白い面になる
    if (!clamped) {
        float high = max(rgb.r, max(rgb.g, rgb.b));
        if (high > 1.0) {
            rgb /= high;
        }
    }
    frag_color = vec4(to_linear(clamp(rgb, 0.0, 1.0)), base.a);
}
"""
)


_GRADIENT_MAP = _shader(
    _SRGB
    + _LUMA601
    + """
uniform float strength;
uniform vec4 dark_color;
uniform vec4 light_color;

void main() {
    // 明るさを 0..1 の位置と見て、暗部色から明部色へ並べた帯から色を拾う
    vec4 base = texture(u_texture, v_uv);
    vec3 rgb = to_srgb(base.rgb);
    float position = clamp(dot(rgb, LUMA601), 0.0, 1.0);
    vec3 mapped = mix(to_srgb(dark_color.rgb), to_srgb(light_color.rgb), position);
    float amount = clamp(strength * 0.01, 0.0, 1.0);
    frag_color = vec4(to_linear(mix(rgb, mapped, amount)), base.a);
}
"""
)


_COLOR_RANGE_SHIFT = _shader(
    _SRGB
    + _HSV
    + """
uniform float hue_range;
uniform float saturation_range;
uniform float feather;
uniform vec4 key_color;
uniform vec4 to_color;

void main() {
    // 変換前の色に近い色だけを、変換後の色へ塗り替える
    // 近さは色相の差と彩度の差で測る
    //
    // 色相の範囲と境界は **度** 彩度の範囲は **割合（%）**で受け取る
    // AviUtl の 彩度範囲 は 0..255 の刻みなので、呼ぶ側で % へ直してある
    // （色相の方は AviUtl も度 0..255 として換算すると範囲が広がりすぎる）
    vec4 base = texture(u_texture, v_uv);
    vec3 rgb = to_srgb(base.rgb);
    vec3 hsv = to_hsv(clamp(rgb, 0.0, 1.0));
    vec3 key = to_hsv(clamp(to_srgb(key_color.rgb), 0.0, 1.0));

    float turn = abs(hsv.x - key.x);
    turn = min(turn, 1.0 - turn) * 360.0;   // 1 周でつながっているので近い側を見る

    // 境界は範囲を挟んで前後へ散らす AviUtl2 の絵に合わせた形
    // （外側だけで落とす形にすると、実測との差が 15 → 33 へ開いた）
    //
    // ただし**下限は 0 で止める** 境界補正が範囲より大きいと下限が負になり、
    // 変換前の色そのもの（差 0）でさえ塗り替えが中途半端になる
    float edge = max(feather, 1e-4);
    float near_hue = 1.0 - smoothstep(max(hue_range - edge, 0.0), hue_range + edge, turn);
    // 彩度は 0..1 で比べる 受け取った % をそのまま比べると、
    // しきい値が必ず上回って彩度の範囲が効かなくなる
    float sat_edge = saturation_range * 0.01;
    float sat_slack = edge * 0.01;
    float near_sat = 1.0 - smoothstep(
        max(sat_edge - sat_slack, 0.0), sat_edge + sat_slack, abs(hsv.y - key.y)
    );

    vec3 shifted = mix(rgb, to_srgb(to_color.rgb), near_hue * near_sat);
    frag_color = vec4(to_linear(clamp(shifted, 0.0, 1.0)), base.a);
}
"""
)


#: 閃光 2 パスで、絵の明るい所をぼかしてから光として足す
_FLASH = _shader(
    _SRGB
    + _LUMA601
    + """
uniform float strength;
uniform vec4 light_color;
uniform bool fixed_size;

// 光の広がり（ぼかしの半径 px）
//
// AviUtl2 に 強さ 30・60・100 を描かせて測ると、届く先は 315・352・367px と
// ほとんど変わらず、明るさだけが強さに比例して増えた
// サイズ固定 を付けると届く先が 141px まで縮む
float reach() {
    return fixed_size ? 48.0 : 128.0;
}

void main() {
    // 刻みを広げて数を抑える 1px 刻みで 128px を舐めると、読み出しが増えすぎる
    float span = reach();
    if (u_pass == 0) {
        // 1 パス目 絵の明るさを光のもとにして、横へならす
        vec2 step_ = vec2(span, 0.0) / 24.0 / u_size;
        float sum = 0.0;
        float total = 0.0;
        for (int i = -48; i <= 48; ++i) {
            float far = float(i) / 24.0;
            float w = exp(-0.5 * far * far);
            // 光の量は **不透明度だけ**で決める 明るさを掛けると、
            // 画面いっぱいの下地で色ごとに光の量が変わってしまう
            // AviUtl2 は赤から青の下地でも一様に光色を足していた
            sum += texture(u_texture, v_uv + step_ * float(i)).a * w;
            total += w;
        }
        frag_color = vec4(vec3(sum / max(total, 1e-4)), 1.0);
        return;
    }

    // 2 パス目 縦へならしてから、光色を掛けて元の絵へ足す
    //
    // 元の絵は u_source から取る u_texture は 1 パス目の光のもとなので、
    // そちらを元の絵として使うと画面が灰色の靄だけになる
    //
    // 不透明度も上げる 上げないと、図形の外は透けたままで光が見えない
    vec2 step_ = vec2(0.0, span) / 24.0 / u_size;
    float sum = 0.0;
    float total = 0.0;
    for (int i = -48; i <= 48; ++i) {
        float far = float(i) / 24.0;
        float w = exp(-0.5 * far * far);
        sum += texture(u_texture, v_uv + step_ * float(i)).r * w;
        total += w;
    }
    float glow = sum / max(total, 1e-4);

    vec4 base = texture(u_source, v_uv);
    float amount = clamp(glow * (strength * 0.01) * light_color.a, 0.0, 1.0);
    vec3 light = to_srgb(light_color.rgb) * amount;
    vec3 lit = clamp(to_srgb(max(base.rgb, 0.0)) * base.a + light, 0.0, 1.0);
    float alpha = clamp(base.a + amount, 0.0, 1.0);
    frag_color = vec4(to_linear(lit / max(alpha, 1e-4)), alpha);
}
"""
)


def register_grading_effects() -> None:
    definitions = (
        EffectDefinition(
            kind="color_grade",
            label="拡張色調補正",
            category="色",
            parameters=(
                TrackSpec("luma_gain", "明るさのゲイン", -100, 100, 0, unit="%"),
                TrackSpec("luma_gamma", "明るさのガンマ", -100, 100, 0, unit="%"),
                TrackSpec("luma_lift", "明るさのリフト", -100, 100, 0, unit="%"),
                TrackSpec("luma_offset", "明るさのオフセット", -100, 100, 0, unit="%"),
                TrackSpec("sat_gain", "彩度のゲイン", -100, 100, 0, unit="%"),
                TrackSpec("sat_gamma", "彩度のガンマ", -100, 100, 0, unit="%"),
                TrackSpec("sat_lift", "彩度のリフト", -100, 100, 0, unit="%"),
                TrackSpec("sat_offset", "彩度のオフセット", -100, 100, 0, unit="%"),
                TrackSpec("hue_offset", "色相のずらし", -360, 360, 0, unit="度"),
                TrackSpec("red_gain", "赤のゲイン", -100, 100, 0, unit="%"),
                TrackSpec("red_gamma", "赤のガンマ", -100, 100, 0, unit="%"),
                TrackSpec("red_lift", "赤のリフト", -100, 100, 0, unit="%"),
                TrackSpec("red_offset", "赤のオフセット", -100, 100, 0, unit="%"),
                TrackSpec("green_gain", "緑のゲイン", -100, 100, 0, unit="%"),
                TrackSpec("green_gamma", "緑のガンマ", -100, 100, 0, unit="%"),
                TrackSpec("green_lift", "緑のリフト", -100, 100, 0, unit="%"),
                TrackSpec("green_offset", "緑のオフセット", -100, 100, 0, unit="%"),
                TrackSpec("blue_gain", "青のゲイン", -100, 100, 0, unit="%"),
                TrackSpec("blue_gamma", "青のガンマ", -100, 100, 0, unit="%"),
                TrackSpec("blue_lift", "青のリフト", -100, 100, 0, unit="%"),
                TrackSpec("blue_offset", "青のオフセット", -100, 100, 0, unit="%"),
                CheckSpec("clamped", "飽和する", True),
            ),
            fragment_shader=_COLOR_GRADE,
        ),
        EffectDefinition(
            kind="gradient_map",
            label="グラデーションマップ",
            category="色",
            parameters=(
                TrackSpec("strength", "強さ", 0, 100, 100, unit="%"),
                # 透明度は持たない AviUtl の 暗部色 明部色 も色だけで、
                # ここに透明度を置いても描くときに使い道が無い
                ColorSpec("dark_color", "暗部色", (0.0, 0.0, 0.0, 1.0), with_alpha=False),
                ColorSpec("light_color", "明部色", (1.0, 1.0, 1.0, 1.0), with_alpha=False),
            ),
            fragment_shader=_GRADIENT_MAP,
        ),
        EffectDefinition(
            kind="color_range_shift",
            label="特定色域変換",
            category="色",
            parameters=(
                ColorSpec("key_color", "変換前の色", (1.0, 0.0, 0.0, 1.0)),
                ColorSpec("to_color", "変換後の色", (1.0, 1.0, 1.0, 1.0)),
                TrackSpec("hue_range", "色相の範囲", 0, 180, 22, unit="度"),
                TrackSpec("saturation_range", "彩度の範囲", 0, 100, 38, unit="%"),
                TrackSpec("feather", "境界の柔らかさ", 0, 100, 1, unit="度"),
            ),
            fragment_shader=_COLOR_RANGE_SHIFT,
        ),
        EffectDefinition(
            kind="flash",
            label="閃光",
            category="装飾",
            parameters=(
                TrackSpec("strength", "強さ", 0, 400, 100, unit="%"),
                # AviUtl の X と Y は置いていない 光が動くのではなく画面いっぱいの
                # 薄い靄になる動きで、写すと差がかえって開いた（6.9 → 9.9）
                # 写せていないことは :func:`_note_dropped` が記録に残す
                ColorSpec("light_color", "光色", (1.0, 1.0, 1.0, 1.0)),
                CheckSpec("fixed_size", "サイズ固定", False),
            ),
            fragment_shader=_FLASH,
            passes=2,
        ),
    )
    for definition in definitions:
        registry.register(definition)
