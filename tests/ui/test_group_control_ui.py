"""グループ制御を右クリックの〔追加〕から置き、設定パネルで直せること（利用者の要望）

窓は表示しない（オフスクリーン）
"""

from __future__ import annotations

from dataclasses import replace

import shiboken6
from PySide6.QtWidgets import QApplication

from sashimono.core.commands.fixed import FLIP_EFFECT_KIND, TRANSFORM_EFFECT_KIND
from sashimono.core.model import GROUP_LAYERS, LayerMode
from sashimono.ui.inspector.panel import InspectorPanel, _RowLabel, _Section
from sashimono.ui.timeline import TimelineView
from tests.ui.test_track_add_menu import _find, _point, _wire, view

__all__ = ["view"]


def test_the_add_menu_places_a_group_control(view: TimelineView) -> None:
    # 右クリックの〔追加〕に無いと、グループ制御を置く入口が無い
    settings = replace(view.project.settings, layer_mode=LayerMode.MIXED)
    view.set_project(replace(view.project, settings=settings))
    labels = _wire(view)
    menu = view.build_context_menu(_point(view, "V2", 200))
    _find(menu, "追加", "グループ制御").trigger()
    assert labels == ["グループ制御を追加"]
    placed = [c for t in view.project.timeline.tracks for c in t.clips if c.is_group]
    assert len(placed) == 1
    group = placed[0]
    assert group.source is not None and group.source.params[GROUP_LAYERS] == 1
    # 位置・拡大・回転を持たせる（設定パネルの描画の組で直す）
    assert {e.kind for e in group.effects if e.fixed} == {TRANSFORM_EFFECT_KIND, FLIP_EFFECT_KIND}


def test_the_panel_shows_the_reach_and_hides_blending(
    view: TimelineView, qt_application: QApplication
) -> None:
    # 合成モードとクリッピングは自分の絵を持たないグループ制御では効かない 出すと触らせてしまう
    del qt_application
    _wire(view)
    menu = view.build_context_menu(_point(view, "V2", 200))
    _find(menu, "追加", "グループ制御").trigger()
    group = next(c for t in view.project.timeline.tracks for c in t.clips if c.is_group)
    panel = InspectorPanel()
    try:
        panel.set_project(view.project)
        panel.set_selection((group.id,))
        headings = [s.heading for s in panel.findChildren(_Section)]
        assert "グループ制御" in headings
        names = {label.text() for label in panel.findChildren(_RowLabel)}
        # 名前の列は狭いので短くし、0 の意味は補足に書く
        assert "対象レイヤー数" in names
        assert {"X", "Y", "拡大率", "回転角", "不透明度"} <= names
        assert "合成モード" not in names
        assert "クリッピング" not in names
    finally:
        panel.close()
        shiboken6.delete(panel)
