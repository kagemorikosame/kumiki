"""範囲を決めて掛けるエフェクト 部分モザイク・ぼかしと部分フィルタ（Issue #27）

顔や画面の一部を隠す用途のためのもの 範囲は四角か楕円で、位置・幅・高さ・回転・
境目のなじませ幅・内外の反転をどれもキーフレームで動かせる

- 部分モザイク・ぼかし（``region_blur``） 範囲の中だけにモザイクかぼかしを掛ける
  1 つ積めば済む 隠す用途で一番よく使う形なので、部分フィルタと 2 つ積ませない
- 部分フィルタ（``partial_filter``） 後ろに積んだエフェクトを範囲の中だけに効かせる
  AviUtl の拡張編集の 部分フィルタ と同じ考え方 色調補正や反転なども範囲へ絞れる
  次の部分フィルタか、エフェクトの並びの終わりまでが 1 つの範囲

位置は絵の原点（素材や図形なら絵の中央）から数える フィルタのクリップに積むと
原点は画面の中央なので、画面（プロジェクトの解像度）の中央から右と上が正になる
Y を上が正にするのは、ほかの座標（テキスト・変形・マスク）と向きをそろえるため

範囲の形と式は 2 つで同じ物（:data:`REGION_GLSL`）を使う 別々に書くと、同じ値を
入れたのに部分フィルタとモザイクで範囲がずれる
"""

from __future__ import annotations

from sashimono.effects.builtin import PRELUDE
from sashimono.effects.definition import EffectDefinition, registry
from sashimono.effects.spec import CheckSpec, ParameterSpec, SelectSpec, TrackSpec

__all__ = ["PARTIAL_FILTER", "REGION_BLUR", "REGION_GLSL", "register_region_effects"]

#: 部分フィルタの種別 エンジンがこの種別の後ろを範囲の中だけに効かせる
PARTIAL_FILTER = "partial_filter"
REGION_BLUR = "region_blur"

#: 範囲の項目 2 つのエフェクトで同じ並び・同じ名前にする
#: 名前がそろっていれば、片方で決めた範囲をもう片方へ写すときに読み替えが要らない
_REGION_PARAMETERS: tuple[ParameterSpec, ...] = (
    SelectSpec("shape", "形", (("rect", "四角"), ("ellipse", "円（楕円）")), "rect"),
    TrackSpec("center_x", "中心 X（中央から 右が正）", -8000, 8000, 0, step=1, unit="px"),
    TrackSpec("center_y", "中心 Y（中央から 上が正）", -8000, 8000, 0, step=1, unit="px"),
    TrackSpec("region_width", "幅", 0, 16000, 400, step=1, unit="px"),
    TrackSpec("region_height", "高さ", 0, 16000, 300, step=1, unit="px"),
    TrackSpec("rotation", "回転", -3600, 3600, 0, unit="度"),
    TrackSpec("feather", "境目のぼかし", 0, 1000, 0, step=1, unit="px"),
    CheckSpec("invert", "範囲の外側に掛ける", False),
)

#: 範囲の式 ``region_coverage`` は画素（Y は上が正）が範囲にどれだけ入るかを 0..1 で返す
REGION_GLSL = """
uniform int shape;
uniform float center_x;
uniform float center_y;
uniform float region_width;
uniform float region_height;
uniform float rotation;
uniform float feather;
uniform bool invert;

vec2 region_center() { return object_origin() + vec2(center_x, center_y); }

// 範囲を回す前の向きへ戻した、中心からの位置 回転は変形（transform）と同じく
// 正で時計回り 逆にすると、同じ角度を入れた絵と範囲が逆へ傾く
vec2 region_local(vec2 pixel) {
    vec2 delta = pixel - region_center();
    float angle = radians(rotation);
    float c = cos(angle);
    float s = sin(angle);
    return vec2(c * delta.x - s * delta.y, s * delta.x + c * delta.y);
}

float region_coverage(vec2 pixel) {
    vec2 local = region_local(pixel);
    vec2 half_size = max(vec2(region_width, region_height) * 0.5, vec2(0.5));
    // なじませる幅は境目から内側へ取る 外へ広げると、隠したい所の縁が
    // 範囲の外まで薄く漏れ、決めた大きさより広く掛かって見える
    float edge = max(feather, 0.0001);
    float inside;
    if (shape == 0) {
        vec2 distance = half_size - abs(local);
        inside = min(smoothstep(0.0, edge, distance.x), smoothstep(0.0, edge, distance.y));
    } else {
        // 楕円は半径 1 の円に直して測る なじませる幅は短い方の半径で割って揃える
        float radius = length(local / half_size);
        float scale = min(half_size.x, half_size.y);
        inside = smoothstep(0.0, edge / scale, 1.0 - radius);
    }
    return invert ? 1.0 - inside : inside;
}

// 掛ける前（before）と掛けた後（after）を範囲で混ぜる 事前乗算で混ぜないと、
// 透明な所の色（多くは黒）が境目に混ざって縁が黒ずむ
vec4 region_mix(vec4 before, vec4 after, float amount) {
    return unpremul(mix(premul(before), premul(after), clamp(amount, 0.0, 1.0)));
}
"""


