"""標準エフェクト。

シェーダはすべてリニア空間・ストレートアルファで受け取り、同じ形で返す。
ぼかしを伴うものは内部で事前乗算アルファに直してから畳む。ストレートのまま
畳むと、透明な画素の色（多くは黒）が混ざって縁が黒ずむ。

使える uniform は :class:`~kumiki.effects.definition.EffectDefinition` の
説明を参照。
"""

from __future__ import annotations

from kumiki.effects.definition import EffectDefinition, registry
from kumiki.effects.spec import CheckSpec, ColorSpec, SelectSpec, TrackSpec, ValueSpec

__all__ = ["PRELUDE", "register_builtin_effects"]

#: すべてのフラグメントシェーダの先頭に付く共通部分。
#: uniform の宣言と、よく使う小さな関数を置く。
PRELUDE = """
#version 430 core
in vec2 v_uv;
out vec4 frag_color;

uniform sampler2D u_texture;   // 直前のパスの出力（リニア・ストレートアルファ）
uniform sampler2D u_source;    // このエフェクトに入ってきた元の絵
uniform vec2 u_size;           // 入力の大きさ（ピクセル）
uniform int u_pass;            // 複数パスのときの通し番号
uniform float u_time;          // クリップ先頭からの経過秒
uniform float u_frame;         // クリップ先頭からの経過フレーム

const vec3 LUMA = vec3(0.2126, 0.7152, 0.0722);  // Rec.709

vec4 premul(vec4 c) { return vec4(c.rgb * c.a, c.a); }
vec4 unpremul(vec4 c) { return c.a > 0.0001 ? vec4(c.rgb / c.a, c.a) : vec4(0.0); }

// 事前乗算アルファでぼかす。ストレートのまま畳むと、透明な画素の色が
// 混ざって縁が黒ずむ。
vec4 blur1d(sampler2D tex, vec2 uv, vec2 direction, float radius) {
    if (radius < 0.5) return texture(tex, uv);
    float sigma = max(radius * 0.5, 0.5);
    vec2 step = direction / u_size;
    vec4 sum = premul(texture(tex, uv));
    float total = 1.0;
    int taps = int(min(radius, 96.0));
    for (int i = 1; i <= taps; ++i) {
        float offset = float(i);
        float w = exp(-0.5 * offset * offset / (sigma * sigma));
        sum += premul(texture(tex, uv + step * offset)) * w;
        sum += premul(texture(tex, uv - step * offset)) * w;
        total += 2.0 * w;
    }
    return unpremul(sum / total);
}

// 0..1 の擬似乱数。
float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}
"""


def _shader(body: str) -> str:
    return PRELUDE + body


_COLOR = _shader("""
uniform float brightness;
uniform float contrast;
uniform float saturation;
uniform float hue;

void main() {
    vec4 color = texture(u_texture, v_uv);
    vec3 rgb = color.rgb;

    rgb *= 1.0 + brightness / 100.0;

    // コントラストの支点は 0.18。リニア空間での中間グレーがそこにあるので、
    // 0.5 を支点にすると暗部だけが極端に動く。
    rgb = (rgb - 0.18) * (1.0 + contrast / 100.0) + 0.18;

    float luma = dot(max(rgb, 0.0), LUMA);
    rgb = mix(vec3(luma), rgb, 1.0 + saturation / 100.0);

    if (abs(hue) > 0.001) {
        float angle = radians(hue);
        float c = cos(angle);
        float s = sin(angle);
        // YIQ 空間での回転。輝度を保ったまま色相だけを回せる。
        mat3 rotation = mat3(
            0.299 + 0.701 * c + 0.168 * s, 0.587 - 0.587 * c + 0.330 * s,
            0.114 - 0.114 * c - 0.497 * s,
            0.299 - 0.299 * c - 0.328 * s, 0.587 + 0.413 * c + 0.035 * s,
            0.114 - 0.114 * c + 0.292 * s,
            0.299 - 0.300 * c + 1.250 * s, 0.587 - 0.588 * c - 1.050 * s,
            0.114 + 0.886 * c - 0.203 * s
        );
        rgb = rgb * rotation;
    }

    frag_color = vec4(max(rgb, 0.0), color.a);
}
""")


_BLUR = _shader("""
uniform float radius;

void main() {
    // 横と縦に分けて畳む。1 回で 2 次元のカーネルを回すと、計算量が半径の 2 乗になる。
    vec2 direction = u_pass == 0 ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
    frag_color = blur1d(u_texture, v_uv, direction, radius);
}
""")


