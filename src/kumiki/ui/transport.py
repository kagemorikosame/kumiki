"""再生コントロールとタイムコード表示。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QWidget

from kumiki.core.timebase import FrameRate, format_timecode
from kumiki.engine.render import RenderQuality
from kumiki.ui.theme import Colors

__all__ = ["TransportBar"]

#: 再生品質の選択肢。分母が大きいほど軽くなる。
QUALITY_CHOICES: tuple[tuple[str, int], ...] = (
    ("フル画質", 1),
    ("1/2 画質", 2),
    ("1/4 画質", 4),
)


class TransportBar(QWidget):
    """再生・停止・コマ送りと、現在位置の表示。"""

    play_toggled = Signal()
    step_requested = Signal(int)
    jump_requested = Signal(int)
    quality_changed = Signal(object)

    def __init__(self, rate: FrameRate, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rate = rate
        self._frame = 0
        self._duration = 0

        self._to_start = _tool_button("|◀", "先頭へ (Home)")
        self._back = _tool_button("◀|", "1 フレーム戻る (←)")
        self._play = _tool_button("▶", "再生 / 停止 (Space)")
        self._forward = _tool_button("|▶", "1 フレーム進む (→)")
        self._to_end = _tool_button("▶|", "末尾へ (End)")

        self._to_start.clicked.connect(lambda: self.jump_requested.emit(0))
        self._back.clicked.connect(lambda: self.step_requested.emit(-1))
        self._play.clicked.connect(self.play_toggled.emit)
        self._forward.clicked.connect(lambda: self.step_requested.emit(1))
        self._to_end.clicked.connect(lambda: self.jump_requested.emit(self._duration))

        self._timecode = QLabel(self)
        monospace = QFont("Consolas")
        monospace.setStyleHint(QFont.StyleHint.Monospace)
        monospace.setPointSizeF(11)
        self._timecode.setFont(monospace)
        self._timecode.setStyleSheet(f"color: {Colors.TEXT.name()};")

        self._duration_label = QLabel(self)
        self._duration_label.setFont(monospace)
        self._duration_label.setStyleSheet(f"color: {Colors.TEXT_MUTED.name()};")

        self._quality = QComboBox(self)
        for label, divisor in QUALITY_CHOICES:
            self._quality.addItem(label, divisor)
        self._quality.currentIndexChanged.connect(self._on_quality_changed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)
        for button in (self._to_start, self._back, self._play, self._forward, self._to_end):
            layout.addWidget(button)
        layout.addSpacing(12)
        layout.addWidget(self._timecode)
        layout.addWidget(QLabel("/", self))
        layout.addWidget(self._duration_label)
        layout.addStretch(1)
        layout.addWidget(QLabel("再生品質", self))
        layout.addWidget(self._quality)

        self._refresh()

    def set_rate(self, rate: FrameRate) -> None:
        self._rate = rate
        self._refresh()

    def set_frame(self, frame: int) -> None:
        self._frame = max(0, frame)
        self._refresh()

    def set_duration(self, frames: int) -> None:
        self._duration = max(0, frames)
        self._refresh()

    def set_playing(self, playing: bool) -> None:
        self._play.setText("⏸" if playing else "▶")

    def _on_quality_changed(self, index: int) -> None:
        self.quality_changed.emit(RenderQuality(int(self._quality.itemData(index))))

    def _refresh(self) -> None:
        self._timecode.setText(format_timecode(self._frame, self._rate))
        self._duration_label.setText(format_timecode(self._duration, self._rate))


def _tool_button(text: str, tooltip: str) -> QPushButton:
    button = QPushButton(text)
    button.setToolTip(tooltip)
    button.setFixedWidth(38)
    # ボタンにフォーカスが入ると、Space が再生ではなくボタンの押下になる。
    # 再生ソフトで一番使うキーなので、そこは奪わせない。
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return button
