"""テンプレートを置くときに、画像・音声のアイテムを素材として登録する（#71）

YMM4 の ``ImageItem`` ``AudioItem`` や AviUtl の ``画像ファイル`` ``音声ファイル`` は
素材ファイルのパスだけを持っている（:attr:`MappedObject.media_path`） 置く側が素材へ
登録して ``media_id`` を結ばないと、クリップは描かれず鳴らない

ここでは素材を開く道具を偽物に差し替えて、登録と結び方の決まりだけを見る
実物のテンプレートと実際の素材で描く・鳴らすのは ``test_real_ymm4_media.py``
"""

from __future__ import annotations

import os
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.catalog import gather_media, place
from sashimono.compat.mapped import MappedObject
from sashimono.compat.ymm4.template import map_template
from sashimono.core.commands import AddTrack, Command
from sashimono.core.model import (
    AnimatedValue,
    AudioStreamInfo,
    Clip,
    Effect,
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


def movie(path: Path) -> MediaItem:
    """映像と音声を両方持つ素材"""
    return MediaItem(
        path=path,
        duration=Fraction(2),
        video_streams=still(path).video_streams,
        audio_streams=sound(path).audio_streams,
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
        if path.suffix == ".mp4":
            return movie(path)
        return None


def media_object(
    path: Path | str,
    kind: str,
    *,
    layer: int = 1,
    start: int = 0,
    with_sound: bool = False,
    audio_effects: tuple[Effect, ...] = (),
) -> MappedObject:
    return MappedObject(
        clip=Clip(timeline_start=start, duration=30),
        layer=layer,
        media_path=str(path),
        kind=kind,
        with_sound=with_sound,
        audio_effects=audio_effects,
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
    project = Project.create()
    plan = gather_media(objects, project, FakeProbe())
    assert plan.missing == (absent,)
    assert plan.commands == ()

    project = apply(project, place(objects, project, media=plan.media))
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


def test_audio_avoids_a_busy_or_locked_audio_track(files: tuple[Path, Path]) -> None:
    """ほかの音と重なる音声トラックやロックされた音声トラックには置かない

    そこへ置くと ``AddClip`` が断り、1 回の Undo にまとめた配置が画像や素材の登録まで
    全部取り消されて、テンプレートが 1 つも置けない 空きが無ければ新しく作る
    """
    _, effect = files
    busy = Track(kind=TrackKind.AUDIO, name="A1", clips=(Clip(timeline_start=0, duration=100),))
    locked = Track(kind=TrackKind.AUDIO, name="A2", locked=True)
    project = AddTrack(locked).apply(AddTrack(busy).apply(Project.create()))
    project = put([media_object(effect, "音声ファイル", layer=4)], project, FakeProbe())

    tracks = list(project.timeline.audio_tracks())
    assert len(tracks) == 3
    assert [len(track.clips) for track in tracks] == [1, 0, 1]
    assert tracks[2].clips[0].media_id == project.media[0].id


def test_overlapping_sounds_of_one_layer_get_separate_tracks(files: tuple[Path, Path]) -> None:
    """同じレイヤーで時間の重なる音は、別々の音声トラックへ置く

    レイヤーごとに 1 本へまとめると、2 つ目の ``AddClip`` が重なりで断られ、
    素材の登録を含む配置全体が取り消される
    """
    _, effect = files
    sounds = [
        media_object(effect, "音声ファイル", layer=4, start=0),
        media_object(effect, "音声ファイル", layer=4, start=10),
    ]
    project = put(sounds, Project.create(), FakeProbe())

    tracks = list(project.timeline.audio_tracks())
    assert [len(track.clips) for track in tracks] == [1, 1]


def test_audio_avoids_a_muted_audio_track(files: tuple[Path, Path]) -> None:
    # ミュートされたトラックへ置くと、置けたと出るのに再生にも書き出しにも入らない
    _, effect = files
    muted = Track(kind=TrackKind.AUDIO, name="A1", muted=True)
    project = AddTrack(muted).apply(Project.create())
    project = put([media_object(effect, "音声ファイル", layer=4)], project, FakeProbe())

    tracks = list(project.timeline.audio_tracks())
    assert [len(track.clips) for track in tracks] == [0, 1]
    assert not tracks[1].muted


def test_a_video_with_sound_also_lands_on_an_audio_track(tmp_path: Path) -> None:
    """音も持つ動画は、映像と音のクリップへ分けて置く（Issue #89）

    映像トラックへ 1 本置くだけだと、映像トラックの音は混ぜない決まりなので
    置いた動画の音が鳴らない 素材の読み込み（``insert_media``）と同じ形にする
    """
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")
    project = put(
        [media_object(movie_file, "動画ファイル", with_sound=True)], Project.create(), FakeProbe()
    )

    (picture,) = clips_of(project, TrackKind.VIDEO)
    (sound,) = clips_of(project, TrackKind.AUDIO)
    assert picture.media_id == sound.media_id == project.media[0].id
    assert (sound.timeline_start, sound.duration) == (picture.timeline_start, picture.duration)


def test_the_sound_half_starts_where_the_picture_does(tmp_path: Path) -> None:
    """切り出した位置は音のクリップにも付く

    映像だけに付けると、切り出して使うテンプレート（YMM4 の ``ContentOffset``）で
    絵と声が別の所から始まる
    """
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")
    trimmed = replace(
        media_object(movie_file, "動画ファイル", with_sound=True),
        clip=Clip(timeline_start=0, duration=30, source_in=Fraction(6)),
    )
    project = put([trimmed], Project.create(), FakeProbe())

    (picture,) = clips_of(project, TrackKind.VIDEO)
    (sound,) = clips_of(project, TrackKind.AUDIO)
    assert picture.source_in == sound.source_in == Fraction(6)


def test_the_two_halves_of_a_video_are_linked(tmp_path: Path) -> None:
    """分けた映像と音はリンクで結ぶ 結ばないと片方を動かしただけで絵と音がずれる"""
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")
    project = put(
        [media_object(movie_file, "動画ファイル", with_sound=True)], Project.create(), FakeProbe()
    )

    (picture,) = clips_of(project, TrackKind.VIDEO)
    (sound,) = clips_of(project, TrackKind.AUDIO)
    assert picture.link_group is not None
    assert picture.link_group == sound.link_group
    assert picture.id != sound.id
    # 使うストリームは素材から引く 0 のままだと、映像が 0 番の素材で音が鳴らない
    assert picture.stream_index == movie(movie_file).video_streams[0].index
    assert sound.stream_index == movie(movie_file).audio_streams[0].index


def test_a_video_without_sound_of_its_own_stays_one_clip(tmp_path: Path) -> None:
    """``with_sound`` を立てていない動画は分けない

    AviUtl は同じ動画を「動画ファイル」と「音声ファイル」の 2 つのオブジェクトで
    書き出す 分けると音声のオブジェクトと合わせて音が二重に鳴る
    """
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")
    project = put([media_object(movie_file, "動画ファイル")], Project.create(), FakeProbe())

    assert len(clips_of(project, TrackKind.VIDEO)) == 1
    assert clips_of(project, TrackKind.AUDIO) == []


def test_the_sound_of_a_video_carries_the_volume(tmp_path: Path) -> None:
    """音量は音のクリップに付く 映像のクリップに付けても音は変わらない"""
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")
    volume = Effect(kind="audio_volume", params={"volume": AnimatedValue(50.0)})
    objects = [media_object(movie_file, "動画ファイル", with_sound=True, audio_effects=(volume,))]
    project = put(objects, Project.create(), FakeProbe())

    (picture,) = clips_of(project, TrackKind.VIDEO)
    (sound,) = clips_of(project, TrackKind.AUDIO)
    assert [effect.kind for effect in sound.effects] == ["audio_volume"]
    assert picture.effects == ()


def test_a_video_with_sound_inside_a_group_is_split_too(tmp_path: Path) -> None:
    """まとめた中身の動画も、シーンの中で映像と音へ分ける

    シーンへ置く再帰は同じ :func:`place` を通る ここで分け忘れると、
    合成グループに入れた動画だけが無音になる
    """
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")
    inner = media_object(movie_file, "動画ファイル", with_sound=True)
    group = MappedObject(clip=Clip(timeline_start=0, duration=30), layer=1, children=(inner,))
    project = put([group], Project.create(), FakeProbe())

    (scene,) = project.scenes
    kinds = [track.kind for track in scene.timeline.tracks for _ in track.clips]
    assert kinds == [TrackKind.VIDEO, TrackKind.AUDIO]


def test_audio_uses_an_existing_track_when_it_is_free_there(files: tuple[Path, Path]) -> None:
    # 置く範囲の外にだけ音がある音声トラックは使ってよい 範囲を見ずに断ると、
    # 使える音声トラックがあるのに置くたびに新しいトラックが増える
    _, effect = files
    later = Track(kind=TrackKind.AUDIO, name="A1", clips=(Clip(timeline_start=200, duration=50),))
    project = AddTrack(later).apply(Project.create())
    project = put([media_object(effect, "音声ファイル", layer=4)], project, FakeProbe())

    (track,) = project.timeline.audio_tracks()
    assert len(track.clips) == 2


def _held_video(movie_file: Path, clip: Clip) -> MappedObject:
    """素材の終わりの後も最後の絵を出す印を立てた動画（YMM4 の動画アイテム）"""
    return replace(
        media_object(movie_file, "動画ファイル", with_sound=True), clip=clip, hold_last_frame=True
    )


def test_a_video_longer_than_its_source_holds_the_last_picture(tmp_path: Path) -> None:
    """素材より長い動画は、素材の最後のフレームの時刻で絵を止める（Issue #115）

    素材は 2 秒・30fps なので最後の絵は 59/30 秒 止めないと、素材の終わりから後が
    何も映らない（YMM4 は最後の絵を枠の終わりまで出す）
    音のクリップには持たせない 音は素材の終わりの後は無音で、止める物ではない
    """
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")
    project = put(
        [_held_video(movie_file, Clip(timeline_start=0, duration=90))],
        Project.create(),
        FakeProbe(),
    )

    (picture,) = clips_of(project, TrackKind.VIDEO)
    (heard,) = clips_of(project, TrackKind.AUDIO)
    assert picture.hold_at == Fraction(59, 30)
    assert heard.hold_at is None


def test_the_offset_counts_toward_running_past_the_end(tmp_path: Path) -> None:
    # 切り出した位置から先の残りで比べる 素材の長さだけで比べると、途中から使う動画が
    # 素材の終わりを越えても止まらない
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")
    clip = Clip(timeline_start=0, duration=45, source_in=Fraction(1))
    project = put([_held_video(movie_file, clip)], Project.create(), FakeProbe())

    (picture,) = clips_of(project, TrackKind.VIDEO)
    assert picture.hold_at == Fraction(59, 30)


def test_a_video_within_its_source_is_not_held(tmp_path: Path) -> None:
    # 止まらないクリップに持たせると、設定画面に「絵を止める」が出て何を止めたのか分からない
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")
    project = put(
        [_held_video(movie_file, Clip(timeline_start=0, duration=60))],
        Project.create(),
        FakeProbe(),
    )

    (picture,) = clips_of(project, TrackKind.VIDEO)
    assert picture.hold_at is None


def test_a_video_without_the_mark_is_not_held(tmp_path: Path) -> None:
    # AviUtl の動画ファイルは印を立てない 素材の終わりの後をどう描くかは測っていない
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")
    unmarked = replace(
        media_object(movie_file, "動画ファイル"), clip=Clip(timeline_start=0, duration=90)
    )
    project = put([unmarked], Project.create(), FakeProbe())

    (picture,) = clips_of(project, TrackKind.VIDEO)
    assert picture.hold_at is None


def test_a_video_of_unknown_length_is_not_held(tmp_path: Path) -> None:
    """長さの分からない素材（0 と読めた物）は止めない

    どこが最後か決められない 0 から 1 フレーム引くと負になり、クリップが作れず
    置く所で落ちる
    """
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")

    def unknown(path: Path) -> MediaItem:
        return replace(movie(path), duration=Fraction(0))

    objects = [_held_video(movie_file, Clip(timeline_start=0, duration=90))]
    project = Project.create()
    plan = gather_media(objects, project, unknown)
    project = apply(project, [*plan.commands, *place(objects, project, media=plan.media)])

    (picture,) = clips_of(project, TrackKind.VIDEO)
    assert picture.hold_at is None


def test_a_stopped_video_keeps_its_own_hold(tmp_path: Path) -> None:
    # 再生速度 0 で頭に止めた物を、最後の絵で止め直さない 止める絵が頭から最後へ変わる
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")
    clip = Clip(timeline_start=0, duration=90, source_in=Fraction(1, 2), hold_at=Fraction(1, 2))
    project = put([_held_video(movie_file, clip)], Project.create(), FakeProbe())

    (picture,) = clips_of(project, TrackKind.VIDEO)
    (heard,) = clips_of(project, TrackKind.AUDIO)
    assert picture.hold_at == Fraction(1, 2)
    assert heard.hold_at is None


def test_a_ymm4_video_longer_than_its_source_is_held_without_a_report(tmp_path: Path) -> None:
    """YMM4 の動画アイテムを読んで置くまで通す 素材 2 秒・枠 90 フレーム（3 秒）"""
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")
    item = {
        "$type": "YukkuriMovieMaker.Project.Items.VideoItem, YukkuriMovieMaker",
        "FilePath": str(movie_file),
        "PlaybackRate": 100.0,
        "ContentOffset": "00:00:00",
        "IsLooped": False,
        "Length": 90,
    }
    report = CompatibilityReport()
    objects = map_template([item], report=report)
    project = put(objects, Project.create(), FakeProbe())

    (picture,) = clips_of(project, TrackKind.VIDEO)
    assert picture.hold_at == Fraction(59, 30)
    assert not report.lines()


def test_a_video_whose_sound_runs_longer_holds_its_last_picture(tmp_path: Path) -> None:
    """音の方が長い素材は、映像の道の終わりで止める（#115 のレビュー）

    素材は音 3 秒・映像 2 秒 コンテナの長さ（3 秒）で止める時刻を決めると、映像の
    最後のフレームより後ろを読みに行き、止めた後もデコーダが毎フレーム動く
    2.5 秒の枠はコンテナの中に収まるが、映像の終わりは越える
    """
    movie_file = tmp_path / "映像.mp4"
    movie_file.write_bytes(b"")

    def longer_sound(path: Path) -> MediaItem:
        made = movie(path)
        (stream,) = made.video_streams
        return replace(
            made, duration=Fraction(3), video_streams=(replace(stream, end_time=Fraction(2)),)
        )

    objects = [_held_video(movie_file, Clip(timeline_start=0, duration=75))]
    project = Project.create()
    plan = gather_media(objects, project, longer_sound)
    project = apply(project, [*plan.commands, *place(objects, project, media=plan.media)])

    (picture,) = clips_of(project, TrackKind.VIDEO)
    assert picture.hold_at == Fraction(59, 30)