_GLOW = _shader("""
uniform float threshold;
uniform float intensity;
uniform float radius;

void main() {
    vec2 direction = u_pass == 0 ? vec2(1.0, 0.0) : vec2(0.0, 1.0);

    if (u_pass == 0) {
        // 明るい部分だけを抜き出してから、横方向にぼかす。
        float sigma = max(radius * 0.5, 0.5);
        vec2 step = direction / u_size;
        vec4 sum = vec4(0.0);
        float total = 0.0;
        int taps = int(min(radius, 96.0));
        for (int i = -taps; i <= taps; ++i) {
            float w = exp(-0.5 * float(i * i) / (sigma * sigma));
            vec4 c = texture(u_texture, v_uv + step * float(i));
            float luma = dot(c.rgb, LUMA);
            float keep = smoothstep(threshold, threshold + 0.1, luma);
            sum += vec4(c.rgb * c.a * keep, c.a * keep) * w;
            total += w;
        }
        frag_color = sum / max(total, 0.0001);
        return;
    }

    // 縦方向にぼかしてから、元の絵へ加算する。
    float sigma = max(radius * 0.5, 0.5);
    vec2 step = direction / u_size;
    vec4 sum = vec4(0.0);
    float total = 0.0;
    int taps = int(min(radius, 96.0));
    for (int i = -taps; i <= taps; ++i) {
        float w = exp(-0.5 * float(i * i) / (sigma * sigma));
        sum += texture(u_texture, v_uv + step * float(i)) * w;
        total += w;
    }
    vec4 halo = sum / max(total, 0.0001);

    vec4 base = texture(u_source, v_uv);
    vec3 lit = base.rgb * base.a + halo.rgb * (intensity / 100.0);
    float alpha = clamp(base.a + halo.a * (intensity / 100.0), 0.0, 1.0);
    frag_color = unpremul(vec4(lit, alpha));
}
""")


_CHROMA_KEY = _shader("""
uniform vec4 key_color;
uniform float similarity;
uniform float smoothness;
uniform float spill;

void main() {
    vec4 color = texture(u_texture, v_uv);

    // 色相と彩度で比べる。明るさの違いで抜けが変わると、照明ムラのある
    // 実写グリーンバックがまったく抜けない。
    float key_luma = dot(key_color.rgb, LUMA);
    float luma = dot(color.rgb, LUMA);
    vec2 difference = (color.rgb - vec3(luma)).xy - (key_color.rgb - vec3(key_luma)).xy;
    float distance = length(difference);

    float tolerance = similarity / 100.0;
    float feather = max(smoothness / 100.0, 0.001);
    float alpha = smoothstep(tolerance, tolerance + feather, distance);

    vec3 rgb = color.rgb;
    if (spill > 0.0) {
        // 縁に残る背景色を、輝度を保ったまま抜く。
        float excess = dot(rgb - vec3(luma), normalize(key_color.rgb - vec3(key_luma) + 1e-6));
        rgb -= normalize(key_color.rgb - vec3(key_luma) + 1e-6)
             * max(excess, 0.0) * (spill / 100.0);
    }

    frag_color = vec4(max(rgb, 0.0), color.a * alpha);
}
""")


_TRANSFORM = _shader("""
uniform float pos_x;
uniform float pos_y;
uniform float scale;
uniform float scale_y;
uniform float rotation;
uniform float anchor_x;
uniform float anchor_y;

void main() {
    // 出力の座標から入力の座標を逆算する。前方に写すと隙間が空く。
    //
    // v_uv の Y は上向き。設定の Y も上向き（正で上へ動く）なので、
    // ここでは符号をそろえるだけでよい。反転させると、同じ「Y」の表示なのに
    // テキストや影と上下が逆に動くことになる。
    vec2 pixel = v_uv * u_size;
    vec2 anchor = u_size * 0.5 + vec2(anchor_x, anchor_y);

    pixel -= anchor + vec2(pos_x, pos_y);

    float angle = radians(-rotation);
    float c = cos(angle);
    float s = sin(angle);
    pixel = mat2(c, -s, s, c) * pixel;

    float sx = max(scale / 100.0, 0.0001);
    float sy = max(scale_y / 100.0, 0.0001) * sx;
    pixel /= vec2(sx, sy);
    pixel += anchor;

    vec2 uv = pixel / u_size;
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
        frag_color = vec4(0.0);
        return;
    }
    frag_color = texture(u_texture, uv);
}
""")


