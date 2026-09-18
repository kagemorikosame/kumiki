"""AviUtl のトラックバーに付く「移動方法」を読む

AviUtl は動きの付いた数値項目を 1 行で書く 値を並べ、最後に移動方法と旗を足す形

.. code-block:: none

    X=-0.01,149.16,300.00,直線移動,0
    中心X=0.00,0.00,0.00,補間移動,3
    Z軸回転=0.00,90.00,反復移動,4|15
    拡大率=100.000,100.000,200.000,瞬間移動,8|100+time*10
    縦横比=0.000
    再生位置=0.967,6.151,再生範囲,0

書き方は AviUtl2（v2.1.6a）に実際に作らせたエイリアスから読み取った
（``tests/fixtures/aviutl/README.md`` に作り方を書いてある）

- 値は中間点の数だけ並ぶ オブジェクトの ``frame=開始,中間点…,終了`` と対になる
  時間制御やスクリプトで動く移動方法は中間点を見ないので、値は 2 つのまま
- 旗はビットで、``1`` が加速、``2`` が減速、``4`` がスクリプトの設定値あり、
  ``8`` が参照式 ``|`` の後ろに設定値か式が入る
- 移動方法が書かれていなければ、ただの 1 つの値

**ここで写せるのは、値の並びと中間点までの補間**
時間制御の曲線・スクリプトの動き（ランダム移動・反復移動・回転・移動量指定）・
参照式は再現できないので、先頭の値だけ使って未対応として記録する
黙って直線で動かすと、実物と違う動きが出たまま気付けない
"""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Callable
from dataclasses import dataclass

from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.core.model import AnimatedValue, Interpolation, Keyframe

__all__ = [
    "Motion",
    "animated_value",
    "parse_motion",
]

#: ``値,値,…[,移動方法,旗[|設定値]]``
#: 参照式にはカンマが入りうるので、``|`` の後ろは切らずに丸ごと持つ
_LINE = re.compile(
    r"^(?P<values>-?\d+(?:\.\d+)?(?:\s*,\s*-?\d+(?:\.\d+)?)*)"
    r"(?:\s*,\s*(?P<method>[^,]+?)\s*,\s*(?P<flags>\d+)(?:\|(?P<extra>.*))?)?$"
)

#: 旗のビット
FLAG_ACCELERATE = 1
FLAG_DECELERATE = 2
FLAG_SCRIPT = 4
FLAG_EXPRESSION = 8

#: そのまま補間できる移動方法 値は中間点ごとの補間の仕方
_INTERPOLATIONS: dict[str, Interpolation] = {
    "直線移動": Interpolation.LINEAR,
    "直線移動(時間制御)": Interpolation.LINEAR,
    "直線移動(回転)": Interpolation.LINEAR,
    "補間移動": Interpolation.EASE_IN_OUT,
    "補間移動(時間制御)": Interpolation.EASE_IN_OUT,
    "補間移動(回転)": Interpolation.EASE_IN_OUT,
    "瞬間移動": Interpolation.HOLD,
    # 素材の再生範囲 値は秒で、動きではなく切り出す区間を表す
    "再生範囲": Interpolation.LINEAR,
}

#: 値が 1 つだけ並ぶときの書かれ方 空は移動方法そのものが書かれていない行
_STILL = frozenset({"", "移動無し"})

#: 中身を写せない移動方法 先頭の値で止めて記録に残す
_UNSUPPORTED = frozenset({"ランダム移動", "反復移動", "回転", "移動量指定"})

#: 時間制御の曲線を持つもの 値の並びは読めるが、時間の伸縮は写せない
_TIME_CONTROLLED = frozenset({"直線移動(時間制御)", "補間移動(時間制御)"})

#: 移動そのものを回転として扱うもの 中心の位置がファイルに出ないので写せない
_ROTATED = frozenset({"直線移動(回転)", "補間移動(回転)"})


@dataclass(frozen=True, slots=True)
class Motion:
    """1 つの数値項目に書かれていた内容"""

    #: 中間点ごとの値 動きが無ければ 1 つだけ
    values: tuple[float, ...]
    #: 移動方法の名前 無ければ空
    method: str = ""
    #: 旗（加速・減速・スクリプト・参照式）
    flags: int = 0
    #: ``|`` の後ろ スクリプトの設定値か参照式
    extra: str = ""

    @property
    def first(self) -> float:
        return self.values[0]

    @property
    def last(self) -> float:
        return self.values[-1]

    @property
    def moves(self) -> bool:
        """値が動くか 同じ値が並ぶだけなら動かないのと同じ"""
        return len(self.values) > 1 and any(value != self.values[0] for value in self.values)


