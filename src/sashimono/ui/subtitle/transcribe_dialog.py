"""起こしの実行ダイアログと、実行環境の導入

字幕起こしの依存は合計で 2 GB を超えるので、**初期状態では入っていない** この
ダイアログが未導入を検出したときは、起こしのボタンの代わりに「環境を導入」を出す
入れるものと実行するコマンドをそのまま画面に見せてから始める 何が入るのか
分からないまま数分のダウンロードが走るのは、それ自体が不具合に見える

導入も起こしもワーカースレッドで動く ウィジェットに触るのはタイマーで拾った
メインスレッド側だけにしてある
"""

from __future__ import annotations

import queue
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sashimono.asr import (
    MODELS,
    JobKind,
    TranscribeOptions,
    TranscriptionService,
    install_command,
    install_runtime,
    runtime_status,
)
from sashimono.asr.service import Job
from sashimono.core.model import MediaItem, Transcript
from sashimono.ui.theme import Colors

__all__ = ["TranscribeDialog"]

#: ワーカーからの知らせを拾う間隔（ミリ秒）
POLL_MS = 100

#: 選べる言語 自動判定は精度が落ちるので、既定は日本語にしておく
LANGUAGES: tuple[tuple[str | None, str], ...] = (
    ("ja", "日本語"),
    ("en", "英語"),
    (None, "自動判定"),
)


