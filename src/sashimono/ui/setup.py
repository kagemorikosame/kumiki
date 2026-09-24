"""未導入の機能を、その場で用意するための部品

字幕起こしと AI 連携で同じものを使う 導入の見せ方は機能が変わっても同じ
（何が入るかを出す → 実行する → ログを流す → 状態を出し直す）なので、
1 か所にまとめてある

導入は子プロセスなので、ログはキューで受けてタイマーで拾う ワーカースレッドから
ウィジェットを触ると Qt が落ちる
"""

from __future__ import annotations

import queue
import threading

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sashimono.runtime import (
    FeaturePack,
    PackStatus,
    install_command,
    install_runtime,
    refresh_runtime,
    restart_note,
)
from sashimono.ui.theme import Colors

__all__ = ["SetupSection"]

#: 導入ログを拾う間隔（ミリ秒）
POLL_MS = 120


class SetupSection(QWidget):
    """機能の導入状況を出し、その場で入れられるようにする"""

    #: 導入が終わった 引数は成功したか
    finished = Signal(bool)
    #: 状態を見直した 引数は「いま使えるか」
    changed = Signal(bool)

    def __init__(self, pack: FeaturePack, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pack = pack
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._done: threading.Event | None = None
        self._code = 0

        self._status = QLabel(self)
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {Colors.TEXT_MUTED.name()};")

        self._extra = QCheckBox(pack.extra_label or "追加分も入れる", self)
        self._extra.setChecked(True)
        self._extra.setVisible(bool(pack.extra))
        self._extra.toggled.connect(self.refresh)

        self._button = QPushButton("環境を導入", self)
        self._button.clicked.connect(self.start)

        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)

        self._log = QPlainTextEdit(self)
        self._log.setReadOnly(True)
        self._log.setVisible(False)
        self._log.setMaximumBlockCount(2000)
        self._log.setMaximumHeight(160)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._extra)
        row.addStretch(1)
        row.addWidget(self._button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._status)
        layout.addLayout(row)
        layout.addWidget(self._progress)
        layout.addWidget(self._log)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self.refresh()

    # --- 状態 ---

    @property
    def status(self) -> PackStatus:
        return self._pack.status()

    @property
    def extra(self) -> bool:
        """追加分（CUDA など）を入れる／使う選択"""
        return self._extra.isChecked()

    @property
    def busy(self) -> bool:
        return self._done is not None

    def refresh(self) -> None:
        """導入状況を見直して表示を作り直す"""
        status = self.status
        self._button.setText("環境を更新" if status.installed else "環境を導入")
        self._extra.setEnabled(not status.extra_installed and not self.busy)

        lines = [status.summary()]
        if not status.installed:
            packages = "、".join(status.missing(extra=self.extra))
            lines.append(f"入れるもの: {packages}")
            size = status.download_mb(extra=self.extra)
            if size:
                lines.append(f"ダウンロードは {_readable(size)} ほどです")
        self._status.setText("\n".join(lines))
        self.changed.emit(status.ready)

    def command_text(self) -> str:
        """これから実行するコマンド 画面に見せるため"""
        return " ".join(install_command(self._pack, extra=self.extra))

    # --- 導入 ---

    def start(self) -> None:
        if self.busy:
            return
        argv = install_command(self._pack, extra=self.extra)
        self._log.setVisible(True)
        self._log.clear()
        self._progress.setVisible(True)
        self._button.setEnabled(False)
        self._extra.setEnabled(False)
        self._status.setText("導入しています 数分かかることがあります")

        done = threading.Event()
        self._done = done

        def run() -> None:
            code = install_runtime(
                command=argv, on_output=self._log_queue.put, should_cancel=done.is_set
            )
            self._code = code
            self._log_queue.put(
                "導入が完了しました" if code == 0 else f"導入に失敗しました（コード {code}）"
            )
            done.set()

        threading.Thread(
            target=run, name=f"sashimono-install-{self._pack.key}", daemon=True
        ).start()
        self._timer.start()

    def cancel(self) -> None:
        if self._done is not None:
            self._done.set()

    def _poll(self) -> None:
        while True:
            try:
                self._log.appendPlainText(self._log_queue.get_nowait())
            except queue.Empty:
                break

        done = self._done
        if done is None or not done.is_set():
            return
        self._done = None
        self._timer.stop()
        self._progress.setVisible(False)
        self._button.setEnabled(True)
        succeeded = self._code == 0
        # 状態を見直す前に import の道を作り直す 先に見直すと、配布版では
        # 入れたばかりのものが見えず「未導入」のまま止まる
        loaded = refresh_runtime() if succeeded else ()
        self.refresh()
        if succeeded:
            note = restart_note(loaded, visible=self.status.installed)
            self._status.setText(f"{self._status.text()}\n{note}")
        self.finished.emit(succeeded)


def _readable(megabytes: int) -> str:
    return f"{megabytes / 1000:.1f} GB" if megabytes >= 1000 else f"{megabytes} MB"
