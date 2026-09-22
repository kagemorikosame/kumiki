"""絵を分けて別々に動かすエフェクト AviUtl の分身まわりの効果に当たるもの

値の意味は **AviUtl2 に描かせた絵を測って**決めた（推測していない）
測り方は ``tools/aviutl_compare.py`` の見本を作り、``田田田`` と並べた大きな文字に
効果を積んで、どこがどう動いたかを読む

碁盤の目に切って 1 マスずつ動かすもの（``座標の拡大縮小(個別オブジェクト)`` と
``座標の回転(個別オブジェクト)``）は、**マスの位置だけ**を軸のまわりで動かす
中身は縮めも回しもしない 2x1 を 90 度回すと左半分が真上へ行き、文字は立ったまま、
3x3 を 50% にすると断片が中心へ詰まって重なる（Issue #42）
マスの真ん中を軸に中身を縮める読み方では、断片が元の場所に散ったままになる

シェーダはリニア空間・ストレートアルファで受け取り、同じ形で返す
"""

from __future__ import annotations

from sashimono.effects.builtin import PRELUDE
from sashimono.effects.definition import EffectDefinition, registry
from sashimono.effects.spec import CheckSpec, TrackSpec

__all__ = ["register_spawn_effects"]


def _shader(body: str) -> str:
    return PRELUDE + body


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

        // ランダム角度 の向きは**位置とは別の乱数**で決める
        // 位置に使った dice.x を流用すると、右へ置いた写しほど一方向、
        // 左へ置いた写しほど逆向きに傾いて、位置と角度が連動する
        float turn = radians(-angle);
        if (random_angle) {
            turn *= noise_at(float(i) + 512.0).x * 2.0 - 1.0;
        }
        float cs = cos(turn);
        float sn = sin(turn);
        vec2 local = pixel - centre - vec2(offset.x, -offset.y);
        vec2 source = centre + vec2(local.x * cs - local.y * sn, local.x * sn + local.y * cs);
        // 後から撒いた写しほど手前 AviUtl も並べた順に描く
        // 引数を逆にすると最初の 1 枚がいつも手前になり、
        // 透ける絵を撒いたときの重なり方が変わる
        stacked = over(sample_pixel(source), stacked);
    }
    frag_color = stacked;
}
""")


_PIECES = _shader("""
uniform float columns;
uniform float rows;
uniform float scale;
uniform float angle;
uniform float center_x;
uniform float center_y;
uniform float offset_x;
uniform float offset_y;

void main() {
    vec2 pixel = v_uv * u_size;
    vec2 low = u_object.xy;
    vec2 high = u_object.zw;
    vec2 counts = vec2(max(floor(columns + 0.5), 1.0), max(floor(rows + 0.5), 1.0));
    vec2 cell = (high - low) / counts;
    vec2 pivot = object_center() + vec2(center_x, center_y);

    // マスの**位置だけ**を軸のまわりで拡大・回転する 中身は縮めも回しもしない
    // 実測で 2x1 を 90 度回すと、左半分が真上へ動いて文字は立ったままだった
    // 回転は AviUtl と同じく正で時計回り（Y は上が正なので符号が逆に見える）
    float s = max(scale * 0.01, 0.0001);
    float turn = radians(angle);
    mat2 spin = mat2(cos(turn), -sin(turn), sin(turn), cos(turn));
    mat2 move = spin * s;
    mat2 back = inverse(move);

    // 画素がどのマスから来たかは、マスの真ん中を戻した位置の近くにしか無い
    // 全部のマスを回すと 64x64 に分けたときに 1 画素 4096 回読むことになる
    // ずらし は全部のマスを同じだけ動かす 軸の違う拡大と回転を続けて積んだものを
    // 1 つにまとめると、拡大・回転のほかに平行移動が残る（写す側の _merge_pieces）
    vec2 shift = vec2(offset_x, offset_y);
    vec2 guess = pivot + back * (pixel - shift - pivot);
    float reach = length(cell) * 0.5 / s + 1.0;
    ivec2 first = ivec2(clamp(floor((guess - reach - low) / cell), vec2(0.0), counts - 1.0));
    ivec2 last = ivec2(clamp(floor((guess + reach - low) / cell), vec2(0.0), counts - 1.0));

    vec4 stacked = vec4(0.0);
    // AviUtl は上の段の左から分けた順に描き、後のマスほど手前に来る
    // ここでは手前（下の段の右）から奥へ読み、下に敷いていく
    // 手前から読めば、重なりが不透明になった所で奥のマスを読まずに済む
    // （縮めて断片が重なるほど、1 画素で読むマスが増える）
    for (int row = first.y; row <= last.y; ++row) {
        for (int column = last.x; column >= first.x; --column) {
            vec2 corner = low + vec2(column, row) * cell;
            vec2 middle = corner + cell * 0.5;
            vec2 source = pixel - (pivot + move * (middle - pivot) + shift - middle);
            vec2 inside = source - corner;
            if (inside.x < 0.0 || inside.y < 0.0 || inside.x >= cell.x || inside.y >= cell.y) {
                continue;
            }
            stacked = over(stacked, sample_pixel(source));
            if (stacked.a >= 0.999) {
                frag_color = stacked;
                return;
            }
        }
    }
    frag_color = stacked;
}
""")


def register_spawn_effects() -> None:
    definitions = (
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
        EffectDefinition(
            kind="split_pieces",
            label="分割して並べ直す",
            category="変形",
            parameters=(
                TrackSpec("columns", "横分割数", 1, 64, 1, step=1),
                TrackSpec("rows", "縦分割数", 1, 64, 1, step=1),
                TrackSpec("scale", "間隔の拡大率", 0, 5000, 100, unit="%"),
                TrackSpec("angle", "位置の回転", -3600, 3600, 0, unit="度"),
                TrackSpec("center_x", "中心 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("center_y", "中心 Y", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("offset_x", "ずらし X", -8000, 8000, 0, step=1, unit="px"),
                TrackSpec("offset_y", "ずらし Y", -8000, 8000, 0, step=1, unit="px"),
            ),
            fragment_shader=_PIECES,
        ),
    )
    for definition in definitions:
        registry.register(definition)
