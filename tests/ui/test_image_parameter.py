"""エフェクトの画像の項目（画像合成の画像、縁取りの模様）を画面で選べるか"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog

from kumiki.effects import FileSpec, registry
from kumiki.ui.inspector.widgets import FileEditor, create_editor


@pytest.mark.parametrize(("kind", "name"), [("image_blend", "image_file"), ("border", "pattern")])
def test_image_items_get_a_file_picker_for_images(
    qt_application: QApplication, monkeypatch: pytest.MonkeyPatch, kind: str, name: str
) -> None:
    # 入力欄だけだと、パスを手で打つしかない 選ぶ窓は画像に絞る
    del qt_application
    spec = registry.require(kind).spec(name)
    assert isinstance(spec, FileSpec) and spec.texture
    editor = create_editor(spec)
    assert isinstance(editor, FileEditor)

    offered: list[str] = []

    def choose(*args: object) -> tuple[str, str]:
        offered.append(str(args[3]))
        return "C:/模様.png", ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(choose))
    chosen: list[object] = []
    editor.value_changed.connect(chosen.append)
    editor._choose()
    assert "*.png" in offered[0]
    assert chosen == ["C:/模様.png"]