def parse_motion(raw: str | None) -> Motion | None:
    """1 行の値を読む 数として読めなければ ``None``"""
    if raw is None:
        return None
    matched = _LINE.match(raw.strip())
    if matched is None:
        return None
    values = tuple(float(part) for part in matched["values"].split(","))
    if not all(math.isfinite(value) for value in values):
        # 正規表現の形からは来ないはずだが、来たら補間の先で GL の値が壊れる
        return None
    method = (matched["method"] or "").strip()
    flags = int(matched["flags"]) if matched["flags"] else 0
    return Motion(values=values, method=method, flags=flags, extra=matched["extra"] or "")


def _interpolation(motion: Motion) -> Interpolation:
    """中間点から次の中間点までの補間の仕方

    加速と減速はどちらも旗で付く 両方立っていれば両端が滑らかになる
    """
    base = _INTERPOLATIONS.get(motion.method, Interpolation.LINEAR)
    if base is not Interpolation.EASE_IN_OUT:
        return base
    accelerate = bool(motion.flags & FLAG_ACCELERATE)
    decelerate = bool(motion.flags & FLAG_DECELERATE)
    if accelerate and decelerate:
        return Interpolation.EASE_IN_OUT
    if accelerate:
        return Interpolation.EASE_IN
    if decelerate:
        return Interpolation.EASE_OUT
    return Interpolation.LINEAR


def _note(motion: Motion, log: CompatibilityReport, label: str) -> bool:
    """写せない所を記録する 返り値は「値の並びを使えるか」"""
    if motion.flags & FLAG_EXPRESSION:
        log.note_missing(f"AviUtl の参照式: {label}")
        return False
    if motion.method in _UNSUPPORTED:
        log.note_missing(f"AviUtl の移動方法: {motion.method}")
        return False
    if motion.method not in _INTERPOLATIONS and motion.method not in _STILL:
        log.note_missing(f"AviUtl の移動方法: {motion.method}")
        return False
    if motion.method in _TIME_CONTROLLED:
        # 時間制御の曲線はトラックバーの行に出ない 値の並びだけ写して記録に残す
        log.note_missing("AviUtl の時間制御（時間の伸縮は写せない）")
    if motion.method in _ROTATED:
        log.note_missing("AviUtl の移動方法の回転（中心の位置が書かれていない）")
    if motion.method.startswith("補間移動"):
        log.note_missing("AviUtl の補間移動（イージングで代用した近似）")
    return True


def animated_value(
    raw: str | None,
    *,
    points: tuple[int, ...],
    log: CompatibilityReport,
    label: str,
    default: float = 0.0,
    convert: Callable[[float], float] | None = None,
) -> AnimatedValue:
    """1 行の値を :class:`AnimatedValue` へ

    ``points`` はオブジェクトの開始・中間点・終了をクリップ先頭からのフレームで
    並べたもの 値の数がこれと合うときだけキーフレームを置く 合わないときは
    先頭と末尾を区間の両端に置く（時間制御やスクリプトの移動方法は中間点を
    見ないので、値が 2 つしか無い）

    ``convert`` は値を写すときの変換 Y のように向きが逆の項目で使う
    """
    scale = convert if convert is not None else (lambda value: value)
    motion = parse_motion(raw)
    if motion is None:
        # 項目そのものが無い行 既定値にも変換を掛ける（透明度 0 は不透明 1）
        return AnimatedValue(scale(default))

    # 写せるかどうかは先に見る 値が動かなくても、参照式やスクリプトなら
    # 時間で変わりうる 記録に残さないと、静止したことに気付けない
    usable = _note(motion, log, label)
    if not motion.moves or not usable:
        return AnimatedValue(scale(motion.first))

    frames = _frames_for(motion, points)
    if frames is None:
        log.note_missing(f"AviUtl の中間点と値の数が合わない: {label}")
        return AnimatedValue(scale(motion.first))
    interpolation = _interpolation(motion)
    keyframes = tuple(
        Keyframe(frame=frame, value=scale(value), interpolation=interpolation)
        for frame, value in zip(frames, motion.values, strict=True)
    )
    return AnimatedValue(scale(motion.first), keyframes)


def _frames_for(motion: Motion, points: tuple[int, ...]) -> tuple[int, ...] | None:
    """値 1 つずつを置くフレーム 置けなければ ``None``

    同じフレームが 2 つ並ぶ形は返さない ``AnimatedValue`` は同じフレームの
    キーフレームを拒む（例外になる） 1 フレームのオブジェクトや、中間点が
    前の点より手前にある壊れたファイルで起きる
    """
    if len(points) < 2:
        return None
    if len(motion.values) == len(points):
        chosen = points
    elif len(motion.values) == 2:
        # 中間点を見ない移動方法 区間の両端へ置く
        chosen = (points[0], points[-1])
    else:
        # 値と中間点の数が食い違うファイル
        return None
    if any(right <= left for left, right in itertools.pairwise(chosen)):
        return None
    return chosen
