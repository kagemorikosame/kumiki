"""パラメータ 1 つ分の入力欄

種類ごとにウィジェットを 1 つずつ用意し、:class:`~sashimono.effects.ParameterSpec`
から自動で選ぶ エフェクトを増やしても UI を書き足す必要は無く、AviUtl の
スクリプトを読み込んだとき（P5）も同じ経路で設定欄が出る
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QWidget,
)

from sashimono.core.model import AnimatedValue, ParamValue
from sashimono.effects import (
    CheckSpec,
    ColorSpec,
    FileSpec,
    FontSpec,
    GridSpec,
    ParameterSpec,
    SelectSpec,
    TextSpec,
    TrackSpec,
    ValueSpec,
)
from sashimono.ui.theme import Colors

__all__ = ["ParameterEditor", "create_editor"]

#: スライダーは整数しか扱えないので、この倍率で小数を載せる
_SLIDER_SCALE = 1000
#: Qt の整数の欄（``QSpinBox`` ``QSlider``）が持てる範囲 C++ の ``int`` は 4 バイト
_INT_MIN = -(2**31)
_INT_MAX = 2**31 - 1


def _qt_int(number: float) -> int:
    """Qt の整数の欄へ渡せる値へ丸める

    収まらない値をそのまま渡すと、shiboken が警告を出したうえで Qt 側の値が化け、
    入れられる範囲が意図と違ってしまう（#148） 仕様は AviUtl のスクリプトからも
    来るので、どれほど大きな範囲が書かれていてもここで収める
    """
    return min(max(int(number), _INT_MIN), _INT_MAX)


class ParameterEditor(QWidget):
    """パラメータ入力欄の共通の親

    値が確定したら :attr:`value_changed` を出す ドラッグ中の途中経過は
    :attr:`value_previewed` で、こちらは履歴に残さない前提
    """

    #: 値が確定した 履歴に残る変更
    value_changed = Signal(object)
    #: ドラッグ中の途中経過 プレビューだけ更新する
    value_previewed = Signal(object)

    def __init__(self, spec: ParameterSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self._updating = False

    def set_value(self, value: ParamValue | None) -> None:
        """外から値を入れ直す 信号は出さない

        入れ直しで信号を出すと、プロジェクトの更新 → UI 更新 → 変更通知 →
        プロジェクトの更新、と回り続ける
        """
        raise NotImplementedError

    def _emit(self, value: ParamValue) -> None:
        if not self._updating:
            self.value_changed.emit(value)

    def _preview(self, value: ParamValue) -> None:
        if not self._updating:
            self.value_previewed.emit(value)


class TrackEditor(ParameterEditor):
    """数値スライダーと数値欄の組

    スライダーだけだと細かい値を入れられず、数値欄だけだと感覚的に動かせない
    両方を出して同期させる
    """

    def __init__(self, spec: TrackSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent)
        self._spec = spec

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setRange(
            _qt_int(spec.minimum * _SLIDER_SCALE), _qt_int(spec.maximum * _SLIDER_SCALE)
        )
        self._slider.valueChanged.connect(self._on_slider)
        self._slider.sliderReleased.connect(self._on_release)

        self._number = QDoubleSpinBox(self)
        self._number.setRange(spec.minimum, spec.maximum)
        self._number.setSingleStep(spec.step)
        self._number.setDecimals(0 if spec.step >= 1 else 2)
        self._number.setSuffix(f" {spec.unit}" if spec.unit else "")
        self._number.setFixedWidth(96)
        self._number.setKeyboardTracking(False)
        self._number.valueChanged.connect(self._on_number)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._slider, 1)
        layout.addWidget(self._number)

        self.set_value(spec.default_value())

    def set_value(self, value: ParamValue | None) -> None:
        animated = self._spec.coerce(value)
        self._apply(animated.static if not animated.is_animated else animated.at(0))

    def set_animated_value(self, value: float) -> None:
        """キーフレームで決まった現在値を表示に反映する"""
        self._apply(value)

    def _apply(self, number: float) -> None:
        self._updating = True
        try:
            self._number.setValue(number)
            self._slider.setValue(_qt_int(number * _SLIDER_SCALE))
        finally:
            self._updating = False

    def _on_slider(self, raw: int) -> None:
        if self._updating:
            return
        number = raw / _SLIDER_SCALE
        self._updating = True
        try:
            self._number.setValue(number)
        finally:
            self._updating = False
        # ドラッグ中は履歴に残さない 1 回のドラッグで数十の取り消し段ができると
        # 元の値まで戻すのに数十回押すことになる
        self._preview(AnimatedValue(static=number))

    def _on_release(self) -> None:
        self._emit(AnimatedValue(static=self._slider.value() / _SLIDER_SCALE))

    def _on_number(self, number: float) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            self._slider.setValue(_qt_int(number * _SLIDER_SCALE))
        finally:
            self._updating = False
        self._emit(AnimatedValue(static=number))


class CheckEditor(ParameterEditor):
    def __init__(self, spec: CheckSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent)
        self._spec = spec
        self._box = QCheckBox(self)
        self._box.toggled.connect(lambda state: self._emit(bool(state)))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._box)
        layout.addStretch(1)
        self.set_value(spec.default_value())

    def set_value(self, value: ParamValue | None) -> None:
        self._updating = True
        try:
            self._box.setChecked(self._spec.coerce(value))
        finally:
            self._updating = False


class ColorEditor(ParameterEditor):
    """色見本のボタン 押すと色選択ダイアログが出る"""

    def __init__(self, spec: ColorSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent)
        self._spec = spec
        self._value: tuple[float, ...] = spec.default_value()

        self._button = QPushButton(self)
        self._button.setFixedHeight(24)
        self._button.clicked.connect(self._choose)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._button, 1)
        self.set_value(spec.default_value())

    def set_value(self, value: ParamValue | None) -> None:
        self._value = self._spec.coerce(value)
        red, green, blue, alpha = (round(c * 255) for c in self._value)
        # 明るい色の上に黒、暗い色の上に白を置く どちらか一方だと必ず読めなくなる
        luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
        text = "#000000" if luminance > 140 else "#ffffff"
        self._button.setStyleSheet(
            f"background-color: rgba({red}, {green}, {blue}, {alpha});"
            f"color: {text}; border: 1px solid {Colors.BORDER.name()};"
        )
        self._button.setText(f"#{red:02X}{green:02X}{blue:02X}")

    def _choose(self) -> None:
        from PySide6.QtGui import QColor

        red, green, blue, alpha = (round(c * 255) for c in self._value)
        options = (
            QColorDialog.ColorDialogOption.ShowAlphaChannel
            if self._spec.with_alpha
            else QColorDialog.ColorDialogOption(0)
        )
        chosen = QColorDialog.getColor(QColor(red, green, blue, alpha), self, "色を選ぶ", options)
        if not chosen.isValid():
            return
        value = (
            chosen.red() / 255.0,
            chosen.green() / 255.0,
            chosen.blue() / 255.0,
            chosen.alpha() / 255.0,
        )
        self.set_value(value)
        self._emit(value)


class SelectEditor(ParameterEditor):
    def __init__(self, spec: SelectSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent)
        self._spec = spec
        self._box = QComboBox(self)
        for identifier, label in spec.choices:
            self._box.addItem(label, identifier)
        self._box.currentIndexChanged.connect(
            lambda index: self._emit(str(self._box.itemData(index)))
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._box, 1)
        self.set_value(spec.default_value())

    def set_value(self, value: ParamValue | None) -> None:
        self._updating = True
        try:
            index = self._box.findData(self._spec.coerce(value))
            self._box.setCurrentIndex(max(0, index))
        finally:
            self._updating = False


class TextEditor(ParameterEditor):
    def __init__(self, spec: TextSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent)
        self._spec = spec
        self._multiline = spec.multiline

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if spec.multiline:
            self._area = QPlainTextEdit(self)
            self._area.setFixedHeight(72)
            # 入力のたびに確定させる テキストは打った結果をすぐ見たい
            self._area.textChanged.connect(lambda: self._emit(self._area.toPlainText()))
            layout.addWidget(self._area, 1)
        else:
            self._line = QLineEdit(self)
            self._line.textChanged.connect(self._emit)
            layout.addWidget(self._line, 1)

        self.set_value(spec.default_value())

    def set_value(self, value: ParamValue | None) -> None:
        text = self._spec.coerce(value)
        self._updating = True
        try:
            if self._multiline:
                if self._area.toPlainText() != text:
                    self._area.setPlainText(text)
            elif self._line.text() != text:
                self._line.setText(text)
        finally:
            self._updating = False


class FontEditor(ParameterEditor):
    def __init__(self, spec: FontSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent)
        self._spec = spec
        self._box = QFontComboBox(self)
        self._box.currentFontChanged.connect(lambda font: self._emit(font.family()))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._box, 1)
        self.set_value(spec.default_value())

    def set_value(self, value: ParamValue | None) -> None:
        from PySide6.QtGui import QFont

        self._updating = True
        try:
            self._box.setCurrentFont(QFont(self._spec.coerce(value)))
        finally:
            self._updating = False


class FileEditor(ParameterEditor):
    def __init__(self, spec: FileSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent)
        self._spec = spec
        self._line = QLineEdit(self)
        self._line.editingFinished.connect(lambda: self._emit(self._line.text()))

        browse = QPushButton("…", self)
        browse.setFixedWidth(32)
        browse.clicked.connect(self._choose)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._line, 1)
        layout.addWidget(browse)
        self.set_value(spec.default_value())

    def set_value(self, value: ParamValue | None) -> None:
        self._updating = True
        try:
            self._line.setText(self._spec.coerce(value))
        finally:
            self._updating = False

    def _choose(self) -> None:
        if self._spec.directory:
            chosen = QFileDialog.getExistingDirectory(self, self._spec.label, self._line.text())
        else:
            chosen, _ = QFileDialog.getOpenFileName(
                self, self._spec.label, self._line.text(), self._spec.filter
            )
        if chosen:
            self.set_value(chosen)
            self._emit(chosen)


class ValueEditor(ParameterEditor):
    def __init__(self, spec: ValueSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent)
        self._spec = spec
        self._box = QSpinBox(self)
        self._box.setRange(_qt_int(spec.minimum), _qt_int(spec.maximum))
        self._box.setKeyboardTracking(False)
        self._box.valueChanged.connect(self._emit)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._box)
        layout.addStretch(1)
        self.set_value(spec.default_value())

    def set_value(self, value: ParamValue | None) -> None:
        self._updating = True
        try:
            self._box.setValue(_qt_int(self._spec.coerce(value)))
        finally:
            self._updating = False


class GridEditor(ParameterEditor):
    """格子の大きさを見せるだけの欄 触らせない

    点は 5x5 で 50 個あり、並べても読めないし、ずらして直したいときに
    欲しいのは数値欄ではなく画面上の掴み手 互換で読み込んだ格子が
    「消えた」と思われないよう、大きさだけは出しておく
    """

    def __init__(self, spec: GridSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent)
        self._spec = spec
        self._label = QLabel(self)
        self._label.setEnabled(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)
        layout.addStretch(1)
        self.set_value(spec.default_value())

    def set_value(self, value: ParamValue | None) -> None:
        columns, rows = self._spec.size(self._spec.coerce(value))
        if columns < 2 or rows < 2:
            self._label.setText("なし（四隅で変形）")
            return
        self._label.setText(f"{columns}x{rows} の格子")


def create_editor(spec: ParameterSpec, parent: QWidget | None = None) -> ParameterEditor:
    """仕様に合う入力欄を作る"""
    if isinstance(spec, TrackSpec):
        return TrackEditor(spec, parent)
    if isinstance(spec, CheckSpec):
        return CheckEditor(spec, parent)
    if isinstance(spec, ColorSpec):
        return ColorEditor(spec, parent)
    if isinstance(spec, SelectSpec):
        return SelectEditor(spec, parent)
    if isinstance(spec, TextSpec):
        return TextEditor(spec, parent)
    if isinstance(spec, FontSpec):
        return FontEditor(spec, parent)
    if isinstance(spec, FileSpec):
        return FileEditor(spec, parent)
    if isinstance(spec, GridSpec):
        return GridEditor(spec, parent)
    return ValueEditor(spec, parent)
