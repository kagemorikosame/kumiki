"""絵を歪めるエフェクト AviUtl の変形まわりの効果に当たるもの

値の意味は **AviUtl2 に描かせた絵を測って**決めた（推測していない）
測り方は ``tools/aviutl_compare.py`` の見本を作り、``田田田`` と並べた大きな文字に
効果を 1 つだけ掛けて、どこがどう動いたかを読む
（一色の面に掛けると、変形しても絵が変わらず何も読めない）

シェーダはリニア空間・ストレートアルファで受け取り、同じ形で返す
"""

from __future__ import annotations

from kumiki.effects.builtin import PRELUDE
from kumiki.effects.definition import EffectDefinition, registry
from kumiki.effects.spec import CheckSpec, SelectSpec, TrackSpec

__all__ = ["register_warp_effects"]


def _shader(body: str) -> str:
    return PRELUDE + body


_EXPAND = _shader("""
uniform float top;
uniform float bottom;
uniform float left;
uniform float right;

void main() {
    // AviUtl の 領域拡張 はオブジェクトの入れ物を四方へ広げる
    // 絵は入れ物の真ん中に置かれたままなので、**片側だけ広げると絵が半分ずれる**
    //
    // AviUtl2 に 上=200 を描かせると絵が下へ 100、右=300 で左へ 150 動いた
    // 入れ物の大きさそのものは、こちらでは初めから画面より広く取ってあるので、
    // 見た目に残るのはこのずれだけ
    //
    // 画素の Y は上が正 下へ動かすには負を足す
    vec2 shift = vec2(-(right - left), -(top - bottom)) * 0.5;
    frag_color = sample_pixel(v_uv * u_size - shift);
}
""")


_MIRROR = _shader("""
uniform int side;
uniform float opacity;
uniform float falloff;
uniform float gap;

void main() {
    vec2 pixel = v_uv * u_size;
    vec4 base = texture(u_texture, v_uv);

    // 折り返す線は絵の置かれた範囲（u_object）の縁
    //
    // AviUtl の線はこれより 20px ほど外にある（文字の採寸の違い 実測で鏡像の
    // 位置が 36px ずれる＝差 6.0） 絵の中身の範囲は角丸や中心基準の動きも
    // 使っているので、ここだけのために意味を変えない
    //
    // 境目調整 は線をさらに外へ動かす 鏡像はその倍だけ離れる
    // （実測 40 で 80px 離れた）
    float line_;
    float away;      // 線から外への距離（正なら鏡像の側）
    if (side == 0) {            // 下側
        line_ = u_object.y - gap;
        away = line_ - pixel.y;
    } else if (side == 1) {     // 上側
        line_ = u_object.w + gap;
        away = pixel.y - line_;
    } else if (side == 2) {     // 左側
        line_ = u_object.x - gap;
        away = line_ - pixel.x;
    } else {                    // 右側
        line_ = u_object.z + gap;
        away = pixel.x - line_;
    }

    if (away <= 0.0) {
        frag_color = base;
        return;
    }

    vec2 mirrored = pixel;
    if (side == 0 || side == 1) {
        mirrored.y = 2.0 * line_ - pixel.y;
    } else {
        mirrored.x = 2.0 * line_ - pixel.x;
    }
    vec4 image = sample_pixel(mirrored);

    // 減衰 は線から離れるほど薄くする 0 なら薄くならない
    float span = max((side < 2 ? object_size().y : object_size().x), 1.0);
    float fade = clamp(1.0 - (away / span) * (falloff * 0.01), 0.0, 1.0);
    image.a *= fade * clamp(1.0 - opacity * 0.01, 0.0, 1.0);
    frag_color = over(base, image);
}
""")


