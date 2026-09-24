"""つまみの端を掴んで伸び縮みさせると、表示の大きさが変わるスクロールバー

Premiere のタイムラインの下にあるバーと同じ使い方 つまみは「いま見えている範囲」
なので、端を掴んで広げれば広く（縮小）、狭めれば細かく（拡大）見える
つまみの中ほどを掴んだときは、今までどおりのスクロールになる

バーは範囲を決めるだけで、拡大率やトラックの高さは知らない 決めた範囲を信号で外へ出し、
表示を変えるのはビューの仕事にする（横は拡大率、縦はトラックの高さと、意味が違うため）
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QScrollBar, QStyle, QStyleOptionSlider, QWidget

__all__ = ["SpanEdge", "ZoomScrollBar"]

#: つまみの端として掴める幅（画素） 広いと中を掴んでスクロールしたいのに伸び縮みになる
EDGE_GRAB = 6

#: つまみをこれより短くしない（画素） 両端が重なると、次にどちらの端を掴んだのか決まらない
MIN_SPAN_PIXELS = 12


class SpanEdge(Enum):
    START = "start"
    END = "end"


class ZoomScrollBar(QScrollBar):
    """つまみの両端で、見えている範囲の広さを変えられるスクロールバー

    伸び縮みの間は、押した時点の目盛り（範囲の全体とバーの長さ）で値を数える 伸ばすたびに
    ビューが範囲を付け替えるので、今の目盛りで数えると、つまみが指から逃げていく
    """

    #: 端を掴んだ
    span_started = Signal()
    #: 端を動かした 引数は押した時点の目盛りで数えた、見えている範囲の始まりと終わりと、
    #: 動かしているのが始まりの側か（反対の端は動かさない）
    span_dragged = Signal(float, float, bool)
    #: 端を放した
    span_finished = Signal()

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self.setMouseTracking(True)
        self._edge: SpanEdge | None = None
        self._press = 0
        self._span = (0.0, 0.0)
        self._units_per_pixel = 1.0
        self._total = 0.0

    # --- 位置 ---

    def _along(self, point: QPoint) -> int:
        return point.x() if self.orientation() is Qt.Orientation.Horizontal else point.y()

    def _control_rect(self, control: QStyle.SubControl) -> QRect:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_ScrollBar, option, control, self
        )

    def handle_rect(self) -> QRect:
        """つまみの矩形"""
        return self._control_rect(QStyle.SubControl.SC_ScrollBarSlider)

    def edge_at(self, point: QPoint) -> SpanEdge | None:
        """``point`` がつまみのどちらかの端の上なら、その端

        つまみが短いときは、掴める幅をつまみの 4 分の 1 までにする 端だけで埋まると、
        スクロールのために中を掴めなくなる
        """
        handle = self.handle_rect()
        if not handle.isValid() or not handle.contains(point):
            return None
        horizontal = self.orientation() is Qt.Orientation.Horizontal
        start = handle.left() if horizontal else handle.top()
        end = handle.right() if horizontal else handle.bottom()
        grab = min(EDGE_GRAB, max(2, (end - start) // 4))
        along = self._along(point)
        if along - start <= grab:
            return SpanEdge.START
        if end - along <= grab:
            return SpanEdge.END
        return None

    @property
    def resizing(self) -> bool:
        return self._edge is not None

    # --- マウス ---

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt の命名規約
        edge = (
            self.edge_at(event.position().toPoint())
            if event.button() == Qt.MouseButton.LeftButton
            else None
        )
        if edge is None:
            super().mousePressEvent(event)
            return
        groove = self._control_rect(QStyle.SubControl.SC_ScrollBarGroove)
        length = (
            groove.width() if self.orientation() is Qt.Orientation.Horizontal else groove.height()
        )
        self._total = float(self.maximum() - self.minimum() + self.pageStep())
        self._units_per_pixel = self._total / max(1, length)
        self._press = self._along(event.position().toPoint())
        self._span = (float(self.value()), float(self.value() + self.pageStep()))
        self._edge = edge
        self.span_started.emit()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt の命名規約
        point = event.position().toPoint()
        if self._edge is None:
            if not event.buttons():
                self._show_cursor(self.edge_at(point))
            super().mouseMoveEvent(event)
            return
        moved = (self._along(point) - self._press) * self._units_per_pixel
        least = MIN_SPAN_PIXELS * self._units_per_pixel
        start, end = self._span
        if self._edge is SpanEdge.START:
            start = min(max(0.0, start + moved), end - least)
        else:
            end = max(min(self._total, end + moved), start + least)
        self.span_dragged.emit(start, end, self._edge is SpanEdge.START)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt の命名規約
        if self._edge is None:
            super().mouseReleaseEvent(event)
            return
        self._edge = None
        self._show_cursor(self.edge_at(event.position().toPoint()))
        self.span_finished.emit()
        event.accept()

    def _show_cursor(self, edge: SpanEdge | None) -> None:
        """端の上では伸び縮みの形にする 形が変わらないと、端を掴めることに気付けない"""
        if edge is None:
            self.unsetCursor()
        elif self.orientation() is Qt.Orientation.Horizontal:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
