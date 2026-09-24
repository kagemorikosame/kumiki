"""試験で使うクリップボードの偽物（#154）

本物へ書くと、試験を走らせるたびに本人がコピーしていた物が消える 同時に走るほかの
作業（別の worktree の verify など）と取り合うと読み戻しが空になり、試験が落ちる
"""

from __future__ import annotations

from PySide6.QtGui import QImage


class FakeClipboard:
    """アプリが使う分だけを持つクリップボード

    本物と同じく、最後に置いた 1 つだけを持つ 文字を置けば絵は消える
    """

    def __init__(self) -> None:
        self._text = ""
        self._image = QImage()
        #: 本物へ書こうとして止められた口の名前 conftest の見張りが足し、後片付けで見る
        self.refused: list[str] = []

    def setText(self, text: str, /) -> None:  # noqa: N802 Qt の名前に合わせる
        self._text = text
        self._image = QImage()

    def setImage(self, image: QImage, /) -> None:  # noqa: N802 Qt の名前に合わせる
        self._text = ""
        self._image = QImage(image)

    def text(self) -> str:
        return self._text

    def image(self) -> QImage:
        return QImage(self._image)
