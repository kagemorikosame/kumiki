"""動きのエフェクト 揺らす・繰り返す・登場と退場・歪める・複製する

YMM4 の配布テンプレートに出てくるものを、同じ効き方になるように作ったもの
（:mod:`sashimono.compat.ymm4.effects` が写す） YMM4 の実装は公開されていないので、
**値の意味は配布物に入っていた値の並びから読み取っている** 例えば ``InOutZoom`` の
``X`` が 0 と 100 の 2 通りで入っていたことから、「その軸を縮めるかどうかの割合」と
読んでいる 並べて見比べて違いが見つかったら、ここを直す

どれも絵を置いたバッファ（画面と同じ大きさ）の上で、出力の画素から入力の画素を
逆算する 前へ写すと隙間が空く 位置の Y は上が正（ほかのエフェクトと同じ）
"""

from __future__ import annotations

from sashimono.effects.builtin import PRELUDE
from sashimono.effects.definition import EffectDefinition, registry
from sashimono.effects.easing import EASING_KINDS, EASING_MODES
from sashimono.effects.spec import CheckSpec, GridSpec, SelectSpec, TrackSpec, ValueSpec

__all__ = ["EASING_KINDS", "EASING_MODES", "MESH_MAX_POINTS", "register_motion_effects"]


def _shader(body: str) -> str:
    return PRELUDE + body


def _easing() -> tuple[SelectSpec, SelectSpec]:
    return (
        SelectSpec("easing", "イージング", EASING_KINDS, "linear"),
        SelectSpec("easing_mode", "イージングの向き", EASING_MODES, "in"),
    )


def _interval() -> TrackSpec:
    return TrackSpec("interval", "間隔", 0, 60, 0, step=0.01, unit="秒")


def _seed() -> ValueSpec:
    return ValueSpec("seed", "シード", 0, 0, 9999)


#: 登場・退場の進み具合 1 で隠れきった状態、0 で元の位置
#: 登場は始まりから ``time`` 秒、退場は終わりの ``time`` 秒 どちらも効くときは強い方
#: 退場は登場を時間で裏返した動き 終わりから数えた残りで登場と同じ曲線を引く
#: （YMM4 の木製看板テロップの退場 回転 174 度・Back・Out は、終わりの 4 フレーム前で
#: 43 度だった 残り 0.22 で 1 - Back_Out(0.22) = 0.25 進み具合を曲線へそのまま渡すと
#: 183 度まで回り切って上下が逆になる #177） 直線と InOut は対称なので どちらでも同じ
#: 形を変える動き（移動・拡大・回転・傾き）は曲線の行き過ぎ（Elastic や Back）をそのまま
#: 使う :func:`hidden_signed` 木製看板テロップの登場（回転 174 度・Elastic・Out）は、
#: 2 フレーム目で YMM4 が逆向きに 55 度ほど回っていた 0 で止めると回らずに立ったまま
#: 濃さとぼかしは 0〜1 に収める :func:`hidden_amount` 行き過ぎると 100% を超えた濃さになる
_IN_OUT = """
uniform bool effect_in;
uniform bool effect_out;
uniform float effect_time;
uniform int easing;
uniform int easing_mode;

float hidden_signed() {
    float span = max(effect_time, 0.0001);
    float amount = 0.0;
    if (effect_in && u_time < span) amount = 1.0 - ease(u_time / span, easing, easing_mode);
    if (effect_out) {
        float remaining = (u_duration - u_time) / span;
        if (remaining < 1.0) {
            float leaving = 1.0 - ease(remaining, easing, easing_mode);
            // 登場と退場が重なる短いクリップは、大きく動いている方を取る
            if (abs(leaving) > abs(amount)) amount = leaving;
        }
    }
    return amount;
}

float hidden_amount() {
    return clamp(hidden_signed(), 0.0, 1.0);
}
"""

#: 乱数を取り直す区切り 間隔が 0 なら毎フレーム
_STEP = """
float random_tick() {
    return interval > 0.0 ? floor(u_time / interval) : floor(u_frame + 0.5);
}
float random_signed(float tick, float salt) {
    return hash(vec2(tick * 0.731 + float(seed) * 13.1, salt)) * 2.0 - 1.0;
}
"""

#: 行って戻る動き 0 → 1 → 0 を ``interval`` 秒で 1 往復
#: 中央揃え（元の位置を挟んで振れる）は :func:`repeat_swing` で -0.5〜0.5 を返す
#: InOut の中央揃えは、元の位置から振れ始める（周期の 4 分の 1 だけ進めた所から）
#: YMM4 の書き出しを 1 コマずつ測ると、反復回転（Sine と Jump）・反復移動と反復拡大
#: （呼吸アニメーション Sine）はどれも頭のコマが元の位置で、4 分の 1 周期で振れ切った
#: 頭から振れ切った所にいると、動きが 4 分の 1 周期ずれて縁の差が 45 ほど出ていた（#205）
#: Out の中央揃え（ジャンプ繰り返し Circ・Out）は頭で振れ切った所にいて、今の式と合う
#: In と、中央揃えでない往復は測った物が無いので今の式のまま
_WAVE = """
float repeat_wave_at(float shift) {
    float span = max(interval, 0.0001);
    float phase = fract(u_time / span + shift);
    float there = phase < 0.5 ? phase * 2.0 : 2.0 - phase * 2.0;
    return ease(there, easing, easing_mode);
}
float repeat_wave() {
    return repeat_wave_at(0.0);
}
float repeat_swing() {
    return repeat_wave_at(easing_mode == 2 ? 0.25 : 0.0) - 0.5;
}
"""


_RANDOM_MOVE = _shader(
    """
uniform float range_x;
uniform float range_y;
uniform float range_z;
uniform float interval;
uniform int seed;
"""
    + _STEP
    + """
void main() {
    float tick = random_tick();
    vec2 offset = vec2(random_signed(tick, 1.0) * range_x, random_signed(tick, 2.0) * range_y);
    float scale = depth_scale(random_signed(tick, 3.0) * range_z);
    vec2 centre = object_center() + offset;
    vec2 pixel = v_uv * u_size;
    frag_color = sample_pixel(object_center() + (pixel - centre) / scale);
}
"""
)

#: 変形の支点 既定は絵の中央 YMM4 の中心点エフェクトは端や画面の中央も選ぶ
_PIVOT = """
uniform float anchor_x;
uniform float anchor_y;
uniform int pivot_h;
uniform int pivot_v;

vec2 pivot_point() {
    vec2 p = object_center();
    if (pivot_h == 0) p.x = u_size.x * 0.5;
    if (pivot_h == 1) p.x = u_object.x;
    if (pivot_h == 2) p.x = u_object.z;
    if (pivot_v == 0) p.y = u_size.y * 0.5;
    if (pivot_v == 1) p.y = u_object.w;
    if (pivot_v == 2) p.y = u_object.y;
    if (pivot_h == 4) p.x = object_origin().x;
    if (pivot_v == 4) p.y = object_origin().y;
    return p + vec2(anchor_x, anchor_y);
}

// 支点が既定（絵の中央）から動かされているか
bool pivot_chosen() {
    return pivot_h != 3 || pivot_v != 3 || anchor_x != 0.0 || anchor_y != 0.0;
}
"""


def _pivot_specs() -> tuple[TrackSpec | SelectSpec, ...]:
    return (
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
            "center",
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
            "middle",
        ),
    )