_CROP = _shader("""
uniform float top;
uniform float bottom;
uniform float left;
uniform float right;
uniform float feather;

void main() {
    vec2 pixel = v_uv * u_size;
    // v_uv は GL の向き（下が 0）。上下の指定を画像の向きに合わせる。
    float from_top = u_size.y - pixel.y;
    float from_bottom = pixel.y;

    float edge = max(feather, 0.0001);
    float alpha = smoothstep(0.0, edge, from_top - top)
                * smoothstep(0.0, edge, from_bottom - bottom)
                * smoothstep(0.0, edge, pixel.x - left)
                * smoothstep(0.0, edge, (u_size.x - pixel.x) - right);

    vec4 color = texture(u_texture, v_uv);
    frag_color = vec4(color.rgb, color.a * alpha);
}
""")


_BORDER = _shader("""
uniform float width;
uniform vec4 color;

void main() {
    vec4 base = texture(u_texture, v_uv);
    if (width < 0.5) {
        frag_color = base;
        return;
    }

    // 周囲を見て、近くに不透明な画素があれば縁として塗る。
    float coverage = 0.0;
    int steps = int(min(width, 32.0));
    for (int y = -steps; y <= steps; ++y) {
        for (int x = -steps; x <= steps; ++x) {
            vec2 offset = vec2(float(x), float(y));
            if (length(offset) > width) continue;
            coverage = max(coverage, texture(u_texture, v_uv + offset / u_size).a);
        }
    }

    // 縁の上に元の絵を重ねる（over 合成）。
    vec4 edge = vec4(color.rgb, color.a * coverage);
    vec3 rgb = base.rgb * base.a + edge.rgb * edge.a * (1.0 - base.a);
    float alpha = base.a + edge.a * (1.0 - base.a);
    frag_color = unpremul(vec4(rgb, alpha));
}
""")


_SHADOW = _shader("""
uniform float offset_x;
uniform float offset_y;
uniform float blur;
uniform float opacity;
uniform vec4 color;

void main() {
    if (u_pass == 0) {
        // 影の形を作って横にぼかす。位置は元の絵からずらす。
        // Y は正が上。変形エフェクトの pos_y と揃えてある。ここだけ逆にすると、
        // 同じ「Y」という表示で上下が反対に動くことになる。
        vec2 shift = -vec2(offset_x, offset_y) / u_size;
        vec4 shape = blur1d(u_texture, v_uv + shift, vec2(1.0, 0.0), blur);
        frag_color = vec4(color.rgb, shape.a * color.a * (opacity / 100.0));
        return;
    }

    vec4 shadow = blur1d(u_texture, v_uv, vec2(0.0, 1.0), blur);
    vec4 base = texture(u_source, v_uv);

    // 影の上に元の絵を載せる。
    vec3 rgb = base.rgb * base.a + shadow.rgb * shadow.a * (1.0 - base.a);
    float alpha = base.a + shadow.a * (1.0 - base.a);
    frag_color = unpremul(vec4(rgb, alpha));
}
""")


_SHARPEN = _shader("""
uniform float strength;

void main() {
    vec2 texel = 1.0 / u_size;
    vec4 center = texture(u_texture, v_uv);
    vec4 sum = premul(texture(u_texture, v_uv + vec2(texel.x, 0.0)))
             + premul(texture(u_texture, v_uv - vec2(texel.x, 0.0)))
             + premul(texture(u_texture, v_uv + vec2(0.0, texel.y)))
             + premul(texture(u_texture, v_uv - vec2(0.0, texel.y)));
    vec4 blurred = unpremul(sum * 0.25);

    // アンシャープマスク。ぼかしとの差を戻す量で鋭さが決まる。
    vec3 rgb = center.rgb + (center.rgb - blurred.rgb) * (strength / 100.0);
    frag_color = vec4(max(rgb, 0.0), center.a);
}
""")


