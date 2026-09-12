"""タイムラインで何本かのクリップを選んで、まとめて扱う

選び方（Ctrl・Shift・囲む）と、選んだものへの操作（動かす・消す・コピー）が
正しいコマンド 1 つになって出てくるかを見る 中身の正しさは core 側
（test_multi_clip_commands.py）で見る
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from kumiki.core.commands import (
    Command,
    MoveClips,
    RemoveClips,
    SetTrackHeights,
    insert_media,
)
from kumiki.core.model import Clip, ClipId, MediaItem, Project, Track, TrackKind
from kumiki.effects.sources import TEXT
from kumiki.engine.cache import MediaAnalyzer
from kumiki.ui.main_window import MainWindow
from kumiki.ui.timeline import TimelineView

_CTRL = Qt.KeyboardModifier.ControlModifier
_SHIFT = Qt.KeyboardModifier.ShiftModifier
_LEFT = Qt.MouseButton.LeftButton


def _project() -> Project:
    """V1 に 2 本（0〜30、40〜70）、V2 に 1 本（0〜30）"""
    base = Project.create()

    def text(start: int) -> Clip:
        return Clip(timeline_start=start, duration=30, source=TEXT.create())

    tracks = (
        Track(TrackKind.VIDEO, "V1", (text(0), text(40))),
        Track(TrackKind.VIDEO, "V2", (text(0),)),
        Track(TrackKind.AUDIO, "A1"),
    )
    return base.with_timeline(replace(base.timeline, tracks=tracks))


@pytest.fixture
def view(qt_application: QApplication) -> Iterator[TimelineView]:
    del qt_application
    analyzer = MediaAnalyzer(sample_rate=48000, channels=2)
    created = TimelineView(_project(), analyzer)
    created.resize(900, 400)
    yield created
    analyzer.close()


def _ids(view: TimelineView) -> tuple[ClipId, ClipId, ClipId]:
    v1, v2 = view.project.timeline.tracks[0], view.project.timeline.tracks[1]
    return v1.clips[0].id, v1.clips[1].id, v2.clips[0].id


def _point(view: TimelineView, track: int, frame: int) -> QPoint:
    """``tracks[track]`` の上の点 画面の並びは映像が下から積まれるので、ID で引く"""
    wanted = view.project.timeline.tracks[track].id
    band = next(b for b in view._layout.bands(view.project.timeline) if b.track.id == wanted)
    return QPoint(int(view._layout.frame_to_x(frame)), band.top + band.height // 2)


def _received(view: TimelineView) -> list[list[Command]]:
    received: list[list[Command]] = []
    view.commands_requested.connect(lambda commands, _label: received.append(commands))
    return received


class TestChoosing:
    def test_ctrl_click_adds_and_removes(self, view: TimelineView) -> None:
        # 壊れると、2 本目を押したときに 1 本目の選択が消えてまとめて扱えない
        a, b, _ = _ids(view)
        QTest.mouseClick(view, _LEFT, pos=_point(view, 0, 10))
        QTest.mouseClick(view, _LEFT, _CTRL, _point(view, 0, 50))
        assert list(view.selected_clips) == [a, b]
        QTest.mouseClick(view, _LEFT, _CTRL, _point(view, 0, 10))
        assert list(view.selected_clips) == [b]

    def test_shift_click_takes_everything_in_between(self, view: TimelineView) -> None:
        # 起点と今のクリップを両隅にした範囲 間にあるクリップが漏れると、1 本ずつ
        # Ctrl で足し直すことになる
        a, b, c = _ids(view)
        QTest.mouseClick(view, _LEFT, pos=_point(view, 1, 10))
        QTest.mouseClick(view, _LEFT, _SHIFT, _point(view, 0, 50))
        assert set(view.selected_clips) == {a, b, c}
        assert view.selected_clip == b

    def test_shift_range_follows_the_order_on_screen(self, view: TimelineView) -> None:
        # 映像は下から積むので、画面では V2・V1・A1 の順 モデルの並びで範囲を取ると、
        # 画面で間に見えている V1 が抜ける
        project = view.project
        audio = Clip(timeline_start=0, duration=30, source=TEXT.create())
        a1 = project.timeline.tracks[2]
        view.set_project(
            project.with_timeline(project.timeline.replace_track(a1.with_clips((audio,))))
        )
        a, _, c = _ids(view)
        view.select(c)
        view._select_range(c, audio.id)
        assert a in view.selected_clips

    def test_dragging_on_empty_space_draws_a_box(self, view: TimelineView) -> None:
        # 壊れると、たくさんのクリップを 1 本ずつクリックして選ぶことになる
        start, end = _point(view, 0, 80), _point(view, 1, 5)
        QTest.mousePress(view, _LEFT, pos=start)
        QTest.mouseMove(view, end)
        QTest.mouseRelease(view, _LEFT, pos=end)
        assert set(view.selected_clips) == set(_ids(view))

    def test_a_click_on_empty_space_clears_and_moves_the_playhead(self, view: TimelineView) -> None:
        # 囲む操作を足しても、空いた所のクリックの意味（選択を解いて移動）は変えない
        view.select_all()
        QTest.mouseClick(view, _LEFT, pos=_point(view, 0, 80))
        assert view.selected_clips == ()
        assert view.playhead == 80

    def test_escape_clears_the_selection(self, view: TimelineView) -> None:
        # 壊れると選択を解く手段がクリックしか無く、次の操作が前の選択へ掛かる
        view.select_all()
        QTest.keyClick(view, Qt.Key.Key_Escape)
        assert view.selected_clips == ()

    def test_removed_clips_leave_the_selection(self, view: TimelineView) -> None:
        # 消えたクリップの ID が残ると、次の操作が「見つからない」で失敗する
        a, b, _ = _ids(view)
        view.set_selection((a, b))
        view.set_project(RemoveClips((b,)).apply(view.project))
        assert view.selected_clips == (a,)


class TestActingOnMany:
    def test_dragging_one_of_them_moves_them_all(self, view: TimelineView) -> None:
        # 壊れると、掴んだ 1 本だけが動いて並びが崩れる
        a, _, c = _ids(view)
        view.set_selection((a, c))
        received = _received(view)
        QTest.mousePress(view, _LEFT, pos=_point(view, 0, 10))
        QTest.mouseMove(view, _point(view, 0, 100))
        QTest.mouseRelease(view, _LEFT, pos=_point(view, 0, 100))
        ((command,),) = received
        assert isinstance(command, MoveClips)
        assert set(command.clip_ids) == {a, c}
        assert command.delta > 0

    def test_a_plain_click_on_a_selected_clip_keeps_the_rest(self, view: TimelineView) -> None:
        # 掴んだ瞬間に選び直すと、まとめて動かすことができない
        a, b, _ = _ids(view)
        view.set_selection((a, b))
        QTest.mousePress(view, _LEFT, pos=_point(view, 0, 10))
        QTest.mouseRelease(view, _LEFT, pos=_point(view, 0, 10))
        assert set(view.selected_clips) == {a, b}
        assert view.selected_clip == a

    def test_ctrl_click_can_grab_what_it_just_added(self, view: TimelineView) -> None:
        # Ctrl を押したまま足したクリップを掴めないと、足すたびに手を離して掴み直す
        a, _, c = _ids(view)
        view.select(a)
        received = _received(view)
        QTest.mousePress(view, _LEFT, _CTRL, _point(view, 1, 10))
        QTest.mouseMove(view, _point(view, 1, 100))
        QTest.mouseRelease(view, _LEFT, _CTRL, _point(view, 1, 100))
        ((command,),) = received
        assert isinstance(command, MoveClips)
        assert set(command.clip_ids) == {a, c}

    def test_a_group_stops_at_the_start_together(self, view: TimelineView) -> None:
        # 掴んだ 1 本だけで 0 に止めると、前にいる別のクリップが先頭より前へ出て、
        # 離したときに断られる（枠では動けたように見えたのに）
        _, b, c = _ids(view)
        view.set_selection((c, b))
        received = _received(view)
        QTest.mousePress(view, _LEFT, pos=_point(view, 0, 50))
        QTest.mouseMove(view, _point(view, 0, 0))
        QTest.mouseRelease(view, _LEFT, pos=_point(view, 0, 0))
        # c は 0 から始まっているので、どこへ引いても前へは動けない
        assert received == []

    def test_locked_clips_stay_out_of_a_group_move(self, view: TimelineView) -> None:
        # Ctrl+A はロックしたトラックも選ぶ そのまま渡すと、ほかも一緒に動かせなくなる
        project = view.project
        v2 = project.timeline.tracks[1]
        view.set_project(
            project.with_timeline(project.timeline.replace_track(replace(v2, locked=True)))
        )
        a, b, c = _ids(view)
        view.select_all()
        received = _received(view)
        QTest.mousePress(view, _LEFT, pos=_point(view, 0, 10))
        QTest.mouseMove(view, _point(view, 0, 100))
        QTest.mouseRelease(view, _LEFT, pos=_point(view, 0, 100))
        ((command,),) = received
        assert isinstance(command, MoveClips)
        assert set(command.clip_ids) == {a, b}
        assert c not in command.clip_ids

    def test_a_clip_with_a_locked_partner_stays_out(
        self, view: TimelineView, video_media: MediaItem
    ) -> None:
        # 相手がロックした組を残すと、枠では動いて見えたのに、離すと全体が断られる
        base = view.project.with_media((video_media,))
        for command in insert_media(base, video_media, at_frame=100):
            base = command.apply(base)
        audio = base.timeline.tracks[2]
        base = base.with_timeline(base.timeline.replace_track(replace(audio, locked=True)))
        view.set_project(base)
        a, b, _ = _ids(view)
        linked = next(
            c.id
            for t in base.timeline.tracks
            if t.kind is TrackKind.VIDEO
            for c in t.clips
            if c.link_group is not None
        )
        view.set_selection((linked, b, a))
        assert set(view._movable_selection()) == {a, b}

    def test_removing_with_ctrl_leaves_the_anchor_on_what_remains(self, view: TimelineView) -> None:
        # 外したクリップが起点に残ると、次の Shift+クリックが選んでいないクリップから
        # 範囲を取る
        a, b, c = _ids(view)
        view.set_selection((a, c))
        QTest.mouseClick(view, _LEFT, _CTRL, _point(view, 1, 10))
        QTest.mouseClick(view, _LEFT, _SHIFT, _point(view, 0, 50))
        assert set(view.selected_clips) == {a, b}

    def test_the_anchor_follows_any_selection(self, view: TimelineView) -> None:
        # AI が選んだあとの Shift+クリックが古い起点から範囲を取ると、見ていない
        # クリップまで選ばれる
        a, b, c = _ids(view)
        view.select(c)
        view.set_selection((a,))
        QTest.mouseClick(view, _LEFT, _SHIFT, _point(view, 0, 50))
        assert set(view.selected_clips) == {a, b}

    def test_delete_is_one_command(self, view: TimelineView) -> None:
        # 1 本ずつのコマンドになると、取り消しを本数ぶん押すことになる
        view.select_all()
        received = _received(view)
        view.delete_selected(ripple=True)
        ((command,),) = received
        assert isinstance(command, RemoveClips)
        assert command.ripple
        assert len(command.clip_ids) == 3

    def test_pasting_selects_everything_pasted(self, window: MainWindow) -> None:
        # 貼ったうち 1 本しか選ばれないと、続けてまとめて動かせない
        timeline = window._timeline
        a, b, _ = _ids(timeline)
        timeline.set_selection((a, b))
        assert timeline.copy_selected()
        window.seek(200)
        assert timeline.paste_at_playhead()
        assert len(timeline.selected_clips) == 2
        assert {a, b}.isdisjoint(timeline.selected_clips)

    def test_the_menu_speaks_for_all_of_them(self, view: TimelineView) -> None:
        # 右クリックで選択が 1 本に戻ると、まとめて消すつもりが 1 本だけ消える
        view.select_all()
        menu = view.build_context_menu(_point(view, 0, 10))
        assert "削除（3 本）" in [action.text() for action in menu.actions()]
        assert len(view.selected_clips) == 3


class TestHeightMerging:
    def test_quick_repeats_ask_to_continue(self, view: TimelineView) -> None:
        # 壊れると、ホイールを回した回数だけ取り消しの段が積まれる
        continued: list[list[Command]] = []
        view.commands_continued.connect(lambda commands, _label: continued.append(commands))
        received = _received(view)
        view.adjust_track_heights(12)
        view.adjust_track_heights(12)
        assert len(received) == 1
        assert len(continued) == 1
        assert isinstance(continued[0][0], SetTrackHeights)

    def test_the_window_undoes_them_at_once(self, window: MainWindow) -> None:
        # 壊れると、高さを変えた回数だけ取り消しを押すことになる
        timeline = window._timeline
        for _ in range(3):
            timeline.adjust_track_heights(12)
        assert window.document.project.timeline.tracks[0].height == 96
        window.undo()
        assert window.document.project.timeline.tracks[0].height == 60


@pytest.fixture
def window(qt_application: QApplication) -> Iterator[MainWindow]:
    del qt_application
    created = MainWindow(_project(), confirm_unsaved=False)
    yield created
    created.close()