_RANDOM_ZOOM = _shader(
    """
uniform float zoom;
uniform float zoom_x;
uniform float zoom_y;
uniform float interval;
uniform int seed;
"""
    + _PIVOT
    + _STEP
    + """
void main() {
    // 拡大率は 100% から zoom% までの間で揺れる 軸ごとの値はその軸に掛かる倍率
    // （配布物では 0 → 100 と瞬間移動させて「出現」に使われていた）
    float r = random_signed(random_tick(), 4.0);
    float swing = 1.0 + r * (zoom / 100.0 - 1.0);
    vec2 scale = vec2(zoom_x, zoom_y) / 100.0 * swing;
    if (scale.x <= 0.0001 || scale.y <= 0.0001) { frag_color = vec4(0.0); return; }
    vec2 centre = pivot_point();
    frag_color = sample_pixel(centre + (v_uv * u_size - centre) / scale);
}
"""
)

_RANDOM_ROTATE = _shader(
    """
uniform float angle_x;
uniform float angle_y;
uniform float angle_z;
uniform bool three_d;
uniform float interval;
uniform int seed;
"""
    + _PIVOT
    + _STEP
    + """
void main() {
    float tick = random_tick();
    vec3 angles = vec3(0.0, 0.0, random_signed(tick, 7.0) * angle_z);
    if (three_d) {
        angles.x = random_signed(tick, 5.0) * angle_x;
        angles.y = random_signed(tick, 6.0) * angle_y;
    }
    vec2 centre = pivot_point();
    frag_color = sample_pixel(centre + untilt(v_uv * u_size - centre, angles));
}
"""
)

_REPEAT_MOVE = _shader(
    """
uniform float move_x;
uniform float move_y;
uniform float move_z;
uniform float interval;
uniform bool centering;
uniform int easing;
uniform int easing_mode;
"""
    + _WAVE
    + """
void main() {
    // 中心揃えなら元の位置を挟んで往復する 揃えなければ元の位置から片側へ
    float k = centering ? repeat_swing() : repeat_wave();
    vec2 centre = object_center() + vec2(move_x, move_y) * k;
    float scale = depth_scale(move_z * k);
    frag_color = sample_pixel(object_center() + (v_uv * u_size - centre) / scale);
}
"""
)

_REPEAT_ROTATE = _shader(
    """
uniform float angle_x;
uniform float angle_y;
uniform float angle_z;
uniform bool three_d;
uniform float interval;
uniform bool centering;
uniform int easing;
uniform int easing_mode;
"""
    + _PIVOT
    + _WAVE
    + """
void main() {
    float k = centering ? repeat_swing() : repeat_wave();
    vec3 angles = vec3(0.0, 0.0, angle_z) * k;
    if (three_d) angles.xy = vec2(angle_x, angle_y) * k;
    vec2 centre = pivot_point();
    frag_color = sample_pixel(centre + untilt(v_uv * u_size - centre, angles));
}
"""
)

_REPEAT_OPACITY = _shader(
    """
uniform float opacity;
uniform float interval;
uniform int easing;
uniform int easing_mode;
"""
    + _WAVE
    + """
void main() {
    // 100% と opacity% の間を往復する
    vec4 color = texture(u_texture, v_uv);
    float level = mix(1.0, clamp(opacity / 100.0, 0.0, 1.0), repeat_wave());
    frag_color = vec4(color.rgb, color.a * level);
}
"""
)

_INOUT_MOVE = _shader(
    """
uniform int direction;
"""
    + _IN_OUT
    + """
void main() {
    // 画面の外から入ってくる 隠れきった状態では、絵の端が画面の端をちょうど越える
    vec2 shift = vec2(0.0);
    if (direction == 0) shift = vec2(0.0, u_size.y - u_object.y);
    if (direction == 1) shift = vec2(0.0, -u_object.w);
    if (direction == 2) shift = vec2(-u_object.z, 0.0);
    if (direction == 3) shift = vec2(u_size.x - u_object.x, 0.0);
    frag_color = sample_pixel(v_uv * u_size - shift * hidden_signed());
}
"""
)

_INOUT_ZOOM = _shader(
    """
uniform float zoom;
uniform float zoom_x;
uniform float zoom_y;
"""
    + _PIVOT
    + _IN_OUT
    + """
void main() {
    // 隠れきった状態の大きさが「拡大率 × 軸の割合」 100% どうしなら大きさは変わらない
    // （YMM4 に描かせて確かめた 0 の軸はその向きに潰れた所から広がる）
    vec2 target = vec2(zoom_x, zoom_y) / 100.0 * (zoom / 100.0);
    vec2 scale = mix(vec2(1.0), target, hidden_signed());
    if (scale.x <= 0.0001 || scale.y <= 0.0001) { frag_color = vec4(0.0); return; }
    vec2 centre = pivot_point();
    frag_color = sample_pixel(centre + (v_uv * u_size - centre) / scale);
}
"""
)

#: 跳ねる動きの 1 周期（跳ねる と 跳ねて登場 で同じ） YMM4 の書き出しを 1 コマずつ測って
#: 合わせた（#205）
#: - 宙にいる間（周期の頭から period 秒）は、真ん中が 高さ × sin(π × 経過 / period) だけ上がり、
#:   真ん中を支点に縦へ 1 + 伸び縮み / 100 倍、横へその逆数倍に伸びる 伸びは宙にいる間ずっと同じ
#: - 着いてから interval 秒は、足元を支点に縦へ 1 - 歪み / 100 × sin(π × 経過 / interval) 倍、
#:   横へその逆数倍に潰れる
#: - 周期の頭と着いた瞬間はどちらでもない元の形
#: 前は周期の頭（地面にいる間）から潰し、宙では伸ばしていなかった 着いてからの潰れが
#: 無く、縦横 3〜5 画素ずれて縁の差が 45 を超えていた
#: 周期の境目の比べは少しゆとりを持つ 経過が 0.3 秒と period 0.3 秒のように同じはずでも、
#: 浮動小数の丸めで宙の側へ入ると、跳ねていないのに伸びた形が 1 コマ出る
_JUMP_SHAPE = """
const float JUMP_EDGE = 0.0001;

void jump_shape(
    float time, float height, float stretch, float period, float distortion, float interval,
    out vec2 lifted, out vec2 tall, out vec2 flat_
) {
    lifted = vec2(0.0);
    tall = vec2(1.0);
    flat_ = vec2(1.0);
    float flight = max(period, 0.01);
    float rest = max(interval, 0.0);
    float phase = mod(time, flight + rest);
    if (phase > JUMP_EDGE && phase < flight - JUMP_EDGE) {
        lifted = vec2(0.0, height * sin(PI * phase / flight));
        float grow = max(1.0 + stretch / 100.0, 0.0001);
        tall = vec2(1.0 / grow, grow);
    } else if (rest > 0.0 && phase > flight + JUMP_EDGE) {
        float press = max(1.0 - distortion / 100.0 * sin(PI * (phase - flight) / rest), 0.0001);
        flat_ = vec2(1.0 / press, press);
    }
}
"""

