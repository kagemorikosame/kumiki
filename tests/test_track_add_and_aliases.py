"""トラックを足す決まり・右クリックした所へ置く決まり・自分で保存するエイリアス（Issue #27）"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from sashimono.core.commands import AddClip, Command, insert_filter, insert_generated
from sashimono.core.commands.insert import is_effect_track, new_track
from sashimono.core.io.aliases import Alias, AliasStore, alias_refusal
from sashimono.core.io.serialize import FORMAT_VERSION, ProjectFileError
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    GroupId,
    MediaItem,
    Project,
    Track,
    TrackKind,
)
from sashimono.effects import registry
from sashimono.effects.sources import TEXT


def _project(*tracks: Track) -> Project:
    base = Project.create()
    return base.with_timeline(replace(base.timeline, tracks=tracks))


def _apply(project: Project, commands: list[Command]) -> Project:
    for command in commands:
        project = command.apply(project)
    return project


class TestNewTrack:
    def test_names_count_up_per_kind(self) -> None:
        project = _project(Track(TrackKind.VIDEO, "V1"), Track(TrackKind.AUDIO, "A1"))
        assert new_track(project, TrackKind.VIDEO).track.name == "V2"
        assert new_track(project, TrackKind.AUDIO).track.name == "A2"
        assert new_track(project, TrackKind.VIDEO, effect=True).track.name == "FX1"

    def test_a_name_left_by_a_removed_track_is_skipped(self) -> None:
        # V2 を消した後に V1 と V3 が残ると、数えただけでは V3 がもう 1 本できる
        project = _project(Track(TrackKind.VIDEO, "V1"), Track(TrackKind.VIDEO, "V3"))
        assert new_track(project, TrackKind.VIDEO).track.name == "V4"

    def test_an_effect_track_is_known_by_its_name(self) -> None:
        # 形式を変えずに見分ける 種類を増やすと古い版で開けなくなる
        project = _project(Track(TrackKind.VIDEO, "V1"))
        track = new_track(project, TrackKind.VIDEO, effect=True).track
        assert track.kind is TrackKind.VIDEO
        assert is_effect_track(track)
        assert not is_effect_track(Track(TrackKind.VIDEO, "FX 素材"))
        assert not is_effect_track(Track(TrackKind.AUDIO, "FX1"))

    def test_an_audio_effect_track_is_refused(self) -> None:
        with pytest.raises(ValueError):
            new_track(_project(), TrackKind.AUDIO, effect=True)

    def test_a_new_track_joins_the_solo(self) -> None:
        # ソロで絞っている間に足したトラックは、ソロが無いと足した所で出なくなる
        project = _project(Track(TrackKind.VIDEO, "V1", solo=True))
        assert new_track(project, TrackKind.VIDEO).track.solo
        assert not new_track(project, TrackKind.AUDIO).track.solo

    def test_video_goes_to_the_end_of_the_order(self) -> None:
        # 並びの末尾が一番上 間に挟まると、足しただけで重なり順が変わる
        project = _project(Track(TrackKind.VIDEO, "V1"), Track(TrackKind.AUDIO, "A1"))
        added = _apply(project, [new_track(project, TrackKind.VIDEO)])
        assert [t.name for t in added.timeline.tracks] == ["V1", "A1", "V2"]
        assert [t.name for t in added.timeline.video_tracks()] == ["V1", "V2"]


class TestPlacingOnAChosenTrack:
    def test_text_goes_to_the_asked_track(self) -> None:
        # いつもの決まり（下から空きを探す）だと V1 へ入り、右クリックした V2 に来ない
        v1, v2 = Track(TrackKind.VIDEO, "V1"), Track(TrackKind.VIDEO, "V2")
        project = _project(v1, v2)
        commands = insert_generated(project, TEXT.create(), at_frame=30, track_id=v2.id)
        (add,) = commands
        assert isinstance(add, AddClip) and add.track_id == v2.id

    def test_a_busy_or_audio_track_falls_back(self) -> None:
        # 断ると、右クリックした所が埋まっていただけで何も置けない
        busy = Clip(timeline_start=0, duration=100, source=TEXT.create())
        v1 = Track(TrackKind.VIDEO, "V1", (busy,))
        a1 = Track(TrackKind.AUDIO, "A1")
        project = _project(v1, a1)
        for asked in (v1.id, a1.id):
            commands = insert_generated(project, TEXT.create(), at_frame=10, track_id=asked)
            placed = _apply(project, commands)
            assert len(list(placed.timeline.video_tracks())) == 2

    def test_a_filter_goes_to_the_asked_track(self) -> None:
        # フィルタの決まり（絵の上）より、右クリックしたトラックを先にする
        v1, v2 = Track(TrackKind.VIDEO, "V1"), Track(TrackKind.VIDEO, "V2")
        project = _project(v1, v2)
        (add,) = insert_filter(project, at_frame=0, track_id=v1.id)
        assert isinstance(add, AddClip) and add.track_id == v1.id
        assert add.clip.is_filter

    def test_the_old_callers_are_unchanged(self) -> None:
        # 位置を渡せるように広げても、渡さなければ前と同じ所へ置く
        busy = Clip(timeline_start=0, duration=100, source=TEXT.create())
        project = _project(Track(TrackKind.VIDEO, "V1", (busy,)), Track(TrackKind.VIDEO, "V2"))
        (add,) = insert_generated(project, TEXT.create(), at_frame=10)
        assert isinstance(add, AddClip)
        assert add.track_id == project.timeline.tracks[1].id


def _styled_text() -> Clip:
    blur = registry.get("blur")
    assert blur is not None
    return Clip(
        timeline_start=120,
        duration=45,
        source=TEXT.create(text="決め台詞"),
        effects=(blur.create(),),
        opacity=AnimatedValue(0.5),
        blend_mode="add",
    )


class TestAliases:
    def test_a_round_trip_keeps_the_contents_but_not_the_place(self, tmp_path: Path) -> None:
        store = AliasStore(tmp_path)
        clip = _styled_text()
        store.save(Alias.of("見出し", clip))
        (loaded,) = store.all()
        assert loaded.name == "見出し"
        placed = loaded.instantiate(at_frame=10)
        assert placed.timeline_start == 10
        assert placed.duration == 45
        assert placed.source == clip.source
        assert placed.opacity == clip.opacity and placed.blend_mode == "add"
        assert [e.kind for e in placed.effects] == ["blur"]
        # ID が重なると、同じエイリアスを 2 回置いたときに片方を消すと両方を探し当てる
        assert placed.id != clip.id
        assert placed.effects[0].id != clip.effects[0].id

    def test_a_group_or_link_is_not_carried(self, tmp_path: Path) -> None:
        # 保存した時のグループを持ち込むと、別のプロジェクトの見知らぬクリップと束になる
        clip = replace(_styled_text(), group_id=GroupId("g1"), link_group=GroupId("l1"))
        alias = Alias.of("束", clip)
        assert alias.clip.group_id is None and alias.clip.link_group is None

    def test_clips_with_media_or_scenes_are_refused(self, video_media: MediaItem) -> None:
        # 素材の道は本人の機械にしか無く、シーンの中身は元のプロジェクトにしか無い
        movie = Clip(timeline_start=0, duration=30, media_id=video_media.id)
        assert alias_refusal(movie) is not None
        with pytest.raises(ValueError):
            Alias.of("動画", movie)
        assert alias_refusal(_styled_text()) is None

    def test_a_newer_alias_is_not_read(self, tmp_path: Path) -> None:
        # 新しい版の項目を読み飛ばして置き直すと、作った物が黙って変わる
        data = Alias.of("新", _styled_text()).to_dict()
        for key, value in (("version", 99), ("clip_version", FORMAT_VERSION + 1)):
            with pytest.raises(ProjectFileError):
                Alias.from_dict({**data, key: value})

    def test_a_broken_file_does_not_hide_the_others(self, tmp_path: Path) -> None:
        store = AliasStore(tmp_path)
        store.save(Alias.of("残る", _styled_text()))
        (tmp_path / "壊れ.smea").write_text("{", "utf-8")
        (tmp_path / "別物.smea").write_text(json.dumps({"format": "other"}), "utf-8")
        assert [a.name for a in store.all()] == ["残る"]

    def test_names_that_windows_rejects_still_save(self, tmp_path: Path) -> None:
        store = AliasStore(tmp_path)
        path = store.save(Alias.of('見出し: "赤"?', _styled_text()))
        assert path.parent == tmp_path
        assert [a.name for a in store.all()] == ['見出し: "赤"?']
