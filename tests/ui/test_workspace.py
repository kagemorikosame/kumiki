"""編集画面まわりの足回り

トラックヘッダのボタン、保存していない変更の扱い、退避からの復元、
画面配置とショートカットの保存 どれも「無くても編集はできる」が、
無いと作業を失ったり毎回やり直したりするもの
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from kumiki.core.commands import AddClip, Command, RenameProject, SetTrackState
from kumiki.core.io import RecoverySession, backup_folder, find_orphans
from kumiki.core.model import Clip, Project, ProjectSettings, Track, TrackKind
from kumiki.effects.sources import TEXT
from kumiki.engine.cache import MediaAnalyzer
from kumiki.ui.main_window import MainWindow
from kumiki.ui.project_settings_dialog import ProjectSettingsDialog
from kumiki.ui.timeline import TimelineView
from kumiki.ui.timeline.painter import track_button_rects
from kumiki.ui.workspace import ShortcutStore, Workspace, find_conflicts

_PORTABLE = QKeySequence.SequenceFormat.PortableText


def _with_tracks() -> Project:
    base = Project.create(ProjectSettings(width=320, height=240))
    tracks = (Track(TrackKind.VIDEO, "V1"), Track(TrackKind.AUDIO, "A1"))
    return base.with_timeline(replace(base.timeline, tracks=tracks))


@pytest.fixture
def window(qt_application: QApplication) -> Iterator[MainWindow]:
    del qt_application
    created = MainWindow(_with_tracks(), confirm_unsaved=False)
    yield created
    created.close()


class TestTrackButtons:
    @pytest.fixture
    def view(self, qt_application: QApplication) -> Iterator[TimelineView]:
        del qt_application
        analyzer = MediaAnalyzer(sample_rate=48000, channels=2)
        created = TimelineView(_with_tracks(), analyzer)
        created.resize(800, 300)
        yield created
        analyzer.close()

    def click_button(self, view: TimelineView, track_name: str, attribute: str) -> None:
        band = next(
            b for b in view._layout.bands(view.project.timeline) if b.track.name == track_name
        )
        rect = next(r for name, _, r in track_button_rects(band) if name == attribute)
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=rect.center())

    def test_a_click_asks_for_the_toggle(self, view: TimelineView) -> None:
        received: list[list[Command]] = []
        view.commands_requested.connect(lambda commands, _label: received.append(commands))
        self.click_button(view, "A1", "solo")

        track = view.project.timeline.tracks[1]
        assert received == [[SetTrackState(track.id, solo=True)]]

    def test_the_playhead_stays_put(self, view: TimelineView) -> None:
        # ミュートを押すたびに見ていた場所が飛ぶと、聞き比べるたびに戻すことになる
        view.set_playhead(40)
        self.click_button(view, "V1", "muted")
        assert view.playhead == 40

    def test_the_rest_of_the_header_still_scrubs(self, view: TimelineView) -> None:
        # ボタンの判定が広がりすぎると、ヘッダを押しても再生ヘッドが先頭へ戻らなくなる
        # ヘッダの x はタイムラインの左端より左なので、フレーム 0 へ行くのが正しい
        view.set_playhead(40)
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=QPoint(10, 280))
        assert view.playhead == 0

    def test_the_keyboard_can_toggle_the_selected_track(self, view: TimelineView) -> None:
        # ボタンは描いた矩形なのでフォーカスが来ない マウスを使えない人が
        # ミュートできなくならないよう、選んだクリップのトラックをキーで切り替える
        track = view.project.timeline.tracks[0]
        clip = Clip(timeline_start=0, duration=30, source=TEXT.create())
        view.set_project(AddClip(track.id, clip).apply(view.project))
        view.select(clip.id)
        received: list[list[Command]] = []
        view.commands_requested.connect(lambda commands, _label: received.append(commands))

        assert view.toggle_selected_track("muted")
        assert received == [[SetTrackState(track.id, muted=True)]]

    def test_the_keyboard_toggle_needs_a_selection(self, view: TimelineView) -> None:
        # どのトラックか分からないまま切り替えると、見えていないトラックが消音される
        assert not view.toggle_selected_track("solo")

    def test_buttons_fit_in_the_smallest_track(self, view: TimelineView) -> None:
        # 名前の下の段に置くと、最小の高さでボタンがはみ出して押せない
        band = view._layout.bands(view.project.timeline)[0]
        small = replace(band, height=28)
        assert all(rect.bottom() < small.bottom for _, _, rect in track_button_rects(small))


class TestUnsavedChanges:
    def test_a_fresh_window_is_clean(self, window: MainWindow) -> None:
        assert not window.is_modified
        assert "*" not in window.windowTitle()

    def test_an_edit_marks_it_and_undo_clears_it(self, window: MainWindow) -> None:
        window.execute(RenameProject("別名"))
        assert window.is_modified
        assert "*" in window.windowTitle()
        window.undo()
        assert not window.is_modified

    def test_autosave_writes_only_when_changed(self, window: MainWindow) -> None:
        window.autosave()
        assert not window._recovery.path.exists()
        window.execute(RenameProject("別名"))
        window.autosave()
        assert window._recovery.path.exists()

    def test_saving_clears_the_autosave_and_backs_up(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        window._path = tmp_path / "本編.kmk"
        window.execute(RenameProject("一回目"))
        window.autosave()
        assert window.save_project()
        assert not window._recovery.path.exists()
        assert not window.is_modified

        window.execute(RenameProject("二回目"))
        assert window.save_project()
        assert len(list(backup_folder(window._path).iterdir())) == 1

    def test_a_live_window_is_never_offered_for_recovery(self, window: MainWindow) -> None:
        window.execute(RenameProject("作業中"))
        window.autosave()
        assert find_orphans() == []


class TestRestoring:
    def test_a_crashed_session_comes_back(self, window: MainWindow, tmp_path: Path) -> None:
        source = tmp_path / "本編.kmk"
        crashed = RecoverySession()
        crashed.save(_with_tracks().renamed("落ちた作業"), source)
        lock = crashed._lock
        assert lock is not None
        lock.close()
        crashed._lock = None

        (crashed.path.parent / f"{crashed.session}.lock").write_text("終了済み", "utf-8")

        (entry,) = find_orphans()
        assert window.restore_recovery(entry)

        assert window.document.project.name == "落ちた作業"
        assert window._path == source
        # 開いただけで保存はしていない 閉じるときに尋ねなければ、復元した意味が無い
        assert window.is_modified
        # 自分の退避へ書き写したので、元の退避は消えている
        assert find_orphans() == []
        assert window._recovery.path.exists()


class TestLayout:
    def test_the_layout_is_saved_on_close(self, qt_application: QApplication) -> None:
        del qt_application
        first = MainWindow(confirm_unsaved=False)
        first.close()
        assert Workspace().path.is_file()

        second = MainWindow(confirm_unsaved=False)
        try:
            assert Workspace().restore(second)
        finally:
            second.close()

    def test_every_panel_has_a_name(self, window: MainWindow) -> None:
        # 名前の無いパネルは、Qt が黙って保存から外す
        from PySide6.QtWidgets import QDockWidget

        assert all(dock.objectName() for dock in window.findChildren(QDockWidget))

    def test_reset_hides_the_graph_editor_again(self, window: MainWindow) -> None:
        window._graph_dock.setVisible(True)
        window.reset_layout()
        assert window._graph_dock.isHidden()


class TestShortcuts:
    def test_an_override_is_applied_at_start(self, qt_application: QApplication) -> None:
        del qt_application
        ShortcutStore().save({"ファイル/保存": "Ctrl+Alt+S", "消えた/項目": "F9"})
        created = MainWindow(confirm_unsaved=False)
        try:
            action, default = created._actions["ファイル/保存"]
            assert action.shortcut().toString(_PORTABLE) == "Ctrl+Alt+S"
            assert default == "Ctrl+S"
        finally:
            created.close()

    def test_a_broken_file_means_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "shortcuts.json"
        path.write_text("{壊れている", "utf-8")
        assert ShortcutStore(path).load() == {}

    def test_conflicts_are_found(self) -> None:
        bindings = {"a": "Ctrl+S", "b": "Ctrl+S", "c": "", "d": ""}
        # 空は「割り当てなし」 いくつあっても重なりではない
        assert find_conflicts(bindings) == {"Ctrl+S": ["a", "b"]}

    def test_names_do_not_follow_the_changing_text(self, window: MainWindow) -> None:
        # 「元に戻す: 分割」のように表示が変わっても、名前は作ったときのまま
        window.execute(RenameProject("別名"))
        assert "編集/元に戻す" in window._actions


class TestSettingsDialog:
    def test_the_preset_follows_the_numbers(self, qt_application: QApplication) -> None:
        del qt_application
        dialog = ProjectSettingsDialog(ProjectSettings())
        dialog._swap()
        assert dialog.resolution() == (1080, 1920)
        assert "縦" in dialog._preset.currentText()

    def test_odd_numbers_cannot_be_confirmed(self, qt_application: QApplication) -> None:
        del qt_application
        dialog = ProjectSettingsDialog(ProjectSettings())
        dialog._width.setValue(1919)
        ok = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert ok is not None
        assert not ok.isEnabled()
