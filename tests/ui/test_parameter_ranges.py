"""設定パネルの数値欄へ、Qt の int に収まらない範囲を渡さないか（#148）

収まらない値を渡すと shiboken は警告を出すだけで先へ進み、Qt 側の範囲が化ける
警告のままだと試験は通ってしまうので、ここでは警告を失敗として扱う
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QSlider, QSpinBox

from sashimono.core.model import AnimatedValue
from sashimono.effects import ParameterSpec, TrackSpec, ValueSpec, registry
from sashimono.effects.sources import source_registry
from sashimono.ui.inspector.widgets import create_editor

INT_MAX = 2**31 - 1


def _every_builtin_spec() -> list[ParameterSpec]:
    specs: list[ParameterSpec] = []
    for effect in registry.all():
        specs.extend(effect.parameters)
    for source in source_registry.all():
        specs.extend(source.parameters)
    return specs


@pytest.mark.filterwarnings("error")
def test_no_builtin_parameter_overflows_the_editor(qt_application: QApplication) -> None:
    # 音声波形の「読む終わり」は上限が 10**10 で、図形を設定パネルに出すたびに警告が出ていた
    del qt_application
    for spec in _every_builtin_spec():
        create_editor(spec).deleteLater()


@pytest.mark.filterwarnings("error")
def test_a_huge_value_range_is_clamped_to_what_qt_can_hold(qt_application: QApplication) -> None:
    # AviUtl のスクリプトはどんな範囲でも書ける 丸めないと、入れられる値が化けた範囲になる
    del qt_application
    editor = create_editor(ValueSpec("far", "遠い", 0, minimum=-(10**12), maximum=10**12))
    box = editor.findChild(QSpinBox)
    assert box is not None
    assert (box.minimum(), box.maximum()) == (-(2**31), INT_MAX)
    editor.set_value(10**11)
    assert box.value() == INT_MAX


@pytest.mark.filterwarnings("error")
def test_a_fine_lower_end_is_not_rounded_out_of_the_range(qt_application: QApplication) -> None:
    # 数値欄の桁が 2 のままだと下限 0.004 が 0.00 に丸められ、スライダーを左端へ
    # 戻したときに仕様の範囲の外の 0.0 がプレビューと保存へ流れる
    del qt_application
    spec = TrackSpec("fine", "細かい", 0.004, 10.0**7, 0.004, step=0.001)
    editor = create_editor(spec)
    slider = editor.findChild(QSlider)
    box = editor.findChild(QDoubleSpinBox)
    assert slider is not None
    assert box is not None
    assert box.minimum() == 0.004
    previewed: list[float] = []
    changed: list[float] = []
    editor.value_previewed.connect(lambda value: previewed.append(value.static))
    editor.value_changed.connect(lambda value: changed.append(value.static))
    slider.setValue(slider.maximum() // 2)
    slider.setValue(slider.minimum())
    slider.sliderReleased.emit()
    assert previewed[-1] == changed[-1] == box.value() == 0.004
    assert all(spec.minimum <= value <= spec.maximum for value in previewed + changed)


def test_a_value_spec_keeps_its_range_inside_what_qt_can_hold() -> None:
    # 範囲が int を超えたまま残ると、仕様では入れられる値が数値欄では入れられず、
    # 数値欄に出る値も仕様の値と食い違う
    spec = ValueSpec("far", "遠い", -(10**12), minimum=-(10**12), maximum=10**12)
    assert (spec.minimum, spec.maximum) == (-(2**31), INT_MAX)
    assert spec.default == -(2**31)
    assert spec.coerce(10**11) == INT_MAX
    beyond = ValueSpec("beyond", "上だけ", 10**11, minimum=10**10, maximum=10**11)
    assert beyond.minimum <= beyond.default <= beyond.maximum == INT_MAX


@pytest.mark.filterwarnings("error")
def test_a_fine_slider_step_sends_the_number_the_box_shows(qt_application: QApplication) -> None:
    # 広い範囲ではスライダー 1 目盛りが数値欄の桁より細かい 元の値を流すと、画面は
    # 0.00 なのにプレビューと保存には 0.004657 が入る
    del qt_application
    editor = create_editor(TrackSpec("far", "遠い", 0.0, 10.0**7, 0.0))
    slider = editor.findChild(QSlider)
    box = editor.findChild(QDoubleSpinBox)
    assert slider is not None
    assert box is not None
    previewed: list[float] = []
    changed: list[float] = []
    editor.value_previewed.connect(lambda value: previewed.append(value.static))
    editor.value_changed.connect(lambda value: changed.append(value.static))
    for position in (1, 12345, slider.maximum() // 3):
        slider.setValue(position)
        slider.sliderReleased.emit()
        assert previewed[-1] == box.value()
        assert changed[-1] == box.value()


@pytest.mark.filterwarnings("error")
def test_a_huge_track_range_keeps_the_slider_usable(qt_application: QApplication) -> None:
    # スライダーは値を 1000 倍して持つので、数値欄より先にあふれる
    del qt_application
    editor = create_editor(TrackSpec("far", "遠い", 0.0, 10.0**7, 0.0))
    slider = editor.findChild(QSlider)
    assert slider is not None
    assert slider.maximum() == INT_MAX
    editor.set_value(10**7)
    assert slider.value() == INT_MAX


@pytest.mark.filterwarnings("error")
@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(0.0, 10.0**7), (-(10.0**7), 10.0**7), (-(10.0**9), -(10.0**6))],
)
def test_the_slider_ends_of_a_huge_range_are_the_spec_ends(
    qt_application: QApplication, minimum: float, maximum: float
) -> None:
    # 端だけを int の上限で切ると、端へ動かしたときに仕様の端ではなく 2147483.647 が
    # 数値欄と保存へ流れる 下限は仕様より下へはみ出した値にもなりうる
    del qt_application
    editor = create_editor(TrackSpec("far", "遠い", minimum, maximum, minimum))
    slider = editor.findChild(QSlider)
    assert slider is not None
    previewed: list[float] = []
    changed: list[float] = []
    editor.value_previewed.connect(lambda value: previewed.append(value.static))
    editor.value_changed.connect(lambda value: changed.append(value.static))
    for position in (
        slider.maximum(),
        slider.minimum(),
        (slider.minimum() + slider.maximum()) // 2,
    ):
        slider.setValue(position)
        slider.sliderReleased.emit()
    assert previewed[0] == maximum
    assert previewed[1] == minimum
    assert changed[:2] == [maximum, minimum]
    assert all(minimum <= value <= maximum for value in previewed + changed)
    # 数値欄から入れた値もスライダーの位置へ正しく戻る
    editor.set_value(AnimatedValue(static=maximum))
    assert slider.value() == slider.maximum()
    editor.set_value(AnimatedValue(static=minimum))
    assert slider.value() == slider.minimum()
