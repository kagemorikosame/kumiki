"""前回落ちたときの作業を拾い直す画面"""

from __future__ import annotations

from typing import Literal

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sashimono.core.io import RecoveryEntry

__all__ = ["RecoveryDialog"]


class RecoveryDialog(QDialog):
    """退避の一覧から 1 つ選ばせる

    「あとで決める」を用意する 起動した直後は別の作業のつもりでいることがあり、
    その場で復元か破棄かを迫ると、よく読まずに破棄を押される 何もしなければ
    次の起動でまた出る
    """

    def __init__(self, entries: list[RecoveryEntry], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("作業の復元")
        self.resize(560, 300)
        self._entries = entries
        self.choice: tuple[Literal["restore", "discard"], RecoveryEntry] | None = None

        message = QLabel(
            "前回、保存しないまま終了した作業があります 復元すると、最後に自動退避した"
            "時点まで戻ります",
            self,
        )
        message.setWordWrap(True)

        self._list = QListWidget(self)
        for entry in entries:
            where = str(entry.source) if entry.source is not None else "一度も保存していない"
            self._list.addItem(f"{entry.name}　{entry.saved_at:%m/%d %H:%M} 退避\n　{where}")
        self._list.setCurrentRow(0)

        restore = QPushButton("復元する", self)
        restore.setDefault(True)
        restore.clicked.connect(lambda: self._choose("restore"))
        drop = QPushButton("破棄する…", self)
        drop.clicked.connect(lambda: self._choose("discard"))
        later = QPushButton("あとで決める", self)
        later.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addWidget(drop)
        buttons.addStretch(1)
        buttons.addWidget(later)
        buttons.addWidget(restore)

        layout = QVBoxLayout(self)
        layout.addWidget(message)
        layout.addWidget(self._list, 1)
        layout.addLayout(buttons)

    def _choose(self, action: Literal["restore", "discard"]) -> None:
        row = self._list.currentRow()
        if not 0 <= row < len(self._entries):
            return
        entry = self._entries[row]
        if action == "discard":
            answer = QMessageBox.question(
                self,
                "破棄の確認",
                f"「{entry.name}」の退避を捨てます 元に戻せません",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Discard:
                return
        self.choice = (action, entry)
        self.accept()