_DISPLACEMENT = _shader("""
uniform float size;
uniform float blur;
uniform float move_x;
uniform float move_y;
uniform int map_kind;

// 歪ませる元になる形 AviUtl の マップの種類 に当たる
// 真ん中で 1、縁で 0 になる値を返す
float edge_of(float far, float radius) {
    // ぼかし 0 は本当にぼかさない 下限を 1px にすると、0 が 1px のぼかしになり
    // 「ぼかさない」を選べなくなる
    if (blur <= 0.0) {
        return far <= radius ? 1.0 : 0.0;
    }
    return 1.0 - smoothstep(radius - blur, radius, far);
}

float map_at(vec2 offset, float radius) {
    if (map_kind == 1) {        // 四角
        return edge_of(max(abs(offset.x), abs(offset.y)), radius);
    }
    if (map_kind == 2) {        // 横（横に伸びた帯 上下で切れる）
        return edge_of(abs(offset.y), radius);
    }
    if (map_kind == 3) {        // 縦（縦に伸びた帯 左右で切れる）
        return edge_of(abs(offset.x), radius);
    }
    return edge_of(length(offset), radius);  // 円
}

void main() {
    vec2 pixel = v_uv * u_size;
    vec2 offset = pixel - object_center();
    float radius = max(size, 1.0) * 0.5;
    float amount = map_at(offset, radius);

    // 変形X と 変形Y のぶん、マップの濃い所ほど大きくずらす
    // 画素の Y は上が正 AviUtl の Y は下が正なので、呼ぶ側で向きを直してある
    vec2 shift = vec2(move_x, move_y) * amount;
    frag_color = sample_pixel(pixel - shift);
}
""")


#: ミラーの向き AviUtl2 は名前で書く（``ミラーの方向=下側``）
_SIDES = (("bottom", "下側"), ("top", "上側"), ("left", "左側"), ("right", "右側"))

#: ディスプレイスメントマップの形 AviUtl2 の ``マップの種類``
#:
#: **円だけが実物と突き合わせて確かめたもの** AviUtl2 に 四角・横・縦 を描かせて
#: 比べたところ、**3 つとも 1 画素も違わず**、円との差も縁の 576 画素だけだった
#: （この見本では形の違いが絵に出ない）
#: そのため 四角・横・縦 の形はこちらの読み方で、実物の写しではない
_MAP_KINDS = (("circle", "円"), ("rect", "四角"), ("horizontal", "横"), ("vertical", "縦"))


_KALEIDOSCOPE = _shader("""
uniform float center_x;
uniform float center_y;
uniform float span;
uniform float angle;
uniform float corners;
uniform float repeats;
uniform float fixed_size;
uniform bool circle_mask;
uniform bool clip_outside;

void main() {
    vec2 origin = object_center();
    vec2 p = v_uv * u_size - origin;

    // 角数は偶数に丸める 鏡を交互に返して 1 周させるので、奇数では継ぎ目が合わない
    float n = max(2.0, floor(corners * 0.5 + 0.5) * 2.0);
    float half_ = PI / n;
    float length_ = max(span, 1.0);
    // 覆う範囲は、鏡の三角を 繰り返し回数 + 1 段ぶん並べた正多角形
    // 実測で 繰り返し 2・長さ 100 の端が 300、3・200 が 800 の辺に乗った
    float outer = (max(floor(repeats + 0.5), 1.0) + 1.0) * length_;
    // 固定サイズ は覆う範囲の差し渡しをその大きさへ縮める（実測 200 で半分）
    float zoom = fixed_size > 0.0 ? fixed_size / (2.0 * outer) : 1.0;
    p /= zoom;

    // 鏡の軸は真下 三角の頂点は軸から ±180/角数 の線の上に乗る
    // 田の縦棒が軸に沿って残り、横棒は鏡の線に消えることから軸が縦だと読んだ
    // 上か下かは、田の上半分と下半分の違いで決めた（真上だと差が 8.0、真下で 3.3）
    const float AXIS = -PI * 0.5;

    // 範囲の外は描かない 角を鏡の線へ畳んでから、軸へ下ろした長さで測る
    float phi = atan(p.y, p.x) - AXIS;
    float folded = abs(mod(phi + half_, 4.0 * half_) - 2.0 * half_) - half_;
    float along = length(p) * cos(folded);
    float apothem = outer * cos(half_);
    if (circle_mask ? length(p) > apothem : along > apothem) {
        frag_color = vec4(0.0);
        return;
    }

    // 畳む 角を 1 つの三角へ折り返し、三角の底辺を越えたら底辺で折り返す
    // 繰り返せば、鏡を三角に組んだ万華鏡と同じ模様になる
    float base = length_ * cos(half_);
    for (int i = 0; i < 256; ++i) {
        float radius = length(p);
        // 中心ちょうどは角が決まらない（atan(0, 0) は実装しだいで NaN になる）
        if (radius < 0.0001) break;
        phi = atan(p.y, p.x) - AXIS;
        folded = abs(mod(phi + half_, 4.0 * half_) - 2.0 * half_) - half_;
        p = radius * vec2(cos(AXIS + folded), sin(AXIS + folded));
        // 軸が真下なので、軸に沿った長さは -p.y
        if (-p.y <= base) break;
        p.y = -2.0 * base - p.y;
    }

    // 回転 は元の絵を鏡の下で回す 模様の形（範囲・鏡の向き）は変わらない
    // 回した見本はまだ無く、万華鏡を筒ごと回すのではなく中の絵を回す道具だという
    // 読み方で決めた 実物と比べて違えば、ここを模様ごと回す形へ直す
    float turn = radians(angle);
    float cs = cos(turn);
    float sn = sin(turn);
    p = vec2(p.x * cs + p.y * sn, -p.x * sn + p.y * cs);

    // 中心 は読む所をずらし、模様はオブジェクトの真ん中に置いたままにする
    // ずらした見本はまだ無く、模様の置き場所が動くなら origin の側をずらす
    vec2 source = origin + vec2(center_x, center_y) + p;
    if (!clip_outside) {
        // 領域外を透過 を外すと、絵の外は縁の色を引き伸ばして埋める
        // 田の字の見本は縁が透明なので、入れても外しても同じ絵だった（差も同じ 3.3）
        // 透明にすると 外す 意味が無くなるので、縁を伸ばす側に読んだ
        source = clamp(source, u_object.xy + 0.5, u_object.zw - 0.5);
    }
    frag_color = sample_pixel(source);
}
""")


