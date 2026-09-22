"""プロジェクト設定（解像度） 新規作成のときはフレームレートも"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sashimono.core.commands.edit import MAX_RESOLUTION, MIN_RESOLUTION
from sashimono.core.model import ProjectSettings
from sashimono.core.timebase import FrameRate

__all__ = ["FRAME_RATE_PRESETS", "RESOLUTION_PRESETS", "ProjectSettingsDialog"]

#: 選べるフレームレート 分数のものは分数のまま持つ 29.97 を小数で持つと、
#: 1 時間で 3 フレーム以上ずれる（:meth:`FrameRate.from_decimal` を参照）
FRAME_RATE_PRESETS: tuple[tuple[str, FrameRate], ...] = (
    ("23.976 fps（映画の NTSC 版）", FrameRate(24000, 1001)),
    ("24 fps（映画）", FrameRate(24)),
    ("25 fps（PAL）", FrameRate(25)),
    ("29.97 fps（テレビ・NTSC）", FrameRate(30000, 1001)),
    ("30 fps（配信・ゆっくり実況）", FrameRate(30)),
    ("50 fps", FrameRate(50)),
    ("59.94 fps", FrameRate(60000, 1001)),
    ("60 fps（ゲーム実況）", FrameRate(60)),
)

#: よく使う解像度 表示名は用途で書く 数字だけだと縦か横かを取り違える
RESOLUTION_PRESETS: tuple[tuple[str, int, int], ...] = (
    ("フル HD 横（1920×1080）", 1920, 1080),
    ("HD 横（1280×720）", 1280, 720),
    ("4K 横（3840×2160）", 3840, 2160),
    ("縦動画・ショート（1080×1920）", 1080, 1920),
    ("正方形（1080×1080）", 1080, 1080),
)

_CUSTOM = "指定する"


class ProjectSettingsDialog(QDialog):
    """解像度を選ぶ フレームレートは ``new`` のとき（新規作成）だけ選ばせる

    タイムラインの位置はフレーム番号で持っている あとからフレームレートを変えると、
    すべてのクリップとキーフレームを換算し直すことになり、端数の丸めで 1 フレームの
    隙間や重なりが出る いまは作るときにだけ決める
    """

    def __init__(
        self, settings: ProjectSettings, parent: QWidget | None = None, *, new: bool = False
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("新規プロジェクト" if new else "プロジェクト設定")
        self._base = settings
        self._rate: QComboBox | None = None

        self._preset = QComboBox(self)
        for label, _, _ in RESOLUTION_PRESETS:
            self._preset.addItem(label)
        self._preset.addItem(_CUSTOM)

        self._width = self._spin(settings.width)
        self._height = self._spin(settings.height)
        swap = QPushButton("縦横を入れ替える", self)
        swap.clicked.connect(self._swap)

        size_row = QHBoxLayout()
        size_row.addWidget(self._width)
        size_row.addWidget(QLabel("×", self))
        size_row.addWidget(self._height)
        size_row.addWidget(swap)

        rate: QWidget
        if new:
            self._rate = QComboBox(self)
            for label, _ in FRAME_RATE_PRESETS:
                self._rate.addItem(label)
            rates = [preset for _, preset in FRAME_RATE_PRESETS]
            if settings.frame_rate in rates:
                self._rate.setCurrentIndex(rates.index(settings.frame_rate))
            rate = self._rate
        else:
            rate = QLabel(f"{settings.frame_rate} fps（作成後は変えられません）", self)
            rate.setEnabled(False)

        self._warning = QLabel(self)
        self._warning.setStyleSheet("color: #e07a5f;")

        form = QFormLayout()
        form.addRow("解像度", self._preset)
        form.addRow("", size_row)
        form.addRow("フレームレート", rate)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._warning)
        layout.addWidget(self._buttons)

        self._preset.currentIndexChanged.connect(self._on_preset)
        self._width.valueChanged.connect(self._sync)
        self._height.valueChanged.connect(self._sync)
        self._sync()

    def resolution(self) -> tuple[int, int]:
        return self._width.value(), self._height.value()

    def settings(self) -> ProjectSettings:
        """選んだ内容を反映した設定 音声の設定などは渡されたものを引き継ぐ"""
        width, height = self.resolution()
        rate = (
            FRAME_RATE_PRESETS[self._rate.currentIndex()][1]
            if self._rate is not None
            else self._base.frame_rate
        )
        return replace(self._base, width=width, height=height, frame_rate=rate)

    def _spin(self, value: int) -> QSpinBox:
        spin = QSpinBox(self)
        spin.setRange(MIN_RESOLUTION, MAX_RESOLUTION)
        spin.setSingleStep(2)
        spin.setValue(value)
        return spin

    def _on_preset(self, index: int) -> None:
        if index >= len(RESOLUTION_PRESETS):
            return
        _, width, height = RESOLUTION_PRESETS[index]
        self._width.setValue(width)
        self._height.setValue(height)

    def _swap(self) -> None:
        width, height = self.resolution()
        self._width.setValue(height)
        self._height.setValue(width)

    def _sync(self) -> None:
        """選択肢の表示と、偶数かどうかの注意を今の数字に合わせる"""
        size = self.resolution()
        matched = next(
            (i for i, (_, w, h) in enumerate(RESOLUTION_PRESETS) if (w, h) == size),
            len(RESOLUTION_PRESETS),
        )
        self._preset.blockSignals(True)
        self._preset.setCurrentIndex(matched)
        self._preset.blockSignals(False)

        odd = any(value % 2 for value in size)
        # 奇数は書き出しで断られる 閉じてからでは気付けないので、ここで押せなくする
        self._warning.setText("縦横とも偶数にしてください（奇数だと書き出せません）" if odd else "")
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(not odd)
