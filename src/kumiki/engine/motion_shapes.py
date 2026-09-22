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

import itertools
import math
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from kumiki.core.model import AnimatedValue, Interpolation

__all__ = [
    "MAX_STARS",
    "PERSPECTIVE",
    "StarField",
    "Trail",
    "TrailPath",
    "TrailPaths",
    "positions_from",
    "sample_value",
    "star_field",
    "start_path",
    "trail",
    "trail_path",
]

#: その時刻（クリップ頭からのフレーム 小数も取る）での位置 Y は下が正
PositionAt = Callable[[float], tuple[float, float]]

#: 整数のフレーム ``[始め, 終わり)`` の位置をまとめて返す ``(フレームの数, 2)``
PositionsIn = Callable[[int, int], np.ndarray]

#: 1 本の軌跡で打つ点の上限 壊れた値（描画間隔 0 で画面の端から端まで動く、など）でも
#: 1 フレームが何秒もかからないように頭を抑える
MAX_TRAIL_POINTS = 20000

#: 覚えておく道のフレームの数の合計の上限 1 フレームに 16 バイト使うので 32MB
#: 超えたら古く使った道から捨てる
MAX_CACHED_FRAMES = 2_000_000

#: 軌跡のために位置を引くフレームの数の上限（60fps で 9 時間余り）
#: 道は今のフレームまでだけを引き、描く側が伸ばしながら覚えておく（:class:`TrailPaths`）
#: 1 本の道が覚える量の上限を超えないよう、同じ値にそろえる これより先では軌跡が
#: 伸びなくなるが、壊れた長さのクリップで何十億フレームも引いて止まるよりよい
MAX_TRAIL_FRAMES = MAX_CACHED_FRAMES

#: 位置として扱う値の範囲（画素） 描く絵は一辺 8192 までなので十分に広い
#: 範囲の外の値を float32 にすると無限大になり、道のりが壊れて描画ごと止まる
_REACHABLE = 1.0e7

#: 先端の向きを探すときに前後へ広げる回数の上限（半フレームずつなので前後 1200 フレーム）
#: 止まったままの長いクリップでは、クリップの端まで探しても向きが決まらない
MAX_HEAD_STEPS = 2400

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


@dataclass(frozen=True, slots=True)
class TrailPath:
    """整数のフレームごとの位置と、頭からたどった道のり

    軌跡を描くたびにクリップ頭から位置を引き直すと、長いクリップほど 1 フレームが
    重くなる（再生全体では 2 乗） 描く側はこれを覚えておき、先へ進んだぶんだけ伸ばす
    位置は float32（画面の範囲なら 1/1000 画素より細かい）、道のりは長く足すので float64
    """

    points: np.ndarray
    reach: np.ndarray

    @property
    def frames(self) -> int:
        return len(self.points) - 1

    def extended(self, positions: PositionsIn, frames: int) -> TrailPath:
        """``frames`` まで伸ばした道 もう届いていればそのまま"""
        stop = min(frames, MAX_TRAIL_FRAMES)
        if stop <= self.frames:
            return self
        more = _clean(positions(self.frames + 1, stop + 1))
        joined = np.concatenate([self.points[-1:], more])
        lengths = np.hypot(*np.diff(joined.astype(np.float64), axis=0).T)
        reach = np.concatenate([self.reach, self.reach[-1] + np.cumsum(lengths)])
        return TrailPath(points=np.concatenate([self.points, more]), reach=reach)