_INOUT_JUMP = _shader(
    """
uniform bool effect_in;
uniform bool effect_out;
uniform bool reverse_in;
uniform bool reverse_out;
uniform float effect_time;
uniform float height;
uniform float stretch;
uniform float period;
uniform float distortion;
uniform float interval;
uniform float offset_x;
uniform float offset_y;
"""
    + _JUMP_SHAPE
    + """
void main() {
    float span = max(effect_time, 0.0001);

    // 登場は offset の位置から跳ねながら元の位置へ、退場は元の位置から offset へ
    float travel = 0.0;
    bool jumping = false;
    if (effect_in && u_time < span) {
        travel = (reverse_in ? -1.0 : 1.0) * (1.0 - u_time / span);
        jumping = true;
    }
    float from_end = u_time - (u_duration - span);
    if (effect_out && from_end > 0.0) {
        travel = (reverse_out ? -1.0 : 1.0) * clamp(from_end / span, 0.0, 1.0);
        jumping = true;
    }

    vec2 lifted = vec2(0.0);
    vec2 tall = vec2(1.0);
    vec2 flat_ = vec2(1.0);
    if (jumping) {
        jump_shape(u_time, height, stretch, period, distortion, interval, lifted, tall, flat_);
    }

    vec2 centre = object_center();
    vec2 foot = vec2(centre.x, centre.y - object_size().y * 0.5);
    vec2 p = v_uv * u_size - vec2(offset_x, offset_y) * travel - lifted;
    p = centre + (p - centre) / tall;
    frag_color = sample_pixel(foot + (p - foot) / flat_);
}
"""
)

_INOUT_GETUP = _shader(
    """
uniform int base;
uniform bool three_d;
"""
    + _PIVOT
    + _IN_OUT
    + """
void main() {
    // 基準の辺を軸に、寝た状態から起き上がる 立体でなければ辺へ向かって潰す
    float amount = hidden_signed();
    vec2 pivot = object_center();
    if (base == 0) pivot.y = u_object.y;
    if (base == 1) pivot.y = u_object.w;
    if (base == 2) pivot.x = u_object.x;
    if (base == 3) pivot.x = u_object.z;
    // 中心点で支点を選んでいれば、そちらを軸にする
    if (pivot_chosen()) pivot = pivot_point();
    vec2 point = v_uv * u_size - pivot;

    if (three_d) {
        vec3 angles = vec3(0.0);
        if (base == 0) angles.x = -90.0 * amount;
        if (base == 1) angles.x = 90.0 * amount;
        if (base == 2) angles.y = 90.0 * amount;
        if (base == 3) angles.y = -90.0 * amount;
        frag_color = sample_pixel(pivot + untilt(point, angles));
        return;
    }
    vec2 scale = vec2(1.0);
    if (base < 2) scale.y = 1.0 - amount; else scale.x = 1.0 - amount;
    if (scale.x <= 0.0001 || scale.y <= 0.0001) { frag_color = vec4(0.0); return; }
    frag_color = sample_pixel(pivot + point / scale);
}
"""
)

_INOUT_FADE = _shader(
    """
uniform float opacity;
"""
    + _IN_OUT
    + """
void main() {
    // 隠れきった状態の不透明度が opacity 途中はその間を直線でつなぐ
    float keep = mix(1.0, clamp(opacity / 100.0, 0.0, 1.0), hidden_amount());
    // ストレートアルファなので薄めるのは不透明度だけ 色まで掛けると縁が黒ずむ
    vec4 color = sample_pixel(v_uv * u_size);
    frag_color = vec4(color.rgb, color.a * keep);
}
"""
)

_INOUT_ROTATE = _shader(
    """
uniform float angle_x;
uniform float angle_y;
uniform float angle_z;
uniform bool three_d;
"""
    + _PIVOT
    + _IN_OUT
    + """
void main() {
    float k = hidden_signed();
    vec3 angles = vec3(0.0, 0.0, angle_z) * k;
    if (three_d) angles.xy = vec2(angle_x, angle_y) * k;
    // 中心点のエフェクトが前にあれば、その点を支点に回る（木製看板テロップは釘の所 #177）
    vec2 centre = pivot_point();
    frag_color = sample_pixel(centre + untilt(v_uv * u_size - centre, angles));
}
"""
)

_INOUT_OFFSET = _shader(
    """
uniform float offset_x;
uniform float offset_y;
uniform float offset_z;
"""
    + _IN_OUT
    + """
void main() {
    // offset_z は手前へ出す量（YMM4 の Value3） 奥行きの拡大率で大きさだけが変わる
    // YMM4 に 300 を描かせると 300 の四角が 428、-300 で 230 になった（#198）
    float k = hidden_signed();
    vec2 centre = object_center() + vec2(offset_x, offset_y) * k;
    float scale = depth_scale(-offset_z * k);
    frag_color = sample_pixel(object_center() + (v_uv * u_size - centre) / scale);
}
"""
)

_INOUT_SKEW = _shader(
    """
uniform float angle_x;
uniform float angle_y;
"""
    + _IN_OUT
    + """
void main() {
    // 傾きの角度そのものを隠れ具合に比例させる tan を比例させると 90 度近くで跳ねる
    float k = hidden_signed();
    vec2 pivot = object_center();
    vec2 point = v_uv * u_size - pivot;
    float tx = tan(radians(clamp(angle_x * k, -89.0, 89.0)));
    float ty = tan(radians(clamp(angle_y * k, -89.0, 89.0)));
    float determinant = 1.0 + tx * ty;
    if (abs(determinant) < 1e-4) { frag_color = vec4(0.0); return; }
    vec2 source = vec2(point.x + tx * point.y, point.y - ty * point.x) / determinant;
    frag_color = sample_pixel(pivot + source);
}
"""
)

_INOUT_BLUR = _shader(
    """
uniform float radius;
"""
    + _IN_OUT
    + """
void main() {
    vec2 direction = u_pass == 0 ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
    frag_color = blur1d(u_texture, v_uv, direction, radius * hidden_amount());
}
"""
)

_SKEW = _shader(
    """
uniform float angle_x;
uniform float angle_y;
uniform float center_x;
uniform float center_y;

void main() {
    // 横の傾きは上下で左右にずれる形 縦の傾きは左右で上下にずれる形
    vec2 pivot = object_center() + vec2(center_x, center_y);
    vec2 point = v_uv * u_size - pivot;
    float tx = tan(radians(clamp(angle_x, -89.0, 89.0)));
    float ty = tan(radians(clamp(angle_y, -89.0, 89.0)));
    // 逆算に使う行列 [[1, tx], [-ty, 1]] の行列式は 1 + tx*ty 符号を取り違えると、
    // 倍率が狂い、両方 45 度で 0 になって絵が消える
    float determinant = 1.0 + tx * ty;
    if (abs(determinant) < 1e-4) { frag_color = vec4(0.0); return; }
    vec2 source = vec2(point.x + tx * point.y, point.y - ty * point.x) / determinant;
    frag_color = sample_pixel(pivot + source);
}
"""
)

_SPIRAL = _shader(
    """
uniform float angle;
uniform bool outer;
"""
    + _PIVOT
    + """
void main() {
    // 中心ほど大きく回す 外側ほど大きく回す指定もある
    vec2 centre = pivot_point();
    vec2 point = v_uv * u_size - centre;
    float reach = max(length(object_size()) * 0.5, 1.0);
    float k = clamp(length(point) / reach, 0.0, 1.0);
    float turn = radians(angle) * (outer ? k : 1.0 - k);
    float c = cos(turn);
    float s = sin(turn);
    frag_color = sample_pixel(centre + mat2(c, s, -s, c) * point);
}
"""
)

