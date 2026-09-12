"""何本かのクリップをまとめて扱うコマンドと、履歴のまとめ方

まとめて扱う操作は「全部できるか、何もしないか」でなければならない 途中まで
動いた状態が 1 段として残ると、どれが動いたのかを人も AI も追えなくなる
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kumiki.core.commands import (
    Document,
    MoveClips,
    RemoveClips,
    RenameProject,
    SetTrackHeights,
    insert_media,
)
from kumiki.core.model import Clip, MediaItem, Project, ProjectSettings, Track, TrackKind
from kumiki.core.timebase import FrameRate
from kumiki.effects.sources import TEXT


def _text(start: int, duration: int = 30) -> Clip:
    return Clip(timeline_start=start, duration=duration, source=TEXT.create())


def _project(*clips: Clip, locked: bool = False) -> Project:
    base = Project.create()
    track = Track(TrackKind.VIDEO, "V1", clips, locked=locked)
    return base.with_timeline(replace(base.timeline, tracks=(track,)))


def _starts(project: Project) -> list[int]:
    return [clip.timeline_start for clip in project.timeline.tracks[0].clips]


class TestMoveClips:
    def test_neighbours_can_shift_into_each_others_place(self) -> None:
        # 1 本ずつ動かすと、前のクリップが後ろのクリップの元の場所に入った瞬間に
        # 重なって失敗する 全体としては重ならないので、まとめてなら動かせる
        first, second = _text(0), _text(30)
        project = _project(first, second)
        moved = MoveClips((first.id, second.id), 30).apply(project)
        assert _starts(moved) == [30, 60]

    def test_the_linked_partner_moves_too(self, video_media: MediaItem) -> None:
        # 映像だけ動くと、音がずれる
        base = Project.create(ProjectSettings(frame_rate=FrameRate(30)))
        for command in insert_media(base, video_media, at_frame=0):
            base = command.apply(base)
        video = base.timeline.tracks[0].clips[0]
        moved = MoveClips((video.id,), 15).apply(base)
        assert [t.clips[0].timeline_start for t in moved.timeline.tracks] == [15, 15]

    def test_a_locked_track_stops_everything(self) -> None:
        # ロックしたトラックのクリップが選択に混ざっていたら、ほかも動かさない
        clip = _text(0)
        project = _project(clip, locked=True)
        with pytest.raises(ValueError, match="ロック"):
            MoveClips((clip.id,), 10).apply(project)

    def test_nothing_goes_before_the_start(self) -> None:
        # 先頭より前へ出たクリップは、どこにも表示されずに消えたように見える
        clip = _text(5)
        with pytest.raises(ValueError, match="先頭"):
            MoveClips((clip.id,), -10).apply(_project(clip))

    def test_a_collision_with_an_unselected_clip_fails(self) -> None:
        # 選んでいないクリップに重なるなら、黙って上書きせずに断る
        moving, staying = _text(0), _text(40)
        with pytest.raises(ValueError):
            MoveClips((moving.id,), 20).apply(_project(moving, staying))


class TestRemoveClips:
    def test_ripple_closes_every_gap(self) -> None:
        # 前から消して詰めると、後ろのクリップの位置がずれて詰める量が狂う
        a, b, c = _text(0), _text(30), _text(60)
        removed = RemoveClips((a.id, b.id), ripple=True).apply(_project(a, b, c))
        assert _starts(removed) == [0]

    def test_a_missing_clip_removes_nothing(self) -> None:
        # 一部だけ消すと、どれが消えたのか分からない
        a = _text(0)
        project = _project(a)
        other = _text(100)
        with pytest.raises(KeyError):
            RemoveClips((a.id, other.id)).apply(project)


class TestCheckpointRollback:
    def test_a_failure_leaves_the_project_as_it_was(self) -> None:
        # 3 本のうち 2 本だけ置かれた状態が 1 段として残ると、戻す段も分からない
        document = Document(_project(_text(0)))
        before = document.project
        with pytest.raises(ValueError), document.checkpoint("まとめて"):
            document.execute(RenameProject("途中"))
            document.execute(MoveClips((before.timeline.tracks[0].clips[0].id,), -5))
        assert document.project is before
        assert not document.can_undo


class TestMerge:
    def _heights(self, document: Document, height: int) -> None:
        track = document.project.timeline.tracks[0]
        with document.checkpoint("高さ", merge=True):
            document.execute(SetTrackHeights(((track.id, height),)))

    def test_continued_changes_share_one_step(self) -> None:
        # 壊れると、ホイールを回した回数だけ取り消すことになる
        document = Document(_project())
        with document.checkpoint("高さ"):
            document.execute(SetTrackHeights(((document.project.timeline.tracks[0].id, 80),)))
        self._heights(document, 100)
        self._heights(document, 120)
        assert document.history_labels == ("高さ",)
        document.undo()
        assert document.project.timeline.tracks[0].height == 60

    def test_nothing_merges_into_a_step_uncovered_by_undo(self) -> None:
        # 取り消したあとの一番上は古い操作 そこへまとめると、関係ない操作と一緒に戻る
        document = Document(_project())
        self._heights(document, 80)
        document.execute(RenameProject("別名"))
        self._heights(document, 100)
        document.undo()
        document.undo()
        self._heights(document, 120)
        assert document.history_labels == ("高さ", "高さ")

    def test_a_different_operation_is_its_own_step(self) -> None:
        document = Document(_project())
        self._heights(document, 80)
        with document.checkpoint("名前", merge=True):
            document.execute(RenameProject("別名"))
        assert document.history_labels == ("高さ", "名前")
