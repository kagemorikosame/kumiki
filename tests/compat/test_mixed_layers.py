"""互換の読み込みを混合の方式（レイヤー）で置く（#27 P6）

混合の方式のプロジェクトでは、YMM4 のテンプレートと AviUtl のエイリアス・``.exo`` を
元のレイヤー番号どおりの混合トラックへ置く 動画アイテムは絵と音を 1 本のクリップで持つ
分ける方式（既定）の置き方は ``test_template_media.py`` などが見ている ここでは分ける方式が
変わっていないことも併せて確かめる

実物のテンプレートを両方の方式で描き比べるのは ``test_real_mixed_layers.py``
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from sashimono.compat.aviutl.exo import parse_exo
from sashimono.compat.aviutl.mapping import map_exo, map_object
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.catalog import gather_media, place
from sashimono.compat.mapped import MappedObject
from sashimono.compat.ymm4.template import map_template
from sashimono.core.commands import AddMedia, AddTrack, Command
from sashimono.core.model import (
    AnimatedValue,
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
from sashimono.core.timebase import FrameRate

RATE = FrameRate(30)


def _audio(index: int) -> AudioStreamInfo:
    return AudioStreamInfo(
        index=index, sample_rate=48000, channels=2, time_base=Fraction(1, 48000), codec="aac"
    )


def _video(index: int) -> VideoStreamInfo:
    return VideoStreamInfo(
        index=index,
        width=64,
        height=36,
        frame_rate=RATE,
        time_base=Fraction(1, 30),
        codec="h264",
        pixel_format="yuv420p",
    )


def movie(path: Path, *, sounds: int = 1) -> MediaItem:
    """映像 1 本（0 番）と音 ``sounds`` 本（1 番から）を持つ素材 長さ 10 秒"""
    return MediaItem(
        path=path,
        duration=Fraction(10),
        video_streams=(_video(0),),
        audio_streams=tuple(_audio(1 + n) for n in range(sounds)),
    )


def sound(path: Path) -> MediaItem:
    return MediaItem(path=path, duration=Fraction(10), audio_streams=(_audio(0),))


class Probe:
    """拡張子で素材の形を決める偽物 ``.mkv`` は音を 2 本持つ動画"""

    def __call__(self, path: Path) -> MediaItem | None:
        if path.suffix == ".mp4":
            return movie(path)
        if path.suffix == ".mkv":
            return movie(path, sounds=2)
        if path.suffix == ".mp3":
            return sound(path)
        return None


def project_of(mode: str) -> Project:
    return Project.create(ProjectSettings(width=320, height=180, frame_rate=RATE, layer_mode=mode))


def apply(project: Project, commands: list[Command]) -> Project:
    for command in commands:
        project = command.apply(project)
    return project


def put(
    objects: list[MappedObject],
    project: Project,
    *,
    report: CompatibilityReport | None = None,
    **options: Any,
) -> Project:
    """UI と同じ順で置く（素材の登録 → 配置）"""
    plan = gather_media(objects, project, Probe())
    placed = place(objects, project, media=plan.media, report=report, **options)
    return apply(project, [*plan.commands, *placed])


def ymm4_item(kind: str, **values: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "$type": f"YukkuriMovieMaker.Project.Items.{kind}, YukkuriMovieMaker",
        "Frame": 0,
        "Length": 60,
        "Layer": 0,
    }
    item.update(values)
    return item


def video_item(path: Path, **values: Any) -> dict[str, Any]:
    return ymm4_item(
        "VideoItem", **{"FilePath": str(path), "Volume": 50.0, "AudioTrackIndex": 0, **values}
    )


def audio_item(path: Path, **values: Any) -> dict[str, Any]:
    return ymm4_item("AudioItem", **{"FilePath": str(path), "AudioTrackIndex": 0, **values})


def text_item(**values: Any) -> dict[str, Any]:
    return ymm4_item("TextItem", **{"Text": "字幕", "FontSize": 40.0, **values})


def mapped(items: list[dict[str, Any]]) -> list[MappedObject]:
    return map_template(items, report=CompatibilityReport())


def layers(project: Project) -> list[Track]:
    return [track for track in project.timeline.tracks if track.kind is TrackKind.MIXED]


def only_clip(track: Track) -> Clip:
    (clip,) = track.clips
    return clip


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    """テンプレートが書いている素材 中身は偽物の道具が決めるので空でよい"""
    names = {"movie": "映像.mp4", "dub": "吹き替え.mkv", "effect": "効果音.mp3"}
    found = {key: tmp_path / name for key, name in names.items()}
    for path in found.values():
        path.write_bytes(b"")
    return found


class TestYmm4:
    def test_a_video_item_is_one_clip_with_its_sound(self, files: dict[str, Path]) -> None:
        """動画アイテムは分けずに 1 本のクリップで絵と音を持つ

        分けると 1 つのレイヤーに同じ位置の 2 本が重なり、置く時点で断られる
        （直す前は混合のプロジェクトでも映像トラックと音声トラックへ分けていた）
        """
        project = put(mapped([video_item(files["movie"])]), project_of(LayerMode.MIXED))

        assert [track.kind for track in project.timeline.tracks] == [TrackKind.MIXED]
        clip = only_clip(project.timeline.tracks[0])
        media = project.media[0]
        assert clip.media_id == media.id
        assert clip.stream_index == media.video_streams[0].index
        assert clip.audio_stream == media.audio_streams[0].index
        assert clip.show_picture
        # 1 本しか無いのにリンクを残すと、あとで別の素材と誤って連動する
        assert clip.link_group is None

    def test_the_video_carries_both_fixed_groups_once(self, files: dict[str, Path]) -> None:
        """絵の欄（反転 → 配置）と音の欄（音量 → フェード）を 1 つずつ持つ

        写した音量（50%）はそのまま音量の欄になる 別に既定の欄を足すと 2 つ並び、
        どちらがパネルの欄なのか分からなくなる
        """
        project = put(mapped([video_item(files["movie"])]), project_of(LayerMode.MIXED))

        clip = only_clip(project.timeline.tracks[0])
        fixed = [effect.kind for effect in clip.effects if effect.fixed]
        assert fixed == ["flip", "transform", "audio_volume", "audio_fade"]
        volume = next(effect for effect in clip.effects if effect.kind == "audio_volume")
        assert volume.params["volume"] == AnimatedValue(50.0)

    def test_an_audio_item_hides_its_picture(self, files: dict[str, Path]) -> None:
        """音声アイテムは同じ並びのレイヤーへ、絵を隠して置く

        絵を描く側に数えると、上のクリップの切り抜きの相手になって何も映らなくなる
        """
        items = [text_item(Layer=0), audio_item(files["effect"], Layer=1)]
        project = put(mapped(items), project_of(LayerMode.MIXED))

        first, second = layers(project)
        assert only_clip(first).source is not None
        heard = only_clip(second)
        assert not heard.show_picture
        assert heard.audio_stream == project.media[0].audio_streams[0].index
        assert [e.kind for e in heard.effects if e.fixed] == ["audio_volume", "audio_fade"]

    def test_layers_follow_the_template(self, files: dict[str, Path]) -> None:
        """YMM4 のレイヤー n（0 始まり）は n + 1 本目のレイヤー 間が空いても詰めない

        YMM4 もこちらも番号の大きいレイヤーほど手前に描く 詰めたり裏返したりすると、
        文字が動画の後ろへ入る
        """
        items = [
            video_item(files["movie"], Layer=0),
            text_item(Layer=2),
            audio_item(files["effect"], Layer=3),
        ]
        project = put(mapped(items), project_of(LayerMode.MIXED))

        found = layers(project)
        assert [track.name for track in found] == [f"レイヤー {n}" for n in (1, 2, 3, 4)]
        assert only_clip(found[0]).media_id is not None
        assert found[1].clips == ()
        assert only_clip(found[2]).source is not None
        assert not only_clip(found[3]).show_picture

    def test_existing_layers_are_reused_by_number(self, files: dict[str, Path]) -> None:
        # 今あるレイヤーはその番号のまま使う 足すと、同じ番号の物が別のレイヤーへ散る
        project = project_of(LayerMode.MIXED)
        project = apply(project, [*_new_layers(project, 2)])
        project = put(mapped([text_item(Layer=1)]), project, at_frame=100)

        assert [len(track.clips) for track in layers(project)] == [0, 1]

    def test_a_composite_group_is_placed_on_layers_inside_its_scene(self) -> None:
        """まとめた中身（シーン）の中も混合の方式で置く

        シーンの中だけ分ける方式で置くと、ひとつの作品に 2 つの置き方が混ざる
        """
        group = ymm4_item("GroupItem", Layer=0, Length=60, GroupRange=2, IsComposite=True)
        objects = mapped([group, text_item(Layer=1), text_item(Layer=2, Text="下")])
        project = put(objects, project_of(LayerMode.MIXED))

        (scene,) = project.scenes
        assert {track.kind for track in scene.timeline.tracks} == {TrackKind.MIXED}
        assert {track.kind for track in project.timeline.tracks} == {TrackKind.MIXED}

    def test_the_chosen_audio_track_is_heard(self, files: dict[str, Path]) -> None:
        """``AudioTrackIndex`` 1 は素材の 2 本目の音 数えて残さない

        直す前は写せない物として数え、1 本目の音を鳴らしていた（吹き替えの動画で元の言語が鳴る）
        """
        report = CompatibilityReport()
        items = [video_item(files["dub"], AudioTrackIndex=1)]
        project = put(mapped(items), project_of(LayerMode.MIXED), report=report)

        clip = only_clip(project.timeline.tracks[0])
        assert clip.audio_stream == project.media[0].audio_streams[1].index
        assert not any("AudioTrackIndex" in line for line in report.lines())

    def test_a_missing_audio_track_falls_back_and_is_counted(self, files: dict[str, Path]) -> None:
        # 素材に無い本数のまま置くと、混合トラックは置く時点で断り、テンプレートごと置けない
        report = CompatibilityReport()
        items = [video_item(files["movie"], AudioTrackIndex=3)]
        project = put(mapped(items), project_of(LayerMode.MIXED), report=report)

        clip = only_clip(project.timeline.tracks[0])
        assert clip.audio_stream == project.media[0].audio_streams[0].index
        assert any("AudioTrackIndex" in line for line in report.lines())

    def test_a_right_clicked_layer_takes_everything(self, files: dict[str, Path]) -> None:
        # レイヤーは絵も音も受けるので、選んだレイヤーへまとめて置く
        project = project_of(LayerMode.MIXED)
        project = apply(project, [*_new_layers(project, 3)])
        chosen = layers(project)[2]
        items = [audio_item(files["effect"], Layer=0)]
        project = put(mapped(items), project, track_id=chosen.id)

        assert [len(track.clips) for track in layers(project)] == [0, 0, 1]

    @pytest.mark.parametrize("hidden", ["muted", "outside_solo"])
    def test_a_right_clicked_layer_that_is_silent_is_passed_over(
        self, files: dict[str, Path], hidden: str
    ) -> None:
        """ミュートやソロの外のレイヤーを選んでも、そこへは置かず元のレイヤー番号へ回す

        置くと、置いた直後からプレビューにも書き出しにも出ない（PR #165 のレビュー）
        """
        chosen = Track(kind=TrackKind.MIXED, name="レイヤー 1", muted=hidden == "muted")
        other = Track(kind=TrackKind.MIXED, name="レイヤー 2", solo=hidden == "outside_solo")
        project = apply(project_of(LayerMode.MIXED), [AddTrack(chosen), AddTrack(other)])
        project = put(mapped([text_item(Layer=1)]), project, track_id=chosen.id)

        assert [len(track.clips) for track in layers(project)] == [0, 1]

    def test_a_video_with_sound_needs_a_chosen_layer_that_is_heard(
        self, files: dict[str, Path]
    ) -> None:
        """絵と音を持つ動画は、選んだレイヤーが映るだけでなく鳴ることも見る

        音声トラックをソロにしている間はレイヤーの音は鳴らない 絵だけ見て置くと、
        置いた動画の音が聞こえない（PR #165 のレビュー）
        """
        chosen = Track(kind=TrackKind.MIXED, name="レイヤー 1")
        soloed = Track(kind=TrackKind.AUDIO, name="A1", solo=True)
        project = apply(project_of(LayerMode.MIXED), [AddTrack(chosen), AddTrack(soloed)])
        items = [video_item(files["movie"], Layer=1)]
        project = put(mapped(items), project, track_id=chosen.id)

        first, second = layers(project)
        assert first.clips == ()
        assert only_clip(second).audio_stream is not None

    def test_a_scene_needs_a_chosen_layer_that_is_heard(self) -> None:
        """まとめた中身（シーン）も鳴る物として見る シーンは音の番号を持たずに鳴る

        番号の有無で決めると、音のソロで鳴らないレイヤーへ置き、シーンの中の音が
        聞こえない（PR #165 のレビュー）
        """
        chosen = Track(kind=TrackKind.MIXED, name="レイヤー 1")
        soloed = Track(kind=TrackKind.AUDIO, name="A1", solo=True)
        project = apply(project_of(LayerMode.MIXED), [AddTrack(chosen), AddTrack(soloed)])
        group = ymm4_item("GroupItem", Layer=1, Length=60, GroupRange=1, IsComposite=True)
        objects = mapped([group, text_item(Layer=2)])
        project = put(objects, project, track_id=chosen.id)

        first, *rest = layers(project)
        assert first.clips == ()
        assert [clip.scene_id is not None for track in rest for clip in track.clips] == [True]

    def test_a_silent_picture_ignores_what_is_heard(self, files: dict[str, Path]) -> None:
        # 音を鳴らさない文字は映れば足りる 鳴るかまで見ると、音のソロの間は選んだ所へ置けない
        chosen = Track(kind=TrackKind.MIXED, name="レイヤー 1")
        soloed = Track(kind=TrackKind.AUDIO, name="A1", solo=True)
        project = apply(project_of(LayerMode.MIXED), [AddTrack(chosen), AddTrack(soloed)])
        project = put(mapped([text_item(Layer=1)]), project, track_id=chosen.id)

        assert len(layers(project)[0].clips) == 1

    def test_a_sound_is_not_put_on_a_muted_chosen_layer(self, files: dict[str, Path]) -> None:
        # 音だけの物も同じ 鳴らないレイヤーへ置くと、置いたのに聞こえない
        chosen = Track(kind=TrackKind.MIXED, name="レイヤー 1", muted=True)
        project = apply(project_of(LayerMode.MIXED), [AddTrack(chosen)])
        project = put(mapped([audio_item(files["effect"], Layer=1)]), project, track_id=chosen.id)

        first, second = layers(project)
        assert first.clips == ()
        assert not only_clip(second).show_picture


class TestSeparatedStaysTheSame:
    """分ける方式（既定）は今までどおり 絵と音を 2 本に分けてリンクで結ぶ"""

    def test_a_video_item_is_still_split(self, files: dict[str, Path]) -> None:
        # 分けないと音声トラックに音のクリップが無く鳴らない リンクが外れると、片方だけ
        # 動かしたときに絵と音がずれる 番号（audio_stream）を持たせると、映像トラックへ
        # 置く時点で断られる
        project = put(mapped([video_item(files["movie"])]), project_of(LayerMode.SEPARATED))

        kinds = sorted(track.kind.value for track in project.timeline.tracks)
        assert kinds == ["audio", "video"]
        picture, heard = (only_clip(track) for track in project.timeline.tracks)
        assert picture.link_group is not None
        assert picture.link_group == heard.link_group
        assert picture.audio_stream is None
        assert heard.audio_stream is None

    def test_the_first_sound_is_still_read_and_the_choice_counted(
        self, files: dict[str, Path]
    ) -> None:
        """分ける方式は 1 本目の音のまま 選んだ音を鳴らせないので数えて残す

        選び直すと、分ける方式で読んだ作品の音が今までと変わる
        """
        report = CompatibilityReport()
        items = [video_item(files["dub"], AudioTrackIndex=1)]
        project = put(mapped(items), project_of(LayerMode.SEPARATED), report=report)

        heard = next(
            only_clip(track) for track in project.timeline.tracks if track.kind is TrackKind.AUDIO
        )
        assert heard.stream_index == project.media[0].audio_streams[0].index
        assert any("AudioTrackIndex" in line for line in report.lines())


def _new_layers(project: Project, count: int) -> list[Command]:
    from sashimono.core.commands.layers import new_layer

    commands: list[Command] = []
    for _ in range(count):
        new_layer(project, commands)
    return commands


#: AviUtl2 v2.1.6a が書く素材オブジェクト（``tests/compat/test_mapping.py`` と同じ項目）
AVIUTL_MOVIE = (
    "effect.name=動画ファイル\n再生位置=0.000,0.000,再生範囲,0\n再生速度=100.00\n"
    "ファイル={path}\nトラック=0\nループ再生=0\n音声付き=0\nYUV=\nfps調整=0\n"
)
AVIUTL_SOUND = (
    "effect.name=音声ファイル\n再生位置=0.000,10.000,再生範囲,0\n再生速度=100.00\n"
    "ファイル={path}\nトラック=0\nループ再生=0\n"
)


def aviutl_pair(path: Path) -> str:
    """同じ動画を 動画ファイル（レイヤー 1）と 音声ファイル（レイヤー 2）で書いた物"""
    movie_part = AVIUTL_MOVIE.replace("{path}", str(path))
    sound_part = AVIUTL_SOUND.replace("{path}", str(path))
    return (
        f"[0]\nlayer=1\nframe=0,59\n[0.0]\n{movie_part}"
        f"[1]\nlayer=2\nframe=0,59\n[1.0]\n{sound_part}"
    )


class TestAviUtl:
    def test_a_movie_and_its_sound_stay_two_objects(self, files: dict[str, Path]) -> None:
        """動画ファイルは音を持たず、音声ファイルは絵を隠して、それぞれのレイヤーへ置く

        動画ファイルにも音を持たせると、同じ動画の音が 音声ファイル と合わせて二重に鳴る
        """
        exo = parse_exo(aviutl_pair(files["movie"]))
        objects = [
            item
            for obj in exo.objects
            if (item := map_object(obj, RATE, report=CompatibilityReport())) is not None
        ]
        project = put(objects, project_of(LayerMode.MIXED))

        first, second = layers(project)
        picture, heard = only_clip(first), only_clip(second)
        media = project.media[0]
        assert picture.media_id == heard.media_id == media.id
        assert picture.show_picture and picture.audio_stream is None
        assert not heard.show_picture
        assert heard.audio_stream == media.audio_streams[0].index

    def test_the_exo_import_uses_layers_too(self, files: dict[str, Path]) -> None:
        """``.exo`` の読み込み（map_exo）もレイヤー番号どおりのレイヤーへ置く

        直す前は混合のプロジェクトでも映像トラックへ置き、音声ファイル は映像トラックに
        置けずに読み込みごと断られていた
        """
        path = files["movie"]
        media = movie(path)
        project = project_of(LayerMode.MIXED)
        commands = map_exo(
            parse_exo(aviutl_pair(path)),
            project,
            media={str(path): media.id},
            items=(media,),
            report=CompatibilityReport(),
        )
        project = apply(project, [AddMedia(media), *commands])

        first, second = layers(project)
        assert [track.kind for track in project.timeline.tracks] == [TrackKind.MIXED] * 2
        assert only_clip(first).audio_stream is None
        heard = only_clip(second)
        assert not heard.show_picture
        assert heard.audio_stream == media.audio_streams[0].index

    def test_a_movie_with_its_own_sound_is_counted(self, files: dict[str, Path]) -> None:
        # 音声付きの動画ファイルは音を写していない 黙って落とすと、消えた音に気付けない
        text = aviutl_pair(files["movie"]).replace("音声付き=0", "音声付き=1")
        report = CompatibilityReport()
        for obj in parse_exo(text).objects:
            map_object(obj, RATE, report=report)
        assert any("音声付き" in line for line in report.lines())


def test_a_clip_is_never_split_on_layers(files: dict[str, Path]) -> None:
    """混合の方式では、置いた物の数だけクリップができる（分けた音のクリップを作らない）"""
    objects = mapped(
        [
            video_item(files["movie"], Layer=0),
            audio_item(files["effect"], Layer=1),
            text_item(Layer=2),
        ]
    )
    project = put(objects, project_of(LayerMode.MIXED))
    clips = [clip for track in project.timeline.tracks for clip in track.clips]
    assert len(clips) == len(objects)
    assert all(clip.link_group is None for clip in clips)


def test_the_placed_position_is_kept_on_layers(files: dict[str, Path]) -> None:
    # 置く位置へずらすのは分ける方式と同じ ずらし忘れると元のタイムラインの位置へ行く
    objects = [
        replace(item, clip=replace(item.clip, timeline_start=40)) for item in mapped([text_item()])
    ]
    project = put(objects, project_of(LayerMode.MIXED), at_frame=7)
    assert only_clip(layers(project)[0]).timeline_start == 7
