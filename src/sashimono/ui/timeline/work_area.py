"""タイムラインの書き出し範囲 目盛りの上の Shift+ドラッグで決め、帯で見せる

:class:`~sashimono.ui.timeline.view.TimelineView` からは入口を数行呼ぶだけにして、
決め方・描き方・右クリックの項目はここにまとめる ビューはほかの操作（クリップの
移動や再生ヘッド）も抱えていて、範囲の都合を中へ混ぜると読み分けにくくなる
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QMenu

from sashimono.core.commands import Command, SetWorkArea
from sashimono.ui.theme import Colors, Metrics
from sashimono.ui.timeline.layout import TimelineLayout

__all__ = ["CLEAR_TEXT", "HINT_TEXT", "WorkAreaEditor"]

#: 右クリックに出す名前 目盛りや帯の上で出すので「書き出し」は付けない テストもこの名前で探す
CLEAR_TEXT = "範囲を解除"
#: 範囲が無いときに目盛りの右クリックへ出す案内 Shift+ドラッグは見ただけでは分からない
HINT_TEXT = "目盛りを Shift+ドラッグで書き出し範囲を指定"

#: 目盛りの上の帯の高さ（画素） 目盛りの字に被せると時刻が読めなくなるので、下の端に細く敷く
_BAND_HEIGHT = 6

Area = tuple[int, int]


class WorkAreaEditor:
    """書き出し範囲を決める操作と、その見せ方

    ドラッグの途中は描くだけで、離したときに :class:`SetWorkArea` を 1 つ出す
    途中をコマンドにすると、取り消しの履歴が動かした分だけ積もる
    """

    def __init__(self, request: Callable[[list[Command], str], None]) -> None:
        self._request = request
        #: ドラッグを始めた縁（フレーム）と、今の縁 ドラッグしていなければ ``None``
        self._anchor: int | None = None
        self._current = 0

    @property
    def dragging(self) -> bool:
        return self._anchor is not None

    def press(
        self, layout: TimelineLayout, position: QPoint, modifiers: Qt.KeyboardModifier
    ) -> bool:
        """目盛りの上の Shift+押下なら範囲のドラッグを始めて真を返す

        Shift の無い押下は今までどおり再生ヘッドを動かす（呼んだ側が続ける）
        ヘッダの上は対象にしない そこにはフレームが無い
        """
        if not modifiers & Qt.KeyboardModifier.ShiftModifier:
            return False
        if position.y() >= Metrics.RULER_HEIGHT or position.x() < Metrics.TRACK_HEADER_WIDTH:
            return False
        self._anchor = self._current = _edge_at(layout, position.x())
        return True

    def move(self, layout: TimelineLayout, x: float) -> None:
        if self._anchor is not None:
            self._current = _edge_at(layout, x)

    def release(self, existing: Area | None) -> None:
        """ドラッグを終えて、決まった範囲をコマンドにする

        幅が 0（Shift+クリックだけ）なら何もしない 今の範囲を消すと、
        クリックのつもりの操作で決めてあった範囲が消える 解除は右クリックから
        """
        area = self.preview()
        self._anchor = None
        if area is not None and area != existing:
            self._request([SetWorkArea(area)], "書き出し範囲を指定")

    def preview(self) -> Area | None:
        """ドラッグ中の範囲 幅が 0 なら ``None``"""
        if self._anchor is None:
            return None
        start, end = sorted((self._anchor, self._current))
        return (start, end) if end > start else None

    def shown(self, existing: Area | None) -> Area | None:
        """画面に出す範囲 ドラッグ中はその途中、そうでなければモデルの範囲"""
        return self.preview() if self.dragging else existing

    def clear(self, existing: Area | None) -> bool:
        """範囲を解除する 無ければ何もせず偽を返す"""
        if existing is None:
            return False
        self._request([SetWorkArea(None)], "書き出し範囲を解除")
        return True

    # --- 描画 ---

    def paint_tracks(
        self,
        painter: QPainter,
        layout: TimelineLayout,
        width: int,
        height: int,
        existing: Area | None,
    ) -> None:
        """トラックの上に範囲を薄く重ねる クリップの色が読める程度の濃さに留める"""
        span = _span(layout, width, self.shown(existing))
        if span is None:
            return
        left, right = span
        painter.fillRect(
            QRectF(left, Metrics.RULER_HEIGHT, right - left, height - Metrics.RULER_HEIGHT),
            Colors.WORK_AREA_TINT,
        )

    def paint_ruler(
        self, painter: QPainter, layout: TimelineLayout, width: int, existing: Area | None
    ) -> None:
        """目盛りの下の端に色付きの帯を敷き、両端に縦の線を引く"""
        area = self.shown(existing)
        span = _span(layout, width, area)
        if span is None or area is None:
            return
        left, right = span
        top = Metrics.RULER_HEIGHT - _BAND_HEIGHT
        painter.fillRect(QRectF(left, top, right - left, _BAND_HEIGHT), Colors.WORK_AREA)
        painter.setPen(QPen(Colors.WORK_AREA_EDGE, 1))
        # 縁が見えている時だけ線を引く 画面の外で切った所に線があると、そこが範囲の端に見える
        for frame in area:
            x = layout.frame_to_x(frame)
            if Metrics.TRACK_HEADER_WIDTH <= x <= width:
                painter.drawLine(QPointF(x, 0), QPointF(x, Metrics.RULER_HEIGHT))

    # --- 右クリック ---

    def add_menu_actions(
        self, menu: QMenu, layout: TimelineLayout, position: QPoint, existing: Area | None
    ) -> None:
        """目盛りの上、または範囲の上なら「範囲を解除」を足す

        範囲の外のトラックの上には出さない クリップの操作が並ぶ中に関係の無い
        項目が毎回混ざると、目当ての項目を探しにくい
        """
        on_ruler = position.y() < Metrics.RULER_HEIGHT
        inside = (
            existing is not None and existing[0] <= layout.x_to_frame(position.x()) < existing[1]
        )
        if not on_ruler and not inside:
            return
        menu.addSeparator()
        if existing is None:
            hint = menu.addAction(HINT_TEXT)
            hint.setEnabled(False)
            return
        clear = menu.addAction(CLEAR_TEXT)
        clear.triggered.connect(lambda _checked=False: self.clear(existing))


def _edge_at(layout: TimelineLayout, x: float) -> int:
    """x に一番近いフレームの境目 範囲の縁は境目に置く

    押した所のフレームを切り捨てで取ると、右へ引いたときに最後のコマが範囲から
    こぼれ、左へ引いたときは 1 コマ余計に入る 近い方の境目に寄せれば向きで変わらない
    """
    return max(0, round(layout.x_to_frame(x)))


def _span(layout: TimelineLayout, width: int, area: Area | None) -> tuple[float, float] | None:
    """範囲の見えている部分の左右の x ヘッダの下へ潜る所は切る"""
    if area is None:
        return None
    left = max(float(Metrics.TRACK_HEADER_WIDTH), layout.frame_to_x(area[0]))
    right = min(float(width), layout.frame_to_x(area[1]))
    return (left, right) if right > left else None
