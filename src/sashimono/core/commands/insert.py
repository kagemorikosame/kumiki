"""素材をタイムラインへ入れる、という一連の操作

「読み込んで置く」は 1 つのコマンドではなく、素材の登録・トラックの用意・
映像と音声のクリップ配置・両者のリンク、の組み合わせになる UI からも AI からも
同じ手順を踏みたいので、ここに置いてコマンドの列として返す

コマンドを返すだけで実行はしない 呼び出し側がチェックポイントで括れば、
まとめて 1 回の Undo で戻せる
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from fractions import Fraction

from sashimono.core.commands.base import Command
from sashimono.core.commands.edit import AddClip, AddMedia, AddTrack
from sashimono.core.model import (
    FILTER_KIND,
    Clip,
    Effect,
    GeneratedSource,
    MediaItem,
    Project,
    Track,
    TrackId,
    TrackKind,
    new_group_id,
)
from sashimono.core.timebase import Rounding, seconds_to_frame

__all__ = [
    "DEFAULT_GENERATED_FRAMES",
    "DEFAULT_STILL_FRAMES",
    "EFFECT_TRACK_PREFIX",
    "insert_clip",
    "insert_filter",
    "insert_generated",
    "insert_media",
    "is_effect_track",
    "new_track",
    "place_media",
]

#: 置く先のトラックを選ぶ関数 （種類, 置く長さ, 積んでいるコマンド）→ トラック
_TrackPicker = Callable[[TrackKind, int, list[Command]], Track]

#: 静止画をタイムラインへ置くときの既定の長さ（フレーム）
#: 30fps で 5 秒 Premiere の既定と同じくらい
DEFAULT_STILL_FRAMES = 150

#: テキストや図形を置くときの既定の長さ（フレーム） 30fps で 5 秒
DEFAULT_GENERATED_FRAMES = 150

#: エフェクトトラック（:func:`new_track`）の名前の頭
EFFECT_TRACK_PREFIX = "FX"

_EFFECT_TRACK_NAME = re.compile(rf"{EFFECT_TRACK_PREFIX}\d+")


def insert_media(
    project: Project, media: MediaItem, *, at_frame: int | None = None
) -> list[Command]:
    """素材をメディアプールへ入れ、タイムラインの末尾（または指定位置）へ置く

    映像と音声を持つ素材は、別々のトラックへ展開して同じリンクグループに入れる
    片方を動かせばもう片方も追従し、分割も同時に行われる
    """
    start = project.duration if at_frame is None else max(0, at_frame)
    return _place(
        project,
        media,
        start,
        lambda kind, _duration, commands: _find_or_create(project, kind, commands),
    )


def place_media(
    project: Project,
    media: Sequence[MediaItem],
    *,
    at_frame: int,
    track_id: TrackId | None = None,
) -> list[Command]:
    """素材を、落とした位置（フレームとトラック）から順に並べて置く

    タイムラインへのドラッグ＆ドロップのためのもの 何本かを落としたら、
    落とした位置から隙間なく後ろへ並べる（読み込みの並び順と同じ）

    トラックは、落としたトラックが種類に合って空いていればそこへ置く 合わない・
    ロックしている・その範囲に別のクリップがいるときは、同じ種類で空いている
    トラックへ回し、それも無ければ新しく作る 落とした所へ無理に置くと、
    重なりで断られて何も置かれない
    映像と音声を持つ素材は、落とした側の種類だけがそのトラックへ入り、
    もう片方は合う種類の空いたトラックへ入る
    """
    commands: list[Command] = []
    cursor = max(0, at_frame)
    for item in media:
        placed = _place(project, item, cursor, _free_picker(project, cursor, track_id))
        for command in placed:
            project = command.apply(project)
        commands.extend(placed)
        ends = [c.clip.timeline_end for c in placed if isinstance(c, AddClip)]
        cursor = max([cursor, *ends])
    return commands


def _place(project: Project, media: MediaItem, start: int, pick: _TrackPicker) -> list[Command]:
    """素材を 1 本、``start`` から置くコマンド トラックは ``pick`` が決める"""
    commands: list[Command] = []
    if project.find_media(media.id) is None:
        commands.append(AddMedia(media))

    duration = _timeline_duration(project, media)
    if duration <= 0:
        return commands

    # 映像と音声の両方があるときだけリンクする 1 本しかないのにグループを
    # 付けると、あとで別の素材と誤って連動する余地を残すことになる
    group = new_group_id() if media.has_video and media.has_audio else None

    if media.has_video or media.is_still:
        track = pick(TrackKind.VIDEO, duration, commands)
        commands.append(
            AddClip(
                track.id,
                Clip(
                    timeline_start=start,
                    duration=duration,
                    media_id=media.id,
                    stream_index=media.video_streams[0].index if media.has_video else 0,
                    link_group=group,
                ),
            )
        )

    if media.has_audio:
        track = pick(TrackKind.AUDIO, duration, commands)
        commands.append(
            AddClip(
                track.id,
                Clip(
                    timeline_start=start,
                    duration=duration,
                    media_id=media.id,
                    stream_index=media.audio_streams[0].index,
                    link_group=group,
                ),
            )
        )

    return commands


def _timeline_duration(project: Project, media: MediaItem) -> int:
    """素材をタイムラインへ置いたときの長さ（フレーム）"""
    if media.is_still:
        return DEFAULT_STILL_FRAMES
    if media.duration <= 0:
        return 0
    # 切り上げる 切り捨てると素材の末尾が 1 フレーム欠ける
    return max(1, seconds_to_frame(Fraction(media.duration), project.rate, Rounding.CEIL))


def _find_or_create(project: Project, kind: TrackKind, commands: list[Command]) -> Track:
    """その種類のトラックを探し、無ければ作るコマンドを積んで返す

    すでに ``commands`` の中で作ったトラックも対象にする 映像と音声を続けて
    置くときに、同じ種類のトラックを 2 本作ってしまわないように
    """
    for command in commands:
        if isinstance(command, AddTrack) and command.track.kind is kind:
            return command.track

    existing = [t for t in project.timeline.tracks if t.kind is kind and not t.locked]
    if existing:
        return existing[0]

    return _new_track(project, kind, commands)


def _free_picker(project: Project, start: int, preferred: TrackId | None) -> _TrackPicker:
    def pick(kind: TrackKind, duration: int, commands: list[Command]) -> Track:
        return _free_or_create(project, kind, start, duration, commands, preferred)

    return pick


def _free_or_create(
    project: Project,
    kind: TrackKind,
    start: int,
    duration: int,
    commands: list[Command],
    preferred: TrackId | None,
) -> Track:
    """``[start, start + duration)`` が空いている ``kind`` のトラック 無ければ作る

    落としたトラック（``preferred``）を先に見る 並びの順だけで探すと、
    V3 へ落としたのに空いている V1 へ入り、落とした所と違う所に出る
    """
    for command in commands:
        if isinstance(command, AddTrack) and command.track.kind is kind:
            return command.track

    candidates = [t for t in project.timeline.tracks if t.kind is kind and not t.locked]
    candidates.sort(key=lambda track: track.id != preferred)
    for track in candidates:
        if not any(clip.overlaps(start, start + duration) for clip in track.clips):
            return track
    return _new_track(project, kind, commands)


def _new_track(project: Project, kind: TrackKind, commands: list[Command]) -> Track:
    prefix = "V" if kind is TrackKind.VIDEO else "A"
    index = sum(1 for t in project.timeline.tracks if t.kind is kind) + 1
    track = Track(kind=kind, name=_unused_name(project, prefix, index))
    commands.append(AddTrack(track))
    return track


def new_track(project: Project, kind: TrackKind, *, effect: bool = False) -> AddTrack:
    """空のトラックを 1 本足すコマンド 映像は一番上、音声は一番下へ入る

    トラックの並びの末尾へ足す 映像は並びの後ろほど手前（上）に重なり、音声は後ろほど
    下に並ぶ（:meth:`~sashimono.ui.timeline.layout.TimelineLayout.bands`） 読み込みや
    フィルタが新しく作るトラックと同じ所なので、足したトラックの位置が操作ごとに変わらない

    ``effect`` はフィルタを置くための映像トラック（エフェクトトラック） 形式は変えず、
    名前（``FX1`` など）で見分ける 映像トラックと別の種類にすると、読み込み・書き出し・
    互換の読み込みまですべてが新しい種類を知る必要があり、古い版で開けなくなる
    一番上に入るので、置いたフィルタがほかの映像トラックすべてに掛かる

    ソロで絞っている種類なら、足すトラックにもソロを付ける 付けないと足した所で外れ、
    置いたクリップが出ない（:func:`_filter_track` と同じ決まり）
    """
    if effect and kind is not TrackKind.VIDEO:
        raise ValueError("エフェクトトラックは映像トラックとして作る")
    same = [t for t in project.timeline.tracks if t.kind is kind]
    if effect:
        name = _unused_name(
            project, EFFECT_TRACK_PREFIX, sum(1 for t in same if is_effect_track(t)) + 1
        )
    else:
        name = _unused_name(project, "V" if kind is TrackKind.VIDEO else "A", len(same) + 1)
    soloed = any(t.solo and not t.muted for t in same)
    return AddTrack(Track(kind=kind, name=name, solo=soloed))


def _unused_name(project: Project, prefix: str, number: int) -> str:
    """``prefix`` と番号の、まだ使われていないトラック名 ``number`` から数える

    消したトラックの名前が残っていると、本数を数えただけでは同じ名前が 2 本並ぶ
    （V2 を消して V1 と V3 が残ると、もう 1 本の V3 ができる） 空いている番号まで進める
    """
    names = {t.name for t in project.timeline.tracks}
    while f"{prefix}{number}" in names:
        number += 1
    return f"{prefix}{number}"


def is_effect_track(track: Track) -> bool:
    """フィルタを置くために足した映像トラックか（名前が ``FX`` と番号）"""
    return track.kind is TrackKind.VIDEO and _EFFECT_TRACK_NAME.fullmatch(track.name) is not None


def insert_generated(
    project: Project,
    source: GeneratedSource,
    *,
    at_frame: int | None = None,
    duration: int = DEFAULT_GENERATED_FRAMES,
    track_id: TrackId | None = None,
) -> list[Command]:
    """テキストや図形をタイムラインへ置く

    素材を持たないので、置く先は必ず映像トラック 既存のクリップと重ならない
    よう、指定位置に空きが無ければ新しいトラックを作る テロップは元の映像に
    重ねたいのが普通で、既存クリップを避けて後ろへ並べるのは意図と違う

    ``track_id`` は置きたいトラック（右クリックした所） 置けなければいつもの決まりで選ぶ
    """
    return insert_clip(
        project,
        Clip(timeline_start=0, duration=duration, source=source),
        at_frame=at_frame,
        track_id=track_id,
    )


def insert_filter(
    project: Project,
    *,
    at_frame: int | None = None,
    duration: int = DEFAULT_GENERATED_FRAMES,
    effects: tuple[Effect, ...] = (),
    track_id: TrackId | None = None,
) -> list[Command]:
    """フィルタのクリップ（:data:`~sashimono.core.model.FILTER_KIND`）を置く

    置き先の決め方は :func:`_filter_track` ``track_id`` は :func:`insert_generated` と同じ
    """
    return insert_clip(
        project,
        Clip(
            timeline_start=0,
            duration=duration,
            source=GeneratedSource(kind=FILTER_KIND),
            effects=effects,
        ),
        at_frame=at_frame,
        track_id=track_id,
    )


def insert_clip(
    project: Project,
    clip: Clip,
    *,
    at_frame: int | None = None,
    track_id: TrackId | None = None,
) -> list[Command]:
    """素材を持たないクリップ（生成オブジェクト・フィルタ・シーン）を、中身のまま置く

    位置だけを ``at_frame``（省けば末尾）へ移す 長さ・エフェクト・不透明度はそのまま
    保存したエイリアスも、テキストやフィルタと同じ決まりで置き先が決まる

    ``track_id`` を渡すと、そのトラックが映像でロックされておらず、範囲が空いていれば
    そこへ置く 右クリックした所へ置かないと、どのトラックに入ったのかを探すことになる
    置けないとき（音声のトラック・埋まっている所）は断らず、いつもの決まりで選ぶ
    """
    commands: list[Command] = []
    start = project.duration if at_frame is None else max(0, at_frame)
    placed = replace(clip, timeline_start=start)
    track = _wanted_track(project, track_id, start, placed.timeline_end)
    if track is None:
        track = (
            _filter_track(project, start, placed.timeline_end, commands)
            if placed.is_filter
            else _free_video_track(project, start, placed.duration, commands)
        )
    commands.append(AddClip(track.id, placed))
    return commands


def _wanted_track(project: Project, track_id: TrackId | None, start: int, end: int) -> Track | None:
    """頼まれたトラックへ ``[start, end)`` を置けるならそのトラック 置けなければ ``None``"""
    if track_id is None:
        return None
    track = project.timeline.find_track(track_id)
    if track is None or track.kind is not TrackKind.VIDEO or track.locked:
        return None
    if any(clip.overlaps(start, end) for clip in track.clips):
        return None
    return track


def _filter_track(project: Project, start: int, end: int, commands: list[Command]) -> Track:
    """フィルタを置くトラック 無ければ作るコマンドを ``commands`` へ積んで返す

    フィルタはそれより**下**のトラックにしか効かない テキストや図形と同じく下から
    空きを探すと、範囲にある絵より下へ入り、置いたのに何も変わらないことがある
    範囲に絵のある一番上のトラックより上で空いているトラックを使い、無ければ一番上に作る

    見るのは描かれるトラック（:meth:`~sashimono.core.model.Timeline.active_tracks`）だけ
    ミュートしたトラックやソロの外のトラックへ置くと、置いたのにプレビューにも書き出しにも
    効かない 絵の有無も、描かれないトラックの物は数えない（見えない絵より上に置く理由が無い）
    ソロで絞っている間に新しく作るトラックは、ソロを付けて作る 付けないと作った所で外れる
    """
    timeline = project.timeline
    video = list(timeline.video_tracks())
    drawn = {track.id for track in timeline.active_tracks(TrackKind.VIDEO)}
    top = max(
        (
            index
            for index, track in enumerate(video)
            if track.id in drawn and any(clip.overlaps(start, end) for clip in track.clips)
        ),
        default=-1,
    )
    track = next(
        (
            candidate
            for candidate in video[top + 1 :]
            if candidate.id in drawn
            and not candidate.locked
            and not any(clip.overlaps(start, end) for clip in candidate.clips)
        ),
        None,
    )
    if track is None:
        soloed = any(t.solo and not t.muted for t in video)
        name = _unused_name(project, "V", len(video) + 1)
        track = Track(kind=TrackKind.VIDEO, name=name, solo=soloed)
        # 末尾へ足す 映像トラックの重ね順は並びの順なので、末尾が一番上になる
        commands.append(AddTrack(track))
    return track


def _free_video_track(
    project: Project, start: int, duration: int, commands: list[Command]
) -> Track:
    """``[start, start + duration)`` が空いている映像トラックを探す 無ければ作る"""
    for track in project.timeline.video_tracks():
        if track.locked:
            continue
        if not any(clip.overlaps(start, start + duration) for clip in track.clips):
            return track

    index = sum(1 for t in project.timeline.tracks if t.kind is TrackKind.VIDEO) + 1
    track = Track(kind=TrackKind.VIDEO, name=_unused_name(project, "V", index))
    commands.append(AddTrack(track))
    return track