_NOISE = _shader("""
uniform float strength;
uniform bool monochrome;
uniform int seed;
uniform bool animate;

void main() {
    vec4 color = texture(u_texture, v_uv);
    vec2 p = v_uv * u_size + float(seed) * 17.0;
    if (animate) p += u_frame * 13.0;

    vec3 noise;
    if (monochrome) {
        noise = vec3(hash(p) - 0.5);
    } else {
        noise = vec3(hash(p), hash(p + 41.7), hash(p + 93.1)) - 0.5;
    }

    frag_color = vec4(max(color.rgb + noise * (strength / 100.0), 0.0), color.a);
}
""")


_MOSAIC = _shader("""
uniform float size;

void main() {
    float block = max(size, 1.0);
    vec2 pixel = floor(v_uv * u_size / block) * block + block * 0.5;
    frag_color = texture(u_texture, pixel / u_size);
}
""")


_MASK = _shader("""
uniform int shape;
uniform float center_x;
uniform float center_y;
uniform float mask_width;
uniform float mask_height;
uniform float feather;
uniform bool invert;

void main() {
    vec2 pixel = v_uv * u_size;
    // v_uv の Y は上向き。設定の Y も上向き。
    vec2 centre = u_size * 0.5 + vec2(center_x, center_y);
    vec2 half_size = max(vec2(mask_width, mask_height) * 0.5, vec2(0.5));
    vec2 delta = pixel - centre;
    float edge = max(feather, 0.0001);

    float inside;
    if (shape == 0) {
        // 矩形。各辺からの距離のうち最も内側を採る。
        vec2 distance = half_size - abs(delta);
        inside = min(smoothstep(0.0, edge, distance.x), smoothstep(0.0, edge, distance.y));
    } else {
        // 楕円。正規化してから半径 1 の円として測る。
        float radius = length(delta / half_size);
        float scale = min(half_size.x, half_size.y);
        inside = smoothstep(0.0, edge / scale, 1.0 - radius);
    }

    if (invert) inside = 1.0 - inside;
    vec4 color = texture(u_texture, v_uv);
    frag_color = vec4(color.rgb, color.a * inside);
}
""")


_GRADIENT = _shader("""
uniform float strength;
uniform float center_x;
uniform float center_y;
uniform float angle;
uniform float span;
uniform int shape;
uniform vec4 start_color;
uniform vec4 end_color;

void main() {
    vec4 base = texture(u_texture, v_uv);

    // 中心を原点、右と下を正とした画素座標。
    // v_uv の Y は上向きなので、ここで下向きに直す。設定の Y も上向きなので
    // 中心のずらし量も同じように反転する。
    vec2 pixel = (v_uv - 0.5) * u_size;
    pixel.y = -pixel.y;
    vec2 centre = vec2(center_x, -center_y);
    float length_ = max(span, 1.0);

    float t;
    if (shape == 1) {
        // 円形。中心からの距離。
        t = length(pixel - centre) / length_;
    } else {
        // 線形。角度 0 で左から右、90 で上から下。
        float radian = radians(angle);
        vec2 direction = vec2(cos(radian), sin(radian));
        t = dot(pixel - centre, direction) / length_ + 0.5;
    }

    vec4 ramp = mix(start_color, end_color, clamp(t, 0.0, 1.0));
    // 元の絵の不透明度はそのまま。グラデーションは色だけを塗り替える。
    float amount = clamp(strength * 0.01, 0.0, 1.0) * ramp.a;
    frag_color = vec4(mix(base.rgb, ramp.rgb, amount), base.a);
}
""")


_FILL = _shader("""
uniform vec4 color;
uniform float amount;

void main() {
    // 形はそのままに、色だけを塗る。不透明度に触ると輪郭の外まで色が出る。
    vec4 base = texture(u_texture, v_uv);
    float mixing = clamp(amount * 0.01, 0.0, 1.0) * color.a;
    frag_color = vec4(mix(base.rgb, color.rgb, mixing), base.a);
}
""")


_OPACITY = _shader("""
uniform float amount;

void main() {
    vec4 color = texture(u_texture, v_uv);
    frag_color = vec4(color.rgb, color.a * clamp(amount * 0.01, 0.0, 1.0));
}
""")


_DIRECTIONAL_BLUR = _shader("""
uniform float radius;
uniform float angle;

void main() {
    // 1 方向にだけ伸ばす。角度 0 で横、90 で縦。
    float radian = radians(angle);
    frag_color = blur1d(u_texture, v_uv, vec2(cos(radian), sin(radian)), radius);
}
""")