_WAVE_DISTORT = _shader(
    """
uniform float angle;
uniform float direction;
uniform float amplitude;
uniform float wavelength;
uniform float period;

void main() {
    // angle の向きに進む波で、direction の向きへ揺らす
    vec2 pixel = v_uv * u_size;
    vec2 travel = vec2(cos(radians(angle)), sin(radians(angle)));
    vec2 sway = vec2(cos(radians(direction)), sin(radians(direction)));
    float distance = dot(pixel - object_center(), travel);
    float phase = distance / max(wavelength, 1.0) - (period > 0.0 ? u_time / period : 0.0);
    frag_color = sample_pixel(pixel - sway * amplitude * sin(2.0 * PI * phase));
}
"""
)

#: 格子の 1 辺の点数の上限 シェーダの配列の大きさと
#: :class:`~sashimono.effects.spec.GridSpec` の上限を揃えること
#: 片方だけ増やすと、配列の外を読むか、渡したのに効かない点が出る
MESH_MAX_POINTS = 9

_MESH = _shader(
    f"""
uniform float point0_x;
uniform float point0_y;
uniform float point1_x;
uniform float point1_y;
uniform float point2_x;
uniform float point2_y;
uniform float point3_x;
uniform float point3_y;
// 格子（grid）の点数と、各点のずれ 左上から行ごとに並ぶ
// 点数が 0 なら格子は無く、上の四隅のスライダで動く（既存のプロジェクトはこちら）
uniform int grid_columns;
uniform int grid_rows;
const int MESH_MAX = {MESH_MAX_POINTS};
uniform vec2 grid_points[MESH_MAX * MESH_MAX];
"""
    + """
float cross2(vec2 a, vec2 b) { return a.x * b.y - a.y * b.x; }

// 四隅を動かした四角形の中で、点が元のどこにあたるか（双一次補間の逆）
vec2 inverse_bilinear(vec2 p, vec2 a, vec2 b, vec2 c, vec2 d) {
    vec2 e = b - a;
    vec2 f = d - a;
    vec2 g = a - b + c - d;
    vec2 h = p - a;
    float k2 = cross2(g, f);
    float k1 = cross2(e, f) + cross2(h, g);
    float k0 = cross2(h, e);
    float v;
    if (abs(k2) < 1e-4) {
        // 潰れた四角形では割る数が 0 になる 範囲外を返して描かない
        if (abs(k1) < 1e-6) return vec2(-1.0);
        v = -k0 / k1;
    } else {
        float w = k1 * k1 - 4.0 * k0 * k2;
        if (w < 0.0) return vec2(-1.0);
        w = sqrt(w);
        v = (-k1 - w) / (2.0 * k2);
        if (v < 0.0 || v > 1.0) v = (-k1 + w) / (2.0 * k2);
    }
    float across_x = e.x + g.x * v;
    float across_y = e.y + g.y * v;
    if (abs(across_x) >= abs(across_y)) {
        if (abs(across_x) < 1e-6) return vec2(-1.0);
        return vec2((h.x - f.x * v) / across_x, v);
    }
    if (abs(across_y) < 1e-6) return vec2(-1.0);
    return vec2((h.y - f.y * v) / across_y, v);
}

// 格子の点 1 つのずれ 番号は左上から行ごと
// 格子が無いときは四隅のスライダを同じ順（左上・右上・左下・右下）に並べ替えて返す
vec2 mesh_offset(int index) {
    if (grid_columns >= 2 && grid_rows >= 2) {
        return grid_points[clamp(index, 0, MESH_MAX * MESH_MAX - 1)];
    }
    // スライダは一周の順（左上・右上・右下・左下）で持っている
    if (index == 0) return vec2(point0_x, point0_y);
    if (index == 1) return vec2(point1_x, point1_y);
    if (index == 2) return vec2(point3_x, point3_y);
    return vec2(point2_x, point2_y);
}

// 変形後の格子の点 (column, row) row は上から数える Y は上が正なので下ほど小さい
vec2 mesh_point(int column, int row, int columns, int rows) {
    float u = float(column) / float(columns - 1);
    float v = float(row) / float(rows - 1);
    vec2 base = vec2(mix(u_object.x, u_object.z, u), mix(u_object.w, u_object.y, v));
    return base + mesh_offset(row * columns + column);
}

void main() {
    // 格子が無ければ 2x2（四隅のスライダ）として扱う 既存のプロジェクトはこの道を通る
    int columns = grid_columns >= 2 ? min(grid_columns, MESH_MAX) : 2;
    int rows = grid_rows >= 2 ? min(grid_rows, MESH_MAX) : 2;
    vec2 pixel = v_uv * u_size;
    // 出力の画素が入る**変形後のセル**を探す セルを 1 つしか見ないと、真ん中の点を
    // 動かしたときに端まで一緒に歪む 上限は 8x8 = 64 回なので総当たりで足りる
    for (int row = 0; row + 1 < MESH_MAX; ++row) {
        if (row + 1 >= rows) break;
        for (int column = 0; column + 1 < MESH_MAX; ++column) {
            if (column + 1 >= columns) break;
            vec2 top_left = mesh_point(column, row, columns, rows);
            vec2 top_right = mesh_point(column + 1, row, columns, rows);
            vec2 bottom_left = mesh_point(column, row + 1, columns, rows);
            vec2 bottom_right = mesh_point(column + 1, row + 1, columns, rows);
            // 四隅を囲む四角から外れていれば、逆双一次補間まで行かずに次のセルへ
            // 画素ごとに最大 64 回の補間を回すと、大きな絵でプレビューが重くなる
            vec2 low = min(min(top_left, top_right), min(bottom_left, bottom_right));
            vec2 high = max(max(top_left, top_right), max(bottom_left, bottom_right));
            if (any(lessThan(pixel, low)) || any(greaterThan(pixel, high))) continue;
            vec2 uv = inverse_bilinear(pixel, bottom_left, bottom_right, top_right, top_left);
            // NaN は比較がすべて偽になり範囲の判定をすり抜けるので、先に弾く
            bool broken = any(isnan(uv)) || any(isinf(uv));
            if (broken || uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) continue;
            // セルの中の位置を、絵全体の位置へ uv.y は下からなので、行は下から数え直す
            float across = (float(column) + uv.x) / float(columns - 1);
            float along = (float(rows - 2 - row) + uv.y) / float(rows - 1);
            frag_color = sample_pixel(u_object.xy + vec2(across, along) * object_size());
            return;
        }
    }
    frag_color = vec4(0.0);
}
"""
)

_CIRCULAR_DUPLICATE = _shader(
    """
uniform int count;
uniform float radius;
uniform float circumference;
uniform bool synced;

void main() {
    // 絵を円周に並べる 1 つ目は真上 手前ほど後に置いた複製
    vec2 centre = object_center();
    vec2 pixel = v_uv * u_size;
    int copies = clamp(count, 1, 64);
    float sweep = 2.0 * PI * clamp(circumference / 100.0, 0.0, 1.0);
    // 一周するなら最後の複製と最初の複製が重ならないよう個数で割る 一周しないなら
    // 両端に置くので 1 つ少なく割る
    float slots = circumference >= 100.0 ? float(copies) : float(max(copies - 1, 1));
    float spacing = sweep / slots;
    vec4 result = vec4(0.0);
    for (int i = 0; i < copies; ++i) {
        float a = spacing * float(i);
        vec2 place = centre + vec2(sin(a), cos(a)) * radius;
        vec2 point = pixel - place;
        if (synced) {
            float c = cos(a);
            float s = sin(a);
            point = mat2(c, -s, s, c) * point;
        }
        result = over(sample_pixel(centre + point), result);
    }
    frag_color = result;
}
"""
)

