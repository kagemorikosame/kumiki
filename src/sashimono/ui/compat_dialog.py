"""互換性レポート 何が足りなくて動かないのかを見せる

AviUtl の ``obj`` API は広い 全部を一度に実装することはできないので、大事なのは
「動かない」ことではなく**何が足りないかが分かること**にした

スクリプトが未対応の関数を呼んだら記録され、ここに使用回数つきで並ぶ 次に何を
実装すべきかを、勘ではなく実際に使われた回数で決められる
"""

from __future__ import annotations

import ctypes
import os
import re
from collections.abc import Sequence
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

__all__ = ["CompatibilityDialog", "mask_user_folders", "report_text", "short_path", "user_folders"]

#: 写した文面で、本人のホームフォルダの代わりに置く文字
#: 失敗の記録には OS の文言がそのまま入り、ファイルの場所（ユーザー名を含む）が混じる
#: 不具合の報告は公開の Issue に貼られるので、名前が出ないように伏せる
HOME_PLACEHOLDER = "%USERPROFILE%"

#: ホームのほかに伏せる置き場 （環境変数, 置き換える文字）
#: 普段はどちらもホームの下にあるが、移動プロファイルや組織の設定でホームの外
#: （ネットワークの置き場など）へ向いていることがあり、その場所にも利用者名が入る
#: ホームだけを伏せると、そうした機械では名前がそのまま残る
#: 環境変数の名前で置くのは、伏せたあとも「設定の置き場の中」だと読めるようにするため
#: Windows 以外の置き場（`core/userdirs.py` が使う XDG の 4 つ）も同じ理由で伏せる
#: ホームの外へ向けた機械では、ホームを伏せても名前が残る
_FOLDER_VARIABLES = (
    ("APPDATA", "%APPDATA%"),
    ("LOCALAPPDATA", "%LOCALAPPDATA%"),
    ("XDG_CONFIG_HOME", "$XDG_CONFIG_HOME"),
    ("XDG_STATE_HOME", "$XDG_STATE_HOME"),
    ("XDG_CACHE_HOME", "$XDG_CACHE_HOME"),
    ("XDG_DATA_HOME", "$XDG_DATA_HOME"),
)

#: 区切りとみなす文字 Windows は ``/`` も ``\`` も受け付け、OS の文言や
#: スクリプトの書いた場所ではどちらも混ざる
_SEPARATORS = r"[\\/]+"

#: 伏せた場所のすぐ後に来てよい文字（区切り・空白・引用符・括弧など）
#: これ以外が続くときは名前の途中なので伏せない ``C:\Users\kage`` を伏せるときに
#: ``C:\Users\kagemori`` の頭だけを伏せると、残りから名前が読める
_FOLDER_END = r"(?![^\\/\s\"'<>|:;,)\]}])"


def user_folders(home: Path | None = None) -> list[tuple[str, str]]:
    r"""伏せる置き場と、その代わりに置く文字

    Windows では 8.3 形式の短い名前（``C:\Users\KAGEMO~1``）も足す 古い API や
    一部の DLL は場所を短い形で返し、それが失敗の文言にそのまま入る 短い形にも
    利用者名の頭が残るので、長い形だけを伏せると公開の Issue に名前が出る
    """
    longs = [(str(home if home is not None else Path.home()), HOME_PLACEHOLDER)]
    for variable, placeholder in _FOLDER_VARIABLES:
        value = os.environ.get(variable)
        if value:
            longs.append((value, placeholder))
    folders = list(longs)
    for folder, placeholder in longs:
        short = short_path(folder)
        # 同じ形を重ねても害は無いが、伏せる回数が増えるだけなので足さない
        if short and short.casefold() != folder.casefold():
            folders.append((short, placeholder))
    return folders


def short_path(folder: str) -> str | None:
    """8.3 形式の短い名前 求められなければ ``None``

    Windows 以外・8.3 を切ってある置き場・無い場所・呼び出しの失敗は、どれも
    ``None`` にして長い形だけで続ける 伏せる対象を足すための手当てで、取れない
    からといってコピーそのものを止めるほどのことではない
    """
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    try:
        get_short = windll.kernel32.GetShortPathNameW
        # 1 回目で要る長さを聞き、2 回目で受け取る 長さを決め打ちすると、深い置き場で切れる
        size = int(get_short(folder, None, 0))
        if size <= 0:
            return None
        buffer = ctypes.create_unicode_buffer(size)
        written = int(get_short(folder, buffer, size))
    except (AttributeError, OSError, ValueError):
        return None
    if written <= 0 or written >= size:
        return None
    return buffer.value or None


def mask_user_folders(text: str, folders: Sequence[tuple[str, str]]) -> str:
    r"""文面の中の置き場を、書き方の揺れごと伏せる

    完全一致で置き換えると、``c:\users\…`` と ``C:\Users\…``、``/`` と ``\``
    の違う書き方が素通りする Windows の場所は大文字と小文字を区別しないので、
    どれも同じ場所で、同じように利用者名を含む

    長い場所から伏せる ``%APPDATA%`` はホームの下にあるので、先にホームを伏せると
    ``%USERPROFILE%\AppData\Roaming`` になり、設定の置き場だと読みにくくなる
    """
    for folder, placeholder in sorted(folders, key=lambda pair: len(pair[0]), reverse=True):
        parts = [part for part in re.split(_SEPARATORS, folder) if part]
        # 短すぎる場所（根だけなど）で置き換えると、関係ない文字まで伏せてしまう
        if len(parts) < 2:
            continue
        pattern = _SEPARATORS.join(re.escape(part) for part in parts) + _FOLDER_END
        # 頭の区切り（ネットワークの置き場の ``\\server`` や、Windows 以外の ``/home``）も
        # 伏せる側に含める 残すと ``\\%APPDATA%`` のような読めない形になる
        if re.match(_SEPARATORS, folder):
            pattern = _SEPARATORS + pattern
        # 置き換える文字は escape して渡す 素のままだと ``\`` を置き換えの書式として読まれる
        replacement = placeholder.replace("\\", "\\\\")
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def report_text(
    report: CompatibilityReport,
    scripts: int,
    folders: Sequence[tuple[str, str]] | None = None,
) -> str:
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
    return mask_user_folders("\n".join(lines), folders if folders is not None else user_folders())


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
