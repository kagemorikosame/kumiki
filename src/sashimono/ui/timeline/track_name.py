"""トラック（レイヤー）の名前をヘッダの上でその場で書き換える入力欄

別の窓（名前を尋ねる窓）を出すと、どのトラックの名前を変えているのかが窓に隠れる
ヘッダの名前の所へ重ねて出し、Enter か外を押したら決め、Esc で取りやめる

自分では名前を変えない 決めた名前を知らせるだけで、ビューが
:class:`~sashimono.core.commands.RenameTrack` を出す（取り消しの 1 段）
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtWidgets import QLineEdit, QWidget

from sashimono.core.model import TrackId

__all__ = ["TrackNameEditor"]


class TrackNameEditor(QLineEdit):
    """1 本のトラックの名前の入力欄 決めるか取りやめると自分で消える"""

    #: 名前を決めた （トラック, 名前） 空なら既定の名前へ戻す（コマンドの決まり）
    committed = Signal(str, str)

    def __init__(self, parent: QWidget, track_id: TrackId, name: str) -> None:
        super().__init__(name, parent)
        self.track_id = track_id
        #: 決めたか取りやめたか Enter で決めた後に消えるときも editingFinished が
        #: もう 1 度来る 見ないと、同じ名前の変更が 2 段積まれる
        self._done = False
        self.setToolTip("Enter で決める Esc で取りやめる 空にすると既定の名前に戻る")
        self.editingFinished.connect(self.commit)

    @property
    def done(self) -> bool:
        return self._done

    def commit(self) -> None:
        if self._done:
            return
        self._done = True
        self.committed.emit(str(self.track_id), self.text())
        self._close()

    def cancel(self) -> None:
        if self._done:
            return
        self._done = True
        self._close()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt の命名規約
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802 - Qt の命名規約
        # 外を押したら決める（表計算のセルと同じ） 取りやめにすると、打った名前が
        # 押し方によって黙って消える
        super().focusOutEvent(event)
        self.commit()

    def _close(self) -> None:
        self.hide()
        self.deleteLater()