_LUMINANCE_KEY = _shader("""
uniform float threshold;
uniform float smoothness;
uniform bool invert;

void main() {
    vec4 color = texture(u_texture, v_uv);
    float luma = dot(max(color.rgb, 0.0), LUMA);

    float edge = clamp(threshold * 0.01, 0.0, 1.0);
    float feather = max(smoothness * 0.01, 0.001);
    // 明るいところを残す。反転すると暗いところが残る。
    float keep = smoothstep(edge - feather, edge + feather, luma);
    if (invert) keep = 1.0 - keep;

    frag_color = vec4(color.rgb, color.a * keep);
}
""")


def register_builtin_effects() -> None:
    """標準エフェクトを一覧へ登録する。読み込み時に 1 度だけ呼ばれる。"""
    if "color" in registry:
        return

    registry.register(
        EffectDefinition(
            kind="color",
            label="色調補正",
            category="色",
            parameters=(
                TrackSpec("brightness", "明るさ", -100, 100, 0, unit="%"),
                TrackSpec("contrast", "コントラスト", -100, 300, 0, unit="%"),
                TrackSpec("saturation", "彩度", -100, 300, 0, unit="%"),
                TrackSpec("hue", "色相", -180, 180, 0, unit="度"),
            ),
            fragment_shader=_COLOR,
        )
    )

    registry.register(
        EffectDefinition(
            kind="blur",
            label="ぼかし",
            category="ぼかし",
            parameters=(TrackSpec("radius", "範囲", 0, 96, 8, unit="px"),),
            fragment_shader=_BLUR,
            passes=2,
        )
    )

    registry.register(
        EffectDefinition(
            kind="glow",
            label="グロー",
            category="ぼかし",
            parameters=(
                TrackSpec("threshold", "しきい値", 0, 1, 0.6, step=0.01),
                TrackSpec("intensity", "強さ", 0, 400, 100, unit="%"),
                TrackSpec("radius", "範囲", 0, 96, 24, unit="px"),
            ),
            fragment_shader=_GLOW,
            passes=2,
        )
    )

    registry.register(
        EffectDefinition(
            kind="chroma_key",
            label="クロマキー",
            category="合成",
            parameters=(
                ColorSpec("key_color", "キー色", (0.0, 1.0, 0.0, 1.0), with_alpha=False),
                TrackSpec("similarity", "類似度", 0, 100, 20, unit="%"),
                TrackSpec("smoothness", "境界のぼかし", 0, 100, 10, unit="%"),
                TrackSpec("spill", "色かぶり除去", 0, 100, 50, unit="%"),
            ),
            fragment_shader=_CHROMA_KEY,
        )
    )

    registry.register(
        EffectDefinition(
            kind="transform",
            label="変形",
            category="変形",
            parameters=(
                TrackSpec("pos_x", "X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("pos_y", "Y", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("scale", "拡大率", 1, 800, 100, unit="%"),
                TrackSpec("scale_y", "縦の拡大率", 1, 800, 100, unit="%"),
                TrackSpec("rotation", "回転", -3600, 3600, 0, unit="度"),
                TrackSpec("anchor_x", "中心 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("anchor_y", "中心 Y", -4000, 4000, 0, step=1, unit="px"),
            ),
            fragment_shader=_TRANSFORM,
        )
    )

    registry.register(
        EffectDefinition(
            kind="crop",
            label="クリッピング",
            category="変形",
            parameters=(
                TrackSpec("top", "上", 0, 4000, 0, step=1, unit="px"),
                TrackSpec("bottom", "下", 0, 4000, 0, step=1, unit="px"),
                TrackSpec("left", "左", 0, 4000, 0, step=1, unit="px"),
                TrackSpec("right", "右", 0, 4000, 0, step=1, unit="px"),
                TrackSpec("feather", "ぼかし", 0, 200, 0, unit="px"),
            ),
            fragment_shader=_CROP,
        )
    )

    registry.register(
        EffectDefinition(
            kind="border",
            label="縁取り",
            category="装飾",
            parameters=(
                TrackSpec("width", "太さ", 0, 32, 4, unit="px"),
                ColorSpec("color", "色", (1.0, 1.0, 1.0, 1.0)),
            ),
            fragment_shader=_BORDER,
        )
    )

    registry.register(
        EffectDefinition(
            kind="gradient",
            label="グラデーション",
            category="装飾",
            parameters=(
                TrackSpec("strength", "強さ", 0, 100, 100, unit="%"),
                ColorSpec("start_color", "開始色", (1.0, 1.0, 1.0, 1.0)),
                ColorSpec("end_color", "終了色", (0.0, 0.0, 0.0, 1.0)),
                SelectSpec("shape", "形状", (("linear", "線形"), ("radial", "円形")), "linear"),
                TrackSpec("angle", "角度", -360, 360, 90, unit="度"),
                TrackSpec("span", "幅", 1, 4000, 100, step=1, unit="px"),
                TrackSpec("center_x", "中心 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("center_y", "中心 Y", -4000, 4000, 0, step=1, unit="px"),
            ),
            fragment_shader=_GRADIENT,
        )
    )

    registry.register(
        EffectDefinition(
            kind="shadow",
            label="影",
            category="装飾",
            parameters=(
                TrackSpec("offset_x", "X", -200, 200, 6, step=1, unit="px"),
                TrackSpec("offset_y", "Y", -200, 200, -6, step=1, unit="px"),
                TrackSpec("blur", "ぼかし", 0, 96, 6, unit="px"),
                TrackSpec("opacity", "濃さ", 0, 100, 70, unit="%"),
                ColorSpec("color", "色", (0.0, 0.0, 0.0, 1.0)),
            ),
            fragment_shader=_SHADOW,
            passes=2,
        )
    )

    registry.register(
        EffectDefinition(
            kind="fill",
            label="単色塗り",
            category="色",
            parameters=(
                ColorSpec("color", "色", (1.0, 1.0, 1.0, 1.0)),
                TrackSpec("amount", "強さ", 0, 100, 100, unit="%"),
            ),
            fragment_shader=_FILL,
        )
    )

    registry.register(
        EffectDefinition(
            kind="opacity",
            label="不透明度",
            category="合成",
            parameters=(TrackSpec("amount", "不透明度", 0, 100, 100, unit="%"),),
            fragment_shader=_OPACITY,
        )
    )

    registry.register(
        EffectDefinition(
            kind="directional_blur",
            label="方向ぼかし",
            category="ぼかし",
            parameters=(
                TrackSpec("radius", "範囲", 0, 96, 16, unit="px"),
                TrackSpec("angle", "角度", -360, 360, 0, unit="度"),
            ),
            fragment_shader=_DIRECTIONAL_BLUR,
        )
    )

    registry.register(
        EffectDefinition(
            kind="luminance_key",
            label="輝度キー",
            category="合成",
            parameters=(
                TrackSpec("threshold", "しきい値", 0, 100, 50, unit="%"),
                TrackSpec("smoothness", "境界のぼかし", 0, 100, 10, unit="%"),
                CheckSpec("invert", "暗いところを残す", False),
            ),
            fragment_shader=_LUMINANCE_KEY,
        )
    )

    registry.register(
        EffectDefinition(
            kind="sharpen",
            label="シャープ",
            category="ぼかし",
            parameters=(TrackSpec("strength", "強さ", 0, 400, 100, unit="%"),),
            fragment_shader=_SHARPEN,
        )
    )

    registry.register(
        EffectDefinition(
            kind="noise",
            label="ノイズ",
            category="装飾",
            parameters=(
                TrackSpec("strength", "強さ", 0, 100, 20, unit="%"),
                CheckSpec("monochrome", "白黒", True),
                CheckSpec("animate", "時間で変化", True),
                ValueSpec("seed", "シード", 0, 0, 9999),
            ),
            fragment_shader=_NOISE,
        )
    )

    registry.register(
        EffectDefinition(
            kind="mosaic",
            label="モザイク",
            category="装飾",
            parameters=(TrackSpec("size", "大きさ", 1, 200, 16, step=1, unit="px"),),
            fragment_shader=_MOSAIC,
        )
    )

    registry.register(
        EffectDefinition(
            kind="mask",
            label="マスク",
            category="合成",
            parameters=(
                SelectSpec("shape", "形", (("rect", "矩形"), ("ellipse", "楕円")), "ellipse"),
                TrackSpec("center_x", "中心 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("center_y", "中心 Y", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("mask_width", "幅", 0, 8000, 640, step=1, unit="px"),
                TrackSpec("mask_height", "高さ", 0, 8000, 360, step=1, unit="px"),
                TrackSpec("feather", "ぼかし", 0, 400, 0, unit="px"),
                CheckSpec("invert", "反転", False),
            ),
            fragment_shader=_MASK,
        )
    )


register_builtin_effects()
