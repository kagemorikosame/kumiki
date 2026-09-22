"""テンプレートを置くときに、画像・音声のアイテムを素材として登録する（#71）

YMM4 の ``ImageItem`` ``AudioItem`` や AviUtl の ``画像ファイル`` ``音声ファイル`` は
素材ファイルのパスだけを持っている（:attr:`MappedObject.media_path`） 置く側が素材へ
登録して ``media_id`` を結ばないと、クリップは描かれず鳴らない

ここでは素材を開く道具を偽物に差し替えて、登録と結び方の決まりだけを見る
実物のテンプレートと実際の素材で描く・鳴らすのは ``test_real_ymm4_media.py``
"""

from __future__ import annotations

import os
from fractions import Fraction
from pathlib import Path

import pytest

from sashimono.compat.catalog import gather_media, place
from sashimono.compat.mapped import MappedObject
from sashimono.core.commands import AddTrack, Command
from sashimono.core.model import (
    AudioStreamInfo,
    Clip,
    GeneratedSource,
    MediaItem,
    Project,
    Track,
    TrackKind,
    VideoStreamInfo,
)
from sashimono.core.timebase import FrameRate


def still(path: Path) -> MediaItem:
    """静止画の素材 長さ 0 で音声を持たない"""
    return MediaItem(
        path=path,
        video_streams=(
            VideoStreamInfo(
                index=0,
                width=64,
                height=64,
                frame_rate=FrameRate(30),
                time_base=Fraction(1, 30),
                codec="png",
                pixel_format="rgba",
            ),
        ),
    )


def sound(path: Path) -> MediaItem:
    """音声だけの素材"""
    return MediaItem(
        path=path,
        duration=Fraction(2),
        audio_streams=(
            AudioStreamInfo(
                index=0,
                sample_rate=44100,
                channels=2,
                time_base=Fraction(1, 44100),
                codec="mp3",
            ),
        ),
    )


class FakeProbe:
    """拡張子で素材の形を決める偽物 開いた回数を数える"""

    def __init__(self) -> None:
        self.opened: list[Path] = []

    def __call__(self, path: Path) -> MediaItem | None:
        self.opened.append(path)
        if path.suffix == ".png":
            return still(path)
        if path.suffix == ".mp3":
            return sound(path)
        return None


def media_object(path: Path | str, kind: str, *, layer: int = 1, start: int = 0) -> MappedObject:
    return MappedObject(
        clip=Clip(timeline_start=start, duration=30),
        layer=layer,
        media_path=str(path),
        kind=kind,
    )


def apply(project: Project, commands: list[Command]) -> Project:
    for command in commands:
        project = command.apply(project)
    return project


def put(
    objects: list[MappedObject], project: Project, probe: FakeProbe, *, at_frame: int = 0
) -> Project:
    """UI と同じ順で置く（素材の登録 → 配置）"""
    plan = gather_media(objects, project, probe)
    placed = place(objects, project, at_frame=at_frame, media=plan.media)
    return apply(project, [*plan.commands, *placed])


def clips_of(project: Project, kind: TrackKind) -> list[Clip]:
    return [clip for track in project.timeline.tracks if track.kind is kind for clip in track.clips]


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    picture = tmp_path / "syuutyuu.png"
    picture.write_bytes(b"")
    effect = tmp_path / "coin04.mp3"
    effect.write_bytes(b"")
    return picture, effect


def test_an_image_item_is_drawn_from_a_registered_media(files: tuple[Path, Path]) -> None:
    """画像のアイテムを置くと、素材一覧に載り、クリップがその素材を指す

    結ばないと ``media_id`` の無いクリップになり、置いても何も描かれない
    """
    picture, _ = files
    project = put([media_object(picture, "画像ファイル")], Project.create(), FakeProbe())

    assert [item.path for item in project.media] == [picture]
    (clip,) = clips_of(project, TrackKind.VIDEO)
    assert clip.media_id == project.media[0].id


def test_an_audio_item_is_placed_on_an_audio_track(files: tuple[Path, Path]) -> None:
    """音声のアイテムは音声トラックへ置く

    映像トラックの素材の音は鳴らない決まり（シーンの音だけを混ぜる） しかも音声しか無い
    素材を映像トラックへ置くと、置く時点で断られてテンプレート全体が置けなくなる
    """
    _, effect = files
    project = put([media_object(effect, "音声ファイル", layer=4)], Project.create(), FakeProbe())

    (clip,) = clips_of(project, TrackKind.AUDIO)
    assert clip.media_id == project.media[0].id
    assert clips_of(project, TrackKind.VIDEO) == []