class TranscribeDialog(QDialog):
    """1 つの素材を起こす

    結果は :attr:`transcript` に入る 呼び出し側がそれをコマンドにして履歴へ載せる
    ここでプロジェクトを書き換えないのは、UI と AI が同じ入口を通るという方針を
    崩さないため
    """

    def __init__(
        self, media: MediaItem, service: TranscriptionService, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"字幕起こし — {media.name}")
        self.resize(560, 420)

        self._media = media
        self._service = service
        self._job: Job | None = None
        self.transcript: Transcript | None = None

        #: 導入ワーカーからのログ スレッドをまたぐのでキューで受ける
        self._install_log: queue.Queue[str] = queue.Queue()
        self._install_done: threading.Event | None = None
        self._install_code = 0

        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._refresh_availability()

    # --- 組み立て ---

    def _build(self) -> None:
        self._model = QComboBox(self)
        for info in MODELS:
            self._model.addItem(info.describe(), info.name)

        self._language = QComboBox(self)
        for code, label in LANGUAGES:
            self._language.addItem(label, code)

        self._prompt = QLineEdit(self)
        self._prompt.setPlaceholderText("固有名詞など（任意）")

        self._words = QCheckBox("単語ごとの時刻も取る（分割の精度が上がる・遅くなる）", self)
        self._gpu = QCheckBox("GPU を使う", self)
        self._gpu.setChecked(True)
        self._gpu.toggled.connect(self._describe_install)

        form = QFormLayout()
        form.addRow("モデル", self._model)
        form.addRow("言語", self._language)
        form.addRow("ヒント", self._prompt)
        form.addRow("", self._words)
        form.addRow("", self._gpu)

        self._status = QLabel(self)
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {Colors.TEXT_MUTED.name()};")

        self._log = QPlainTextEdit(self)
        self._log.setReadOnly(True)
        self._log.setVisible(False)
        self._log.setMaximumBlockCount(2000)

        self._progress = QProgressBar(self)
        self._progress.setRange(0, 1000)
        self._progress.setVisible(False)

        self._install_button = QPushButton("環境を導入", self)
        self._install_button.clicked.connect(self._start_install)
        self._run_button = QPushButton("起こす", self)
        self._run_button.setDefault(True)
        self._run_button.clicked.connect(self._start_transcribe)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        buttons.rejected.connect(self.reject)
        self._cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if self._cancel_button is not None:
            # 既定の文言は環境の言語に従うので、ここで日本語に固定する
            self._cancel_button.setText("閉じる")

        actions = QHBoxLayout()
        actions.addWidget(self._install_button)
        actions.addStretch(1)
        actions.addWidget(self._run_button)
        actions.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._status)
        layout.addWidget(self._progress)
        layout.addWidget(self._log, 1)
        layout.addLayout(actions)

    # --- 状態 ---

    def _refresh_availability(self) -> None:
        """導入状況を見て、押せるボタンを決める"""
        status = runtime_status()
        self._run_button.setEnabled(status.installed)
        self._install_button.setEnabled(True)
        # 未導入のときも触れるようにする ここが「GPU 版を入れるか」の選択を
        # 兼ねていて、切れば CUDA ランタイム（2 GB 弱）を落とさずに済む
        self._gpu.setEnabled(status.extra_installed or not status.installed)

        if status.installed:
            self._install_button.setText("環境を更新")
            self._status.setText(status.summary())
            if not status.extra_installed:
                self._gpu.setChecked(False)
            return

        self._install_button.setText("環境を導入")
        self._describe_install()

    def _describe_install(self) -> None:
        """これから入るものを出す 何が落ちてくるのか分かってから始められるように"""
        status = runtime_status()
        if status.installed:
            return
        cuda = self._gpu.isChecked()
        packages = "、".join(status.missing(extra=cuda))
        size = "2 GB" if cuda else "300 MB"
        self._status.setText(
            f"{status.summary()}\n入れるもの: {packages}\n"
            f"初回は {size} ほどのダウンロードがあります"
        )

    def _set_busy(self, busy: bool, *, message: str = "") -> None:
        self._run_button.setEnabled(not busy and runtime_status().ready)
        self._install_button.setEnabled(not busy)
        self._model.setEnabled(not busy)
        self._language.setEnabled(not busy)
        self._progress.setVisible(busy)
        if self._cancel_button is not None:
            self._cancel_button.setText("中断" if busy else "閉じる")
        if message:
            self._status.setText(message)

    # --- 導入 ---

    def _start_install(self) -> None:
        command = install_command(cuda=self._gpu.isChecked())
        self._log.setVisible(True)
        self._log.clear()
        self._set_busy(True, message="導入しています 数分かかります")
        self._progress.setRange(0, 0)  # 進み具合が分からないので流れる表示にする

        done = threading.Event()
        self._install_done = done

        def run() -> None:
            code = install_runtime(
                command=command, on_output=self._install_log.put, should_cancel=done.is_set
            )
            self._install_code = code
            self._install_log.put(
                "導入が完了しました" if code == 0 else f"導入に失敗しました（コード {code}）"
            )
            done.set()

        threading.Thread(target=run, name="sashimono-asr-install", daemon=True).start()
        self._timer.start()

    # --- 起こし ---

    def _start_transcribe(self) -> None:
        options = TranscribeOptions(
            model=str(self._model.currentData()),
            language=self._language.currentData(),
            device="cuda" if self._gpu.isChecked() else "cpu",
            compute_type="float16" if self._gpu.isChecked() else "int8",
            word_timestamps=self._words.isChecked(),
            initial_prompt=self._prompt.text().strip(),
        )
        try:
            self._job = self._service.start(self._media.id, self._media.path, options)
        except RuntimeError as exc:
            self._status.setText(str(exc))
            return

        self._progress.setRange(0, 1000)
        self._set_busy(True, message="起こしています 初回はモデルの取得に時間がかかります")
        self._timer.start()

    # --- ワーカーの見張り ---

    def _poll(self) -> None:
        self._drain_install_log()
        self._drain_job()

    def _drain_install_log(self) -> None:
        while True:
            try:
                self._log.appendPlainText(self._install_log.get_nowait())
            except queue.Empty:
                break

        done = self._install_done
        if done is None or not done.is_set():
            return
        self._install_done = None
        self._timer.stop()
        self._progress.setRange(0, 1000)
        self._set_busy(False)
        self._refresh_availability()
        if self._install_code == 0:
            self._status.setText("導入が終わりました そのまま起こせます")

    def _drain_job(self) -> None:
        job = self._job
        if job is None:
            return
        for event in job.poll():
            if event.kind is JobKind.PROGRESS:
                self._progress.setValue(int(event.ratio * 1000))
                self._status.setText(event.message)
                continue

            self._timer.stop()
            self._job = None
            self._set_busy(False)
            if event.kind is JobKind.DONE and event.transcript is not None:
                self.transcript = event.transcript
                self.accept()
            else:
                self._status.setText(event.message or "終了した")
            return

    # --- 終了 ---

    def reject(self) -> None:
        """中断 走っているものがあれば止めてから閉じる

        起こしは GPU を占有する 閉じたのに裏で回り続けると、次の操作が刺さる
        """
        if self._job is not None:
            self._job.cancel()
            self._status.setText("中断しています")
            return
        if self._install_done is not None:
            self._install_done.set()
            self._status.setText("中断しています")
            return
        self._timer.stop()
        super().reject()
