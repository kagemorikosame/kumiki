"""アプリケーションの入口。

.venv\Scripts\python.exe -m novaedit
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication

from novaedit.asr import activate_runtime
from novaedit.core.io import ProjectFileError, load_project
from novaedit.engine.gpu import preferred_surface_format
from novaedit.ui.main_window import MainWindow
from novaedit.ui.theme import STYLE_SHEET

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv if argv is None else argv

    # ソフト内から導入した字幕起こしの実行環境を import できるようにする。
    # 通常の実行では何もしない（パッケージ版のためだけの手当て）。
    activate_runtime()

    # サーフェス形式は QApplication を作る前に決めておく必要がある。
    # 後から設定しても、ウィジェットのコンテキストには反映されない。
    QSurfaceFormat.setDefaultFormat(preferred_surface_format())

    application = QApplication(arguments)
    application.setApplicationName("NovaEdit")
    application.setStyleSheet(STYLE_SHEET)

    project = None
    if len(arguments) > 1:
        try:
            project = load_project(Path(arguments[1]))
        except ProjectFileError as exc:
            print(f"プロジェクトを開けない: {exc}", file=sys.stderr)

    window = MainWindow(project)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
