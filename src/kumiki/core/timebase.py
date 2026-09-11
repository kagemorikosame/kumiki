"""時間表現とその相互変換。

編集ソフトでは同じ「時刻」が 4 つの単位で現れる。

* フレーム番号 — タイムライン上の位置。整数。
* 秒 — 人間向けの表示と、素材をまたぐときの共通単位。
* サンプル番号 — オーディオの位置。整数。
* PTS — コンテナ内の時刻。整数 + タイムベース。

これらを浮動小数で往復させると誤差が溜まり、長尺で音ズレになる。そこで秒は必ず
:class:`fractions.Fraction` で持ち、変換はすべてこのモジュールに集約する。プロジェクト全体で
ここ以外に変換式を書かないこと。

このモジュールは意図的に GUI にもエンジンにも依存しない。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

__all__ = [
    "FrameRate",
    "Rounding",
    "format_timecode",
    "frame_to_sample",
    "frame_to_seconds",
    "parse_timecode",
    "pts_to_seconds",
    "sample_to_frame",
    "sample_to_seconds",
    "seconds_to_frame",
    "seconds_to_pts",
    "seconds_to_sample",
]


class Rounding(Enum):
    """連続量を整数の位置へ落とすときの丸め方。

    どれを選ぶかは意味で決まる。「この秒数を含む最後のフレーム」を知りたいなら
    :attr:`FLOOR`、「最も近いフレーム」なら :attr:`NEAREST`、「この秒数以降の最初の
    フレーム」なら :attr:`CEIL`。既定値を置かず呼び出し側に必ず選ばせるのは、
    暗黙の丸めがフレームずれの温床になるため。
    """

    FLOOR = "floor"
    NEAREST = "nearest"
    CEIL = "ceil"


def _apply(value: Fraction, rounding: Rounding) -> int:
    """有理数を整数に落とす。

    :attr:`Rounding.NEAREST` は「0.5 は常に大きい方へ」とする。Python 組み込みの
    ``round`` は偶数丸めなので、隣り合うフレームで丸め方向が変わってしまい使えない。
    """
    if rounding is Rounding.FLOOR:
        return math.floor(value)
    if rounding is Rounding.CEIL:
        return math.ceil(value)
    return math.floor(value + Fraction(1, 2))


# 放送規格のフレームレート。小数入力をここに吸着させる。
_COMMON_RATES: tuple[Fraction, ...] = (
    Fraction(24000, 1001),
    Fraction(24),
    Fraction(25),
    Fraction(30000, 1001),
    Fraction(30),
    Fraction(48000, 1001),
    Fraction(48),
    Fraction(50),
    Fraction(60000, 1001),
    Fraction(60),
    Fraction(120000, 1001),
    Fraction(120),
)

# ドロップフレームが定義されているのは 29.97 と 59.94 のみ。
_DROP_FRAME_RATES: dict[Fraction, int] = {
    Fraction(30000, 1001): 2,
    Fraction(60000, 1001): 4,
}


@dataclass(frozen=True, slots=True, order=False)
class FrameRate:
    """フレームレート。29.97 のような分数レートを正確に保持する。

    ``num`` / ``den`` は既約である必要はないが、比較と辞書キーのために
    :meth:`__post_init__` で既約化する。
    """

    num: int
    den: int = 1

    def __post_init__(self) -> None:
        if self.num <= 0 or self.den <= 0:
            raise ValueError(f"フレームレートは正でなければならない: {self.num}/{self.den}")
        reduced = Fraction(self.num, self.den)
        object.__setattr__(self, "num", reduced.numerator)
        object.__setattr__(self, "den", reduced.denominator)

    @property
    def fps(self) -> Fraction:
        """1 秒あたりのフレーム数。"""
        return Fraction(self.num, self.den)

    @property
    def frame_duration(self) -> Fraction:
        """1 フレームの長さ（秒）。"""
        return Fraction(self.den, self.num)

    @property
    def nominal_fps(self) -> int:
        """タイムコード表示に使う整数フレームレート。

        29.97 なら 30、23.976 なら 24。タイムコードは実レートではなく
        この整数値でフレーム桁を数える。
        """
        return _apply(self.fps, Rounding.NEAREST)

    @property
    def is_drop_frame_capable(self) -> bool:
        """ドロップフレームタイムコードが定義されているレートか。"""
        return self.fps in _DROP_FRAME_RATES

    @classmethod
    def parse(cls, text: str) -> FrameRate:
        """``"30"`` ``"29.97"`` ``"30000/1001"`` のいずれかから生成する。"""
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("フレームレートが空文字列")
        if "/" in cleaned:
            num_text, _, den_text = cleaned.partition("/")
            return cls(int(num_text), int(den_text))
        return cls.from_decimal(Fraction(cleaned))

    @classmethod
    def from_decimal(cls, value: Fraction | float) -> FrameRate:
        """小数表記から生成する。放送規格に十分近ければそちらへ吸着させる。

        29.97 と書かれたものは 2997/100 ではなく 30000/1001 を意図している。
        この吸着を入れないと、1 時間の素材で 3 フレーム以上ずれる。
        """
        exact = Fraction(value).limit_denominator(1_000_000)
        if exact <= 0:
            raise ValueError(f"フレームレートは正でなければならない: {value}")
        for candidate in _COMMON_RATES:
            # 0.01% 以内なら同一とみなす。29.97 と 30000/1001 の差は約 0.003%。
            if abs(candidate - exact) / candidate < Fraction(1, 10_000):
                return cls(candidate.numerator, candidate.denominator)
        return cls(exact.numerator, exact.denominator)

    def __str__(self) -> str:
        if self.den == 1:
            return str(self.num)
        return f"{float(self.fps):.3f}".rstrip("0").rstrip(".")


def frame_to_seconds(frame: int, rate: FrameRate) -> Fraction:
    """フレーム番号をその開始時刻（秒）へ。誤差なし。"""
    return frame * rate.frame_duration


def seconds_to_frame(
    seconds: Fraction | int, rate: FrameRate, rounding: Rounding = Rounding.FLOOR
) -> int:
    """秒をフレーム番号へ。

    既定が :attr:`Rounding.FLOOR` なのは「その時刻に表示されているフレーム」を
    返すのが再生位置として自然なため。
    """
    return _apply(Fraction(seconds) * rate.fps, rounding)


def seconds_to_sample(
    seconds: Fraction | int, sample_rate: int, rounding: Rounding = Rounding.NEAREST
) -> int:
    """秒をサンプル番号へ。

    既定が :attr:`Rounding.NEAREST` なのは、音は 1 サンプルの切り捨てより
    最寄りへの吸着の方が波形の連続性を保てるため。
    """
    _check_sample_rate(sample_rate)
    return _apply(Fraction(seconds) * sample_rate, rounding)


def sample_to_seconds(sample: int, sample_rate: int) -> Fraction:
    """サンプル番号を秒へ。誤差なし。"""
    _check_sample_rate(sample_rate)
    return Fraction(sample, sample_rate)


def frame_to_sample(
    frame: int, rate: FrameRate, sample_rate: int, rounding: Rounding = Rounding.NEAREST
) -> int:
    """フレーム番号を、そのフレームが始まるサンプル番号へ。"""
    return seconds_to_sample(frame_to_seconds(frame, rate), sample_rate, rounding)


def sample_to_frame(
    sample: int, sample_rate: int, rate: FrameRate, rounding: Rounding = Rounding.FLOOR
) -> int:
    """サンプル番号を、それを含むフレーム番号へ。"""
    return seconds_to_frame(sample_to_seconds(sample, sample_rate), rate, rounding)


def pts_to_seconds(pts: int, time_base: Fraction) -> Fraction:
    """コンテナの PTS を秒へ。``time_base`` は PyAV の ``stream.time_base``。"""
    if time_base <= 0:
        raise ValueError(f"タイムベースは正でなければならない: {time_base}")
    return pts * time_base


def seconds_to_pts(
    seconds: Fraction | int, time_base: Fraction, rounding: Rounding = Rounding.FLOOR
) -> int:
    """秒をコンテナの PTS へ。

    既定が :attr:`Rounding.FLOOR` なのはシーク用途を想定しているため。目的の時刻を
    超えない位置へシークしておけば、そこから前進デコードして目的フレームに到達できる。
    切り上げてしまうと目的のフレームを飛び越す。
    """
    if time_base <= 0:
        raise ValueError(f"タイムベースは正でなければならない: {time_base}")
    return _apply(Fraction(seconds) / time_base, rounding)


_TIMECODE_RE = re.compile(
    r"^(?P<sign>-)?(?P<hours>\d+):(?P<minutes>\d{1,2})"
    r":(?P<seconds>\d{1,2})(?P<sep>[:;])(?P<frames>\d{1,3})$"
)


def format_timecode(frame: int, rate: FrameRate, *, drop_frame: bool | None = None) -> str:
    """フレーム番号を ``HH:MM:SS:FF`` 形式のタイムコードへ。

    ドロップフレームでは慣例に従い最後の区切りを ``;`` にする。``drop_frame`` を
    省略した場合、29.97 / 59.94 では有効、それ以外では無効になる。ドロップフレームは
    フレームを間引くのではなく番号を飛ばす方式なので、映像は 1 コマも失われない。
    """
    if drop_frame is None:
        drop_frame = rate.is_drop_frame_capable
    if drop_frame and not rate.is_drop_frame_capable:
        raise ValueError(f"{rate} にドロップフレームは定義されていない")

    sign = "-" if frame < 0 else ""
    counted = abs(frame)
    nominal = rate.nominal_fps

    if drop_frame:
        counted = _to_drop_frame_number(counted, rate)

    frames = counted % nominal
    total_seconds = counted // nominal
    seconds = total_seconds % 60
    minutes = (total_seconds // 60) % 60
    hours = total_seconds // 3600

    separator = ";" if drop_frame else ":"
    width = len(str(nominal - 1))
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{frames:0{width}d}"


def parse_timecode(text: str, rate: FrameRate, *, drop_frame: bool | None = None) -> int:
    """``HH:MM:SS:FF`` 形式のタイムコードをフレーム番号へ。

    区切りが ``;`` ならドロップフレームとみなす。``drop_frame`` を明示した場合は
    そちらが優先される。
    """
    match = _TIMECODE_RE.match(text.strip())
    if match is None:
        raise ValueError(f"タイムコードとして解釈できない: {text!r}")

    if drop_frame is None:
        drop_frame = match["sep"] == ";"
    if drop_frame and not rate.is_drop_frame_capable:
        raise ValueError(f"{rate} にドロップフレームは定義されていない")

    nominal = rate.nominal_fps
    hours = int(match["hours"])
    minutes = int(match["minutes"])
    seconds = int(match["seconds"])
    frames = int(match["frames"])

    if minutes > 59 or seconds > 59:
        raise ValueError(f"分・秒が範囲外: {text!r}")
    if frames >= nominal:
        raise ValueError(f"フレーム桁が {rate} の範囲外: {text!r}")

    counted = ((hours * 60 + minutes) * 60 + seconds) * nominal + frames
    if drop_frame:
        counted = _from_drop_frame_number(counted, hours, minutes, rate)
        if counted < 0:
            raise ValueError(f"存在しないドロップフレームタイムコード: {text!r}")

    return -counted if match["sign"] else counted


def _to_drop_frame_number(frame: int, rate: FrameRate) -> int:
    """実フレーム数を、番号を飛ばした後のカウントへ変換する。

    29.97fps は 1 秒あたり 30 フレームより僅かに少ないので、30 で数え続けると
    実時間から 1 時間あたり 3.6 秒ずれる。そこで 10 分ごとの 9 分間について
    毎分先頭の 2 番を欠番にし、時計と一致させる。
    """
    drop = _DROP_FRAME_RATES[rate.fps]
    nominal = rate.nominal_fps
    frames_per_10min = _apply(rate.fps * 600, Rounding.NEAREST)
    frames_per_min = nominal * 60 - drop

    blocks, remainder = divmod(frame, frames_per_10min)
    skipped = drop * 9 * blocks
    if remainder > drop:
        skipped += drop * ((remainder - drop) // frames_per_min)
    return frame + skipped


def _from_drop_frame_number(counted: int, hours: int, minutes: int, rate: FrameRate) -> int:
    """:func:`_to_drop_frame_number` の逆変換。"""
    drop = _DROP_FRAME_RATES[rate.fps]
    total_minutes = hours * 60 + minutes
    return counted - drop * (total_minutes - total_minutes // 10)


def _check_sample_rate(sample_rate: int) -> None:
    if sample_rate <= 0:
        raise ValueError(f"サンプリングレートは正でなければならない: {sample_rate}")
