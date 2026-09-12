"""プロジェクト設定（解像度）"""

from __future__ import annotations

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

from kumiki.core.commands.edit import MAX_RESOLUTION, MIN_RESOLUTION
from kumiki.core.model import ProjectSettings

__all__ = ["RESOLUTION_PRESETS", "ProjectSettingsDialog"]

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
    """解像度を選ぶ フレームレートは見せるだけで変えさせない

    タイムラインの位置はフレーム番号で持っている あとからフレームレートを変えると、
    すべてのクリップとキーフレームを換算し直すことになり、端数の丸めで 1 フレームの
    隙間や重なりが出る いまは作るときにだけ決める
    """

    def __init__(self, settings: ProjectSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("プロジェクト設定")

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

        fps = settings.frame_rate
        rate = QLabel(f"{float(fps.fps):g} fps（作成後は変えられません）", self)
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
