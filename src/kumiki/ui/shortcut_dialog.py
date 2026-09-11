"""ショートカットの割り当てを変える画面"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from kumiki.ui.workspace import find_conflicts

__all__ = ["ShortcutDialog", "ShortcutRow"]

_PORTABLE = QKeySequence.SequenceFormat.PortableText


@dataclass(frozen=True, slots=True)
class ShortcutRow:
    """1 行分 ``action`` は「メニュー/項目」の形の名前"""

    action: str
    current: str
    default: str


class ShortcutDialog(QDialog):
    """操作ごとにキーを割り当てる 重なりがあれば閉じさせない"""

    def __init__(self, rows: list[ShortcutRow], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ショートカットの設定")
        self.resize(520, 560)
        self._rows = rows
        self._editors: list[QKeySequenceEdit] = []

        table = QTableWidget(len(rows), 2, self)
        table.setHorizontalHeaderLabels(["操作", "キー"])
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(1, 180)
        for index, row in enumerate(rows):
            label = QTableWidgetItem(row.action.replace("/", " › "))
            label.setToolTip(f"既定: {row.default or 'なし'}")
            table.setItem(index, 0, label)
            editor = QKeySequenceEdit(QKeySequence(row.current, _PORTABLE), table)
            # 2 打鍵の組み合わせは受け付けない 押し始めたまま次の入力を待つ
            # 動きになり、1 打鍵の割り当てと重なったときに分かりにくい
            editor.setMaximumSequenceLength(1)
            editor.setClearButtonEnabled(True)
            table.setCellWidget(index, 1, editor)
            self._editors.append(editor)

        note = QLabel("欄を選んでキーを押すと割り当てます 空にすると割り当てなし", self)
        note.setWordWrap(True)

        reset = QPushButton("すべて既定に戻す", self)
        reset.clicked.connect(self._reset)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(table, 1)
        layout.addWidget(reset)
        layout.addWidget(buttons)

    def bindings(self) -> dict[str, str]:
        """いまの割り当て 操作 → キー（空文字は割り当てなし）"""
        return {
            row.action: editor.keySequence().toString(_PORTABLE)
            for row, editor in zip(self._rows, self._editors, strict=True)
        }

    def accept(self) -> None:
        conflicts = find_conflicts(self.bindings())
        if conflicts:
            lines = [
                f"{key}: {'、'.join(action.replace('/', ' › ') for action in actions)}"
                for key, actions in conflicts.items()
            ]
            QMessageBox.warning(
                self,
                "キーが重なっています",
                "同じキーに複数の操作があると、どれも動きません\n\n" + "\n".join(lines),
            )
            return
        super().accept()

    def _reset(self) -> None:
        for row, editor in zip(self._rows, self._editors, strict=True):
            editor.setKeySequence(QKeySequence(row.default, _PORTABLE))
