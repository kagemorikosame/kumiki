"""絵を分けて別々に動かすエフェクト AviUtl の分身まわりの効果に当たるもの

値の意味は **AviUtl2 に描かせた絵を測って**決めた（推測していない）
測り方は ``tools/aviutl_compare.py`` の見本を作り、``田田田`` と並べた大きな文字に
効果を積んで、どこがどう動いたかを読む

碁盤の目に切って 1 マスずつ動かすもの（``座標の拡大縮小(個別オブジェクト)`` と
``座標の回転(個別オブジェクト)``）は、まだ写し方が分かっていない
AviUtl2 の絵は**切った断片が中心へ詰まった塊**になるのに対し、
マスの真ん中を軸に縮める読み方では断片が散ったままになる
位置そのものも動いているらしく、測り直しが要る（GitHub の Issue で追う）

シェーダはリニア空間・ストレートアルファで受け取り、同じ形で返す
"""

from __future__ import annotations

from kumiki.effects.builtin import PRELUDE
from kumiki.effects.definition import EffectDefinition, registry
from kumiki.effects.spec import CheckSpec, TrackSpec

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
    )
    for definition in definitions:
        registry.register(definition)
