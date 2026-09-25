"""混合の方式（:attr:`~sashimono.core.model.LayerMode.MIXED`）で物を置く所を決める

素材・テキスト・フィルタ・シーン・貼り付け・字幕の焼き込みが、どれも同じ決まりで
混合トラック（レイヤー）を選び、無ければ足す 置く側ごとに書くと、どれか 1 つだけ
重なり順やソロを忘れて、置いたのに見えないクリップができる

方式による分かれ道はここへ寄せ、置く側（:mod:`sashimono.core.commands.insert` など）は
「混合か」を尋ねてここを呼ぶだけにする 置く側の本文（置いたときに付ける固定の
エフェクトなど）と混ざらないようにするため

**重なり順** レイヤー 1 が一番奥で、番号が大きいほど手前（:class:`~sashimono.core.model.Timeline`）
新しいレイヤーは並びの末尾（一番手前）へ足す 絵を置くときは、その範囲で絵を描いている
一番手前のトラックより手前の、空いたレイヤーを使う 奥から空きを探すと、置いたテキストや
フィルタが動画の後ろへ入り、置いたのに見えない
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from sashimono.core.commands.base import Command
from sashimono.core.commands.edit import AddClip, AddTrack
from sashimono.core.model import (
    Clip,
    LayerMode,
    Project,
    Track,
    TrackId,
    TrackKind,
    default_track_name,
    draws_picture,
)

__all__ = [
    "active_layers",
    "free_layer",
    "layer_after",
    "media_placements",
    "new_layer",
    "places_mixed",
    "shows_picture_on_layer",
    "solo_for_new_track",
]


def places_mixed(project: Project) -> bool:
    """これから置く物を混合トラックへ置くか"""
    return project.settings.layer_mode == LayerMode.MIXED


def shows_picture_on_layer(project: Project, clip: Clip) -> bool:
    """``clip`` をレイヤーに置いたときに絵を描くか 音だけの素材と絵を隠したクリップは偽"""
    media = project.find_media(clip.media_id) if clip.media_id is not None else None
    return draws_picture(Track(TrackKind.MIXED), clip, media)


def solo_for_new_track(project: Project, kind: TrackKind, *, picture: bool = True) -> bool:
    """新しく作る ``kind`` のトラックにソロを付けるか

    ソロで絞っている間に作ったトラックは、ソロを付けないと作った所で外れ、置いた物が
    出ない ソロは役割（絵か音か）の中で決まる
    （:meth:`~sashimono.core.model.Timeline.active_picture_tracks` と
    :meth:`~sashimono.core.model.Timeline.active_sound_tracks`）ので、見るのも役割の中にする

    混合トラックは絵と音の両方に入る ``picture`` が真（絵を描く物を置く）なら絵の側、
    偽（音だけの物）なら音の側のソロを見る 両方を見ると、音声トラックをソロにしている
    だけで新しいレイヤーにソロが付き、ほかの映像がすべて消える
    """
    timeline = project.timeline
    if kind is TrackKind.MIXED:
        tracks = timeline.picture_tracks() if picture else timeline.sound_tracks()
    else:
        # 分ける方式は今までどおり同じ種類の中で見る 種類で見ても役割で見ても、
        # 混合トラックが無いうちは同じ答えになる
        tracks = tuple(t for t in timeline.tracks if t.kind is kind)
    return any(t.solo and not t.muted for t in tracks)


def active_layers(project: Project, *, picture: bool) -> tuple[Track, ...]:
    """いま映る（``picture``）か鳴る（偽）レイヤー ミュートとソロの決まりは役割の中で見る

    置く先を選ぶ所（空きを探す・選ばれた所を受ける）がこれで同じ答えを出す 片方だけ
    ミュートを見ないと、右クリックやドロップで選んだ所へだけ見えない物が置ける
    """
    timeline = project.timeline
    tracks = timeline.active_picture_tracks() if picture else timeline.active_sound_tracks()
    return tuple(t for t in tracks if t.kind is TrackKind.MIXED)


def _created(commands: Sequence[Command]) -> list[Track]:
    return [c.track for c in commands if isinstance(c, AddTrack)]


def new_layer(
    project: Project, commands: list[Command], *, picture: bool = True, name: str = ""
) -> Track:
    """レイヤーを 1 本、並びの末尾（一番手前）へ足すコマンドを積んで返す

    名前を省くと「レイヤー n」 ``commands`` の中で先に作ったトラックの名前も避ける
    """
    created = _created(commands)
    if not name:
        taken = {t.name for t in (*project.timeline.tracks, *created)}
        count = sum(1 for t in (*project.timeline.tracks, *created) if t.kind is TrackKind.MIXED)
        name = default_track_name(TrackKind.MIXED, count + 1, taken)
    track = Track(
        kind=TrackKind.MIXED,
        name=name,
        solo=solo_for_new_track(project, TrackKind.MIXED, picture=picture),
    )
    commands.append(AddTrack(track))
    return track


def free_layer(
    project: Project,
    start: int,
    end: int,
    commands: list[Command],
    *,
    picture: bool,
    preferred: TrackId | None = None,
) -> Track:
    """``[start, end)`` が空いているレイヤー 無ければ足すコマンドを積んで返す

    ``preferred``（落とした所・右クリックした所）が空いていて映る（鳴る）なら、重なり順を
    問わずそこへ置く 本人が選んだ所から外すと、どこへ入ったのかを探すことになる

    絵を描く物（``picture``）は、その範囲で絵を描いている一番手前のトラックより手前から
    探す 見るのは映る（ミュートとソロで残る）トラックだけで、置く先も映るレイヤーに限る
    ミュートしたレイヤーへ置くと、置いたのにプレビューにも書き出しにも出ない
    音だけの物は重なりに加わらないので、聞こえるレイヤーのうち奥（レイヤー 1）から探す
    """
    timeline = project.timeline
    created = _created(commands)
    ordered = [*timeline.tracks, *created]

    def free(track: Track) -> bool:
        return not track.locked and not any(clip.overlaps(start, end) for clip in track.clips)

    # 作ったばかりのトラックはソロを引き継いでいるので、映る側に数える
    active = {t.id for t in created} | {t.id for t in active_layers(project, picture=picture)}

    if preferred is not None:
        wanted = next((t for t in ordered if t.id == preferred), None)
        # 選んだ所でも、ミュートやソロの外で映らない・鳴らないレイヤーには置かない
        # 置くと、置いた直後からプレビューにも書き出しにも出ない ほかの空きへ回す
        if (
            wanted is not None
            and wanted.kind is TrackKind.MIXED
            and wanted.id in active
            and free(wanted)
        ):
            return wanted

    top = -1
    if picture:
        # 置く先はレイヤーに限るが、隠す側は映像トラックも数える 方式を切り替えた作品では
        # 映像トラックがレイヤーより手前にあることがあり、数えないと置いた物がその奥に入る
        drawn = {t.id for t in timeline.active_picture_tracks()}
        top = max(
            (
                index
                for index, track in enumerate(timeline.tracks)
                if track.id in drawn
                and any(
                    clip.overlaps(start, end) and project.draws_picture(track, clip)
                    for clip in track.clips
                )
            ),
            default=-1,
        )
    for index, track in enumerate(ordered):
        if index > top and track.kind is TrackKind.MIXED and track.id in active and free(track):
            return track
    return new_layer(project, commands, picture=picture)


def layer_after(
    project: Project, after: Track, start: int, end: int, commands: list[Command]
) -> Track:
    """``after`` より後ろ（番号の大きい側 画面では下）で ``[start, end)`` が空いた、鳴るレイヤー

    無ければ並びの末尾へ足すコマンドを積んで返す 音声ストリームが何本もある素材の 2 本目
    以降の音を置くためのもの（利用者の要望 レイヤー 1 に映像、レイヤー 2 以降に音）

    奥（レイヤー 1）から空きを探す :func:`free_layer` を使うと、絵より奥の空いたレイヤーへ
    音が入り、映像の次のレイヤーから音が並ぶ形にならない 埋まったレイヤーは飛ばして次を見る
    間へ新しいレイヤーを差し込むと、後ろのレイヤーの名前（番号）と並びが食い違う

    ``commands`` の中で先に置いたクリップも埋まりに数える まだ当てていないので、
    トラックのクリップだけを見ると、同じ新しいレイヤーへ 2 本目の音も入れて重なる
    """
    created = _created(commands)
    ordered = [*project.timeline.tracks, *created]
    pending = {
        c.track_id for c in commands if isinstance(c, AddClip) and c.clip.overlaps(start, end)
    }
    # 作ったばかりのトラックはソロを引き継いでいるので、鳴る側に数える
    heard = {t.id for t in created} | {t.id for t in active_layers(project, picture=False)}
    index = next((i for i, t in enumerate(ordered) if t.id == after.id), len(ordered))
    for track in ordered[index + 1 :]:
        if (
            track.kind is TrackKind.MIXED
            and track.id in heard
            and track.id not in pending
            and not track.locked
            and not any(clip.overlaps(start, end) for clip in track.clips)
        ):
            return track
    return new_layer(project, commands, picture=False)


def media_placements(
    project: Project,
    picture: Clip | None,
    sound: Clip | None,
    more_sounds: Sequence[Clip] = (),
) -> list[tuple[TrackKind, Clip]]:
    """素材の絵のクリップと音のクリップを、方式に合わせて置く種類とクリップの組にする

    分ける方式は、絵を映像トラックへ、音を音声トラックへの 2 本（リンクは置く側が付けた
    まま） 混合の方式は、1 本のクリップにまとめて混合トラックへ置く
    まとめるとき、絵のクリップのストリームを絵に、音のクリップのストリームを
    :attr:`Clip.audio_stream` に持ち、エフェクトは絵の物・音の物の順に並べる
    置く側が絵と音のそれぞれに付けたエフェクト（固定の項目）を、ここで選び直さずに
    そのまま持ち越すため 絵の側（反転 → 配置）の後ろに音の側（音量 → フェード）が
    並ぶので、:func:`~sashimono.core.commands.fixed.with_fixed_items` に絵と音の両方を
    頼んだときと同じ並びになる 大きさの決め方（:attr:`Clip.native_size`）なども絵の
    クリップのまま持つ リンクは外す 1 本しか無いのに組を残すと、あとで別の素材と
    誤って連動する余地を残す

    ``more_sounds`` は 2 本目以降の音声ストリームの音のクリップ（音声が何本もある素材）
    分ける方式ではそれぞれを音声トラックへ、混合の方式では 1 本にまとめず、絵だけの
    クリップと音ごとの音だけのクリップに分ける どちらも置く側が付けたリンクのまま
    （絵と音がすべて一緒に動くため） 返す並びは絵・1 本目の音・2 本目の音… の順で、
    置く側はこの順に次のレイヤーへ並べる
    """
    sounds = [s for s in (sound, *more_sounds) if s is not None]
    if not places_mixed(project):
        placements: list[tuple[TrackKind, Clip]] = []
        if picture is not None:
            placements.append((TrackKind.VIDEO, picture))
        placements.extend((TrackKind.AUDIO, s) for s in sounds)
        return placements
    if len(sounds) > 1:
        return _split_on_layers(picture, sounds)
    base = picture if picture is not None else sound
    if base is None:
        return []
    merged = replace(
        base,
        link_group=None,
        audio_stream=sound.stream_index if sound is not None else None,
        show_picture=picture is not None,
        effects=(
            *(picture.effects if picture is not None else ()),
            *(sound.effects if sound is not None else ()),
        ),
    )
    return [(TrackKind.MIXED, merged)]


def _split_on_layers(picture: Clip | None, sounds: list[Clip]) -> list[tuple[TrackKind, Clip]]:
    """絵だけのクリップと、音ごとの音だけのクリップ（どれも混合トラックへ置く形）

    音だけのクリップは、音声トラックのクリップをレイヤーへ移したときと同じ形にする
    （:func:`~sashimono.core.commands.edit._carried_across` 絵の番号は映像ストリーム、
    鳴らす番号は :attr:`Clip.audio_stream`、絵は出さない） 絵の番号を音の番号のままに
    すると、あとで絵を出したときに映像ではない番号でデコーダを開く
    """
    placements: list[tuple[TrackKind, Clip]] = []
    if picture is not None:
        # 絵のクリップに音を持たせると、1 本目の音が絵と音のレイヤーの 2 か所で鳴る
        placements.append((TrackKind.MIXED, replace(picture, audio_stream=None, show_picture=True)))
    for sound in sounds:
        placements.append(
            (
                TrackKind.MIXED,
                replace(
                    sound,
                    stream_index=picture.stream_index
                    if picture is not None
                    else sound.stream_index,
                    audio_stream=sound.stream_index,
                    show_picture=False,
                ),
            )
        )
    return placements