_CRASH = _shader(
    """
uniform float start;
uniform float speed;
uniform float size;
uniform float fly;
uniform float fall;
uniform float delay;
uniform float impact;
uniform float spread;
uniform float spin;

void main() {
    // 絵を size 四方の欠片に割り、欠片ごとに飛ばして落とす
    // 出力の画素に来る欠片を探す範囲は、欠片が見込み位置からずれうる量で決める
    // 周り 7x7 に決め打ちすると、散る量を大きくした設定で画面の中の欠片が消えた（#199）
    //
    // 下限・重さ・飛ぶ速さ・散る速さの定数は画面の画素で決めた値なので u_pixel_scale を掛ける
    // 欠片の大きさ（size）だけ縮めてこちらを縮めないと、画質を落としたプレビューで
    // 欠片が 2 倍・4 倍の速さで飛び散り、書き出しと崩れ方が変わる
    float cell = max(size, 4.0 * u_pixel_scale);
    float elapsed = max(u_time - start, 0.0) * speed / 100.0;
    vec2 pixel = v_uv * u_size;
    vec2 centre = object_center();
    float gravity = 1500.0 * u_pixel_scale * fall / 100.0;
    vec2 base_drop = vec2(0.0, -0.5 * gravity * elapsed * elapsed);
    vec2 guess = floor((pixel - base_drop) / cell);

    // 見込み位置（遅れ無しで落ちた所）から欠片がずれうる量 飛ぶ向きと散らばりの速さの
    // 最大に経過を掛け、遅れて出た欠片の落ち方の違いと、回した欠片の角の張り出しを足す
    // 範囲の外の値（負の速さ・遅れ・重さ）も届きうるので、量はどれも絶対値で見る
    // 負のまま足すと範囲が負になり、どの画素も欠片を 1 つも調べずに絵が消える
    float fly_most = abs(300.0 * u_pixel_scale * fly / 100.0 * impact / 100.0)
                   + abs(200.0 * sqrt(2.0) * u_pixel_scale * impact / 100.0 * spread / 100.0);
    // 欠片ごとの経過は elapsed から遅れの分だけずれる（遅れは 0〜delay の半分秒）
    float lagged = elapsed - 0.5 * delay / 100.0;
    float t_low = max(min(elapsed, lagged), 0.0);
    float t_high = max(max(elapsed, lagged), 0.0);
    float base = elapsed * elapsed;
    float drop_most = 0.5 * abs(gravity)
                    * max(abs(base - t_low * t_low), abs(base - t_high * t_high));
    float reach = fly_most * t_high + drop_most + cell * 0.71;
    // 前の版と同じ周り 7x7 より狭くはしない 大きな絵を細かく割って遠くまで散らす設定だけは、
    // 見込み位置の周り 63x63 で栓をする 栓が無いと 1 画素で何万もの欠片を回し、
    // GPU が止まるほど重くなる
    int radius = clamp(int(ceil(reach / cell)) + 1, 3, 31);
    // 欠片は元の絵の中身の範囲にしか無い 調べる範囲をそこで切れば、散った後でも元の絵の
    // 欠片の数より多くは回らない u_object ではなく u_content で切る 前の変形で広げた絵は
    // u_object の外まである
    ivec2 first = ivec2(floor(u_content.xy / cell));
    ivec2 last = ivec2(floor((u_content.zw - 0.5) / cell));
    ivec2 low = max(ivec2(guess) - radius, first);
    ivec2 high = min(ivec2(guess) + radius, last);

    vec4 result = vec4(0.0);
    for (int iy = low.y; iy <= high.y; ++iy) {
        for (int ix = low.x; ix <= high.x; ++ix) {
            vec2 index = vec2(float(ix), float(iy));
            vec2 home = (index + 0.5) * cell;
            float wait = hash(index + 3.1) * 0.5 * delay / 100.0;
            float t = max(elapsed - wait, 0.0);
            vec2 outward = normalize(home - centre + vec2(0.001)) * 300.0 * u_pixel_scale
                         * fly / 100.0 * impact / 100.0;
            vec2 scatter = (vec2(hash(index + 1.7), hash(index + 9.3)) - 0.5) * 400.0
                         * u_pixel_scale * impact / 100.0 * spread / 100.0;
            vec2 moved = home + (outward + scatter) * t + vec2(0.0, -0.5 * gravity * t * t);
            // 欠片ごとに向きと速さの違う回転 経過に比例して回る
            float turn_angle = (hash(index + 5.1) * 2.0 - 1.0) * 2.0 * PI * spin / 100.0 * t;
            vec2 local = pixel - moved;
            float c_ = cos(turn_angle);
            float s_ = sin(turn_angle);
            local = mat2(c_, s_, -s_, c_) * local;
            vec2 source = home + local;
            if (all(equal(floor(source / cell), index))) {
                result = over(sample_pixel(source), result);
            }
        }
    }
    frag_color = result;
}
"""
)

_NOISE_DISPLACEMENT = _shader(
    """
uniform float amount_x;
uniform float amount_y;
uniform int noise;
uniform float strength;
uniform float threshold;
uniform float levels;
uniform int octaves;
uniform float offset_x;
uniform float offset_y;
uniform float offset_z;
uniform float speed_x;
uniform float speed_y;
uniform float speed_z;
uniform float scale_x;
uniform float scale_y;
uniform float scale_z;

float value_noise(vec2 p, float slice) {
    vec2 cell = floor(p);
    vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash(cell + slice);
    float b = hash(cell + vec2(1.0, 0.0) + slice);
    float c = hash(cell + vec2(0.0, 1.0) + slice);
    float d = hash(cell + vec2(1.0, 1.0) + slice);
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

float channel(vec2 p, float z, float salt) {
    float slice = floor(z) * 17.0 + salt;
    float next = slice + 17.0;
    float blend = fract(z);
    if (noise == 2) {
        // 画素ごとのばらばらな値
        return mix(hash(floor(p * 40.0) + slice), hash(floor(p * 40.0) + next), blend);
    }
    if (noise == 3 || noise == 4) {
        // ボロノイは近い点の面ごとの値、セルは近い点までの距離
        vec2 cell = floor(p);
        float nearest = 10.0;
        float value = 0.0;
        for (int j = -1; j <= 1; ++j) {
            for (int i = -1; i <= 1; ++i) {
                vec2 c = cell + vec2(i, j);
                vec2 point = c + vec2(hash(c + slice + 0.3), hash(c + slice + 0.7));
                float d = length(p - point);
                if (d < nearest) { nearest = d; value = hash(c + slice + 0.9); }
            }
        }
        return noise == 3 ? value : clamp(nearest, 0.0, 1.0);
    }
    if (noise == 0) {
        // ブロック 格子ごとに一様
        return mix(hash(floor(p) + slice), hash(floor(p) + next), blend);
    }
    // パーリン風 オクターブを重ねた滑らかなノイズ
    float total = 0.0;
    float weight = 0.5;
    float norm = 0.0;
    vec2 q = p;
    for (int i = 0; i < clamp(octaves, 1, 8); ++i) {
        total += weight * mix(value_noise(q, slice), value_noise(q, next), blend);
        norm += weight;
        weight *= 0.5;
        q *= 2.0;
    }
    return total / max(norm, 0.0001);
}

void main() {
    // ノイズの値でずらす 100% のノイズの粒は 50 画素、移動量 100 で最大 40 画素ほど
    // どちらも YMM4 に縞を歪めさせた絵（tools/ymm4_probes.py の 3 回目）に合わせた
    // 50 画素は画面の画素なので合成の画素へ縮める 縮めないと、画質を落としたプレビューで
    // 粒が 2 倍・4 倍に粗く出る（位置と速さはエンジンが縮めて渡すので、ここと揃う）
    vec2 pixel = v_uv * u_size;
    vec2 scale = max(vec2(scale_x, scale_y) / 100.0 * 50.0 * u_pixel_scale, vec2(1.0));
    vec2 position = (pixel + vec2(offset_x, offset_y) + vec2(speed_x, speed_y) * u_time) / scale;
    float z = (offset_z + speed_z * u_time) / max(scale_z, 1.0);

    vec2 n = vec2(channel(position, z, 0.0), channel(position, z, 57.0)) * 2.0 - 1.0;
    float cut = clamp(threshold / 100.0, 0.0, 1.0);
    n = mix(vec2(0.0), n, step(vec2(cut), abs(n)));
    float steps = max(levels, 2.0);
    n = floor(n * steps + 0.5) / steps;
    vec2 shift = n * vec2(amount_x, amount_y) * 0.4 * strength / 100.0;
    frag_color = sample_pixel(pixel - shift);
}
"""
)