def register_warp_effects() -> None:
    definitions = (
        EffectDefinition(
            kind="expand_area",
            label="領域拡張",
            category="変形",
            parameters=(
                TrackSpec("top", "上", 0, 4000, 0, step=1, unit="px"),
                TrackSpec("bottom", "下", 0, 4000, 0, step=1, unit="px"),
                TrackSpec("left", "左", 0, 4000, 0, step=1, unit="px"),
                TrackSpec("right", "右", 0, 4000, 0, step=1, unit="px"),
            ),
            fragment_shader=_EXPAND,
            expands_object=("top", "bottom", "left", "right"),
        ),
        EffectDefinition(
            kind="mirror",
            label="ミラー",
            category="変形",
            parameters=(
                SelectSpec("side", "映す向き", _SIDES, "bottom"),
                TrackSpec("opacity", "透明度", 0, 100, 0, unit="%"),
                TrackSpec("falloff", "減衰", 0, 100, 0, unit="%"),
                TrackSpec("gap", "境目調整", -2000, 2000, 0, step=1, unit="px"),
            ),
            fragment_shader=_MIRROR,
        ),
        EffectDefinition(
            kind="displacement_map",
            label="ディスプレイスメントマップ",
            category="変形",
            parameters=(
                TrackSpec("size", "サイズ", 1, 8000, 200, step=1, unit="px"),
                TrackSpec("move_x", "変形 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("move_y", "変形 Y", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("blur", "ぼかし", 0, 500, 5, step=1, unit="px"),
                SelectSpec("map_kind", "マップの種類", _MAP_KINDS, "circle"),
            ),
            fragment_shader=_DISPLACEMENT,
        ),
        EffectDefinition(
            kind="kaleidoscope",
            label="万華鏡",
            category="変形",
            parameters=(
                TrackSpec("center_x", "中心 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("center_y", "中心 Y", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("span", "長さ", 1, 4000, 100, step=1, unit="px"),
                TrackSpec("angle", "回転", -3600, 3600, 0, unit="度"),
                TrackSpec("corners", "角数", 2, 64, 6, step=2),
                TrackSpec("repeats", "繰り返し回数", 1, 32, 1, step=1),
                TrackSpec("fixed_size", "固定サイズ", 0, 8000, 0, step=1, unit="px"),
                CheckSpec("circle_mask", "円形マスク", False),
                CheckSpec("clip_outside", "領域外を透過", False),
            ),
            fragment_shader=_KALEIDOSCOPE,
        ),
    )
    for definition in definitions:
        registry.register(definition)
