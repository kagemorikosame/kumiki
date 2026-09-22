"""エフェクトとキーフレームアニメーション

パラメータは「静的な値」と「キーフレーム列」を区別せず、:class:`AnimatedValue` に
統一している キーフレームが 0 個なら静的値として振る舞うので、UI もレンダラも
分岐を持たずに済む
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from sashimono.core.model.ids import EffectId, new_effect_id

__all__ = [
    "AnimatedValue",
    "Effect",
    "Interpolation",
    "Keyframe",
    "ParamValue",
]


class Interpolation(Enum):
    """キーフレーム間の補間方法

    AviUtl の「移動方法」に対応させる ``HOLD`` が瞬間移動、``LINEAR`` が直線移動、
    ``BEZIER`` が曲線移動にあたる イージング 3 種は頻出なので、毎回ベジェの制御点を
    指定させずに済むよう名前を与えている
    """

    HOLD = "hold"
    LINEAR = "linear"
    EASE_IN = "ease_in"
    EASE_OUT = "ease_out"
    EASE_IN_OUT = "ease_in_out"
    BEZIER = "bezier"


# イージング名から CSS と同じ形の 3 次ベジェ制御点へ
_EASING_CONTROL_POINTS: dict[Interpolation, tuple[float, float, float, float]] = {
    Interpolation.EASE_IN: (0.42, 0.0, 1.0, 1.0),
    Interpolation.EASE_OUT: (0.0, 0.0, 0.58, 1.0),
    Interpolation.EASE_IN_OUT: (0.42, 0.0, 0.58, 1.0),
}


@dataclass(frozen=True, slots=True)
class Keyframe:
    """1 つのキーフレーム

    ``frame`` はクリップ先頭からの相対フレーム タイムライン絶対位置ではないのは、
    クリップを移動してもアニメーションが崩れないようにするため
    """

    frame: int
    value: float
    interpolation: Interpolation = Interpolation.LINEAR
    #: BEZIER のときの制御点 (x1, y1, x2, y2) それ以外では無視される
    control_points: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if self.interpolation is Interpolation.BEZIER and self.control_points is None:
            raise ValueError("BEZIER には control_points が必要")


@dataclass(frozen=True, slots=True)
class AnimatedValue:
    """時間で変化しうる数値パラメータ

    キーフレームが空なら :attr:`static` を返し続ける 1 個以上あるときは
    :attr:`static` は無視され、キーフレーム列から補間される
    """

    static: float = 0.0
    keyframes: tuple[Keyframe, ...] = ()

    def __post_init__(self) -> None:
        frames = [k.frame for k in self.keyframes]
        if frames != sorted(frames):
            raise ValueError("キーフレームは frame の昇順でなければならない")
        if len(set(frames)) != len(frames):
            raise ValueError("同じ frame に複数のキーフレームを置けない")

    @property
    def is_animated(self) -> bool:
        return len(self.keyframes) > 0

    def at(self, frame: float) -> float:
        """クリップ先頭から ``frame`` 番目での値

        フレームの間（小数）も引ける 移動軌跡は半フレームずつ前後を見て、
        先端の向きを決める（整数に丸めると、速い動きで向きが 1 フレーム遅れる）

        最初のキーフレームより前、最後のキーフレームより後では、それぞれ端の値で
        頭打ちになる（外挿しない） 外挿すると、クリップをトリムしただけで
        画面外に飛んでいくような挙動になるため
        """
        if not self.keyframes:
            return self.static

        first = self.keyframes[0]
        if frame <= first.frame:
            return first.value
        last = self.keyframes[-1]
        if frame >= last.frame:
            return last.value

        left, right = self._surrounding(frame)
        if left.interpolation is Interpolation.HOLD:
            return left.value

        span = right.frame - left.frame
        progress = (frame - left.frame) / span
        eased = _ease(progress, left.interpolation, left.control_points)
        return left.value + (right.value - left.value) * eased

    def _surrounding(self, frame: float) -> tuple[Keyframe, Keyframe]:
        """``frame`` を挟む 2 つのキーフレームを返す"""
        # キーフレーム数は多くても数十なので線形探索で十分 ここが重くなったら
        # bisect に置き換える
        for left, right in zip(self.keyframes, self.keyframes[1:], strict=False):
            if left.frame <= frame < right.frame:
                return left, right
        raise AssertionError("端の判定を先に済ませているのでここには来ない")


def _ease(
    progress: float,
    interpolation: Interpolation,
    control_points: tuple[float, float, float, float] | None,
) -> float:
    """0..1 の進捗を補間曲線に通す"""
    if interpolation is Interpolation.LINEAR:
        return progress
    if interpolation is Interpolation.BEZIER:
        if control_points is None:
            raise ValueError("BEZIER には control_points が必要")
        points = control_points
    else:
        points = _EASING_CONTROL_POINTS[interpolation]
    return _cubic_bezier(progress, *points)


def _cubic_bezier(x: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """CSS の ``cubic-bezier()`` と同じ曲線 ``x`` に対する ``y`` を返す

    始点 (0,0) と終点 (1,1) は固定 曲線は媒介変数 t で定義されているので、
    まず ``x`` を与える t を求め、その t での y を返す
    """
    t = _solve_bezier_t(x, x1, x2)
    return _bezier_axis(t, y1, y2)


def _bezier_axis(t: float, p1: float, p2: float) -> float:
    """(0, p1, p2, 1) を制御点とする 3 次ベジェの t における値"""
    inv = 1.0 - t
    return 3.0 * inv * inv * t * p1 + 3.0 * inv * t * t * p2 + t * t * t


def _solve_bezier_t(x: float, x1: float, x2: float) -> float:
    """``_bezier_axis(t, x1, x2) == x`` となる t を求める

    ニュートン法で数回詰めてから二分法で仕上げる ニュートン法だけだと微分が
    0 に近い区間（制御点が端に張り付いた曲線）で発散するため、保険が要る
    """
    t = x
    for _ in range(8):
        error = _bezier_axis(t, x1, x2) - x
        if abs(error) < 1e-7:
            return t
        derivative = _bezier_axis_derivative(t, x1, x2)
        if abs(derivative) < 1e-7:
            break
        t -= error / derivative

    low, high = 0.0, 1.0
    t = x
    for _ in range(32):
        value = _bezier_axis(t, x1, x2)
        if abs(value - x) < 1e-7:
            break
        if value < x:
            low = t
        else:
            high = t
        t = (low + high) / 2.0
    return t


def _bezier_axis_derivative(t: float, p1: float, p2: float) -> float:
    inv = 1.0 - t
    return 3.0 * inv * inv * p1 + 6.0 * inv * t * (p2 - p1) + 3.0 * t * t * (1.0 - p2)


#: エフェクトのパラメータに入りうる値
#: 数値は :class:`AnimatedValue`、それ以外は静的な設定項目
ParamValue = AnimatedValue | bool | int | str | tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Effect:
    """クリップまたはトラックに適用される 1 つのエフェクト

    ``kind`` はエフェクトの種類を示す文字列 ID（例: ``"blur"``、
    ``"aviutl.script:震え@ふるえ"``） レンダラはこれを見て実装を引く
    AviUtl スクリプトも独自エフェクトも同じ器に載せるため、型ではなく ID にしている
    """

    kind: str
    params: dict[str, ParamValue] = field(default_factory=dict)
    enabled: bool = True
    id: EffectId = field(default_factory=new_effect_id)

    def with_param(self, name: str, value: ParamValue) -> Effect:
        """パラメータを 1 つ差し替えた新しい :class:`Effect` を返す"""
        return Effect(
            kind=self.kind,
            params={**self.params, name: value},
            enabled=self.enabled,
            id=self.id,
        )
