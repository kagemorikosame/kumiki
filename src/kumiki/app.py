r"""アプリケーションの入口

.venv\Scripts\python.exe -m kumiki
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon, QSurfaceFormat
from PySide6.QtWidgets import QApplication

from kumiki.asr import activate_runtime
from kumiki.core.io import ProjectFileError, load_project
from kumiki.engine.gpu import preferred_surface_format
from kumiki.resources import ICON_FILE, path_to
from kumiki.runtime import pip_arguments, run_pip
from kumiki.ui.main_window import MainWindow
from kumiki.ui.theme import STYLE_SHEET

__all__ = ["SELF_CHECK_FLAG", "main"]


#: 画面を出さずに、同梱した部品が動くかだけを確かめる
SELF_CHECK_FLAG = "--self-check"


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv if argv is None else argv

    # 配布版は自分自身が pip の代わりになる 導入ボタンは ``sys.executable -m pip`` を
    # 呼ぶが、配布版の sys.executable はこの exe なので、ここで受けないと
    # 導入するつもりで Kumiki がもう 1 つ起動する
    pip_args = pip_arguments(arguments)
    if pip_args is not None:
        return run_pip(pip_args)

    # 自己診断だけを頼まれたときに限る ほかの引数（プロジェクトの場所）と一緒に
    # 渡されたら、開くつもりの起動として扱う 自己診断を優先すると、頼んだ
    # プロジェクトが開かずに黙って終わる
    if list(arguments[1:]) == [SELF_CHECK_FLAG]:
        from kumiki.selfcheck import main as self_check

        return self_check()

    # ソフト内から導入した字幕起こしの実行環境を import できるようにする
    # 通常の実行では何もしない（パッケージ版のためだけの手当て）
    activate_runtime()

    # サーフェス形式は QApplication を作る前に決めておく必要がある
    # 後から設定しても、ウィジェットのコンテキストには反映されない
    QSurfaceFormat.setDefaultFormat(preferred_surface_format())

    application = QApplication(arguments)
    application.setApplicationName("Kumiki")
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