def _in_out_specs() -> tuple[CheckSpec | TrackSpec | SelectSpec, ...]:
    return (
        CheckSpec("effect_in", "登場", True),
        CheckSpec("effect_out", "退場", False),
        TrackSpec("effect_time", "時間", 0, 60, 0.5, step=0.01, unit="秒"),
        *_easing(),
    )


#: ランダムな向きから飛んでくる 向きは ``seed`` で決まる（毎フレームは変えない）
_INOUT_RANDOM_DIRECTION = _shader(
    """
uniform int seed;
uniform float spin;
uniform float light;
"""
    + _IN_OUT
    + """
void main() {
    // 隠れているあいだは画面の外 進むにつれて元の場所へ戻る
    //
    // AviUtl2 に描かせると、時間の半ばではまだ画面の隅にいて、
    // 時間が終わる頃にちょうど元の場所へ収まっていた
    float hidden = hidden_amount();
    float turn = hash(vec2(float(seed) * 7.1, 3.3)) * 6.2831853;
    vec2 away = vec2(cos(turn), sin(turn)) * length(u_size) * hidden;

    // 回転 は飛んでくるあいだに回る**周**の数 着いたら 0 周（元の向き）
    float spun = radians(-spin * 360.0 * hidden);
    float cs = cos(spun);
    float sn = sin(spun);
    vec2 centre = object_center();
    vec2 local = v_uv * u_size - centre - away;
    vec4 color = sample_pixel(centre + vec2(local.x * cs - local.y * sn,
                                            local.x * sn + local.y * cs));

    // ライト は飛んでいるあいだの明るさの足し算（実測で 明 238 → 190 と変わった）
    frag_color = vec4(clamp(color.rgb + light * 0.01 * hidden, 0.0, 1.0), color.a);
}
"""
)


#: 上から落ちてきて濃くなる 落ち始めは ``interval`` 秒までの遅れが乗る
_INOUT_FALL = _shader(
    """
uniform int seed;
uniform float interval;
uniform float distance_;
"""
    + _IN_OUT
    + """
void main() {
    // 遅れ 0・距離 200・加減速なしの見本で、10 フレーム目が 134px 上、
    // 20 フレーム目が 67px 上、30 フレーム目で元の位置だった（＝直線で落ちる）
    // 同時に濃さも 118 → 205 → 239 と上がる
    // 登場と退場の両方を見る 登場側にだけ遅れが乗る
    // hidden_amount() をそのまま使えないのは、遅れを足した時間で測るため
    // 退場を見落とすと、クリップの終わりで絵が残り続ける
    float span = max(effect_time, 0.0001);
    float wait_ = hash(vec2(float(seed) * 3.7, 9.1)) * max(interval, 0.0);
    float hidden = 0.0;
    if (effect_in) hidden = max(hidden, 1.0 - ease((u_time - wait_) / span, easing, easing_mode));
    if (effect_out) {
        hidden = max(hidden, ease((u_time - (u_duration - span)) / span, easing, easing_mode));
    }
    float along = 1.0 - clamp(hidden, 0.0, 1.0);

    // **上から**落ちてくる 画素の Y は上が正なので、引く先を下へずらすと
    // 絵は上に見える 符号を逆にすると下から浮き上がってくる別の動きになる
    // （AviUtl2 の実物は 距離 200 で 200px 上から降りてきた）
    vec4 color = sample_pixel(v_uv * u_size - vec2(0.0, distance_ * (1.0 - along)));
    frag_color = vec4(color.rgb, color.a * along);
}
"""
)


#: 点いたり消えたりしながら現れる
_INOUT_BLINK = _shader(
    """
uniform float interval;
uniform bool even;
"""
    + _IN_OUT
    + """
void main() {
    // 半端な濃さは通らない 実測でも見えるときは元の明るさのまま（239）で、
    // 見えないときは何も無い 薄く出す作りにすると、点滅ではなく溶け込みになる
    float hidden = hidden_amount();
    vec4 color = sample_pixel(v_uv * u_size);
    if (hidden <= 0.0) {
        frag_color = color;
        return;
    }

    // AviUtl2 の実物を 1 フレームずつ読むと、どちらも**乱数ではなかった**
    //
    // 一定にする を付けたとき（点滅間隔 5）
    //     .....#####.....#####.....#####
    //     間隔のぶん消えて、間隔のぶん点く（進み具合に関係なく同じ）
    //
    // 外したとき（点滅間隔 1）
    //     ..........#....#..#..#..#.#.#.#.##.##.####.######
    //     進むほど点いている時間が増える 50 フレーム目までに点いた数は 20 で、
    //     「点いている割合 ＝ 進み具合」を積み上げた量（f^2 / 2T）と一致する
    float step_ = max(interval, 1.0);
    if (even) {
        frag_color = mod(floor(u_frame / step_), 2.0) > 0.5 ? color : vec4(0.0);
        return;
    }

    // 積み上げた量が整数をまたぐフレームだけ点ける
    // 点滅間隔 を大きくすると、またぐ回数が減って 1 回が長くなる
    //
    // 測るのは「登場が始まってからの経過」と「終わりまでの残り」の**短い方**
    // クリップ先頭からのフレーム数で測ると、退場の頃には毎フレーム境界をまたいで
    // 点きっぱなしになり、退場しても絵が消えない
    float span_frames = max(effect_time * max(u_fps, 1.0), 1.0);
    float total = max(u_duration * max(u_fps, 1.0), 1.0);
    float when = effect_in ? u_frame : span_frames;
    if (effect_out) when = min(when, total - u_frame);
    when = max(when, 0.0);

    // 数えるのは**次のフレームまで**の積み上げ 現在までで測ると、
    // 実物より 1 フレーム遅れて点き始める
    float scale = 2.0 * span_frames * step_;
    float now = (when + 1.0) * (when + 1.0) / scale;
    float before = when * when / scale;
    frag_color = floor(now) != floor(before) ? color : vec4(0.0);
}
"""
)


