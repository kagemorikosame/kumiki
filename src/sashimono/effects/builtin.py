"""標準エフェクト

シェーダはすべてリニア空間・ストレートアルファで受け取り、同じ形で返す
ぼかしを伴うものは内部で事前乗算アルファに直してから畳む ストレートのまま
畳むと、透明な画素の色（多くは黒）が混ざって縁が黒ずむ

使える uniform は :class:`~sashimono.effects.definition.EffectDefinition` の
説明を参照
"""

from __future__ import annotations

from sashimono.effects.blending import BLEND_FUNCTIONS, BLEND_MODES
from sashimono.effects.definition import EffectDefinition, registry
from sashimono.effects.spec import (
    IMAGE_FILTER,
    CheckSpec,
    ColorSpec,
    FileSpec,
    SelectSpec,
    TrackSpec,
    ValueSpec,
)

__all__ = ["PRELUDE", "register_builtin_effects"]

#: すべてのフラグメントシェーダの先頭に付く共通部分
#: uniform の宣言と、よく使う小さな関数を置く
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
uniform float u_fps;           // 1 秒あたりのフレーム数
uniform float u_duration;      // クリップの長さ（秒） 退場の動きは終わりから逆算する
uniform vec4 u_object;         // 絵が置かれた範囲（画素、左・下・右・上 Y は上が正）
uniform vec2 u_origin;         // 絵の原点（画素、Y は上が正） ふつうは範囲の中央

const vec3 LUMA = vec3(0.2126, 0.7152, 0.0722);  // Rec.709
const float PI = 3.14159265358979;
// 画面からカメラまでの距離 sashimono.engine.gpu.projection.CAMERA_DISTANCE と同じ値
const float CAMERA = 1024.0;

vec2 object_center() { return (u_object.xy + u_object.zw) * 0.5; }
vec2 object_size() { return max(abs(u_object.zw - u_object.xy), vec2(1.0)); }
// 絵の原点 YMM4 が位置の設定を足し込む点 素材や図形では範囲の中央と同じだが、
// 場面切り替えの場面の絵は画面の中央が原点で、範囲（中身の描かれた所）とずれる
vec2 object_origin() { return u_origin; }

// 画素の位置で読む 外は透明 端を引き伸ばして読むと、動かした絵の外側に
// 縁の色が帯になって伸びる
vec4 sample_pixel(vec2 pixel) {
    vec2 uv = pixel / u_size;
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) return vec4(0.0);
    return texture(u_texture, uv);
}

// ストレートアルファどうしの重ね（上が手前）
vec4 over(vec4 above, vec4 below) {
    float alpha = above.a + below.a * (1.0 - above.a);
    vec3 rgb = above.rgb * above.a + below.rgb * below.a * (1.0 - above.a);
    return alpha > 0.0001 ? vec4(rgb / alpha, alpha) : vec4(0.0);
}

// イージング 種類は 0 直線 1 Sine 2 Quad 3 Cubic 4 Quart 5 Quint 6 Expo 7 Circ
// 8 Back 9 Elastic 10 Bounce 11 Jump、向きは 0 In 1 Out 2 InOut
float bounce_out(float t) {
    if (t < 1.0 / 2.75) return 7.5625 * t * t;
    if (t < 2.0 / 2.75) { t -= 1.5 / 2.75; return 7.5625 * t * t + 0.75; }
    if (t < 2.5 / 2.75) { t -= 2.25 / 2.75; return 7.5625 * t * t + 0.9375; }
    t -= 2.625 / 2.75;
    return 7.5625 * t * t + 0.984375;
}

float ease_in(float t, int kind) {
    if (kind == 1) return 1.0 - cos(t * PI * 0.5);
    if (kind == 2) return t * t;
    if (kind == 3) return t * t * t;
    if (kind == 4) return t * t * t * t;
    if (kind == 5) return t * t * t * t * t;
    if (kind == 6) return t <= 0.0 ? 0.0 : pow(2.0, 10.0 * (t - 1.0));
    if (kind == 7) return 1.0 - sqrt(max(1.0 - t * t, 0.0));
    if (kind == 8) return t * t * (2.70158 * t - 1.70158);
    if (kind == 9) {
        if (t <= 0.0 || t >= 1.0) return t;
        return -pow(2.0, 10.0 * (t - 1.0)) * sin((t - 1.075) * 2.0 * PI / 0.3);
    }
    if (kind == 10) return 1.0 - bounce_out(1.0 - t);
    if (kind == 11) return t >= 1.0 ? 1.0 : 0.0;
    return t;
}