def test_media_inside_a_composite_group_is_linked_too(files: tuple[Path, Path]) -> None:
    """合成するグループ（シーン）の中の画像・音声も素材に結ぶ

    上の段だけ見ると、グループの中の画像はシーンの中で何も描かれない
    """
    picture, effect = files
    group = MappedObject(
        clip=Clip(timeline_start=0, duration=30),
        layer=1,
        kind="scene",
        children=(
            media_object(picture, "画像ファイル", layer=2),
            media_object(effect, "音声ファイル", layer=3),
        ),
    )
    project = put([group], Project.create(), FakeProbe())

    assert len(project.media) == 2
    (scene,) = project.scenes
    by_kind = {track.kind: clip for track in scene.timeline.tracks for clip in track.clips}
    ids = {item.path: item.id for item in project.media}
    assert by_kind[TrackKind.VIDEO].media_id == ids[picture]
    assert by_kind[TrackKind.AUDIO].media_id == ids[effect]


def test_placing_the_same_file_twice_keeps_one_media(files: tuple[Path, Path]) -> None:
    """同じファイルを 2 度置いても素材は 1 つ

    置くたびに登録すると、素材一覧が同じ名前で埋まり、どれを消してよいか分からなくなる
    1 つのテンプレートの中で同じファイルを 2 つのアイテムが使うときも同じ
    """
    picture, _ = files
    probe = FakeProbe()
    twice = [
        media_object(picture, "画像ファイル", layer=1),
        # 書き方の違う同じファイル Windows では大文字小文字も区別しない
        media_object(
            str(picture.parent / "." / picture.name).upper()
            if os.name == "nt"
            else picture.parent / "." / picture.name,
            "画像ファイル",
            layer=2,
        ),
    ]
    project = put(twice, Project.create(), probe)
    again = FakeProbe()
    project = put([media_object(picture, "画像ファイル")], project, again, at_frame=60)

    assert len(project.media) == 1
    assert {clip.media_id for clip in clips_of(project, TrackKind.VIDEO)} == {project.media[0].id}
    # 登録済みの素材は開き直さない 置くたびに開くと、大きい動画で待たされる
    assert len(probe.opened) == 1
    assert again.opened == []


def test_a_missing_file_is_reported_and_the_rest_is_placed(tmp_path: Path) -> None:
    """見つからない素材は数えて返し、ほかのアイテムは置く

    配布物のパスは作者の機械のもの 1 つ見つからないだけで配置全体を止めると、
    文字や図形まで置けなくなる
    """
    absent = r"C:\Users\作者\Documents\効果音\coin04.mp3"
    objects = [media_object(absent, "音声ファイル", layer=4)]
    plan = gather_media(objects, Project.create(), FakeProbe())
    assert plan.missing == (absent,)
    assert plan.commands == ()

    project = apply(Project.create(), place(objects, Project.create(), media=plan.media))
    # 素材が無くても音声は音声トラックに置く 映像トラックにあると、あとで
    # 素材を足しても鳴らない
    (clip,) = clips_of(project, TrackKind.AUDIO)
    assert clip.media_id is None


def test_a_file_next_to_the_template_is_found_by_name(files: tuple[Path, Path]) -> None:
    """書かれたパスに無ければ、テンプレートの置き場で同じ名前を探す

    YMM4 のテンプレートは作者の機械の絶対パス（``C:\\Users\\作者\\…``）を持つ
    素材を添えて配られても、書かれたパスだけを見ると見つからない
    """
    picture, _ = files
    written = r"C:\Users\skki7\Documents\動画プロジェクト\image\syuutyuu.png"
    plan = gather_media(
        [media_object(written, "画像ファイル")], Project.create(), FakeProbe(), near=picture.parent
    )
    assert plan.missing == ()
    assert plan.media[written].path == picture


def test_a_waveform_draws_itself_and_is_not_linked(tmp_path: Path) -> None:
    """音声波形は絵を自分で描くので、素材に結ばない

    結ぶと音声しか無い素材を映像トラックへ置くことになり、置く時点で断られる
    描く音は中身の ``audio_path`` から読むので、結ばなくても波形は出る
    """
    wave = tmp_path / "bgm.mp3"
    wave.write_bytes(b"")
    item = MappedObject(
        clip=Clip(
            timeline_start=0,
            duration=30,
            source=GeneratedSource(
                kind="shape", params={"shape": "waveform", "audio_path": str(wave)}
            ),
        ),
        layer=1,
        media_path=str(wave),
        kind="shape",
    )
    probe = FakeProbe()
    project = put([item], Project.create(), probe)

    assert probe.opened == []
    (clip,) = clips_of(project, TrackKind.VIDEO)
    assert clip.media_id is None


def test_audio_on_a_high_layer_uses_one_audio_track(files: tuple[Path, Path]) -> None:
    # すでにある音声トラックへ置く レイヤー番号の数だけ音声トラックを作ると、
    # 10 段目の効果音 1 つのために空の音声トラックが 9 本増える
    _, effect = files
    existing = Track(kind=TrackKind.AUDIO, name="A1")
    project = AddTrack(existing).apply(Project.create())
    project = put([media_object(effect, "音声ファイル", layer=10)], project, FakeProbe())

    (track,) = project.timeline.audio_tracks()
    assert track.id == existing.id
    assert len(track.clips) == 1
