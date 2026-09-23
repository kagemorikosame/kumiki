"""格子の項目（四隅の変形の細かい格子）が設定画面で壊れないか"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from sashimono.effects import GridSpec, registry
from sashimono.ui.inspector.widgets import GridEditor, create_editor


def test_the_grid_gets_its_own_row_instead_of_a_number_box(qt_application: QApplication) -> None:
    # 数値欄に落ちると、点の並び（tuple）を入れた所で例外になり設定画面が開かない
    del qt_application
    spec = registry.require("mesh_deform").spec("grid")
    assert isinstance(spec, GridSpec)
    editor = create_editor(spec)
    assert isinstance(editor, GridEditor)


def test_the_grid_row_shows_how_big_the_grid_is(qt_application: QApplication) -> None:
    # 大きさも出ないと、互換で読み込んだ格子が消えたように見える
    del qt_application
    spec = registry.require("mesh_deform").spec("grid")
    assert isinstance(spec, GridSpec)
    editor = create_editor(spec)
    assert isinstance(editor, GridEditor)
    editor.set_value((3.0, 5.0, *([0.0] * (3 * 5 * 2))))
    assert "3x5" in editor._label.text()
    editor.set_value(())
    assert "四隅" in editor._label.text()