float ease(float t, int kind, int mode) {
    t = clamp(t, 0.0, 1.0);
    if (mode == 0) return ease_in(t, kind);
    if (mode == 1) return 1.0 - ease_in(1.0 - t, kind);
    return t < 0.5 ? ease_in(t * 2.0, kind) * 0.5 : 1.0 - ease_in(2.0 - t * 2.0, kind) * 0.5;
}

// 行で書いた 3x3 行列 GLSL の mat3 は列で並べるので、転置して渡す
mat3 rows3(vec3 a, vec3 b, vec3 c) { return transpose(mat3(a, b, c)); }

// X → Y → Z の順に回す行列 角度は度 向きは sashimono.engine.gpu.projection.rotate と同じ
// （画面の Y 下向き、奥が正で考える）
mat3 rotation3(vec3 degrees) {
    vec3 r = radians(degrees);
    mat3 rx = rows3(vec3(1.0, 0.0, 0.0), vec3(0.0, cos(r.x), sin(r.x)),
                    vec3(0.0, -sin(r.x), cos(r.x)));
    mat3 ry = rows3(vec3(cos(r.y), 0.0, -sin(r.y)), vec3(0.0, 1.0, 0.0),
                    vec3(sin(r.y), 0.0, cos(r.y)));
    mat3 rz = rows3(vec3(cos(r.z), -sin(r.z), 0.0), vec3(sin(r.z), cos(r.z), 0.0),
                    vec3(0.0, 0.0, 1.0));
    return rz * ry * rx;
}

// 傾けた板の逆算 画面の点（支点からの画素、Y 上向き）が、回す前の板のどこに
// あたるかを返す カメラから画面の点へ伸ばした線と、回した板の面との交点を
// 回す前へ戻す 板が真横を向いて交わらなければ、遠くの点（透明）を返す
// camera はカメラの真正面の点（支点からの画素、Y 上向き） 支点と離れていると、
// 離れた側ほど遠近が付く
vec2 untilt_seen_from(vec2 point, vec3 degrees, vec2 camera) {
    mat3 rotation = rotation3(degrees);
    vec3 normal = rotation * vec3(0.0, 0.0, 1.0);
    vec3 eye = vec3(camera.x, -camera.y, -CAMERA);
    vec3 direction = vec3(point.x - camera.x, -(point.y - camera.y), CAMERA);
    float facing = dot(normal, direction);
    if (abs(facing) < 1e-5) return vec2(1e6);
    float distance = -dot(normal, eye) / facing;
    if (distance <= 0.0) return vec2(1e6);
    vec3 hit = eye + direction * distance;
    vec3 local = transpose(rotation) * hit;
    return vec2(local.x, -local.y);
}

vec2 untilt(vec2 point, vec3 degrees) {
    return untilt_seen_from(point, degrees, vec2(0.0));
}

// 奥行き z（画素、奥が正）へ置いたときの拡大率
float depth_scale(float z) { return CAMERA / max(CAMERA + z, 1.0); }

vec4 premul(vec4 c) { return vec4(c.rgb * c.a, c.a); }
vec4 unpremul(vec4 c) { return c.a > 0.0001 ? vec4(c.rgb / c.a, c.a) : vec4(0.0); }

// 事前乗算アルファでぼかす ストレートのまま畳むと、透明な画素の色が
// 混ざって縁が黒ずむ
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

// 0..1 の擬似乱数
float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

// エフェクトが読む画像（FileSpec の texture）の 1 点 p は画像の左上を原点とする
// 画素（Y は下が正） 画像は上の行から積んであるので、そのまま割れば UV になる
// loop が偽なら画像の外は透明 真なら敷き詰める（AviUtl2 の ループ画像）
vec4 image_pixel(sampler2D image, vec2 size, vec2 p, bool loop) {
    vec2 uv = p / max(size, vec2(1.0));
    if (loop) {
        uv = fract(uv);
    } else if (uv.x < 0.0 || uv.y < 0.0 || uv.x >= 1.0 || uv.y >= 1.0) {
        return vec4(0.0);
    }
    return texture(image, uv);
}
"""


def _shader(body: str) -> str:
    return PRELUDE + body


_COLOR = _shader("""
uniform float brightness;
uniform float contrast;
uniform float saturation;
uniform float hue;
uniform float gain;

