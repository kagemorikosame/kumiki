"""絵を歪めるエフェクト AviUtl の変形まわりの効果に当たるもの

値の意味は **AviUtl2 に描かせた絵を測って**決めた（推測していない）
測り方は ``tools/aviutl_compare.py`` の見本を作り、``田田田`` と並べた大きな文字に
効果を 1 つだけ掛けて、どこがどう動いたかを読む
（一色の面に掛けると、変形しても絵が変わらず何も読めない）

シェーダはリニア空間・ストレートアルファで受け取り、同じ形で返す
"""

from __future__ import annotations

from sashimono.effects.builtin import PRELUDE
from sashimono.effects.definition import EffectDefinition, registry
from sashimono.effects.spec import SelectSpec, TrackSpec

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
    )
    for definition in definitions:
        registry.register(definition)
