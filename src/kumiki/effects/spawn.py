"""絵を分けて別々に動かすエフェクト AviUtl の分身まわりの効果に当たるもの

値の意味は **AviUtl2 に描かせた絵を測って**決めた（推測していない）
測り方は ``tools/aviutl_compare.py`` の見本を作り、``田田田`` と並べた大きな文字に
効果を積んで、どこがどう動いたかを読む

AviUtl は ``オブジェクト分割`` で碁盤の目に切ってから、``座標の拡大縮小(個別
オブジェクト)`` などで 1 マスずつ動かす 分割は単体では絵を変えない（実測でも
分割だけの絵は元と同じ）ので、こちらは**マスの数を動かす側が持つ**
写すときに分割の数を拾って渡す（:mod:`kumiki.compat.aviutl.mapping`）

シェーダはリニア空間・ストレートアルファで受け取り、同じ形で返す
"""

from __future__ import annotations

from kumiki.effects.builtin import PRELUDE
from kumiki.effects.definition import EffectDefinition, registry
from kumiki.effects.spec import CheckSpec, TrackSpec

__all__ = ["register_spawn_effects"]


def _shader(body: str) -> str:
    return PRELUDE + body


#: 碁盤の目の 1 マスを割り出す ``columns`` ``rows`` は 1 以上
_CELLS = """
uniform float columns;
uniform float rows;
uniform float center_x;
uniform float center_y;

//: 絵の置かれた範囲を碁盤の目に切り、``pixel`` の入るマスの左下と大きさを返す
void cell_of(vec2 pixel, out vec2 origin, out vec2 span) {
    vec2 low = u_object.xy;
    span = object_size() / vec2(max(floor(columns), 1.0), max(floor(rows), 1.0));
    vec2 index = floor((pixel - low) / span);
    origin = low + index * span;
}
"""


_SPLIT_ZOOM = _shader(
    _CELLS
    + """
uniform float zoom;

void main() {
    vec2 pixel = v_uv * u_size;
    vec2 origin;
    vec2 span;
    cell_of(pixel, origin, span);

    // 1 マスずつ、マスの真ん中を軸に縮める（拡大率 100 で元のまま）
    // 中心X と 中心Y は軸のずらし 画素の Y は上が正
    //
    // AviUtl2 で 中心X=100 と 拡大率 50 を描かせると絵が 50 ずれた
    // ＝ ずらした軸で縮めたぶん（100 x (1 - 0.5)）
    float scale = max(zoom * 0.01, 1e-4);
    vec2 pivot = origin + span * 0.5 + vec2(center_x, -center_y);
    vec2 source = pivot + (pixel - pivot) / scale;

    // 引く先が隣のマスへ出たら何も描かない
    // 出た先をそのまま読むと、縮めた隙間に隣のマスの絵が覗く
    if (any(lessThan(source, origin)) || any(greaterThan(source, origin + span))) {
        frag_color = vec4(0.0);
        return;
    }
    frag_color = sample_pixel(source);
}
"""
)


_SPLIT_ROTATE = _shader(
    _CELLS
    + """
uniform float angle;

void main() {
    vec2 pixel = v_uv * u_size;
    vec2 origin;
    vec2 span;
    cell_of(pixel, origin, span);

    // 1 マスずつ、マスの真ん中を軸に回す
    float turn = radians(-angle);      // 画面では時計回りが正
    float cs = cos(turn);
    float sn = sin(turn);
    vec2 pivot = origin + span * 0.5 + vec2(center_x, -center_y);
    vec2 offset = pixel - pivot;
    vec2 source = pivot + vec2(offset.x * cs - offset.y * sn, offset.x * sn + offset.y * cs);

    if (any(lessThan(source, origin)) || any(greaterThan(source, origin + span))) {
        frag_color = vec4(0.0);
        return;
    }
    frag_color = sample_pixel(source);
}
"""
)


_SCATTER = _shader("""
uniform float count;
uniform float span;
uniform float angle;
uniform float spread;
uniform bool random_angle;

//: 番号から 0..1 の数を 2 つ作る 毎フレーム同じ並びになるよう、時間を混ぜない
//
// ``sin`` を 2 回呼んで並べるだけだと、2 つの数が揃ってしまい、
// 斜めの線の上にしか散らばらない（実測で縦の広がりが 4 分の 3 しか出なかった）
// 3 つの値を混ぜ合わせてから取り出す
vec2 noise_at(float index) {
    vec3 seed = fract(vec3(index + 1.0) * vec3(0.1031, 0.1030, 0.0973));
    seed += dot(seed, seed.yzx + 33.33);
    return fract((seed.xx + seed.yz) * seed.zy);
}

void main() {
    vec2 pixel = v_uv * u_size;
    vec2 centre = object_center();
    int times = int(clamp(floor(count), 1.0, 256.0));

    vec4 stacked = vec4(0.0);
    for (int i = 0; i < 256; ++i) {
        if (i >= times) break;
        vec2 dice = noise_at(float(i));
        vec2 offset = (dice - 0.5) * span;
        // 拡散 は真ん中から外へ寄せる量 0 なら一様に散らす
        offset *= 1.0 + spread * 0.01 * length(dice - 0.5) * 2.0;

        float turn = radians(-angle);
        if (random_angle) {
            turn = radians(-angle) * (dice.x * 2.0 - 1.0);
        }
        float cs = cos(turn);
        float sn = sin(turn);
        vec2 local = pixel - centre - vec2(offset.x, -offset.y);
        vec2 source = centre + vec2(local.x * cs - local.y * sn, local.x * sn + local.y * cs);
        // 手前に置いた写しほど上 後ろから重ねる
        stacked = over(stacked, sample_pixel(source));
    }
    frag_color = stacked;
}
""")


def register_spawn_effects() -> None:
    cells = (
        TrackSpec("columns", "横の分割数", 1, 256, 1, step=1),
        TrackSpec("rows", "縦の分割数", 1, 256, 1, step=1),
        TrackSpec("center_x", "中心 X", -4000, 4000, 0, step=1, unit="px"),
        TrackSpec("center_y", "中心 Y", -4000, 4000, 0, step=1, unit="px"),
    )
    definitions = (
        EffectDefinition(
            kind="split_zoom",
            label="個別オブジェクトの拡大",
            category="変形",
            parameters=(TrackSpec("zoom", "拡大率", 0, 1000, 100, unit="%"), *cells),
            fragment_shader=_SPLIT_ZOOM,
        ),
        EffectDefinition(
            kind="split_rotate",
            label="個別オブジェクトの回転",
            category="変形",
            parameters=(TrackSpec("angle", "角度", -3600, 3600, 0, unit="度"), *cells),
            fragment_shader=_SPLIT_ROTATE,
        ),
        EffectDefinition(
            kind="scatter",
            label="ランダム配置",
            category="変形",
            parameters=(
                TrackSpec("count", "数", 1, 256, 8, step=1),
                TrackSpec("span", "範囲", 0, 8000, 400, step=1, unit="px"),
                TrackSpec("angle", "回転", -3600, 3600, 0, unit="度"),
                TrackSpec("spread", "拡散", 0, 400, 0, unit="%"),
                CheckSpec("random_angle", "ランダム角度", False),
            ),
            fragment_shader=_SCATTER,
        ),
    )
    for definition in definitions:
        registry.register(definition)
