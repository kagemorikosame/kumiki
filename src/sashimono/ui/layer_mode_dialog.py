"""置き方の方式（混合 ⇔ 映像と音声に分ける）を途中で切り替えるときに尋ねるダイアログ（Issue #27）

利用者の決定で、切り替えるたびに「今のトラックも変換する」か「これから置く物だけ変える」かを
尋ねる どちらが良いかは作品による（途中まで分けて作った物をそのまま残したい人もいる）ので、
黙ってどちらかに決めない 変換で引き継げない物（ミュートとソロの効き方など）は、選ぶ前に
一覧で見せる 変換してから気付くと、どこが変わったのかを探すことになる
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sashimono.core.model import LayerMode

__all__ = ["LayerModeDialog"]

_NAMES = {LayerMode.MIXED: "混合（レイヤー）", LayerMode.SEPARATED: "映像と音声に分ける"}

_WHAT_HAPPENS = {
    LayerMode.MIXED: (
        "映像トラックを並びのままレイヤーにし、音声トラックの音をリンクした映像へまとめます\n"
        "相手の無い音（BGM など）は、絵を描かないレイヤーへ移します"
    ),
    LayerMode.SEPARATED: (
        "レイヤーを映像トラックと音声トラックに分けます\n"
        "絵と音を持つクリップは 2 本に分けてリンクで結びます"
    ),
}


class LayerModeDialog(QDialog):
    """``target`` の方式へ切り替えるときに、置いてあるトラックも変換するかを尋ねる

    ``notices`` は変換で引き継げない物の一覧（:func:`~sashimono.core.commands.convert_layers`）
    ``convertible`` が偽なら変換する物が無いので、変換のボタンは押せなくする
    押せる見た目のまま何も変えないと、変換したのに何も起きなかったように見える
    """

    def __init__(
        self,
        target: str,
        notices: tuple[str, ...],
        parent: QWidget | None = None,
        *,
        convertible: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("置き方の方式を切り替える")
        #: 選んだ答え 真なら今のトラックも変換する 閉じただけなら ``None``
        self.convert: bool | None = None

        heading = QLabel(f"これから置く物の方式を「{_NAMES.get(target, target)}」にします", self)
        explanation = QLabel(
            "今あるトラックも変換しますか\n" + _WHAT_HAPPENS.get(target, ""),
            self,
        )
        explanation.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(explanation)

        if not convertible:
            layout.addWidget(QLabel("変換するトラックはありません", self))
        elif notices:
            layout.addWidget(QLabel("変換すると、次の物は同じようには引き継げません", self))
            self.notices = QListWidget(self)
            self.notices.addItems(list(notices))
            layout.addWidget(self.notices)
        else:
            layout.addWidget(QLabel("変換しても、書き出す絵と音は変わりません", self))

        buttons = QDialogButtonBox(self)
        self.convert_button = QPushButton("今のトラックも変換する", self)
        self.convert_button.setEnabled(convertible)
        self.keep_button = QPushButton("これから置く物だけ変える", self)
        buttons.addButton(self.convert_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.keep_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.convert_button.clicked.connect(lambda: self._choose(convert=True))
        self.keep_button.clicked.connect(lambda: self._choose(convert=False))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        # 既定のボタン（Enter）は変換しない側 うっかり Enter で作品の作りを変えないため
        self.keep_button.setDefault(True)

    def _choose(self, *, convert: bool) -> None:
        self.convert = convert
        self.accept()
