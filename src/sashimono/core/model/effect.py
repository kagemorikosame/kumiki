"""エフェクトとキーフレームアニメーション

パラメータは「静的な値」と「キーフレーム列」を区別せず、:class:`AnimatedValue` に
統一している キーフレームが 0 個なら静的値として振る舞うので、UI もレンダラも
分岐を持たずに済む
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from sashimono.core.model.easing import CURVES, ease
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
    #: イージング 3 種のときの曲線の名前（``back`` ``expo`` など） 空なら CSS と同じ曲線
    #: YMM4 の移動方法（``Back_InOut`` など）は曲線ごとに形が違う 3 種の曲線へ丸めると、
    #: 行き過ぎて戻る動きが消え、ローテンショントランジションの回り方が最大 30 度ずれた
    curve: str = ""

    def __post_init__(self) -> None:
        if self.interpolation is Interpolation.BEZIER and self.control_points is None:
            raise ValueError("BEZIER には control_points が必要")
        if self.curve and self.curve not in CURVES:
            raise ValueError(f"未知の曲線: {self.curve!r}")
        if self.curve and self.interpolation not in _CURVE_MODES:
            # 直線や瞬間移動では曲線を読まない 持たせると、効かない名前がファイルに残り、
            # 読んだ人が「Back で動くはず」と取り違える
            raise ValueError(f"曲線の名前はイージングの点にだけ付く: {self.interpolation.value}")


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
        eased = _ease(progress, left.interpolation, left.control_points, left.curve)
        return left.value + (right.value - left.value) * eased

    def with_keyframe_at(self, frame: int) -> AnimatedValue:
        """``frame`` に点を足し、どのフレームの値も足す前と変えない値

        タイムラインの線の Ctrl+クリックで使う 線の上に点を打っただけで形が変わると、
        フェードの途中へ点を足して後半だけ直す、ということができない
        新しい点の値は足す前のその位置の値 区間の途中へ足すときは、左の点の出方で分ける
        - 直線 そのまま 2 本の直線になる
        - 瞬間移動 新しい点も瞬間移動にする（左の値のまま次の点まで止まる）
        - イージング 3 種とベジェ 曲線を新しい点で 2 つに切り（de Casteljau）、左右の点に
          切った後の制御点を持たせる
        - 名前付きの曲線（``back`` など） 切れる形ではないので、左の点の出方を引き継ぐ
          この区間だけは形が変わる
        すでに点のあるフレームなら何も変えない
        """
        level = self.at(frame)
        if any(k.frame == frame for k in self.keyframes):
            return self
        added = Keyframe(frame=frame, value=level)
        keyframes = list(self.keyframes)
        # 最初の点より前と最後の点より後は値が端で止まっている 同じ値の点を足しても形は変わらない
        if self.keyframes and self.keyframes[0].frame < frame < self.keyframes[-1].frame:
            left, right = self._surrounding(frame)
            index = keyframes.index(left)
            progress = (frame - left.frame) / (right.frame - left.frame)
            keyframes[index], added = _split_segment(left, added, progress)
        keyframes.append(added)
        return AnimatedValue(
            static=self.static, keyframes=tuple(sorted(keyframes, key=lambda k: k.frame))
        )

    def _surrounding(self, frame: float) -> tuple[Keyframe, Keyframe]:
        """``frame`` を挟む 2 つのキーフレームを返す"""
        # キーフレーム数は多くても数十なので線形探索で十分 ここが重くなったら
        # bisect に置き換える
        for left, right in zip(self.keyframes, self.keyframes[1:], strict=False):
            if left.frame <= frame < right.frame:
                return left, right
        raise AssertionError("端の判定を先に済ませているのでここには来ない")


#: イージング 3 種と、名前付きの曲線の向き
_CURVE_MODES: dict[Interpolation, str] = {
    Interpolation.EASE_IN: "in",
    Interpolation.EASE_OUT: "out",
    Interpolation.EASE_IN_OUT: "inout",
}


def _ease(
    progress: float,
    interpolation: Interpolation,
    control_points: tuple[float, float, float, float] | None,
    curve: str = "",
) -> float:
    """0..1 の進捗を補間曲線に通す"""
    if interpolation is Interpolation.LINEAR:
        return progress
    mode = _CURVE_MODES.get(interpolation)
    if curve and mode is not None:
        return ease(progress, curve, mode)
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


def _split_segment(left: Keyframe, added: Keyframe, progress: float) -> tuple[Keyframe, Keyframe]:
    """左の点から次の点までの区間を ``progress`` の所で切り、``(左の点, 足す点)`` を返す

    足す点の値は切る前のその位置の値（:meth:`AnimatedValue.at`）で渡される
    """
    if left.interpolation is Interpolation.LINEAR:
        return left, added
    if left.interpolation is Interpolation.HOLD:
        return left, replace(added, interpolation=Interpolation.HOLD)
    if left.curve:
        return left, replace(added, interpolation=left.interpolation, curve=left.curve)
    points = (
        left.control_points
        if left.interpolation is Interpolation.BEZIER and left.control_points is not None
        else _EASING_CONTROL_POINTS[left.interpolation]
    )
    x1, y1, x2, y2 = points
    t = _solve_bezier_t(progress, x1, x2)
    # de Casteljau で (0,0) (x1,y1) (x2,y2) (1,1) を t で 2 つに切る
    ax, ay = x1 * t, y1 * t
    bx, by = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
    cx, cy = x2 + (1.0 - x2) * t, y2 + (1.0 - y2) * t
    dx, dy = ax + (bx - ax) * t, ay + (by - ay) * t
    ex, ey = bx + (cx - bx) * t, by + (cy - by) * t
    mx, my = dx + (ex - dx) * t, dy + (ey - dy) * t
    if not (1e-6 < mx < 1 - 1e-6 and abs(my) > 1e-6 and abs(1.0 - my) > 1e-6):
        # 切った所で値が端と同じだと、縦を 0〜1 に引き伸ばせない 左の出方を引き継ぐ
        return left, replace(added, interpolation=left.interpolation, control_points=points)
    first = (ax / mx, ay / my, dx / mx, dy / my)
    second = (
        (ex - mx) / (1.0 - mx),
        (ey - my) / (1.0 - my),
        (cx - mx) / (1.0 - mx),
        (cy - my) / (1.0 - my),
    )
    return (
        replace(left, interpolation=Interpolation.BEZIER, control_points=first, curve=""),
        replace(added, interpolation=Interpolation.BEZIER, control_points=second),
    )


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

    ``fixed`` はクリップが最初から持つ項目（YMM4 の描画・音声の欄にあたる）の印
    外すことと並べ替えることを命令の側で断る 無効にはできる 同じ種類を重ねて
    掛けたいときは、印の無いふつうのエフェクトとして足す
    """

    kind: str
    params: dict[str, ParamValue] = field(default_factory=dict)
    enabled: bool = True
    id: EffectId = field(default_factory=new_effect_id)
    fixed: bool = False

    def with_param(self, name: str, value: ParamValue) -> Effect:
        """パラメータを 1 つ差し替えた新しい :class:`Effect` を返す

        ``replace`` で作る 欄を並べて作り直すと、あとで足した欄（``fixed`` など）を
        書き忘れたときに、値を 1 つ触っただけで印が落ちる
        """
        return replace(self, params={**self.params, name: value})
