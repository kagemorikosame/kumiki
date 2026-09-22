"""時間を追わないと形が決まらない図形の、形の計算（移動軌跡と星空）

どちらも AviUtl2 のカスタムオブジェクト（本体に付いてくる ``script.obj2`` の
``ライン(移動軌跡)`` と ``星``）を写したもの 1 フレームの設定だけでは絵が決まらず、

* 移動軌跡は、クリップ頭から今までに**通った位置**をたどって線にする
* 星空は、粒ごとに**時間で進む位相**を持ち、奥から手前へ流れてくる

描くのは :mod:`kumiki.engine.sources`（Qt） ここは数だけを扱い、Qt を持ち込まない
形の決まりを Qt 抜きで試験できるようにするため

座標はどちらも**画面の中心からの画素で、Y は下が正**（AviUtl のまま） 描く側で
画面へ置くときに Y の向きを直さずに済み、実物の式と 1 行ずつ突き合わせられる
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

__all__ = [
    "MAX_STARS",
    "PERSPECTIVE",
    "StarField",
    "Trail",
    "star_field",
    "trail",
]

#: その時刻（クリップ頭からのフレーム 小数も取る）での位置 Y は下が正
PositionAt = Callable[[float], tuple[float, float]]

#: 1 本の軌跡で打つ点の上限 壊れた値（描画間隔 0 で画面の端から端まで動く、など）でも
#: 1 フレームが何秒もかからないように頭を抑える
MAX_TRAIL_POINTS = 20000

#: 1 本の軌跡のために位置を引くフレームの数の上限（60fps で 5 分半）
#: 実物は毎フレーム、クリップ頭から今までの位置を引き直す 止まっている長い区間では
#: 押す点が増えないので点の上限が効かず、長いクリップの再生がフレームごとに重くなる
#: これより長い軌跡は、今から上限ぶん前のフレームから描く（それより前の跡は出ない）
MAX_TRAIL_FRAMES = 20000

#: 先端の向きを探すときに前後へ広げる回数の上限（半フレームずつなので前後 5000 フレーム）
#: 止まったままの長いクリップでは、クリップの端まで探しても向きが決まらない
MAX_HEAD_STEPS = 10000

#: 先端の向きを決めるのに要る移動量（画素） 実物が ``4*4`` と書いている値
_HEAD_REACH = 16.0

#: 星空の粒の数の上限 実物の ``個数`` の範囲の上限と同じ
MAX_STARS = 5000

#: AviUtl の奥行きの見え方 Z が 0 の面で等倍、カメラは手前 1024 の所にある
#: 星空の粒の広がりと速さを実物から測ると、この値で合った（:func:`star_field`）
PERSPECTIVE = 1024.0


@dataclass(frozen=True, slots=True)
class Trail:
    """移動軌跡の形

    ``stamps`` は線の芯として押す円の中心 ``bands`` はつなぐ帯（始点と終点）
    ``head`` は先端の図形の中心 先端を描かないときは ``None``
    ``head_turn`` は先端の図形を回す角度（ラジアン、画面で時計回りが正）
    """

    stamps: tuple[tuple[float, float], ...]
    bands: tuple[tuple[float, float, float, float], ...]
    last: tuple[float, float]
    head: tuple[float, float] | None
    head_turn: float


def trail(
    position: PositionAt,
    *,
    frame: float,
    total: float,
    line_width: float,
    interval: float,
    min_step: float,
    fixed_speed: float,
    head_size: float,
    head_angle: float,
    head_offset: float,
) -> Trail:
    """``ライン(移動軌跡)`` の形 実物の式をそのまま移した

    線は「一定の間隔ごとに円を押す」ことで引く 間隔は ``ライン幅 × 描画間隔 / 100``
    （``最小間隔`` と 1 画素より細かくはしない） 間隔を広げると点線になるのは、
    この押し方だから 折れ線として引くと、間隔の設定が効かなくなる

    ``固定速度`` が 0 なら、クリップ頭から今の位置までをたどる 0 より大きいと、
    動きの時刻に関係なく **1 フレームにその画素ずつ**道をたどって伸びていく

    時間はすべてクリップ頭からのフレームで持つ（実物は秒 ``f/obj.framerate`` で
    持つが、割って掛け戻すだけなので同じ位置になる）
    """
    half = line_width / 2.0
    step = max(half * interval / 50.0, min_step, 1.0)
    # 押す円の間隔の数で頭を抑える 間隔を細かくした長い道でも止まらない
    budget = MAX_TRAIL_POINTS
    # 固定速度はクリップ頭から道をたどって伸びるので、頭から数える
    # 動きどおりなら今から上限ぶん前から たどるフレームの数を上限で抑える
    first = 0 if fixed_speed > 0 else max(0, math.floor(frame) - MAX_TRAIL_FRAMES)
    last_frame = min(max(total, frame) + 2.0, float(first + MAX_TRAIL_FRAMES))

    now = float(first)
    visited = first
    left = 0.0
    run = 1.0
    walked = 0.0
    end_x, end_y = position(float(first))
    start_x, start_y = end_x, end_y
    x0, y0 = end_x, end_y
    x1, y1 = x0, y0
    stamps: list[tuple[float, float]] = []
    bands: list[tuple[float, float, float, float]] = []

    while now < frame and budget > 0:
        budget -= 1
        remaining = step
        while remaining > 0:
            if left <= 0:
                if now >= frame or visited > last_frame:
                    break
                start_x, start_y = end_x, end_y
                visited += 1
                now = float(visited)
                end_x, end_y = position(now)
                left = math.hypot(end_x - start_x, end_y - start_y)
                run = left
            if left > remaining:
                walked += remaining
                left -= remaining
                remaining = 0.0
                x1 = end_x + (start_x - end_x) * left / run
                y1 = end_y + (start_y - end_y) * left / run
            else:
                walked += left
                remaining -= left
                left = 0.0
                x1, y1 = end_x, end_y
            if fixed_speed > 0:
                if now >= total:
                    break
                now = walked / fixed_speed
            elif left > 0:
                now = visited - left / run
        stamps.append((x0, y0))
        bands.append((x0, y0, x1, y1))
        x0, y0 = x1, y1
        if visited > last_frame:
            break

    if fixed_speed > 0:
        # 止まっている所（進む量 0）で割らない 実物はここで 0 を 0 で割り、
        # 先端の向きが決まらないまま描く
        now = visited - (left / run if run > 0 else 0.0)
    else:
        now = frame
        x0, y0 = position(frame)
    last = (x0, y0)

    if head_size <= 0:
        return Trail(tuple(stamps), tuple(bands), last, None, 0.0)

    # 先端の向き 今の時刻から前後へ半フレームずつ広げ、16 画素以上動いた所で決める
    # 止まっている間は上向き（角度 0）
    turn = 0.0
    back = ahead = now
    for _ in range(MAX_HEAD_STEPS):
        if not (back > 0 or ahead < total):
            break
        back = max(back - 0.5, 0.0)
        ahead = min(ahead + 0.5, total)
        before, after = position(back), position(ahead)
        dx, dy = before[0] - after[0], before[1] - after[1]
        if math.hypot(dx, dy) >= _HEAD_REACH:
            turn = math.atan2(dx, dy)
            break
    turn += math.radians(head_angle)
    # 先端位置補正 50% で図形の中心が今の位置に来る 70% なら大きさの 2 割だけ先へ出る
    shift = head_size * (head_offset - 50.0) / 100.0
    head = (x0 - math.sin(turn) * shift, y0 - math.cos(turn) * shift)
    # 実物の回し方は、図形の上（先端）を進む向きへ向ける回転になる
    # 画面の座標（Y は下が正）で時計回りを正とすると、ちょうど符号が逆
    return Trail(tuple(stamps), tuple(bands), last, head, -turn)


@dataclass(frozen=True, slots=True)
class StarField:
    """星空の粒 1 つずつの中心・大きさの倍率・不透明度 見えない粒は含めない"""

    x: np.ndarray
    y: np.ndarray
    scale: np.ndarray
    alpha: np.ndarray


def star_field(
    *,
    seconds: float,
    count: float,
    speed: float,
    spread: float,
    depth: float,
    fade_in: float,
    fade_out: float,
    screen_width: float,
    screen_height: float,
) -> StarField:
    """``星`` の粒 実物の式をそのまま移した

    粒は ``i/個数`` ずつずらした位相を持ち、1 周する間に奥（``奥行き × 1024``）から
    手前（カメラの前 512）まで来る 1 周するたびに、置く位置を引き直す
    位置は画面の大きさ × ``広がり`` の範囲に散らばり、遠近で縮めて見せる

    速さは ``速度 / 奥行き`` 周（1 秒あたり） 奥行きを深くすると遅くなるのは実物のまま

    乱数は実物と同じ並びにはならない（``obj.rand`` の中身は公開されていない）
    同じ種と周回からは同じ位置が出るので、同じフレームは何度描いても同じ絵になる
    """
    # 読み込んだファイルやキーフレームの値は、設定の範囲（5000 まで）を守っていない
    # ことがある そのまま個数にすると巨大な配列を作って描画が止まる
    n = int(min(max(count, 0), MAX_STARS)) if math.isfinite(count) else 0
    if n == 0:
        empty = np.zeros(0)
        return StarField(empty, empty, empty, empty)
    rate = speed / depth if depth != 0 else speed
    if rate != 0:
        # フェードの秒も周の単位へ直す 実物は速さが負なら負のまま比べる（効かなくなる）
        fade_in *= rate
        fade_out *= rate
    phase_base = rate * seconds

    index = np.arange(1, n + 1, dtype=np.float64)
    phase = phase_base + index / n
    lap = np.floor(phase)
    phase = phase - lap
    z = (1.0 - phase) * depth * 1024.0 - 512.0
    half_w, half_h = screen_width / 2.0, screen_height / 2.0
    x = _rand(-half_w, half_w, index, lap) * spread
    y = _rand(-half_h, half_h, index + n, lap) * spread

    alpha = np.ones(n)
    if fade_in != 0:
        early = phase < fade_in
        alpha = np.where(early, alpha * phase / fade_in, alpha)
    if fade_out != 0:
        late = 1.0 - phase < fade_out
        alpha = np.where(late, alpha * (1.0 - phase) / fade_out, alpha)

    # カメラより手前（後ろ）へ来た粒は映らない 奥行き 0 でも 512 手前に居るだけなので
    # 実物の値の範囲では起きないが、割り算を 0 で割らないために外す
    distance = PERSPECTIVE + z
    # 無限大や非数（壊れた速さ・広がり）で出た粒も外す 描く側で座標が壊れる
    seen = (
        (distance > 1e-6)
        & (alpha > 0)
        & np.isfinite(distance)
        & np.isfinite(alpha)
        & np.isfinite(x)
        & np.isfinite(y)
    )
    scale = np.where(seen, PERSPECTIVE / np.where(seen, distance, 1.0), 0.0)
    return StarField(
        x=(x * scale)[seen],
        y=(y * scale)[seen],
        scale=scale[seen],
        alpha=np.clip(alpha, 0.0, 1.0)[seen],
    )


def _rand(low: float, high: float, seed: np.ndarray, lap: np.ndarray) -> np.ndarray:
    """``obj.rand(low, high, seed, frame)`` の代わり 両端を含む整数を返す

    種と周回の組から決まる（時刻には依らない） 同じ粒は 1 周の間同じ所に居て、
    周回が変わったときだけ置き直される 実物もこの決まりで、途中で位置を
    引き直すと、流れていく粒が毎フレーム跳ぶ
    """
    key = seed.astype(np.int64).astype(np.uint64) * np.uint64(0x9E3779B97F4A7C15)
    key ^= lap.astype(np.int64).astype(np.uint64) * np.uint64(0xC2B2AE3D27D4EB4F)
    # splitmix64 の混ぜ方 種が 1 つ違うだけでも、まったく別の値になるようにする
    key ^= key >> np.uint64(30)
    key *= np.uint64(0xBF58476D1CE4E5B9)
    key ^= key >> np.uint64(27)
    key *= np.uint64(0x94D049BB133111EB)
    key ^= key >> np.uint64(31)
    unit = (key >> np.uint64(11)).astype(np.float64) / float(1 << 53)
    lo, hi = math.ceil(low), math.floor(high)
    if hi < lo:
        return np.full(seed.shape, float(lo))
    return np.floor(lo + unit * (hi - lo + 1)).astype(np.float64)
