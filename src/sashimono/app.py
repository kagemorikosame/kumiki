r"""アプリケーションの入口

.venv\Scripts\python.exe -m sashimono

**一番上では Qt を読まない** 読むのは、いつもの起動（:func:`_start_editor`）の中
配布版は同じ exe が 3 つの役をする（編集画面・自己診断・導入ボタンの pip）
一番上で Qt と編集画面を読むと、

- Qt の部品が欠けた配布版では、自己診断にたどり着く前に落ちる
  （結果を見せる窓も出ない 欠けたことを知りたいまさにそのときに何も出ない）
- 導入ボタンの pip も、Qt 一式を読み込んでから走ることになる
"""

from __future__ import annotations

import sys
from pathlib import Path

# この 2 つは Qt を読まない（読まないことを試験で押さえている）
from sashimono.asr import activate_runtime
from sashimono.runtime import pip_arguments, run_pip

__all__ = ["SELF_CHECK_FLAG", "main"]

#: 画面を出さずに、同梱した部品が動くかだけを確かめる
SELF_CHECK_FLAG = "--self-check"


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv if argv is None else argv

    # 配布版は自分自身が pip の代わりになる 導入ボタンは ``sys.executable -m pip`` を
    # 呼ぶが、配布版の sys.executable はこの exe なので、ここで受けないと
    # 導入するつもりで Sashimono がもう 1 つ起動する
    pip_args = pip_arguments(arguments)
    if pip_args is not None:
        return run_pip(pip_args)

    # 自己診断だけを頼まれたときに限る ほかの引数（プロジェクトの場所）と一緒に
    # 渡されたら、開くつもりの起動として扱う 自己診断を優先すると、頼んだ
    # プロジェクトが開かずに黙って終わる
    if list(arguments[1:]) == [SELF_CHECK_FLAG]:
        from sashimono.selfcheck import main as self_check

        return self_check()

    return _start_editor(arguments)


def _start_editor(arguments: list[str]) -> int:
    """いつもの起動 Qt と編集画面はここで初めて読む"""
    # ソフト内から導入した字幕起こしの実行環境を import できるようにする
    # 通常の実行では何もしない（パッケージ版のためだけの手当て）
    activate_runtime()

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon, QSurfaceFormat
    from PySide6.QtWidgets import QApplication

    from sashimono.core.io import ProjectFileError, load_project
    from sashimono.engine.gpu import preferred_surface_format
    from sashimono.resources import ICON_FILE, path_to
    from sashimono.ui.main_window import MainWindow
    from sashimono.ui.theme import STYLE_SHEET

    # サーフェス形式は QApplication を作る前に決めておく必要がある
    # 後から設定しても、ウィジェットのコンテキストには反映されない
    QSurfaceFormat.setDefaultFormat(preferred_surface_format())

    application = QApplication(arguments)
    application.setApplicationName("Sashimono")
    application.setWindowIcon(QIcon(str(path_to(ICON_FILE))))
    application.setStyleSheet(STYLE_SHEET)

    project = None
    path = None
    if len(arguments) > 1:
        try:
            project = load_project(Path(arguments[1]))
            path = Path(arguments[1])
        except ProjectFileError as exc:
            print(f"プロジェクトを開けない: {exc}", file=sys.stderr)

    window = MainWindow(project, path=path)
    window.show()
    # 窓が描かれてから尋ねる 先に尋ねると、何のソフトの話かが分からない
    QTimer.singleShot(0, window.offer_recovery)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
