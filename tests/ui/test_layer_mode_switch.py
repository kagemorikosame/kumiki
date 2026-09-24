"""置き方の方式を途中で切り替えるときに尋ねるダイアログ（Issue #27）

利用者の決定で、切り替えるたびに「今のトラックも変換する」「これから置く物だけ変える」を
尋ねる 選んだ答えと違う物が実行されると、作品の作りが黙って変わるか、変えたつもりの
トラックが前の方式のまま残る 変換の中身は core 側の試験で見る
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from sashimono.core.commands import SetTrackState, insert_media
from sashimono.core.model import LayerMode, MediaItem, Project, Scene, TrackKind
from sashimono.ui.layer_mode_dialog import LayerModeDialog
from sashimono.ui.main_window import MainWindow


def _placed(media: MediaItem) -> Project:
    project = Project.create()
    for command in insert_media(project, media):
        project = command.apply(project)
    return project


@pytest.fixture
def window(qt_application: QApplication, video_media: MediaItem) -> Iterator[MainWindow]:
    del qt_application
    created = MainWindow(_placed(video_media), confirm_unsaved=False)
    yield created
    created.close()


def _answer(
    monkeypatch: pytest.MonkeyPatch, pick: Callable[[LayerModeDialog], None]
) -> list[LayerModeDialog]:
    """ダイアログを開いた所で ``pick`` を押したことにする 開いたダイアログを返す"""
    opened: list[LayerModeDialog] = []

    def fake_exec(self: LayerModeDialog) -> int:
        opened.append(self)
        pick(self)
        return int(self.result())

    monkeypatch.setattr(LayerModeDialog, "exec", fake_exec)
    return opened


def _kinds(window: MainWindow) -> set[TrackKind]:
    return {t.kind for t in window.document.project.timeline.tracks}


def test_converting_changes_the_mode_and_the_tracks_in_one_undo(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 方式だけ変わってトラックが残ると、変換を選んだのに映像と音声の 2 本が並んだまま
    _answer(monkeypatch, lambda dialog: dialog.convert_button.click())
    before = window.document.project
    assert window.switch_layer_mode()
    assert window.document.project.settings.layer_mode == LayerMode.MIXED
    assert _kinds(window) == {TrackKind.MIXED}
    # 2 段に分かれると、1 回戻しただけでは方式だけ戻った作品になる
    window.undo()
    assert window.document.project is before


def test_keeping_the_tracks_changes_only_the_mode(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 「これから置く物だけ」なのにトラックまで変わると、答えを聞いた意味が無い
    _answer(monkeypatch, lambda dialog: dialog.keep_button.click())
    before = window.document.project.timeline
    assert window.switch_layer_mode()
    assert window.document.project.settings.layer_mode == LayerMode.MIXED
    assert window.document.project.timeline == before


def test_cancel_changes_nothing(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    # やめたのに方式が変わると、次に置く物の置き方が黙って変わり、取り消しの段も 1 つ増える
    _answer(monkeypatch, lambda dialog: dialog.reject())
    before = window.document.project
    assert not window.switch_layer_mode()
    assert window.document.project is before


def test_it_asks_again_on_the_way_back(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    # 毎回尋ねる決まり 2 回目から黙って前の答えを使うと、戻すときに選べない
    opened = _answer(monkeypatch, lambda dialog: dialog.convert_button.click())
    window.switch_layer_mode()
    window.switch_layer_mode()
    assert len(opened) == 2
    assert window.document.project.settings.layer_mode == LayerMode.SEPARATED
    assert _kinds(window) == {TrackKind.VIDEO, TrackKind.AUDIO}


def test_what_cannot_carry_over_is_listed_before_choosing(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 変換してから気付くと、どこが変わったのかを探すことになる
    video = next(window.document.project.timeline.video_tracks())
    window.document.execute(SetTrackState(video.id, solo=True))
    opened = _answer(monkeypatch, lambda dialog: dialog.reject())
    window.switch_layer_mode()
    (dialog,) = opened
    listed = [dialog.notices.item(i).text() for i in range(dialog.notices.count())]
    assert listed, "ソロの効き方が変わるのに一覧が空"


def test_nothing_to_convert_disables_the_convert_button(
    qt_application: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 変換する物が無いのに押せると、押しても何も起きずに壊れたように見える
    del qt_application
    created = MainWindow(Project.create(), confirm_unsaved=False)
    try:
        opened = _answer(monkeypatch, lambda dialog: dialog.keep_button.click())
        assert created.switch_layer_mode()
        assert not opened[0].convert_button.isEnabled()
    finally:
        created.close()


def test_scenes_are_converted_even_while_one_is_open(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, video_media: MediaItem
) -> None:
    # 開いているシーンの中へ包んで実行すると、そのシーンだけが変わりメインが残る
    inner = _placed(video_media).timeline
    project = window.document.project
    scene = Scene(name="OP", timeline=inner)
    window.document.reset(project.with_scenes((scene,)))
    window.open_scene(scene.id)
    _answer(monkeypatch, lambda dialog: dialog.convert_button.click())
    assert window.switch_layer_mode()
    converted = window.document.project
    assert {t.kind for t in converted.timeline.tracks} == {TrackKind.MIXED}
    assert {t.kind for t in converted.scenes[0].timeline.tracks} == {TrackKind.MIXED}


def test_the_dialog_result_is_accepted_for_both_answers(qt_application: QApplication) -> None:
    # どちらの答えも「受け付けた」で閉じる 片方が取り消し扱いだと、選んだのに何も起きない
    del qt_application
    for button in ("convert_button", "keep_button"):
        dialog = LayerModeDialog(LayerMode.MIXED, ())
        getattr(dialog, button).click()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.convert is (button == "convert_button")