def _clean(points: np.ndarray) -> np.ndarray:
    finite = np.nan_to_num(np.asarray(points, dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    cleaned: np.ndarray = np.clip(finite, -_REACHABLE, _REACHABLE).astype(np.float32)
    return cleaned


def start_path(positions: PositionsIn) -> TrailPath:
    """フレーム 0 だけの道"""
    return TrailPath(points=_clean(positions(0, 1)), reach=np.zeros(1))


def sample_value(value: AnimatedValue, first: int, stop: int) -> np.ndarray:
    """``AnimatedValue.at`` を整数のフレーム ``[first, stop)`` でまとめて引く

    直線と止まった区間は配列の計算で済ませる 何十万フレームを 1 つずつ引くと、
    長いクリップの初めの 1 枚に何秒もかかる イージングの区間だけ 1 つずつ引く
    （曲線の解き方を 2 か所に持たないため）
    """
    frames = np.arange(first, stop, dtype=np.float64)
    keys = value.keyframes
    if not keys:
        return np.full(len(frames), value.static, dtype=np.float64)
    out = np.empty(len(frames), dtype=np.float64)
    out[frames <= keys[0].frame] = keys[0].value
    out[frames >= keys[-1].frame] = keys[-1].value
    inner = (frames > keys[0].frame) & (frames < keys[-1].frame)
    for left, right in itertools.pairwise(keys):
        mask = inner & (frames >= left.frame) & (frames < right.frame)
        if not mask.any():
            continue
        if left.interpolation is Interpolation.HOLD or left.value == right.value:
            out[mask] = left.value
        elif left.interpolation is Interpolation.LINEAR:
            progress = (frames[mask] - left.frame) / (right.frame - left.frame)
            out[mask] = left.value + (right.value - left.value) * progress
        else:
            out[mask] = [value.at(float(at)) for at in frames[mask]]
    return out


def positions_from(position: PositionAt) -> PositionsIn:
    """1 つずつ引く位置を、まとめて引く形へ（試験や、式を持たない動きに使う）"""

    def many(first: int, stop: int) -> np.ndarray:
        return np.array([position(float(index)) for index in range(first, stop)], dtype=np.float64)

    return many


def trail_path(position: PositionAt, frames: float) -> TrailPath:
    """フレーム 0 から ``frames`` までの位置を引いて道にする"""
    positions = positions_from(position)
    return start_path(positions).extended(positions, max(math.ceil(frames), 0))


class TrailPaths:
    """動きごとの道を、要る所まで伸ばしながら覚えておく

    同じ動きのクリップを描くたびに、頭から全部の位置を引き直さない 初めて描くときも
    今のフレームまでしか引かない（クリップの長さぶんを先に引くと、長いクリップの
    初めの 1 枚が止まる） 覚える量は :data:`MAX_CACHED_FRAMES` で抑える
    """

    def __init__(self, budget: int = MAX_CACHED_FRAMES) -> None:
        self._budget = budget
        self._paths: OrderedDict[object, TrailPath] = OrderedDict()
        # プレビューと書き出しは別のスレッドで同時に描く 1 つの置き場を共有するので鍵を掛ける
        self._lock = threading.Lock()

    def get(self, key: object, positions: PositionsIn, frames: int) -> TrailPath:
        with self._lock:
            return self._get(key, positions, frames)

    def _get(self, key: object, positions: PositionsIn, frames: int) -> TrailPath:
        path = self._paths.pop(key, None)
        if path is None:
            path = start_path(positions)
        if frames > path.frames:
            # 少しずつ伸ばすと毎フレーム配列を作り直すので、倍ずつ先まで引く
            # （再生が進むたびに 1 フレームずつ足すと、つなぐ手間が 2 乗になる）
            path = path.extended(positions, max(frames, min(path.frames * 2, frames + 600)))
        self._paths[key] = path
        self._trim()
        return path

    def clear(self) -> None:
        with self._lock:
            self._paths.clear()

    def _trim(self) -> None:
        total = sum(len(path.points) for path in self._paths.values())
        while total > self._budget and len(self._paths) > 1:
            _, dropped = self._paths.popitem(last=False)
            total -= len(dropped.points)


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
    path: TrailPath | None = None,
    paths: Callable[[int], TrailPath] | None = None,
) -> Trail:
    """``ライン(移動軌跡)`` の形 実物の式を、道のりの計算に置き直して移した

    線は「一定の間隔ごとに円を押す」ことで引く 間隔は ``ライン幅 × 描画間隔 / 100``
    （``最小間隔`` と 1 画素より細かくはしない） 実物はフレームを 1 つずつ進めながら
    道のりを数えて円を置くが、置く所は「道のりが間隔の倍数の所」なので、道のりを
    先に数えておけば同じ所に置ける 間隔を広げると点線になるのは、この押し方だから

    ``固定速度`` が 0 なら、クリップ頭から今の位置までをたどる 0 より大きいと、
    動きの時刻に関係なく **1 フレームにその画素ずつ**道をたどって伸びていく

    時間はすべてクリップ頭からのフレームで持つ（実物は秒 ``f/obj.framerate`` で
    持つが、割って掛け戻すだけなので同じ位置になる） 道は ``paths``（そのフレーム
    まで届いた道を返す）から受け取る 渡されなければ ``path``、それも無ければここで引く
    """
    half = line_width / 2.0
    step = max(half * interval / 50.0, min_step, 1.0)
    if paths is None:
        whole = path if path is not None else trail_path(position, max(total, frame))

        def paths(_frames: int) -> TrailPath:
            return whole

    last_frame = min(math.ceil(max(total, 0.0)), MAX_TRAIL_FRAMES)
    path = paths(min(max(math.ceil(frame), 0), last_frame))
    if fixed_speed > 0:
        # 固定速度は道のりで伸びる 今の道のりに届くまで、クリップの終わりを上限に
        # 道を先へ伸ばす（動きより速く伸ばすと、今のフレームより先の道が要る）
        target = frame * fixed_speed
        while path.reach[-1] < target and path.frames < last_frame:
            path = paths(min(max(path.frames * 2, path.frames + 1), last_frame))
    frames = np.arange(len(path.points), dtype=np.float64)

    if fixed_speed > 0:
        # 1 フレームに固定速度ぶん クリップの終わりまでの道のりで止まる
        end = min(total, float(path.frames))
        limit = float(np.interp(end, frames, path.reach))
        walked = min(frame * fixed_speed, limit)
        now = _time_at(path, walked)
        last = _point_at(path, walked)
    else:
        now = frame
        walked = float(np.interp(min(frame, float(path.frames)), frames, path.reach))
        last = position(frame)

    # 道のりが間隔の倍数の所に円を押す 多すぎるときは今に近い側を残す
    # （古い側を残すと、今の位置との間が空いた線になる）
    # 道のりは位置を範囲へ収めたうえで数えているので有限だが、念のため見てから整数にする
    count = math.ceil(walked / step) if walked > 0 and math.isfinite(walked) else 0
    first = max(0, count - MAX_TRAIL_POINTS)
    distances = np.arange(first, count, dtype=np.float64) * step
    xs, ys = _points_at(path, distances)
    stamps = tuple(zip(xs.tolist(), ys.tolist(), strict=True))
    ends = [*stamps[1:], last] if stamps else []
    bands = tuple((x0, y0, x1, y1) for (x0, y0), (x1, y1) in zip(stamps, ends, strict=True))

    if head_size <= 0:
        return Trail(stamps, bands, last, None, 0.0)

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
    x0, y0 = last
    head = (x0 - math.sin(turn) * shift, y0 - math.cos(turn) * shift)
    # 実物の回し方は、図形の上（先端）を進む向きへ向ける回転になる
    # 画面の座標（Y は下が正）で時計回りを正とすると、ちょうど符号が逆
    return Trail(stamps, bands, last, head, -turn)


def _moving(path: TrailPath) -> np.ndarray:
    """道のりが伸びるフレームだけ 止まっている間の同じ道のりが並ぶと、道のりから
    位置を引くときにどのフレームを取るかが決まらない"""
    keep: np.ndarray = np.concatenate([[True], np.diff(path.reach) > 0])
    return keep


def _points_at(path: TrailPath, distances: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    keep = _moving(path)
    reach = path.reach[keep]
    return (
        np.interp(distances, reach, path.points[keep, 0]),
        np.interp(distances, reach, path.points[keep, 1]),
    )


def _point_at(path: TrailPath, distance: float) -> tuple[float, float]:
    xs, ys = _points_at(path, np.array([distance]))
    return float(xs[0]), float(ys[0])


def _time_at(path: TrailPath, distance: float) -> float:
    """その道のりに着いたフレーム（小数） 止まっている間は、着いた最初のフレーム"""
    keep = _moving(path)
    frames = np.arange(len(path.points), dtype=np.float64)[keep]
    return float(np.interp(distance, path.reach[keep], frames))


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
