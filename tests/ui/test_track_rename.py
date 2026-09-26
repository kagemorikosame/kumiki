"""タイムラインのヘッダでトラック（レイヤー）の名前を変える（利用者の要望）

入口はヘッダの名前のダブルクリックと右クリックの「名前を変更…」 どちらもヘッダの上に
入力欄を出し、Enter か外を押すと決まり、Esc で取りやめる 変更は取り消せる
窓は表示しない（オフスクリーン）
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu

from sashimono.core.commands import Command, Document
from sashimono.core.model import LayerMode, Project, ProjectSettings, Track, TrackKind
from sashimono.engine.cache import MediaAnalyzer
from sashimono.ui.timeline import TimelineView
from sashimono.ui.timeline.painter import track_button_rects, track_name_rect
from sashimono.ui.timeline.track_name import TrackNameEditor


def _project() -> Project:
    base = Project.create(ProjectSettings(layer_mode=LayerMode.MIXED))
    tracks = (Track(TrackKind.MIXED, "レイヤー 1"), Track(TrackKind.MIXED, "レイヤー 2"))
    return base.with_timeline(replace(base.timeline, tracks=tracks))


class _Harness:
    """窓の代わり 出たコマンドを文書に積み、取り消せるようにする"""

    def __init__(self, view: TimelineView) -> None:
        self.view = view
        self.document = Document(view.project)
        self.labels: list[str] = []
        view.commands_requested.connect(self._run)

    def _run(self, commands: list[Command], label: str) -> None:
        with self.document.checkpoint(label):
            for command in commands:
                self.document.execute(command)
        self.labels.append(label)
        self.view.set_project(self.document.project)

    def undo(self) -> None:
        self.document.undo()
        self.view.set_project(self.document.project)


@pytest.fixture
def view(qt_application: QApplication) -> Iterator[TimelineView]:
    del qt_application
    analyzer = MediaAnalyzer(sample_rate=48000, channels=2)
    created = TimelineView(_project(), analyzer)
    created.resize(900, 400)
    created.show()
    QApplication.processEvents()
    yield created
    created.close()
    analyzer.close()


def _name_point(view: TimelineView, name: str) -> QPoint:
    band = next(b for b in view.view_layout.bands(view.project.timeline) if b.track.name == name)
    return track_name_rect(band).center()


def _editor(view: TimelineView) -> TrackNameEditor:
    editors = [e for e in view.findChildren(TrackNameEditor) if not e.done]
    assert len(editors) == 1, "名前の入力欄が出ていない"
    return editors[0]


def _names(view: TimelineView) -> list[str]:
    return [t.name for t in view.project.timeline.tracks]


class TestDoubleClick:
    def test_typing_a_name_and_enter_renames_the_track(self, view: TimelineView) -> None:
        # 名前を付けられないと、レイヤーが増えた作品でどれが何かを番号で覚えることになる
        harness = _Harness(view)
        QTest.mouseDClick(view, Qt.MouseButton.LeftButton, pos=_name_point(view, "レイヤー 2"))
        editor = _editor(view)
        assert editor.text() == "レイヤー 2"
        # 打つのは insert で 日本語を QTest.keyClicks で打つと、Windows ではキーの番号に
        # 直せずにプロセスごと落ちる（試験の道具の都合）
        editor.selectAll()
        editor.insert("ナレーション")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert _names(view) == ["レイヤー 1", "ナレーション"]
        assert harness.labels == ["トラックの名前を変更"], "取り消し 1 回で戻る 1 つの操作にする"
        harness.undo()
        assert _names(view) == ["レイヤー 1", "レイヤー 2"]

    def test_escape_leaves_the_name(self, view: TimelineView) -> None:
        # 取りやめられないと、開いてしまった入力欄を閉じるだけで名前が変わる
        harness = _Harness(view)
        QTest.mouseDClick(view, Qt.MouseButton.LeftButton, pos=_name_point(view, "レイヤー 1"))
        editor = _editor(view)
        editor.insert("消えるはず")
        QTest.keyClick(editor, Qt.Key.Key_Escape)
        assert editor.done
        assert _names(view) == ["レイヤー 1", "レイヤー 2"]
        assert harness.labels == []

    def test_an_empty_name_goes_back_to_the_default(self, view: TimelineView) -> None:
        # 空の名前のまま残ると、見出しが種類の名前だけになり、どのレイヤーか分からない
        harness = _Harness(view)
        assert view.rename_track(view.project.timeline.tracks[1].id, "BGM")
        QTest.mouseDClick(view, Qt.MouseButton.LeftButton, pos=_name_point(view, "BGM"))
        editor = _editor(view)
        editor.clear()
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert _names(view) == ["レイヤー 1", "レイヤー 2"]
        assert len(harness.labels) == 2

    def test_the_mute_button_is_not_a_name(self, view: TimelineView) -> None:
        # ボタンを続けて押しただけで入力欄が出ると、ミュートの切り替えが名前の変更に化ける
        band = view.view_layout.bands(view.project.timeline)[0]
        button = track_button_rects(band)[0][2]
        QTest.mouseDClick(view, Qt.MouseButton.LeftButton, pos=button.center())
        assert [e for e in view.findChildren(TrackNameEditor) if not e.done] == []


class TestContextMenu:
    def test_the_menu_opens_the_name_editor(self, view: TimelineView) -> None:
        # 右クリックに無いと、ダブルクリックで名前を変えられることに気付けない
        harness = _Harness(view)
        band = view.view_layout.bands(view.project.timeline)[0]
        menu = view.build_context_menu(QPoint(400, band.top + band.height // 2))
        action = next(
            (a for a in menu.actions() if a.text() == f"{band.track.name} の名前を変更…"), None
        )
        assert isinstance(action, QAction)
        action.trigger()
        editor = _editor(view)
        editor.setText("テロップ")
        editor.commit()
        assert _names(view)[0] == "テロップ"
        assert harness.labels == ["トラックの名前を変更"]
        assert isinstance(menu, QMenu)
