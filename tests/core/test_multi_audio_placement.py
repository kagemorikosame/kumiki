"""音声ストリームが 2 本以上ある素材の置き方（Issue #27 の流れ）

利用者の要望は「レイヤー 1 に映像、レイヤー 2 以降に音声が入り、トラックが無ければ
自動で足す」 1 本目の音だけを置くと、2 本目以降（ゲームの録画のマイクの声など）は
タイムラインのどこにも無く、鳴らす手段が無い

設定（``split_audio``）で今までどおり 1 本目だけを映像と一緒に置くこともできる
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from sashimono.core.commands import (
    AddClip,
    AddTrack,
    Command,
    Document,
    MoveClip,
    insert_media,
    place_media,
)
from sashimono.core.commands.fixed import SOUND_FIXED
from sashimono.core.model import (
    AudioStreamInfo,
    Clip,
    LayerMode,
    MediaItem,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
    VideoStreamInfo,
)
from tests.conftest import RATE_30


def _stream(index: int) -> AudioStreamInfo:
    return AudioStreamInfo(
        index=index, sample_rate=48000, channels=2, time_base=Fraction(1, 48000), codec="aac"
    )


@pytest.fixture
def two_voices() -> MediaItem:
    """10 秒の 1080p30 素材 映像 1 本と音声 2 本（ゲームの音とマイクの声）"""
    return MediaItem(
        path=Path("C:/素材/録画.mkv"),
        duration=Fraction(10),
        video_streams=(
            VideoStreamInfo(
                index=0,
                width=1920,
                height=1080,
                frame_rate=RATE_30,
                time_base=Fraction(1, 15360),
                codec="h264",
            ),
        ),
        audio_streams=(_stream(1), _stream(2)),
    )


@pytest.fixture
def two_tracks_only() -> MediaItem:
    """絵の無い 30 秒の素材 音声 2 本（2 か国語の吹き替えなど）"""
    return MediaItem(
        path=Path("C:/素材/吹き替え.mka"),
        duration=Fraction(30),
        audio_streams=(_stream(0), _stream(1)),
    )


def _mixed(*tracks: Track) -> Project:
    base = Project.create(ProjectSettings(frame_rate=RATE_30, layer_mode=LayerMode.MIXED))
    return base.with_timeline(replace(base.timeline, tracks=tracks))


def _separated(*tracks: Track) -> Project:
    base = Project.create(ProjectSettings(frame_rate=RATE_30))
    return base.with_timeline(replace(base.timeline, tracks=tracks))


def _apply(project: Project, commands: list[Command]) -> Project:
    for command in commands:
        project = command.apply(project)
    return project


def _only_clip(track: Track) -> Clip:
    (clip,) = track.clips
    return clip


class TestMixedLayers:
    def test_the_picture_and_each_voice_get_their_own_layer(self, two_voices: MediaItem) -> None:
        # 1 本のクリップにまとめると、2 本目の声がどこにも置かれず鳴らない
        placed = _apply(_mixed(), insert_media(_mixed(), two_voices))
        layers = placed.timeline.tracks
        assert [t.kind for t in layers] == [TrackKind.MIXED] * 3
        assert [t.name for t in layers] == ["レイヤー 1", "レイヤー 2", "レイヤー 3"]
        picture, first, second = (_only_clip(t) for t in layers)

        # 絵のクリップが音も持つと、1 本目の声が 2 回鳴る
        assert picture.show_picture
        assert picture.audio_stream is None
        assert placed.draws_picture(layers[0], picture)
        assert not placed.plays_sound(layers[0], picture)

        for layer, clip, stream in ((layers[1], first, 1), (layers[2], second, 2)):
            assert clip.audio_stream == stream
            # 絵を出す印のままだと、同じ動画の絵が 3 枚重なる
            assert not clip.show_picture
            assert not placed.draws_picture(layer, clip)
            assert placed.plays_sound(layer, clip)

    def test_every_part_is_linked_together(self, two_voices: MediaItem) -> None:
        # 組が別れていると、絵を動かしたときに声だけ元の位置に残って口がずれる
        commands = insert_media(_mixed(), two_voices)
        groups = {c.clip.link_group for c in commands if isinstance(c, AddClip)}
        assert len(groups) == 1
        assert None not in groups

        placed = _apply(_mixed(), commands)
        picture = _only_clip(placed.timeline.tracks[0])
        moved = MoveClip(picture.id, 60).apply(placed)
        assert [_only_clip(t).timeline_start for t in moved.timeline.tracks] == [60, 60, 60]

    def test_each_voice_has_the_fixed_volume_and_fade(self, two_voices: MediaItem) -> None:
        # 固定の欄が無いと、置いた直後に 2 本目の声だけ音量をいじれない
        placed = _apply(_mixed(), insert_media(_mixed(), two_voices))
        for layer in placed.timeline.tracks[1:]:
            clip = _only_clip(layer)
            assert [e.kind for e in clip.effects if e.fixed] == list(SOUND_FIXED)
        # 絵のクリップに音の欄を残すと、鳴らない音量の欄がパネルに出る
        picture = _only_clip(placed.timeline.tracks[0])
        assert not any(e.kind in SOUND_FIXED for e in picture.effects)

    def test_a_busy_layer_is_skipped_and_a_new_one_is_added(self, two_voices: MediaItem) -> None:
        # 埋まったレイヤーへ置くと重なりで断られ、何も置かれない
        bgm = Clip(timeline_start=0, duration=600, show_picture=False)
        busy = Track(TrackKind.MIXED, "レイヤー 2", clips=(bgm,))
        project = _mixed(
            Track(TrackKind.MIXED, "レイヤー 1"), busy, Track(TrackKind.MIXED, "レイヤー 3")
        )
        placed = _apply(project, insert_media(project, two_voices, at_frame=0))
        names = [t.name for t in placed.timeline.tracks]
        assert names == ["レイヤー 1", "レイヤー 2", "レイヤー 3", "レイヤー 4"]
        first, _, third, fourth = placed.timeline.tracks
        assert _only_clip(first).show_picture
        assert _only_clip(third).audio_stream == 1
        assert _only_clip(fourth).audio_stream == 2

    def test_voices_go_below_the_picture_layer(self, two_voices: MediaItem) -> None:
        # 空いた奥のレイヤーへ戻ると「映像の次のレイヤーから音」の並びが崩れる
        text = Clip(timeline_start=0, duration=600)
        project = _mixed(
            Track(TrackKind.MIXED, "レイヤー 1"),
            Track(TrackKind.MIXED, "レイヤー 2", clips=(text,)),
        )
        placed = _apply(project, insert_media(project, two_voices, at_frame=0))
        tracks = placed.timeline.tracks
        # 絵は範囲で絵を描く一番手前（レイヤー 2）より手前の新しいレイヤー 3 へ入る
        assert [t.name for t in tracks] == [f"レイヤー {n}" for n in range(1, 6)]
        assert not tracks[0].clips
        assert _only_clip(tracks[2]).show_picture
        assert [_only_clip(t).audio_stream for t in tracks[3:]] == [1, 2]

    def test_one_undo_removes_everything(self, two_voices: MediaItem) -> None:
        # 何回も取り消すことになると、途中で止めた人に声だけのレイヤーが残る
        document = Document(_mixed())
        with document.checkpoint("配置"):
            for command in insert_media(document.project, two_voices):
                document.execute(command)
        placed = document.project
        assert len(placed.timeline.tracks) == 3
        restored = document.undo()
        assert restored.timeline.tracks == ()
        assert document.project is restored
        assert document.project.media == ()

    def test_the_tracks_are_added_by_commands(self, two_voices: MediaItem) -> None:
        # モデルを直に書き換えると、取り消しの履歴に載らない
        commands = insert_media(_mixed(), two_voices)
        assert sum(isinstance(c, AddTrack) for c in commands) == 3

    def test_sound_only_media_splits_too(self, two_tracks_only: MediaItem) -> None:
        # 絵の無い素材で 2 本目を捨てると、吹き替えのもう一方の言語が消える
        placed = _apply(_mixed(), insert_media(_mixed(), two_tracks_only))
        clips = [_only_clip(t) for t in placed.timeline.tracks]
        assert [c.audio_stream for c in clips] == [0, 1]
        assert not any(c.show_picture for c in clips)
        assert len({c.link_group for c in clips}) == 1
        assert clips[0].link_group is not None

    def test_dropping_follows_the_same_rule(self, two_voices: MediaItem) -> None:
        # 落とした時だけ 1 本にまとまると、入口によって置かれ方が変わる
        layer = Track(TrackKind.MIXED, "レイヤー 1")
        project = _mixed(layer)
        placed = _apply(project, place_media(project, [two_voices], at_frame=30, track_id=layer.id))
        tracks = placed.timeline.tracks
        assert len(tracks) == 3
        assert _only_clip(tracks[0]).show_picture
        assert [_only_clip(t).audio_stream for t in tracks[1:]] == [1, 2]
        assert {_only_clip(t).timeline_start for t in tracks} == {30}

    def test_the_setting_keeps_the_old_single_clip(self, two_voices: MediaItem) -> None:
        # 設定で切ったのに分かれると、1 本で扱いたい人がレイヤーを消して回ることになる
        placed = _apply(_mixed(), insert_media(_mixed(), two_voices, split_audio=False))
        (layer,) = placed.timeline.tracks
        clip = _only_clip(layer)
        assert clip.show_picture
        assert clip.audio_stream == 1
        assert clip.link_group is None

    def test_the_setting_reaches_dropping(self, two_voices: MediaItem) -> None:
        # 落とす入口だけ設定を見ないと、ドロップの時だけレイヤーが増える
        commands = place_media(_mixed(), [two_voices], at_frame=0, split_audio=False)
        assert sum(isinstance(c, AddClip) for c in commands) == 1


class TestSeparatedTracks:
    def test_each_voice_gets_its_own_audio_track(self, two_voices: MediaItem) -> None:
        # 1 本目だけを音声トラックへ置くと、分ける方式でも 2 本目の声が鳴らない
        placed = _apply(_separated(), insert_media(_separated(), two_voices))
        kinds = [t.kind for t in placed.timeline.tracks]
        assert kinds == [TrackKind.VIDEO, TrackKind.AUDIO, TrackKind.AUDIO]
        video, first, second = (_only_clip(t) for t in placed.timeline.tracks)
        assert [first.stream_index, second.stream_index] == [1, 2]
        assert video.link_group is not None
        assert video.link_group == first.link_group == second.link_group
        assert [e.kind for e in second.effects if e.fixed] == list(SOUND_FIXED)

    def test_an_existing_free_audio_track_is_used(self, two_voices: MediaItem) -> None:
        # 空いている音声トラックを飛ばして新しく作ると、置くたびにトラックが増える
        a1, a2 = Track(TrackKind.AUDIO, "A1"), Track(TrackKind.AUDIO, "A2")
        project = _separated(Track(TrackKind.VIDEO, "V1"), a1, a2)
        placed = _apply(project, place_media(project, [two_voices], at_frame=0))
        assert len(placed.timeline.tracks) == 3
        assert [_only_clip(t).stream_index for t in placed.timeline.tracks[1:]] == [1, 2]

    def test_the_setting_keeps_one_audio_clip(self, two_voices: MediaItem) -> None:
        # 設定で切ったのに増えると、今までの置き方に慣れた人が困る
        placed = _apply(_separated(), insert_media(_separated(), two_voices, split_audio=False))
        assert [t.kind for t in placed.timeline.tracks] == [TrackKind.VIDEO, TrackKind.AUDIO]


class TestSingleStreamUnchanged:
    def test_one_voice_is_still_one_clip(self, video_media: MediaItem) -> None:
        # 音が 1 本の素材まで分けると、今までの作品の置き方が変わる
        placed = _apply(_mixed(), insert_media(_mixed(), video_media))
        (layer,) = placed.timeline.tracks
        clip = _only_clip(layer)
        assert clip.show_picture
        assert clip.audio_stream == video_media.audio_streams[0].index
