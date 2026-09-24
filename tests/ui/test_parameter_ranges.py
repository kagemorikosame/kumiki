"""設定パネルの数値欄へ、Qt の int に収まらない範囲を渡さないか（#148）

収まらない値を渡すと shiboken は警告を出すだけで先へ進み、Qt 側の範囲が化ける
警告のままだと試験は通ってしまうので、ここでは警告を失敗として扱う
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QSlider, QSpinBox

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
def test_a_huge_track_range_keeps_the_slider_usable(qt_application: QApplication) -> None:
    # スライダーは値を 1000 倍して持つので、数値欄より先にあふれる
    del qt_application
    editor = create_editor(TrackSpec("far", "遠い", 0.0, 10.0**7, 0.0))
    slider = editor.findChild(QSlider)
    assert slider is not None
    assert slider.maximum() == INT_MAX
    editor.set_value(10**7)
    assert slider.value() == INT_MAX
