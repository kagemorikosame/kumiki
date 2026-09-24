"""設定パネルでの固定の項目の見え方

固定の項目（クリップが最初から持つ欄）は外すことも並べ替えることもできない
押しても断られるボタンを並べると、押して何も起きない理由が分からない
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import shiboken6
from PySide6.QtWidgets import QApplication, QLabel, QToolButton

from sashimono.core.commands import AddEffect, insert_media
from sashimono.core.model import Clip, MediaItem, Project, TrackKind
from sashimono.effects import registry
from sashimono.ui.inspector.panel import InspectorPanel, _Section


def _placed(media: MediaItem) -> Project:
    project = Project.create()
    for command in insert_media(project, media):
        project = command.apply(project)
    return project


def _sound(project: Project) -> Clip:
    (clip,) = [c for t in project.timeline.tracks if t.kind is TrackKind.AUDIO for c in t.clips]
    return clip


@pytest.fixture
def panel(qt_application: QApplication) -> Iterator[InspectorPanel]:
    del qt_application
    created = InspectorPanel()
    yield created
    created.close()
    shiboken6.delete(created)


def _sections(panel: InspectorPanel) -> list[_Section]:
    # 先頭はクリップ自身の欄 その後ろがエフェクトの欄
    return panel._body.findChildren(_Section)[1:]


def _buttons(section: _Section) -> dict[str, QToolButton]:
    return {button.text(): button for button in section.findChildren(QToolButton)}


def test_the_fixed_effect_shows_a_lock_instead_of_remove_and_move(
    panel: InspectorPanel, audio_media: MediaItem
) -> None:
    project = _placed(audio_media)
    clip = _sound(project)
    blur = registry.require("blur").create()
    project = AddEffect(clip.id, blur).apply(project)
    panel.set_project(project)
    panel.set_clip(clip.id)

    fixed, loose = _sections(panel)
    assert fixed.findChild(QLabel, "fixed_lock") is not None
    assert not {"✕", "▲", "▼"} & set(_buttons(fixed))
    # 無効にはできる 切り替えまで消すと、効かせたくないときの逃げ道が無い
    assert _buttons(fixed)["有効"].isEnabled()

    # ふつうのエフェクトは外せるが、固定の物をまたいで上へは動かせない
    loose_buttons = _buttons(loose)
    assert loose.findChild(QLabel, "fixed_lock") is None
    assert loose_buttons["✕"].isEnabled()
    assert not loose_buttons["▲"].isEnabled()
