"""書き出しダイアログと、その進捗

書き出しは別スレッドで走らせる メインスレッドで回すと、数分間 UI が固まって
中止すらできなくなる
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sashimono.core.model import Project
from sashimono.engine.encode import ExportError, ExportSettings, available_video_codecs
from sashimono.engine.encode import export_project as run_export

__all__ = ["ExportDialog"]

#: コーデック名と、画面に出す説明
CODEC_LABELS = {
    "h264_nvenc": "H.264 (NVIDIA GPU)",
    "hevc_nvenc": "H.265 (NVIDIA GPU)",
    "av1_nvenc": "AV1 (NVIDIA GPU)",
    "h264_qsv": "H.264 (Intel GPU)",
    "libx264": "H.264 (CPU)",
    "libx265": "H.265 (CPU)",
}

#: 「自動」の項目に持たせる値 ``None`` は「コーデックが 1 つも無く書き出せない」項目が
#: 使っているので分ける 同じにすると、自動を選んで書き出しを押しても何も起きない
AUTO_CODEC = ""


class _ExportWorker(QObject):
    """別スレッドで書き出しを回す"""

    progressed = Signal(float)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, project: Project, settings: ExportSettings) -> None:
        super().__init__()
        self._project = project
        self._settings = settings
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            path = run_export(
                self._project,
                self._settings,
                progress=self.progressed.emit,
                should_cancel=self._cancel.is_set,
            )
        except ExportError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"書き出しに失敗した: {exc}")
        else:
            self.finished.emit(str(path))


class ExportDialog(QDialog):
    """書き出しの設定と実行"""

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("書き出し")
        self.setModal(True)
        self.resize(480, 260)

        self._project = project
        self._thread: QThread | None = None
        self._worker: _ExportWorker | None = None

        default_name = f"{project.name}.mp4"
        self._path = QLineEdit(str(Path.home() / "Videos" / default_name), self)
        browse = QPushButton("参照…", self)
        browse.clicked.connect(self._choose_path)
        path_row = QHBoxLayout()
        path_row.addWidget(self._path)
        path_row.addWidget(browse)

        self._codec = QComboBox(self)
        codecs = available_video_codecs()
        if codecs:
            # 既定は名指しせず書き出し側に選ばせる 先頭を名指しで渡すと、試しには開けても
            # 作品の大きさで断られたとき（NVENC は幅 4096 まで）に次の候補へ落ちられない
            first = CODEC_LABELS.get(codecs[0], codecs[0])
            self._codec.addItem(f"自動（{first}、使えなければ次の候補）", AUTO_CODEC)
        for name in codecs:
            self._codec.addItem(CODEC_LABELS.get(name, name), name)
        if not codecs:
            self._codec.addItem("利用できるコーデックが無い", None)
            self._codec.setEnabled(False)

        self._bitrate = QSpinBox(self)
        self._bitrate.setRange(1, 200)
        self._bitrate.setValue(12)
        self._bitrate.setSuffix(" Mbps")

        width, height = project.settings.resolution
        summary = (
            f"{width}x{height} / {project.settings.frame_rate} fps / {project.duration} フレーム"
        )

        form = QFormLayout()
        form.addRow("出力先", path_row)
        form.addRow("コーデック", self._codec)
        form.addRow("ビットレート", self._bitrate)
        form.addRow("内容", QLabel(summary, self))

        self._progress = QProgressBar(self)
        self._progress.setRange(0, 1000)
        self._progress.setVisible(False)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("書き出し")
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("閉じる")
        self._buttons.accepted.connect(self._start)
        self._buttons.rejected.connect(self._cancel_or_close)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._progress)
        layout.addStretch(1)
        layout.addWidget(self._buttons)

        if project.duration <= 0:
            self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            form.addRow("", QLabel("タイムラインが空なので書き出せない", self))

    def _choose_path(self) -> None:
        name, _ = QFileDialog.getSaveFileName(
            self, "書き出し先", self._path.text(), "MP4 (*.mp4);;すべてのファイル (*)"
        )
        if name:
            self._path.setText(name)

    def _settings(self) -> ExportSettings | None:
        """画面の選択から書き出しの設定を作る 使えるコーデックが無ければ ``None``"""
        codec = self._codec.currentData()
        if codec is None:
            return None
        return ExportSettings(
            path=Path(self._path.text()),
            video_codec=str(codec) or None,
            video_bitrate=self._bitrate.value() * 1_000_000,
        )

    def _start(self) -> None:
        if self._thread is not None:
            return
        settings = self._settings()
        if settings is None:
            return

        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("中止")

        self._worker = _ExportWorker(self._project, settings)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progressed.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _cancel_or_close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            return
        self.reject()

    def _on_progress(self, value: float) -> None:
        self._progress.setValue(int(value * 1000))

    def _on_finished(self, path: str) -> None:
        self._teardown()
        QMessageBox.information(self, "書き出し", f"書き出しました\n{path}")
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._teardown()
        QMessageBox.warning(self, "書き出し", message)

    def _teardown(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
        self._worker = None
        self._progress.setVisible(False)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("閉じる")

    def closeEvent(self, event: object) -> None:  # noqa: N802 - Qt の命名規約
        # 書き出し中に閉じられたら、スレッドを畳んでから終わる
        # 放置すると Qt がスレッドの生存中に破棄されたと言って落ちる
        if self._worker is not None:
            self._worker.cancel()
            if self._thread is not None:
                self._thread.quit()
                self._thread.wait(5000)
        super().closeEvent(event)  # type: ignore[arg-type]
