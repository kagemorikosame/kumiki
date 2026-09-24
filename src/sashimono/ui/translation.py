"""Qt 標準の文言（確認のボタン・入力欄の右クリック・色やファイルの窓）を日本語にする

Sashimono の画面の文言はすべて日本語で書いているが、Qt が自分で出す文言
（``QMessageBox`` の Save / Discard / Cancel、``QDialogButtonBox`` の Cancel、
``QInputDialog`` や ``QColorDialog`` の部品、入力欄の右クリックの Undo / Copy など）は
翻訳を読ませないと英語のまま出る（Issue #27）

翻訳は PySide6 の wheel に ``translations/qtbase_ja.qm`` として入っている 配布版では
PyInstaller の PySide6 の差し込みが、使う Qt の部品に付く翻訳（QtCore なら qtbase）を
``_internal/PySide6/translations`` へ積む 積まれなかったときは自己診断
（:mod:`sashimono.selfcheck`）の「Qt の日本語訳」が落ちるので、zip を確かめる段で気付ける
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QTranslator

__all__ = ["QT_TRANSLATION", "install_qt_translation", "translation_folders"]

#: 読む翻訳の名前 ``qt_ja`` は ``qtbase_ja`` などを束ねるだけの目録で、配布版には
#: 積まれない（PyInstaller は部品ごとの翻訳しか拾わない）ので、中身の方を直に読む
QT_TRANSLATION = "qtbase_ja"

#: 入れた翻訳を持っておく Python 側の参照が切れると翻訳が壊され、文言が英語へ戻る
_installed: list[QTranslator] = []


def translation_folders() -> list[Path]:
    """翻訳を探す場所 見つかりやすい順

    1 つ目は Qt 自身が知っている場所 開発環境ではこれで見つかる
    配布版では Qt の置き場の数え方が exe の場所を基にするため外れることがあるので、
    PySide6 の包みの隣（PyInstaller が積む場所）も見る
    """
    import PySide6

    folders = [Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))]
    package = Path(PySide6.__file__).resolve().parent / "translations"
    if package not in folders:
        folders.append(package)
    return folders


def install_qt_translation(application: QCoreApplication) -> Path | None:
    """Qt 標準の文言の日本語訳を読む 読めた翻訳の場所、見つからなければ ``None``

    見つからなくても起動は止めない 英語の文言が混ざるだけで、使えなくはならない
    2 回呼んでも同じ翻訳を重ねて入れない
    """
    if _installed:
        return _loaded_folder(_installed[0])
    for folder in translation_folders():
        translator = QTranslator(application)
        if translator.load(QT_TRANSLATION, str(folder)):
            application.installTranslator(translator)
            _installed.append(translator)
            return folder
    return None


def _loaded_folder(translator: QTranslator) -> Path | None:
    name = translator.filePath()
    return Path(name).parent if name else None
