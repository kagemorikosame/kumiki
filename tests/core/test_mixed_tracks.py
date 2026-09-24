"""混合トラック（YMM4 型のレイヤー Issue #27）のモデル・命令・保存

混合トラックは映像・音声・テキストを何でも置ける 1 本のレイヤー 音付きの動画は
絵と音を 1 本のクリップで持つ ここが崩れると、置いた動画の絵か音のどちらかが
黙って消える
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from typing import Any

import pytest

from sashimono.core.commands import (
    AddClip,
    AddTrack,
    GroupClips,
    MoveClip,
    MoveClips,
    RippleCut,
    SetLayerMode,
    SplitClip,
    TrimClip,
)
from sashimono.core.io import ProjectFileError, project_from_dict, project_to_dict
from sashimono.core.io.serialize import FORMAT_VERSION
from sashimono.core.model import (
    Clip,
    GeneratedSource,
    LayerMode,
    MediaItem,
    Project,
    ProjectSettings,
    SceneId,
    Timeline,
    Track,
    TrackKind,
    default_track_name,
    draws_picture,
    plays_sound,
)
from sashimono.core.timebase import FrameRate

RATE = FrameRate(30)


def _names(tracks: tuple[Track, ...]) -> list[str]:
    return [t.name for t in tracks]


def _line(*tracks: Track) -> Timeline:
    return Timeline(rate=RATE, tracks=tracks)


def _mixed_clip(media: MediaItem, **changes: Any) -> Clip:
    """音付きの動画を混合トラックに置いたクリップ 絵は映像の、音は音声のストリーム"""
    clip = Clip(
        timeline_start=0,
        duration=90,
        media_id=media.id,
        stream_index=media.video_streams[0].index,
        audio_stream=media.audio_streams[0].index,
    )
    return replace(clip, **changes)


@pytest.fixture
def mixed_project(video_media: MediaItem, audio_media: MediaItem) -> Project:
    """混合トラック 2 本と映像トラック 1 本 レイヤー 1 に音付きの動画を置いてある"""
    base = Project.create(ProjectSettings(frame_rate=RATE), media=(video_media, audio_media))
    tracks = (
        Track(TrackKind.MIXED, "レイヤー 1", (_mixed_clip(video_media),)),
        Track(TrackKind.MIXED, "レイヤー 2"),
        Track(TrackKind.VIDEO, "V1"),
    )
    return base.with_timeline(replace(base.timeline, tracks=tracks))


class TestRoles:
    def test_mixed_tracks_both_draw_and_sound(self) -> None:
        # 混合トラックが片方の役割にしか入らないと、置いた動画の絵か音が消える
        line = _line(
            Track(TrackKind.VIDEO, "V1"),
            Track(TrackKind.MIXED, "レイヤー 1"),
            Track(TrackKind.AUDIO, "A1"),
            Track(TrackKind.MIXED, "レイヤー 2"),
        )
        assert _names(line.picture_tracks()) == ["V1", "レイヤー 1", "レイヤー 2"]
        assert _names(line.sound_tracks()) == ["レイヤー 1", "A1", "レイヤー 2"]

    def test_the_draw_order_is_the_track_order(self) -> None:
        # 並びの先頭が一番奥 レイヤー 1 が奥で番号が大きいほど手前（YMM4・AviUtl と同じ）
        # 種類ごとに並べ替えると、映像トラックと混合トラックが混ざったときに重なりが裏返る
        line = _line(
            Track(TrackKind.MIXED, "レイヤー 1"),
            Track(TrackKind.VIDEO, "V1"),
            Track(TrackKind.MIXED, "レイヤー 2"),
        )
        assert _names(line.active_picture_tracks()) == ["レイヤー 1", "V1", "レイヤー 2"]

    def test_the_old_kind_query_still_answers_for_separated_projects(self) -> None:
        # 分ける方式だけのタイムラインでは、今までの active_tracks と同じ答え
        line = _line(
            Track(TrackKind.VIDEO, "V1"),
            Track(TrackKind.VIDEO, "V2", solo=True),
            Track(TrackKind.AUDIO, "A1"),
        )
        assert _names(line.active_tracks(TrackKind.VIDEO)) == ["V2"]
        assert _names(line.active_tracks(TrackKind.AUDIO)) == ["A1"]


class TestSolo:
    """ソロは役割（絵か音か）の中で決まる 混合トラックは両方に入る"""

    def test_a_soloed_layer_is_the_only_thing_seen_and_heard(self) -> None:
        # 種類の中で決めると、混合をソロにしても映像トラックが映り、音声トラックも鳴る
        line = _line(
            Track(TrackKind.VIDEO, "V1"),
            Track(TrackKind.MIXED, "レイヤー 1", solo=True),
            Track(TrackKind.MIXED, "レイヤー 2"),
            Track(TrackKind.AUDIO, "A1"),
        )
        assert _names(line.active_picture_tracks()) == ["レイヤー 1"]
        assert _names(line.active_sound_tracks()) == ["レイヤー 1"]

    def test_an_audio_solo_keeps_the_pictures(self) -> None:
        # 分ける方式で音声のソロが絵を消さないのと同じ 消すと、音を聞き比べる間に画面が消える
        line = _line(
            Track(TrackKind.VIDEO, "V1"),
            Track(TrackKind.MIXED, "レイヤー 1"),
            Track(TrackKind.AUDIO, "A1", solo=True),
        )
        assert _names(line.active_picture_tracks()) == ["V1", "レイヤー 1"]
        assert _names(line.active_sound_tracks()) == ["A1"]

    def test_a_video_solo_keeps_the_sound_of_layers(self) -> None:
        line = _line(
            Track(TrackKind.VIDEO, "V1", solo=True),
            Track(TrackKind.MIXED, "レイヤー 1"),
            Track(TrackKind.AUDIO, "A1"),
        )
        assert _names(line.active_picture_tracks()) == ["V1"]
        assert _names(line.active_sound_tracks()) == ["レイヤー 1", "A1"]

    def test_a_muted_layer_is_neither_seen_nor_heard(self) -> None:
        line = _line(Track(TrackKind.MIXED, "レイヤー 1", muted=True))
        assert line.active_picture_tracks() == ()
        assert line.active_sound_tracks() == ()


class TestClipRoles:
    def test_a_video_with_sound_draws_and_plays_on_a_layer(self, video_media: MediaItem) -> None:
        track = Track(TrackKind.MIXED)
        clip = _mixed_clip(video_media)
        assert draws_picture(track, clip, video_media)
        assert plays_sound(track, clip, video_media)

    def test_the_picture_can_be_hidden(self, video_media: MediaItem) -> None:
        track = Track(TrackKind.MIXED)
        clip = _mixed_clip(video_media, show_picture=False)
        assert not draws_picture(track, clip, video_media)
        assert plays_sound(track, clip, video_media)

    def test_no_stream_means_no_sound(self, video_media: MediaItem) -> None:
        # 番号が無いのに鳴らすと、音を切ったつもりの動画が鳴り続ける
        track = Track(TrackKind.MIXED)
        clip = _mixed_clip(video_media, audio_stream=None)
        assert not plays_sound(track, clip, video_media)

    def test_sound_only_media_draws_nothing_on_a_layer(self, audio_media: MediaItem) -> None:
        # 重ねに残すと、上のクリップがそれで切り抜いて何も映らなくなる
        track = Track(TrackKind.MIXED)
        clip = Clip(0, 30, media_id=audio_media.id, audio_stream=0)
        assert not draws_picture(track, clip, audio_media)
        assert plays_sound(track, clip, audio_media)

    def test_generated_clips_draw_but_stay_silent(self) -> None:
        track = Track(TrackKind.MIXED)
        clip = Clip(0, 30, source=GeneratedSource(kind="text"))
        assert draws_picture(track, clip, None)
        assert not plays_sound(track, clip, None)

    def test_scenes_draw_and_play_on_a_layer(self) -> None:
        clip = Clip(0, 30, scene_id=SceneId("scene"))
        assert draws_picture(Track(TrackKind.MIXED), clip, None)
        assert plays_sound(Track(TrackKind.MIXED), clip, None)

    def test_separated_tracks_keep_their_rules(self, video_media: MediaItem) -> None:
        # 映像トラックの動画は鳴らさず（音は組の音声クリップが鳴らす）、音声トラックは描かない
        clip = _mixed_clip(video_media, audio_stream=None, show_picture=False)
        assert draws_picture(Track(TrackKind.VIDEO), clip, video_media)
        assert not plays_sound(Track(TrackKind.VIDEO), clip, video_media)
        assert not draws_picture(Track(TrackKind.AUDIO), clip, video_media)
        assert plays_sound(Track(TrackKind.AUDIO), clip, video_media)

    def test_a_negative_stream_is_refused(self) -> None:
        with pytest.raises(ValueError, match="音声ストリーム"):
            Clip(0, 30, audio_stream=-1)

    def test_layers_are_named_like_ymm4(self) -> None:
        assert default_track_name(TrackKind.MIXED, 1) == "レイヤー 1"
        assert default_track_name(TrackKind.VIDEO, 2) == "V2"
        assert default_track_name(TrackKind.AUDIO, 3) == "A3"


class TestCommands:
    def test_any_media_goes_on_a_layer(
        self, mixed_project: Project, audio_media: MediaItem
    ) -> None:
        # 混合トラックで素材の種類を断ると、BGM をレイヤーに置けない
        layer = mixed_project.timeline.tracks[1]
        clip = Clip(0, 30, media_id=audio_media.id, audio_stream=0)
        placed = AddClip(layer.id, clip).apply(mixed_project)
        assert placed.timeline.tracks[1].clips == (clip,)

    def test_a_sounding_clip_cannot_move_to_a_video_track(self, mixed_project: Project) -> None:
        # 映像トラックはクリップの音を鳴らさない 移せると音が黙って消える
        clip = mixed_project.timeline.tracks[0].clips[0]
        video = mixed_project.timeline.tracks[2]
        with pytest.raises(ValueError, match="音が鳴らなくなる"):
            MoveClip(clip.id, 0, video.id).apply(mixed_project)

    def test_a_silent_clip_can_move_to_a_video_track(self, mixed_project: Project) -> None:
        clip = mixed_project.timeline.tracks[0].clips[0]
        project = _set_clip(mixed_project, replace(clip, audio_stream=None))
        video = project.timeline.tracks[2]
        moved = MoveClip(clip.id, 0, video.id).apply(project)
        assert moved.timeline.tracks[2].clips[0].id == clip.id

    def test_moving_between_layers_keeps_picture_and_sound(self, mixed_project: Project) -> None:
        clip = mixed_project.timeline.tracks[0].clips[0]
        layer_2 = mixed_project.timeline.tracks[1]
        moved = MoveClip(clip.id, 15, layer_2.id).apply(mixed_project)
        (arrived,) = moved.timeline.tracks[1].clips
        assert (arrived.timeline_start, arrived.audio_stream, arrived.show_picture) == (15, 1, True)

    def test_shifting_by_rows_stays_among_layers(self, mixed_project: Project) -> None:
        # 行ずらしが映像トラックへ入ると、音付きの動画が映像トラックへ落ちて音が消える
        clip = mixed_project.timeline.tracks[0].clips[0]
        moved = MoveClips((clip.id,), 0, track_delta=1).apply(mixed_project)
        assert moved.timeline.tracks[1].clips[0].id == clip.id
        with pytest.raises(ValueError):
            MoveClips((clip.id,), 0, track_delta=2).apply(mixed_project)

    def test_split_halves_keep_the_stream_and_the_picture_switch(
        self, mixed_project: Project
    ) -> None:
        # 落とすと、分けた後半だけ音が消える・絵が出る
        clip = mixed_project.timeline.tracks[0].clips[0]
        project = _set_clip(mixed_project, replace(clip, show_picture=False))
        split = SplitClip(clip.id, 30).apply(project)
        halves = split.timeline.tracks[0].clips
        assert len(halves) == 2
        assert all(h.audio_stream == 1 and not h.show_picture for h in halves)
        assert halves[1].source_in == Fraction(1)

    def test_trim_keeps_the_stream(self, mixed_project: Project) -> None:
        clip = mixed_project.timeline.tracks[0].clips[0]
        trimmed = TrimClip(clip.id, head_delta=10).apply(mixed_project)
        (kept,) = trimmed.timeline.tracks[0].clips
        assert kept.audio_stream == 1
        assert kept.timeline_start == 10

    def test_ripple_cut_closes_the_gap_on_layers(self, mixed_project: Project) -> None:
        cut = RippleCut(((30, 60),)).apply(mixed_project)
        clips = cut.timeline.tracks[0].clips
        assert [(c.timeline_start, c.duration) for c in clips] == [(0, 30), (30, 30)]
        assert all(c.audio_stream == 1 for c in clips)

    def test_grouping_works_on_layers(self, mixed_project: Project, audio_media: MediaItem) -> None:
        layer_2 = mixed_project.timeline.tracks[1]
        other = Clip(0, 30, media_id=audio_media.id, audio_stream=0)
        project = AddClip(layer_2.id, other).apply(mixed_project)
        first = project.timeline.tracks[0].clips[0]
        grouped = GroupClips((first.id, other.id)).apply(project)
        groups = {c.group_id for t in grouped.timeline.tracks[:2] for c in t.clips}
        assert len(groups) == 1 and None not in groups


class TestLayerMode:
    def test_the_model_default_is_separated(self) -> None:
        # 既定を混合にすると、古いファイルと今の試験の置き方が黙って変わる
        assert ProjectSettings().layer_mode == LayerMode.SEPARATED

    def test_the_command_changes_only_the_setting(self, mixed_project: Project) -> None:
        changed = SetLayerMode(LayerMode.MIXED).apply(mixed_project)
        assert changed.settings.layer_mode == LayerMode.MIXED
        assert changed.timeline is mixed_project.timeline

    def test_an_unknown_mode_is_refused(self, mixed_project: Project) -> None:
        with pytest.raises(ValueError, match="方式"):
            SetLayerMode("both").apply(mixed_project)
        with pytest.raises(ValueError, match="方式"):
            ProjectSettings(layer_mode="both")


class TestSaving:
    def test_the_format_is_7(self) -> None:
        assert FORMAT_VERSION == 7

    def test_layers_and_their_clips_round_trip(self, mixed_project: Project) -> None:
        # 落とすと、保存して開き直しただけで動画の音が消える・隠した絵が出る
        clip = mixed_project.timeline.tracks[0].clips[0]
        project = _set_clip(mixed_project, replace(clip, show_picture=False))
        project = SetLayerMode(LayerMode.MIXED).apply(project)
        loaded = project_from_dict(project_to_dict(project))
        assert loaded.timeline.tracks[0].kind is TrackKind.MIXED
        assert loaded.timeline.tracks[0].clips[0].audio_stream == 1
        assert loaded.timeline.tracks[0].clips[0].show_picture is False
        assert loaded.settings.layer_mode == LayerMode.MIXED
        assert loaded == project

    def test_version_6_opens_as_separated(self, project: Project, video_media: MediaItem) -> None:
        # 6 までのファイルに項目は無い 変換なしで、分ける方式のまま開けること
        placed = AddClip(project.timeline.tracks[0].id, Clip(0, 30, media_id=video_media.id)).apply(
            project
        )
        data = project_to_dict(placed)
        data["version"] = 6
        del data["settings"]["layer_mode"]
        for track in data["timeline"]["tracks"]:
            for clip in track["clips"]:
                del clip["audio_stream"]
                del clip["show_picture"]
        loaded = project_from_dict(data)
        assert loaded.settings.layer_mode == LayerMode.SEPARATED
        (clip,) = loaded.timeline.tracks[0].clips
        assert (clip.audio_stream, clip.show_picture) == (None, True)
        assert loaded == placed

    def test_a_newer_file_asks_for_an_update(self, mixed_project: Project) -> None:
        data = project_to_dict(mixed_project)
        data["version"] = FORMAT_VERSION + 1
        with pytest.raises(ProjectFileError, match="更新"):
            project_from_dict(data)

    def test_a_broken_stream_number_is_a_file_error(self, mixed_project: Project) -> None:
        # 文字のまま通すと、鳴らすときに型の違いで落ちる
        data = project_to_dict(mixed_project)
        data["timeline"]["tracks"][0]["clips"][0]["audio_stream"] = "1"
        with pytest.raises(ProjectFileError, match="audio_stream"):
            project_from_dict(data)


def test_a_layer_can_be_added(mixed_project: Project) -> None:
    track = Track(TrackKind.MIXED, "レイヤー 3")
    added = AddTrack(track).apply(mixed_project)
    assert added.timeline.tracks[-1].kind is TrackKind.MIXED


def _set_clip(project: Project, clip: Clip) -> Project:
    located = project.timeline.locate_clip(clip.id)
    assert located is not None
    track, _ = located
    clips = tuple(clip if c.id == clip.id else c for c in track.clips)
    return project.with_timeline(project.timeline.replace_track(track.with_clips(clips)))