def register_motion_effects() -> None:
    """動きのエフェクトを一覧へ登録する 何度呼んでも 1 回だけ"""
    if "random_move" in registry:
        return

    definitions = (
        EffectDefinition(
            kind="inout_random_direction",
            label="ランダム方向から登場",
            category="登場・退場",
            parameters=(
                *_in_out_specs(),
                TrackSpec("spin", "回転", -100, 100, 0, unit="周"),
                TrackSpec("light", "ライト", -100, 100, 0, unit="%"),
                _seed(),
            ),
            fragment_shader=_INOUT_RANDOM_DIRECTION,
        ),
        EffectDefinition(
            kind="inout_fall",
            label="ランダム間隔で落ちながら登場",
            category="登場・退場",
            parameters=(
                *_in_out_specs(),
                TrackSpec("distance_", "距離", -8000, 8000, 400, step=1, unit="px"),
                TrackSpec("interval", "間隔", 0, 60, 0, unit="秒"),
                _seed(),
            ),
            fragment_shader=_INOUT_FALL,
        ),
        EffectDefinition(
            kind="inout_blink",
            label="点滅して登場",
            category="登場・退場",
            parameters=(
                *_in_out_specs(),
                TrackSpec("interval", "点滅間隔", 1, 240, 1, step=1, unit="フレーム"),
                CheckSpec("even", "点滅間隔を一定にする", False),
            ),
            fragment_shader=_INOUT_BLINK,
        ),
        EffectDefinition(
            kind="random_move",
            label="ランダム移動",
            category="動き",
            parameters=(
                TrackSpec("range_x", "X の幅", 0, 4000, 10, step=1, unit="px"),
                TrackSpec("range_y", "Y の幅", 0, 4000, 10, step=1, unit="px"),
                TrackSpec("range_z", "Z の幅", 0, 4000, 0, step=1, unit="px"),
                _interval(),
                _seed(),
            ),
            fragment_shader=_RANDOM_MOVE,
        ),
        EffectDefinition(
            kind="random_zoom",
            label="ランダム拡大",
            category="動き",
            parameters=(
                TrackSpec("zoom", "拡大率", 0, 1000, 120, unit="%"),
                TrackSpec("zoom_x", "横の倍率", 0, 1000, 100, unit="%"),
                TrackSpec("zoom_y", "縦の倍率", 0, 1000, 100, unit="%"),
                _interval(),
                _seed(),
                *_pivot_specs(),
            ),
            fragment_shader=_RANDOM_ZOOM,
        ),
        EffectDefinition(
            kind="random_rotate",
            label="ランダム回転",
            category="動き",
            parameters=(
                TrackSpec("angle_x", "X 軸の幅", 0, 360, 0, unit="度"),
                TrackSpec("angle_y", "Y 軸の幅", 0, 360, 0, unit="度"),
                TrackSpec("angle_z", "回転の幅", 0, 360, 10, unit="度"),
                CheckSpec("three_d", "立体", False),
                _interval(),
                _seed(),
                *_pivot_specs(),
            ),
            fragment_shader=_RANDOM_ROTATE,
        ),
        EffectDefinition(
            kind="repeat_move",
            label="反復移動",
            category="動き",
            parameters=(
                TrackSpec("move_x", "X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("move_y", "Y", -4000, 4000, 100, step=1, unit="px"),
                TrackSpec("move_z", "Z", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("interval", "周期", 0.01, 60, 1, step=0.01, unit="秒"),
                CheckSpec("centering", "元の位置を挟んで往復", True),
                *_easing(),
            ),
            fragment_shader=_REPEAT_MOVE,
        ),
        EffectDefinition(
            kind="repeat_rotate",
            label="反復回転",
            category="動き",
            parameters=(
                TrackSpec("angle_x", "X 軸", -3600, 3600, 0, unit="度"),
                TrackSpec("angle_y", "Y 軸", -3600, 3600, 0, unit="度"),
                TrackSpec("angle_z", "回転", -3600, 3600, 30, unit="度"),
                CheckSpec("three_d", "立体", False),
                TrackSpec("interval", "周期", 0.01, 60, 1, step=0.01, unit="秒"),
                CheckSpec("centering", "元の角度を挟んで往復", True),
                *_easing(),
                *_pivot_specs(),
            ),
            fragment_shader=_REPEAT_ROTATE,
        ),
        EffectDefinition(
            kind="repeat_opacity",
            label="反復透明度",
            category="動き",
            parameters=(
                TrackSpec("opacity", "不透明度", 0, 100, 0, unit="%"),
                TrackSpec("interval", "周期", 0.01, 60, 1, step=0.01, unit="秒"),
                *_easing(),
            ),
            fragment_shader=_REPEAT_OPACITY,
        ),
        EffectDefinition(
            kind="inout_move",
            label="画面外から登場",
            category="登場・退場",
            parameters=(
                SelectSpec(
                    "direction",
                    "方向",
                    (("top", "上"), ("bottom", "下"), ("left", "左"), ("right", "右")),
                    "top",
                ),
                *_in_out_specs(),
            ),
            fragment_shader=_INOUT_MOVE,
        ),
        EffectDefinition(
            kind="inout_zoom",
            label="拡大して登場",
            category="登場・退場",
            parameters=(
                TrackSpec("zoom", "隠れたときの拡大率", 0, 1000, 0, unit="%"),
                TrackSpec("zoom_x", "横の割合", 0, 1000, 100, unit="%"),
                TrackSpec("zoom_y", "縦の割合", 0, 1000, 100, unit="%"),
                *_pivot_specs(),
                *_in_out_specs(),
            ),
            fragment_shader=_INOUT_ZOOM,
        ),
        EffectDefinition(
            kind="inout_jump",
            label="跳ねて登場",
            category="登場・退場",
            parameters=(
                CheckSpec("effect_in", "登場", True),
                CheckSpec("reverse_in", "登場を逆向きに", False),
                CheckSpec("effect_out", "退場", False),
                CheckSpec("reverse_out", "退場を逆向きに", False),
                TrackSpec("effect_time", "時間", 0, 36000, 2, step=0.01, unit="秒"),
                TrackSpec("height", "高さ", 0, 4000, 150, step=1, unit="px"),
                TrackSpec("stretch", "伸び縮み", 0, 100, 0, unit="%"),
                TrackSpec("period", "1 回の長さ", 0.01, 60, 0.5, step=0.01, unit="秒"),
                TrackSpec("distortion", "歪み", 0, 100, 0, unit="%"),
                TrackSpec("interval", "間隔", 0, 60, 0, step=0.01, unit="秒"),
                TrackSpec("offset_x", "開始 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("offset_y", "開始 Y", -4000, 4000, 0, step=1, unit="px"),
            ),
            fragment_shader=_INOUT_JUMP,
        ),
        EffectDefinition(
            kind="inout_getup",
            label="起き上がって登場",
            category="登場・退場",
            parameters=(
                SelectSpec(
                    "base",
                    "基準の辺",
                    (("bottom", "下"), ("top", "上"), ("left", "左"), ("right", "右")),
                    "bottom",
                ),
                CheckSpec("three_d", "立体", True),
                *_in_out_specs(),
                *_pivot_specs(),
            ),
            fragment_shader=_INOUT_GETUP,
        ),
        EffectDefinition(
            kind="inout_fade",
            label="フェードで登場",
            category="登場・退場",
            parameters=(
                TrackSpec("opacity", "隠れたときの不透明度", 0, 100, 0, unit="%"),
                *_in_out_specs(),
            ),
            fragment_shader=_INOUT_FADE,
        ),
        EffectDefinition(
            kind="inout_rotate",
            label="回って登場",
            category="登場・退場",
            parameters=(
                TrackSpec("angle_x", "X 軸", -3600, 3600, 0, unit="度"),
                TrackSpec("angle_y", "Y 軸", -3600, 3600, 0, unit="度"),
                TrackSpec("angle_z", "回転", -3600, 3600, 360, unit="度"),
                CheckSpec("three_d", "立体", False),
                *_pivot_specs(),
                *_in_out_specs(),
            ),
            fragment_shader=_INOUT_ROTATE,
        ),
        EffectDefinition(
            kind="inout_offset",
            label="ずれた所から登場",
            category="登場・退場",
            parameters=(
                TrackSpec("offset_x", "X", -4000, 4000, 100, step=1, unit="px"),
                TrackSpec("offset_y", "Y", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("offset_z", "手前へ", -4000, 4000, 0, step=1, unit="px"),
                *_in_out_specs(),
            ),
            fragment_shader=_INOUT_OFFSET,
        ),
        EffectDefinition(
            kind="inout_skew",
            label="傾いて登場",
            category="登場・退場",
            parameters=(
                TrackSpec("angle_x", "横の傾き", -89, 89, 30, unit="度"),
                TrackSpec("angle_y", "縦の傾き", -89, 89, 0, unit="度"),
                *_in_out_specs(),
            ),
            fragment_shader=_INOUT_SKEW,
        ),
        EffectDefinition(
            kind="inout_blur",
            label="ぼけて登場",
            category="登場・退場",
            parameters=(
                TrackSpec("radius", "範囲", 0, 96, 20, unit="px"),
                *_in_out_specs(),
            ),
            fragment_shader=_INOUT_BLUR,
            passes=2,
        ),
        EffectDefinition(
            kind="skew",
            label="斜め変形",
            category="変形",
            parameters=(
                TrackSpec("angle_x", "横の傾き", -89, 89, 0, unit="度"),
                TrackSpec("angle_y", "縦の傾き", -89, 89, 0, unit="度"),
                TrackSpec("center_x", "中心 X", -4000, 4000, 0, step=1, unit="px"),
                TrackSpec("center_y", "中心 Y", -4000, 4000, 0, step=1, unit="px"),
            ),
            fragment_shader=_SKEW,
        ),
        EffectDefinition(
            kind="spiral",
            label="渦巻き",
            category="変形",
            parameters=(
                TrackSpec("angle", "角度", -3600, 3600, 90, unit="度"),
                CheckSpec("outer", "外側ほど回す", False),
                *_pivot_specs(),
            ),
            fragment_shader=_SPIRAL,
            turns_object=True,
        ),
        EffectDefinition(
            kind="wave",
            label="波",
            category="変形",
            parameters=(
                TrackSpec("angle", "進む向き", -360, 360, 0, unit="度"),
                TrackSpec("direction", "揺れる向き", -360, 360, 90, unit="度"),
                TrackSpec("amplitude", "振幅", 0, 2000, 20, step=1, unit="px"),
                TrackSpec("wavelength", "波長", 1, 4000, 100, step=1, unit="px"),
                TrackSpec("period", "周期", 0, 60, 1, step=0.01, unit="秒"),
            ),
            fragment_shader=_WAVE_DISTORT,
        ),
        EffectDefinition(
            kind="mesh_deform",
            label="四隅の変形",
            category="変形",
            parameters=(
                *(
                    TrackSpec(
                        f"point{index}_{axis}", f"{corner} {axis.upper()}", -4000, 4000, 0, 1, "px"
                    )
                    for index, corner in enumerate(("左上", "右上", "右下", "左下"))
                    for axis in ("x", "y")
                ),
                # 四隅より細かい格子 互換層が入れる 入っていなければ上の四隅で動く
                # 点は四隅のスライダと同じく画面の画素のずれ 縮めずに渡すと、画質を
                # 落としたプレビューでだけ格子の歪みが 2 倍・4 倍に出る
                GridSpec("grid", "格子", maximum=MESH_MAX_POINTS, pixels=True),
            ),
            fragment_shader=_MESH,
        ),
        EffectDefinition(
            kind="circular_duplicate",
            label="円形に複製",
            category="変形",
            parameters=(
                ValueSpec("count", "個数", 8, 1, 64),
                TrackSpec("radius", "半径", 0, 4000, 100, step=1, unit="px"),
                TrackSpec("circumference", "円周の割合", 0, 100, 100, unit="%"),
                CheckSpec("synced", "向きを揃えて回す", True),
            ),
            fragment_shader=_CIRCULAR_DUPLICATE,
        ),
        EffectDefinition(
            kind="crash",
            label="破片になって崩れる",
            category="登場・退場",
            parameters=(
                TrackSpec("start", "開始", 0, 3600, 0, step=0.01, unit="秒"),
                TrackSpec("speed", "再生速度", 0, 1000, 100, unit="%"),
                TrackSpec("size", "欠片の大きさ", 4, 400, 50, step=1, unit="px"),
                TrackSpec("fly", "飛ぶ速さ", 0, 1000, 100, unit="%"),
                TrackSpec("fall", "落ちる速さ", 0, 1000, 100, unit="%"),
                TrackSpec("delay", "ばらつき", 0, 1000, 100, unit="%"),
                TrackSpec("impact", "衝撃", 0, 1000, 100, unit="%"),
                TrackSpec("spread", "散らばり", 0, 1000, 100, unit="%"),
                TrackSpec("spin", "欠片の回転", 0, 1000, 0, unit="%"),
            ),
            fragment_shader=_CRASH,
        ),
        EffectDefinition(
            kind="noise_displacement",
            label="ノイズで歪める",
            category="変形",
            parameters=(
                TrackSpec("amount_x", "X のずれ", -4000, 4000, 20, step=1, unit="px"),
                TrackSpec("amount_y", "Y のずれ", -4000, 4000, 20, step=1, unit="px"),
                SelectSpec(
                    "noise",
                    "ノイズ",
                    (
                        ("block", "ブロック"),
                        ("perlin", "パーリン"),
                        ("random", "ランダム"),
                        ("voronoi", "ボロノイ"),
                        ("cellular", "セル"),
                    ),
                    "perlin",
                ),
                TrackSpec("strength", "強さ", 0, 400, 100, unit="%"),
                TrackSpec("threshold", "しきい値", 0, 100, 0, unit="%"),
                TrackSpec("levels", "階調", 2, 256, 256, step=1),
                ValueSpec("octaves", "重ねる数", 5, 1, 8),
                TrackSpec("offset_x", "位置 X", -10000, 10000, 0, step=1, unit="px"),
                TrackSpec("offset_y", "位置 Y", -10000, 10000, 0, step=1, unit="px"),
                TrackSpec("offset_z", "位置 Z", -10000, 10000, 0, step=1),
                # 1 秒に進む画面の画素 単位の px/秒 は PIXEL_UNITS に無いので明に書く
                # 縮めないと、画質を落としたプレビューでだけ模様が速く流れる
                TrackSpec("speed_x", "速さ X", -10000, 10000, 0, step=1, unit="px/秒", pixels=True),
                TrackSpec("speed_y", "速さ Y", -10000, 10000, 0, step=1, unit="px/秒", pixels=True),
                TrackSpec("speed_z", "速さ Z", -10000, 10000, 0, step=1),
                TrackSpec("scale_x", "大きさ X", 1, 10000, 100, unit="%"),
                TrackSpec("scale_y", "大きさ Y", 1, 10000, 100, unit="%"),
                TrackSpec("scale_z", "大きさ Z", 1, 10000, 100, unit="%"),
            ),
            fragment_shader=_NOISE_DISPLACEMENT,
        ),
    )
    for definition in definitions:
        registry.register(definition)
