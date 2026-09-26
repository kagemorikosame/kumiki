"""パラメータ 1 つ分の入力欄

種類ごとにウィジェットを 1 つずつ用意し、:class:`~sashimono.effects.ParameterSpec`
から自動で選ぶ エフェクトを増やしても UI を書き足す必要は無く、AviUtl の
スクリプトを読み込んだとき（P5）も同じ経路で設定欄が出る
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPointF, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
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


#: 数値欄に出す小数の桁の上限 これより細かい刻みは画面で読めない
_MAX_DECIMALS = 6


def _decimals_for(spec: TrackSpec) -> int:
    """刻みと範囲の両端をそのまま表せる小数の桁数

    刻みが 1 以上なら整数、それより細かければ小数第 2 位までをまず取り、刻みや端が
    それより細かい桁を持つなら、その桁まで広げる 足りないと端が丸められ、数値欄が
    仕様の範囲の外の値を返す
    """
    decimals = 0 if spec.step >= 1 else 2
    for number in (spec.step, spec.minimum, spec.maximum):
        needed = next(
            (
                places
                for places in range(_MAX_DECIMALS + 1)
                if abs(round(number, places) - number) <= 1e-9 * max(1.0, abs(number))
            ),
            _MAX_DECIMALS,
        )
        decimals = max(decimals, needed)
    return decimals


class ParameterEditor(QWidget):
    """パラメータ入力欄の共通の親

    値が確定したら :attr:`value_changed` を出す ドラッグ中の途中経過は
    :attr:`value_previewed` で、こちらは履歴に残さない前提
    """

    #: 値が確定した 履歴に残る変更
    value_changed = Signal(object)
    #: ドラッグ中の途中経過 プレビューだけ更新する
    value_previewed = Signal(object)
    #: 初期値へ戻したい（数値のスライダーのダブルクリック） 戻し方はパネルが決める
    #: （キーフレームのある値は再生位置のキーだけを戻す）
    reset_requested = Signal()

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

        # 1000 倍すると int に収まらない広い範囲は、スライダーの端から端を仕様の最小から
        # 最大へ割り当て直す 端だけを int の上限で切ると、スライダーの端が仕様の端を
        # 表さず、範囲の外の値まで数値欄と保存へ流れる
        low = spec.minimum * _SLIDER_SCALE
        high = spec.maximum * _SLIDER_SCALE
        self._stretched = not (low >= _INT_MIN and high <= _INT_MAX)
        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        if self._stretched:
            self._slider.setRange(0, _INT_MAX)
        else:
            self._slider.setRange(int(low), int(high))
        self._slider.valueChanged.connect(self._on_slider)
        self._slider.sliderPressed.connect(self._on_press)
        self._slider.sliderReleased.connect(self._on_release)
        # スライダーのダブルクリックで初期値に戻す 数値欄のダブルクリックは今までどおり
        # 数字を選ぶ（打ち直す）ために残す
        self._slider.installEventFilter(self)
        #: 掴んだときの値 動かさずに離したときに、同じ値の変更を履歴へ積まないため
        self._pressed_at: float | None = None
        #: 2 回目の押下（ダブルクリック）の位置と、そのときの値 動かさずに離したら初期値へ戻し、
        #: 動かしたらふつうのドラッグにする（:meth:`eventFilter`）
        self._double: tuple[QPointF, float] | None = None
        #: 2 回目の押下を動かさずに離した 離したときの確定をせず、初期値へ戻す
        self._resetting = False

        self._number = QDoubleSpinBox(self)
        # 桁数は範囲より先に決める Qt は範囲も値も今の桁数へ丸めるので、後から決めると
        # 下限 0.004 が 0.00 になり、仕様の範囲の外の値が数値欄からプレビューと保存へ流れる
        self._number.setDecimals(_decimals_for(spec))
        self._number.setRange(spec.minimum, spec.maximum)
        self._number.setSingleStep(spec.step)
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
            self._slider.setValue(self._to_slider(number))
        finally:
            self._updating = False

    def _on_slider(self, raw: int) -> None:
        if self._updating:
            return
        number = self._from_slider(raw)
        self._updating = True
        try:
            self._number.setValue(number)
        finally:
            self._updating = False
        # 数値欄が表示の桁へ丸めた値を使う 広い範囲ではスライダー 1 目盛りが表示の桁より
        # 細かく、元の値を流すと、画面の数字とプレビュー・保存の値が食い違う
        # 桁が上限で足りないほど細かい端でも範囲の外へ出さない
        number = self._spec.clamp(self._number.value())
        if not self._slider.isSliderDown() and self._double is None:
            # 溝を押した・矢印キーで動かした 掴んでいないので離したときの知らせが来ない
            # プレビューだけにすると、絵は変わったのに履歴にも保存にも残らず、次に
            # 選び直したときに元の値へ戻る
            self._emit(AnimatedValue(static=number))
            return
        # ドラッグ中は履歴に残さない 1 回のドラッグで数十の取り消し段ができると
        # 元の値まで戻すのに数十回押すことになる
        self._preview(AnimatedValue(static=number))

    def _on_press(self) -> None:
        self._pressed_at = self._number.value()

    def _on_release(self) -> None:
        if self._resetting:
            # ダブルクリック 押した所へ動いた分は確定せず、初期値へ戻す（:meth:`eventFilter`）
            self._pressed_at = None
            return
        # 確定もプレビューと同じ、数値欄に出ている値にそろえる
        number = self._spec.clamp(self._number.value())
        pressed, self._pressed_at = self._pressed_at, None
        if pressed is not None and number == self._spec.clamp(pressed):
            # 掴んで動かさずに離した（ダブルクリックの 1 回目も） 同じ値を確定すると、
            # 戻しても何も変わらない取り消しの段が積まれる
            return
        self._emit(AnimatedValue(static=number))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt の命名規約
        """スライダーのダブルクリックで初期値へ戻す 押したまま動かせばふつうのドラッグ

        2 回目の押下はダブルクリックとして届く ここで握りつぶすと、クリックの直後に
        押したまま動かして値を合わせる操作（利用者の言う長押しの調整）で、つまみが
        掴めずに値が初期値へ飛んだ 押下はスライダーへ通し、動かさずに離したときだけ戻す
        """
        if watched is not self._slider or not isinstance(event, QMouseEvent):
            return super().eventFilter(watched, event)
        kind = event.type()
        if kind == QEvent.Type.MouseButtonDblClick:
            if event.button() == Qt.MouseButton.LeftButton:
                self._double = (event.position(), self._number.value())
        elif kind == QEvent.Type.MouseMove and self._double is not None:
            moved = (event.position() - self._double[0]).manhattanLength()
            if moved >= QApplication.startDragDistance():
                self._double = None
        elif kind == QEvent.Type.MouseButtonRelease and self._double is not None:
            _, before = self._double
            self._double = None
            # 掴んだ扱いをここで解く 離したときの確定（:meth:`_on_release`）は飛ばす
            self._resetting = True
            try:
                self._slider.setSliderDown(False)
            finally:
                self._resetting = False
            # 押した所へ動いた表示を元へ戻してから初期値を頼む 頼んだ値がもう初期値で
            # 何も変わらなくても、確定していない押した所の値が出続けない
            self._apply(before)
            self.reset_requested.emit()
        return super().eventFilter(watched, event)

    def _to_slider(self, number: float) -> int:
        """仕様の値をスライダーの位置へ 範囲の外は端へ寄せる"""
        number = self._spec.clamp(number)
        if not self._stretched:
            return _qt_int(number * _SLIDER_SCALE)
        span = self._spec.maximum - self._spec.minimum
        if span <= 0:
            return 0
        return _qt_int(round((number - self._spec.minimum) / span * _INT_MAX))

    def _from_slider(self, raw: int) -> float:
        """スライダーの位置を仕様の値へ 必ず仕様の範囲に収める"""
        if not self._stretched:
            return self._spec.clamp(raw / _SLIDER_SCALE)
        span = self._spec.maximum - self._spec.minimum
        return self._spec.clamp(self._spec.minimum + raw / _INT_MAX * span)

    def _on_number(self, number: float) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            self._slider.setValue(self._to_slider(number))
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