vec3 encode_srgb(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
}
vec3 decode_srgb(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c));
}

void main() {
    vec4 color = texture(u_texture, v_uv);
    vec3 rgb = color.rgb;

    if (abs(gain - 100.0) > 0.001) {
        // 符号化した値（sRGB）に掛けて、白で頭打ちにする YMM4 の色調補正の「輝度」は
        // この形だった リニアのまま掛けると 150% でも明るさが半分ほどしか上がらず、
        // ペイントトランジションの真ん中が YMM4 より 30 ほど暗く出た
        rgb = decode_srgb(encode_srgb(rgb) * max(gain, 0.0) / 100.0);
    }

    rgb *= 1.0 + brightness / 100.0;

    // コントラストの支点は 0.18 リニア空間での中間グレーがそこにあるので、
    // 0.5 を支点にすると暗部だけが極端に動く
    rgb = (rgb - 0.18) * (1.0 + contrast / 100.0) + 0.18;

    float luma = dot(max(rgb, 0.0), LUMA);
    rgb = mix(vec3(luma), rgb, 1.0 + saturation / 100.0);

    if (abs(hue) > 0.001) {
        float angle = radians(hue);
        float c = cos(angle);
        float s = sin(angle);
        // YIQ 空間での回転 輝度を保ったまま色相だけを回せる
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
    // 横と縦に分けて畳む 1 回で 2 次元のカーネルを回すと、計算量が半径の 2 乗になる
    vec2 direction = u_pass == 0 ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
    frag_color = blur1d(u_texture, v_uv, direction, radius);
}
""")


_GLOW = _shader("""
uniform float threshold;
uniform float intensity;
uniform float radius;
uniform bool tinted;
uniform vec4 tint;

void main() {
    vec2 direction = u_pass == 0 ? vec2(1.0, 0.0) : vec2(0.0, 1.0);

    if (u_pass == 0) {
        // 明るい部分だけを抜き出してから、横方向にぼかす
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

    // 縦方向にぼかしてから、元の絵へ加算する
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
    // 色付けは光の明るさだけを残して色を塗り替える（YMM4 のブルームの色付け）
    if (tinted) halo.rgb = tint.rgb * dot(halo.rgb, LUMA);

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

    // 色相と彩度で比べる 明るさの違いで抜けが変わると、照明ムラのある
    // 実写グリーンバックがまったく抜けない
    float key_luma = dot(key_color.rgb, LUMA);
    float luma = dot(color.rgb, LUMA);
    vec2 difference = (color.rgb - vec3(luma)).xy - (key_color.rgb - vec3(key_luma)).xy;
    float distance = length(difference);

    float tolerance = similarity / 100.0;
    float feather = max(smoothness / 100.0, 0.001);
    float alpha = smoothstep(tolerance, tolerance + feather, distance);

    vec3 rgb = color.rgb;
    if (spill > 0.0) {
        // 縁に残る背景色を、輝度を保ったまま抜く
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
uniform float rotation_x;
uniform float rotation_y;
uniform float anchor_x;
uniform float anchor_y;
uniform int pivot_h;
uniform int pivot_v;
uniform bool move_to_pivot;

void main() {
    // 出力の座標から入力の座標を逆算する 前方に写すと隙間が空く
    //
    // v_uv の Y は上向き 設定の Y も上向き（正で上へ動く）なので、
    // ここでは符号をそろえるだけでよい 反転させると、同じ「Y」の表示なのに
    // テキストや影と上下が逆に動くことになる
    vec2 pixel = v_uv * u_size;
    // 支点の基準 既定は画面の中央 絵の端（YMM4 の中心点）を選ぶと、絵が
    // 置かれた範囲の端になる 画面の中央を既定に残すのは、今までのプロジェクトの
    // 見た目を変えないため
    vec2 base = u_size * 0.5;
    if (pivot_h == 1) base.x = u_object.x;
    if (pivot_h == 2) base.x = u_object.z;
    if (pivot_v == 1) base.y = u_object.w;
    if (pivot_v == 2) base.y = u_object.y;
    if (pivot_h == 3) base.x = object_center().x;
    if (pivot_v == 3) base.y = object_center().y;
    if (pivot_h == 4) base.x = object_origin().x;
    if (pivot_v == 4) base.y = object_origin().y;
    vec2 anchor = base + vec2(anchor_x, anchor_y);
    if (move_to_pivot) {
        // 選んだ中心が、絵の元の中心の位置へ来るように絵ごと動かす（YMM4 の中心点で
        // 「位置を保つ」を切ったとき） 右下を選べば、絵は左上へずれる
        pixel += anchor - object_center();
    }

    pixel -= anchor + vec2(pos_x, pos_y);

    if (rotation_x != 0.0 || rotation_y != 0.0) {
        // 板を傾ける 平面の回転と拡大より先に戻す（掛ける順の逆）
        // カメラは絵の原点の正面 支点の正面に置くと、支点を絵から遠く離したとき
        // （ページめくり風その2 は 1700 画素下）に絵が斜めから見た台形に歪む
        // YMM4 は支点を離しても、原点から見た遠近のまま回した
        pixel = untilt_seen_from(
            pixel, vec3(rotation_x, rotation_y, 0.0), object_origin() - anchor
        );
    }

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
    // v_uv は GL の向き（下が 0） 上下の指定を画像の向きに合わせる
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
uniform sampler2D pattern;
uniform vec2 pattern_size;

// 縁の色 模様の画像があれば色の代わりにそれで塗る
//
// 模様は**縁の分だけ広げた範囲の左上**から敷き詰める AviUtl2 に 200x200 の
// 4 色の画像で、200 の四角形へ縁 10 と 30 を描かせると、起点は 850 と 830
// （四角形の左端 860 から縁の太さぶん外）だった 中央を起点にする画像合成とは違う
// 縁色は模様に混ざらない 縁色を赤にしても、縁は模様の色のままだった
vec4 edge_color() {
    if (pattern_size.x < 1.0 || pattern_size.y < 1.0) return color;
    vec2 origin = vec2(u_object.x - width, u_object.w + width);
    vec2 pixel = v_uv * u_size;
    return image_pixel(pattern, pattern_size, vec2(pixel.x - origin.x, origin.y - pixel.y), true);
}

void main() {
    vec4 base = texture(u_texture, v_uv);
    if (width < 0.5) {
        frag_color = base;
        return;
    }

    // 周囲を見て、近くに不透明な画素があれば縁として塗る
    float coverage = 0.0;
    int steps = int(min(width, 32.0));
    for (int y = -steps; y <= steps; ++y) {
        for (int x = -steps; x <= steps; ++x) {
            vec2 offset = vec2(float(x), float(y));
            if (length(offset) > width) continue;
            coverage = max(coverage, texture(u_texture, v_uv + offset / u_size).a);
        }
    }

    // 縁の上に元の絵を重ねる（over 合成）
    vec4 paint = edge_color();
    vec4 edge = vec4(paint.rgb, paint.a * coverage);
    vec3 rgb = base.rgb * base.a + edge.rgb * edge.a * (1.0 - base.a);
    float alpha = base.a + edge.a * (1.0 - base.a);
    frag_color = unpremul(vec4(rgb, alpha));
}
""")


_IMAGE_BLEND = _shader("""
uniform sampler2D image_file;
uniform vec2 image_file_size;
uniform float offset_x;
uniform float offset_y;
uniform float zoom;
uniform int blend;
uniform bool loop;

void main() {
    vec4 base = texture(u_texture, v_uv);
    // 画像が無い・読めないときは何もしない 絵を消すと、ファイルを
    // 動かしただけで文字が見えなくなり、何が起きたか分からない
    if (image_file_size.x < 1.0 || image_file_size.y < 1.0) {
        frag_color = base;
        return;
    }

    // 画像の**中心**を絵の中心に合わせ、X と Y でずらす
    // AviUtl2 に 200x200 の 4 色の画像を合成させると、ループ画像を切ったときに
    // 画像が文字の真ん中に 1 枚だけ出た ずらす量は画面の画素のまま（拡大率で
    // 縮まない） X=50 Y=30 拡大率 50 で、色の変わり目がちょうど 50 と 30 動いた
    vec2 screen = v_uv * u_size;
    vec2 pixel = screen - object_center() - vec2(offset_x, offset_y);
    pixel /= max(zoom, 0.0001) * 0.01;
    vec2 p = vec2(pixel.x, -pixel.y) + image_file_size * 0.5;
    vec4 picture = image_pixel(image_file, image_file_size, p, loop);

    // 合成モードの番号は AviUtl2 の一覧と同じ並び（SelectSpec の並びとも同じ）
    // 0 前方から合成 1 後方から合成 2 色情報を上書き
    // 3 輝度をアルファ値として上書き 4 輝度をアルファ値として乗算
    // 入れ物（絵の置かれた四角）の外には何も出さない こちらのテキストは画面と
    // 同じ大きさの絵で届くので、ここで切らないと画像が画面いっぱいに広がる
    bool inside = screen.x >= u_object.x && screen.x <= u_object.z
        && screen.y >= u_object.y && screen.y <= u_object.w;
    if (blend != 2 && blend != 4 && !inside) {
        frag_color = base;
        return;
    }
    if (blend == 0 || blend == 1) {
        // 画像は入れ物の四角いっぱいに出る（文字の形では切り抜かない）
        // AviUtl2 では、文字の枠 551x180 が画像で塗られ、前方は文字の上へ、
        // 後方は文字の下へ画像が来た
        frag_color = blend == 0 ? over(picture, base) : over(base, picture);
        return;
    }
    if (blend == 3 || blend == 4) {
        // 画像の明るさ（Rec.601、符号化した値で測る）を濃さにする 色は絵のまま
        // AviUtl2 で白い文字に 4 色の画像を掛けると、赤 74・緑 150・青 29 の灰色
        // （0.299・0.587・0.114 倍）、半透明（128）の白は 128 になった
        vec3 encoded = mix(picture.rgb * 12.92,
                           1.055 * pow(clamp(picture.rgb, 0.0, 1.0), vec3(1.0 / 2.4)) - 0.055,
                           step(0.0031308, picture.rgb));
        float luma = dot(encoded, vec3(0.299, 0.587, 0.114)) * picture.a;
        frag_color = vec4(base.rgb, blend == 4 ? base.a * luma : luma);
        return;
    }
    // 色情報を上書き 色は画像のもの、濃さは絵と画像の掛け算
    // AviUtl2 では、ループ画像を切ったときに画像の外の文字が消え、
    // 半透明（128）の白の所は黒の上で灰色（128）になった
    frag_color = vec4(picture.rgb, base.a * picture.a);
}
""")


_SHADOW = _shader("""
uniform float offset_x;
uniform float offset_y;
uniform float blur;
uniform float opacity;
uniform vec4 color;
uniform float zoom;
uniform float angle;

void main() {
    if (u_pass == 0) {
        // 影の形を作って横にぼかす 位置は元の絵からずらす
        // Y は正が上 変形エフェクトの pos_y と揃えてある ここだけ逆にすると、
        // 同じ「Y」という表示で上下が反対に動くことになる
        // 拡大と回転は絵の中心を支点にする（YMM4 の影と同じ）
        vec2 pixel = v_uv * u_size - object_center() - vec2(offset_x, offset_y);
        float r = radians(-angle);
        pixel = mat2(cos(r), -sin(r), sin(r), cos(r)) * pixel;
        pixel /= max(zoom, 0.0001) * 0.01;
        vec2 shifted = (pixel + object_center()) / u_size;
        vec4 shape = blur1d(u_texture, shifted, vec2(1.0, 0.0), blur);
        frag_color = vec4(color.rgb, shape.a * color.a * (opacity / 100.0));
        return;
    }

    vec4 shadow = blur1d(u_texture, v_uv, vec2(0.0, 1.0), blur);
    vec4 base = texture(u_source, v_uv);

    // 影の上に元の絵を載せる
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

    // アンシャープマスク ぼかしとの差を戻す量で鋭さが決まる
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
    // v_uv の Y は上向き 設定の Y も上向き
    vec2 centre = u_size * 0.5 + vec2(center_x, center_y);
    vec2 half_size = max(vec2(mask_width, mask_height) * 0.5, vec2(0.5));
    vec2 delta = pixel - centre;
    float edge = max(feather, 0.0001);

    float inside;
    if (shape == 0) {
        // 矩形 各辺からの距離のうち最も内側を採る
        vec2 distance = half_size - abs(delta);
        inside = min(smoothstep(0.0, edge, distance.x), smoothstep(0.0, edge, distance.y));
    } else {
        // 楕円 正規化してから半径 1 の円として測る
        float radius = length(delta / half_size);
        float scale = min(half_size.x, half_size.y);
        inside = smoothstep(0.0, edge / scale, 1.0 - radius);
    }

    if (invert) inside = 1.0 - inside;
    vec4 color = texture(u_texture, v_uv);
    frag_color = vec4(color.rgb, color.a * inside);
}
""")


_GRADIENT = _shader(
    BLEND_FUNCTIONS
    + """
uniform float strength;
uniform float center_x;
uniform float center_y;
uniform float angle;
uniform float span;
uniform int shape;
uniform int blend;
uniform vec4 start_color;
uniform vec4 end_color;

// 合成は符号化した値（sRGB）で計算する ここはリニアで持っているので往復する
vec3 to_srgb(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
}
vec3 to_linear(vec3 c) {
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c));
}

void main() {
    vec4 base = texture(u_texture, v_uv);

    // 中心を原点、右と下を正とした画素座標
    // v_uv の Y は上向きなので、ここで下向きに直す 設定の Y も上向きなので
    // 中心のずらし量も同じように反転する
    vec2 pixel = (v_uv - 0.5) * u_size;
    pixel.y = -pixel.y;
    vec2 centre = vec2(center_x, -center_y);
    float length_ = max(span, 1.0);

    float t;
    if (shape == 1) {
        // 円形 中心からの距離
        t = length(pixel - centre) / length_;
    } else {
        // 線形 角度 0 で上から下、90 で右から左（終了色の向き）
        //
        // AviUtl2 に 角度 0 と 90 のグラデーションを描かせて読み取った
        // 0 は上が開始色・下が終了色、90 は右が開始色・左が終了色だった
        // (cos, sin) のまま使うと 0 が左右、90 が上下になり、
        // 画面の端に寄せた配布物の文字が丸ごと終了色（多くは黒）で塗り潰される
        float radian = radians(angle);
        vec2 direction = vec2(-sin(radian), cos(radian));
        t = dot(pixel - centre, direction) / length_ + 0.5;
    }

    // 開始色から終了色への混ぜ方も **符号化した値（sRGB）で計算する**
    // AviUtl2 に赤から青のグラデーションを描かせると真ん中が (117, 0, 122) で、
    // 符号化した値の中点（127 付近）に当たる リニアで混ぜると真ん中が 186 の
    // 明るいマゼンタになり、配布物の中間色が全部派手になる
    //
    // 端をなだらかにしてから混ぜる（smoothstep） AviUtl2 に黒から白の階調を
    // 描かせて位置ごとの明るさを測ると、直線ではなく S 字だった
    // 直線で混ぜると、帯の両端が濃くなりすぎて中ほどが薄い別の絵になる
    float along = clamp(t, 0.0, 1.0);
    along = along * along * (3.0 - 2.0 * along);
    vec3 ramp_rgb = mix(to_srgb(start_color.rgb), to_srgb(end_color.rgb), along);
    float ramp_alpha = mix(start_color.a, end_color.a, along);
    // 元の絵の不透明度はそのまま グラデーションは色だけを塗り替える
    float amount = clamp(strength * 0.01, 0.0, 1.0) * ramp_alpha;
    // 合成の仕方は塗りと同じ関数を使う AviUtl のグラデーションは
    // 加算や乗算で重ねる使い方が多く、通常だけだと配布物の見た目が出ない
    //
    // 合成は **符号化した値（sRGB）で計算する** ここはリニアで持っているので
    // 戻してから混ぜ、最後にリニアへ直す AviUtl も YMM4 も sRGB で混ぜており、
    // リニアのまま掛けると加算や乗算の見た目が別物になる
    vec3 under = to_srgb(base.rgb);
    vec3 rgb = mix(under, blend_colors(blend, under, ramp_rgb), amount);
    frag_color = vec4(to_linear(rgb), base.a);
}
"""
)


_FILL = _shader("""
uniform vec4 color;
uniform float amount;

void main() {
    // 形はそのままに、色だけを塗る 不透明度に触ると輪郭の外まで色が出る
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
    // 1 方向にだけ伸ばす 角度 0 で横、90 で縦
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
    // 明るいところを残す 反転すると暗いところが残る
    float keep = smoothstep(edge - feather, edge + feather, luma);
    if (invert) keep = 1.0 - keep;

    frag_color = vec4(color.rgb, color.a * keep);
}
""")


def register_builtin_effects() -> None:
    """標準エフェクトを一覧へ登録する 読み込み時に 1 度だけ呼ばれる"""
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
                TrackSpec("gain", "輝度（sRGB の値に掛ける）", 0, 400, 100, unit="%"),
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
                CheckSpec("tinted", "光に色を付ける", False),
                ColorSpec("tint", "光の色", (1.0, 1.0, 1.0, 1.0)),
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
                TrackSpec("rotation_x", "X 軸回転", -3600, 3600, 0, unit="度"),
                TrackSpec("rotation_y", "Y 軸回転", -3600, 3600, 0, unit="度"),
                TrackSpec("anchor_x", "中心 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("anchor_y", "中心 Y", -4000, 4000, 0, step=1, unit="px"),
                SelectSpec(
                    "pivot_h",
                    "中心の横",
                    (
                        ("screen", "画面の中央"),
                        ("left", "絵の左端"),
                        ("right", "絵の右端"),
                        ("center", "絵の中央"),
                        ("origin", "絵の原点"),
                    ),
                    "screen",
                ),
                SelectSpec(
                    "pivot_v",
                    "中心の縦",
                    (
                        ("screen", "画面の中央"),
                        ("top", "絵の上端"),
                        ("bottom", "絵の下端"),
                        ("middle", "絵の中央"),
                        ("origin", "絵の原点"),
                    ),
                    "screen",
                ),
                CheckSpec("move_to_pivot", "中心を絵の中央の位置へ寄せる", False),
            ),
            fragment_shader=_TRANSFORM,
            # 動かさず・拡げず・回さず・寄せなければ、支点（中心と基準）をどこへ置いても
            # 元の位置へ戻る（シェーダは支点を引いてから足し戻す）
            idle_when=(
                ("pos_x", 0.0),
                ("pos_y", 0.0),
                ("scale", 100.0),
                ("scale_y", 100.0),
                ("rotation", 0.0),
                ("rotation_x", 0.0),
                ("rotation_y", 0.0),
                ("move_to_pivot", False),
            ),
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
                FileSpec("pattern", "模様の画像", filter=IMAGE_FILTER, texture=True),
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
                SelectSpec("blend", "合成", BLEND_MODES, "normal"),
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
                TrackSpec("zoom", "拡大率", 0, 1000, 100, unit="%"),
                TrackSpec("angle", "回転", -3600, 3600, 0, unit="度"),
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

    registry.register(
        EffectDefinition(
            kind="image_blend",
            label="画像合成",
            category="合成",
            parameters=(
                FileSpec("image_file", "画像", filter=IMAGE_FILTER, texture=True),
                TrackSpec("offset_x", "X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("offset_y", "Y", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("zoom", "拡大率", 0, 1000, 100, unit="%"),
                # 並びは AviUtl2 v2.1.6a の一覧と同じ シェーダはこの番号で分ける
                SelectSpec(
                    "blend",
                    "合成",
                    (
                        ("front", "前方から合成"),
                        ("back", "後方から合成"),
                        ("overwrite", "色情報を上書き"),
                        ("luma_alpha", "輝度をアルファ値として上書き"),
                        ("luma_multiply", "輝度をアルファ値として乗算"),
                    ),
                    "overwrite",
                ),
                CheckSpec("loop", "画像を敷き詰める", True),
            ),
            fragment_shader=_IMAGE_BLEND,
        )
    )


register_builtin_effects()
