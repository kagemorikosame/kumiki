"""ドラッグ中に表示の端へ近づいたら、表示を送って付いていく

再生ヘッドを目盛りで掴んで右へ運んでも、表示の外へは出られなかった（Issue #27）
端に近いほど速く送る 押したまま止まっていても、タイマーで送り続ける
（マウスを動かしたときだけ送ると、端で手を止めた途端に止まる）

送る量の決め方と、タイマーをここに閉じ込める 送った後に何を動かすか（再生ヘッド・
クリップ・囲む枠）はビューが知っているので、送るたびに呼び返す
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QPoint, QRect, QTimer

__all__ = ["EDGE_ZONE", "EdgeScroller", "edge_speed"]

#: 端からこの距離（画素）に入ったら送り始める
EDGE_ZONE = 32

#: 送る間隔（ミリ秒） 60fps の画面で 2 コマに 1 回 細かいと描くのが追いつかない
TICK_MS = 30

#: 端の手前で 1 回に送る量（画素） 端を越えて離れるほど、この何倍かまで速くなる
BASE_STEP = 10.0
MAX_FACTOR = 5.0


def edge_speed(position: int, low: int, high: int) -> float:
    """1 回に送る量（画素） 負で手前（左・上）へ、正で奥（右・下）へ 端から遠ければ 0

    送る速さを端からの距離で変える 同じ速さだと、少しだけ先を見たいときに行き過ぎ、
    遠くへ行きたいときには遅い
    """
    if high - low <= 2 * EDGE_ZONE:
        # 表示が狭すぎて両方の帯が重なるときは送らない どちらへ送るか決まらない
        return 0.0
    if position < low + EDGE_ZONE:
        depth = (low + EDGE_ZONE - position) / EDGE_ZONE
        return -BASE_STEP * min(depth, MAX_FACTOR)
    if position > high - EDGE_ZONE:
        depth = (position - (high - EDGE_ZONE)) / EDGE_ZONE
        return BASE_STEP * min(depth, MAX_FACTOR)
    return 0.0


class EdgeScroller(QObject):
    """ドラッグ中のマウスの位置を覚え、端に近い間だけタイマーで送り続ける"""

    def __init__(self, step: Callable[[float, float, QPoint], None], parent: QObject) -> None:
        """``step`` は送る量（横・縦の画素）とマウスの位置を受け取り、表示を送って続きを描く"""
        super().__init__(parent)
        self._step = step
        self._position = QPoint()
        self._bounds = QRect()
        self._horizontal = False
        self._vertical = False
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.tick)

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def follow(self, position: QPoint, bounds: QRect, *, horizontal: bool, vertical: bool) -> None:
        """マウスが動いた ``bounds`` は送らずに動ける範囲（ヘッダと目盛りを除いた所）"""
        self._position = QPoint(position)
        self._bounds = QRect(bounds)
        self._horizontal = horizontal
        self._vertical = vertical
        dx, dy = self._speeds()
        if dx or dy:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def stop(self) -> None:
        self._timer.stop()

    def _speeds(self) -> tuple[float, float]:
        bounds = self._bounds
        dx = (
            edge_speed(self._position.x(), bounds.left(), bounds.right())
            if self._horizontal
            else 0.0
        )
        dy = (
            edge_speed(self._position.y(), bounds.top(), bounds.bottom()) if self._vertical else 0.0
        )
        return dx, dy

    def tick(self) -> None:
        """1 回送る タイマーから呼ばれる 試験からも直に呼べる"""
        dx, dy = self._speeds()
        if not dx and not dy:
            self._timer.stop()
            return
        self._step(dx, dy, QPoint(self._position))
