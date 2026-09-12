"""クリップのコピー・切り取り・貼り付け

貼り付けは「新しいクリップを置く」ことなので、元のクリップとの縁が切れていることが
いちばん大事 ID やリンクが元と同じままだと、貼ったものを動かしたときに元のものまで
動いたり、片方を消すともう片方まで消えたりする
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kumiki.core.clipboard import copy_clips, cut_commands, paste_commands
from kumiki.core.commands import AddClip, AddTrack, Command, Document, insert_media
from kumiki.core.model import (
    Clip,
    GeneratedSource,
    MediaItem,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from kumiki.core.timebase import FrameRate


def apply(project: Project, commands: list[Command]) -> Project:
    for command in commands:
        project = command.apply(project)
    return project


@pytest.fixture
def linked(video_media: MediaItem) -> Project:
    """映像と音声がリンクした素材を 1 本、先頭に置いたもの（300 フレーム）"""
    base = Project.create(ProjectSettings(frame_rate=FrameRate(30)))
    return apply(base, insert_media(base, video_media, at_frame=0))


def added(commands: list[Command]) -> list[AddClip]:
    return [command for command in commands if isinstance(command, AddClip)]


class TestCopy:
    def test_the_linked_partner_comes_along(self, linked: Project) -> None:
        # 映像だけ運ぶと、貼った映像に音が付いてこない
        video = linked.timeline.tracks[0].clips[0]
        content = copy_clips(linked, [video.id])
        assert len(content.clips) == 2

    def test_an_unknown_clip_is_ignored(self, linked: Project) -> None:
        assert copy_clips(linked, [Clip(timeline_start=0, duration=1).id]).clips == ()


class TestPaste:
    def test_it_lands_at_the_playhead(self, linked: Project) -> None:
        content = copy_clips(linked, [linked.timeline.tracks[0].clips[0].id])
        starts = {
            command.clip.timeline_start for command in added(paste_commands(linked, content, 300))
        }
        assert starts == {300}

    def test_the_copy_is_cut_loose_from_the_original(self, linked: Project) -> None:
        # ID もリンクも元と同じだと、貼ったものを動かすと元まで動く
        original = linked.timeline.tracks[0].clips[0]
        content = copy_clips(linked, [original.id])
        pasted = [command.clip for command in added(paste_commands(linked, content, 300))]
        assert all(clip.id != original.id for clip in pasted)
        groups = {clip.link_group for clip in pasted}
        assert len(groups) == 1
        assert original.link_group not in groups

    def test_two_pastes_are_two_separate_pairs(self, linked: Project) -> None:
        content = copy_clips(linked, [linked.timeline.tracks[0].clips[0].id])
        first = apply(linked, paste_commands(linked, content, 300))
        second = paste_commands(first, content, 600)
        groups_first = {c.link_group for t in first.timeline.tracks for c in t.clips}
        assert {command.clip.link_group for command in added(second)}.isdisjoint(groups_first)

    def test_a_busy_track_sends_it_to_a_new_one(self, linked: Project) -> None:
        # 元の場所に貼ると重なる クリップの重なりは許されないので、別のトラックへ
        content = copy_clips(linked, [linked.timeline.tracks[0].clips[0].id])
        commands = paste_commands(linked, content, 0)
        assert sum(isinstance(command, AddTrack) for command in commands) == 2
        pasted = apply(linked, commands)
        assert len(pasted.timeline.tracks) == 4

    def test_a_locked_track_is_skipped(self, linked: Project) -> None:
        # ロックしたトラックへ貼ると、AddClip が「ロックされている」で失敗する
        video_track = linked.timeline.tracks[0]
        locked = linked.with_timeline(
            linked.timeline.replace_track(replace(video_track, locked=True))
        )
        content = copy_clips(locked, [video_track.clips[0].id])
        commands = paste_commands(locked, content, 300)
        video_target = next(c for c in added(commands) if c.clip.stream_index == 0)
        assert video_target.track_id != video_track.id
        apply(locked, commands)

    def test_a_removed_source_is_refused(self, linked: Project) -> None:
        # 素材の無いクリップを置くと、再生したときに理由の分からない穴になる
        content = copy_clips(linked, [linked.timeline.tracks[0].clips[0].id])
        with pytest.raises(ValueError, match="素材"):
            paste_commands(linked.with_media(()), content, 300)

    def test_a_generated_clip_needs_no_source(self) -> None:
        base = Project.create()
        text = Clip(timeline_start=0, duration=30, source=GeneratedSource(kind="text"))
        track = Track(TrackKind.VIDEO, "V1")
        project = apply(base, [AddTrack(track), AddClip(track.id, text)])
        content = copy_clips(project, [text.id])
        pasted = apply(project, paste_commands(project, content, 30))
        assert len(pasted.timeline.tracks[0].clips) == 2

    def test_a_paste_is_one_undo_step(self, linked: Project) -> None:
        document = Document(linked)
        content = copy_clips(linked, [linked.timeline.tracks[0].clips[0].id])
        with document.checkpoint("貼り付け"):
            for command in paste_commands(linked, content, 300):
                document.execute(command)
        document.undo()
        assert document.project is linked


class TestCut:
    def test_one_removal_takes_the_pair(self, linked: Project) -> None:
        # 組の両方に RemoveClip を出すと、2 つ目が「見つからない」で失敗する
        content = copy_clips(linked, [linked.timeline.tracks[1].clips[0].id])
        commands = cut_commands(linked, content)
        assert len(commands) == 1
        emptied = apply(linked, commands)
        assert all(not track.clips for track in emptied.timeline.tracks)
