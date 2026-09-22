"""互換性レポート 何が足りなくて動かないのかを見せる

AviUtl の ``obj`` API は広い 全部を一度に実装することはできないので、大事なのは
「動かない」ことではなく**何が足りないかが分かること**にした

スクリプトが未対応の関数を呼んだら記録され、ここに使用回数つきで並ぶ 次に何を
実装すべきかを、勘ではなく実際に使われた回数で決められる
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

from sashimono.compat.aviutl.catalog import script_catalog
from sashimono.compat.aviutl.report import CompatibilityReport, global_report
from sashimono.ui.theme import Colors

__all__ = ["CompatibilityDialog"]


class CompatibilityDialog(QDialog):
    """未対応 API と読み込みの失敗を一覧で出す"""

    def __init__(
        self, report: CompatibilityReport | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("AviUtl 互換性レポート")
        self.resize(560, 420)
        self._report = report if report is not None else global_report

        self._summary = QLabel(self)
        self._summary.setWordWrap(True)

        self._scripts = QLabel(self)
        self._scripts.setWordWrap(True)
        self._scripts.setStyleSheet(f"color: {Colors.TEXT_MUTED.name()};")

        self._list = QListWidget(self)

        clear = QPushButton("記録を消す", self)
        clear.clicked.connect(self._clear)
        rescan = QPushButton("スクリプトを読み直す", self)
        rescan.clicked.connect(self._rescan)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close is not None:
            close.setText("閉じる")
        buttons.addButton(rescan, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(clear, QDialogButtonBox.ButtonRole.ResetRole)

        layout = QVBoxLayout(self)
        layout.addWidget(self._summary)
        layout.addWidget(self._scripts)
        layout.addWidget(self._list, 1)
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self) -> None:
        catalog = script_catalog()
        entries = catalog.all()
        self._summary.setText(self._report.summary())

        roots = "、".join(str(root) for root in catalog.roots) or "（設定なし）"
        self._scripts.setText(f"スクリプト {len(entries)} 本を読み込み済み\n探索先: {roots}")

        self._list.clear()
        lines = self._report.lines()
        self._list.addItems(lines if lines else ["まだ記録はありません"])

    def _clear(self) -> None:
        self._report.clear()
        self.refresh()

    def _rescan(self) -> None:
        catalog = script_catalog()
        catalog.scan()
        catalog.register_all()
        self.refresh()