def _shader(body: str) -> str:
    return PRELUDE + REGION_GLSL + body


_REGION_BLUR = _shader("""
uniform int mode;
uniform float mosaic_size;
uniform float blur_radius;

void main() {
    if (mode == 1) {
        // ぼかしは横と縦に分けて畳む 1 回目は横だけ、2 回目で縦に畳んでから範囲で混ぜる
        // 画面全体を畳むのは、範囲の境目の内側でも外の色を拾ってなめらかにつなぐため
        if (u_pass == 0) {
            frag_color = blur1d(u_texture, v_uv, vec2(1.0, 0.0), blur_radius);
            return;
        }
        vec4 blurred = blur1d(u_texture, v_uv, vec2(0.0, 1.0), blur_radius);
        frag_color = region_mix(texture(u_source, v_uv), blurred, region_coverage(v_uv * u_size));
        return;
    }
    // モザイクは 1 回で足りる 1 回目は素通しにして、2 回目で元の絵（u_source）から作る
    if (u_pass == 0) {
        frag_color = texture(u_texture, v_uv);
        return;
    }
    vec2 pixel = v_uv * u_size;
    vec4 original = texture(u_source, v_uv);
    float amount = region_coverage(pixel);
    // 範囲の外は粒を作らない 画面の大半が範囲の外なので、ここで返すと粒を平らす手間が
    // 範囲の中だけで済む
    if (amount <= 0.0) {
        frag_color = original;
        return;
    }
    // 升目は範囲の中心に合わせる 画面の左下に合わせると、範囲を動かしたときに
    // 升目が範囲の中で滑り、隠している顔の上で粒がちらつく
    float block = max(mosaic_size, 1.0);
    vec2 anchor = region_center();
    vec2 corner = floor((pixel - anchor) / block) * block + anchor;
    // 粒の色は升目の中の平均 真ん中の 1 点だけを採ると、細い線（字や髪）がその点に
    // 掛かるかどうかで粒ごと消えたり残ったりし、動画では粒がちらつく
    // 読む点は縦横 8 つまで 線形補間で読むので 1 点が周りの 4 画素を平らす
    int taps = int(clamp(ceil(block / 2.0), 1.0, 8.0));
    float spacing = block / float(taps);
    vec4 sum = vec4(0.0);
    for (int y = 0; y < taps; ++y) {
        for (int x = 0; x < taps; ++x) {
            vec2 at = corner + (vec2(float(x), float(y)) + 0.5) * spacing;
            sum += premul(texture(u_source, clamp(at / u_size, vec2(0.0), vec2(1.0))));
        }
    }
    vec4 tiled = unpremul(sum / float(taps * taps));
    frag_color = region_mix(original, tiled, amount);
}
""")


#: 部分フィルタの閉じ方 ``u_source`` に部分フィルタへ来たときの絵、``u_texture`` に
#: 後ろのエフェクトを掛け終えた絵が入る（:class:`~sashimono.engine.gpu.EffectProcessor`）
_PARTIAL_FILTER = _shader("""
void main() {
    vec4 before = texture(u_source, v_uv);
    vec4 after = texture(u_texture, v_uv);
    frag_color = region_mix(before, after, region_coverage(v_uv * u_size));
}
""")


def register_region_effects() -> None:
    """範囲を決めて掛けるエフェクトを一覧へ登録する 何度呼んでも 1 回だけ"""
    if REGION_BLUR in registry:
        return

    registry.register(
        EffectDefinition(
            kind=REGION_BLUR,
            label="部分モザイク・ぼかし",
            category="ぼかし",
            parameters=(
                SelectSpec(
                    "mode", "掛け方", (("mosaic", "モザイク"), ("blur", "ぼかし")), "mosaic"
                ),
                TrackSpec("mosaic_size", "モザイクの粒の大きさ", 1, 400, 24, step=1, unit="px"),
                TrackSpec("blur_radius", "ぼかしの強さ", 0, 96, 24, unit="px"),
                *_REGION_PARAMETERS,
            ),
            fragment_shader=_REGION_BLUR,
            passes=2,
        )
    )
    registry.register(
        EffectDefinition(
            kind=PARTIAL_FILTER,
            label="部分フィルタ（後ろのエフェクトを範囲だけに掛ける）",
            category="合成",
            parameters=_REGION_PARAMETERS,
            fragment_shader=_PARTIAL_FILTER,
            scopes_following=True,
        )
    )
