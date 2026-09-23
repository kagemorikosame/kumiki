"""互換性レポート 何が足りなくて動かないのかを見せる

AviUtl の ``obj`` API は広い 全部を一度に実装することはできないので、大事なのは
「動かない」ことではなく**何が足りないかが分かること**にした

スクリプトが未対応の関数を呼んだら記録され、ここに使用回数つきで並ぶ 次に何を
実装すべきかを、勘ではなく実際に使われた回数で決められる
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sashimono import __version__
from sashimono.compat.aviutl.catalog import script_catalog
from sashimono.compat.aviutl.report import CompatibilityReport, global_report
from sashimono.ui.theme import Colors

__all__ = ["CompatibilityDialog", "report_text"]

#: 写した文面で、本人のホームフォルダの代わりに置く文字
#: 失敗の記録には OS の文言がそのまま入り、ファイルの場所（ユーザー名を含む）が混じる
#: 不具合の報告は公開の Issue に貼られるので、名前が出ないように伏せる
HOME_PLACEHOLDER = "%USERPROFILE%"


def report_text(report: CompatibilityReport, scripts: int, home: Path | None = None) -> str:
    """不具合の報告に貼る文面 版と、画面に出ている記録を全部入れる

    版を頭に入れるのは、同じ記録でも版によって直っているかが変わるため
    貼る人に版を別に調べさせると、欄が空のまま届く
    探索先（フォルダの場所）は入れない 原因を追うのに要らず、名前が出るだけになる
    """
    lines = [
        f"Sashimono Edit {__version__} 互換性レポート",
        report.summary(),
        f"読み込み済みのスクリプト {scripts} 本",
        *report.lines(),
    ]
    text = "\n".join(lines)
    folder = str(home if home is not None else Path.home())
    # 短すぎる場所（根だけなど）で置き換えると、関係ない文字まで伏せてしまう
    if len(folder) > 3:
        text = text.replace(folder, HOME_PLACEHOLDER)
    return text


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
        # 一覧からは行を 1 つずつしか選べず、Ctrl+C でも写せない 不具合の報告に
        # 貼ってもらうには、全部をまとめて写す口が要る
        copy = QPushButton("内容をコピー", self)
        copy.clicked.connect(self.copy_to_clipboard)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close is not None:
            close.setText("閉じる")
        buttons.addButton(copy, QDialogButtonBox.ButtonRole.ActionRole)
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

    def copy_to_clipboard(self) -> None:
        """画面の記録を、報告に貼れる形でクリップボードへ写す"""
        clipboard = QApplication.clipboard()
        clipboard.setText(report_text(self._report, len(script_catalog().all())))

    def _clear(self) -> None:
        self._report.clear()
        self.refresh()

    def _rescan(self) -> None:
        catalog = script_catalog()
        catalog.scan()
        catalog.register_all()
        self.refresh()
