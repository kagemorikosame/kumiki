"""素材をタイムラインへ入れる、という一連の操作

「読み込んで置く」は 1 つのコマンドではなく、素材の登録・トラックの用意・
映像と音声のクリップ配置・両者のリンク、の組み合わせになる UI からも AI からも
同じ手順を踏みたいので、ここに置いてコマンドの列として返す

コマンドを返すだけで実行はしない 呼び出し側がチェックポイントで括れば、
まとめて 1 回の Undo で戻せる
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from fractions import Fraction

from sashimono.core.commands.base import Command
from sashimono.core.commands.edit import AddClip, AddMedia, AddTrack
from sashimono.core.model import (
    Clip,
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
    "insert_generated",
    "insert_media",
    "place_media",
]

#: 置く先のトラックを選ぶ関数 （種類, 置く長さ, 積んでいるコマンド）→ トラック
_TrackPicker = Callable[[TrackKind, int, list[Command]], Track]

#: 静止画をタイムラインへ置くときの既定の長さ（フレーム）
#: 30fps で 5 秒 Premiere の既定と同じくらい
DEFAULT_STILL_FRAMES = 150

#: テキストや図形を置くときの既定の長さ（フレーム） 30fps で 5 秒
DEFAULT_GENERATED_FRAMES = 150


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
    track = Track(kind=kind, name=f"{prefix}{index}")
    commands.append(AddTrack(track))
    return track


def insert_generated(
    project: Project,
    source: GeneratedSource,
    *,
    at_frame: int | None = None,
    duration: int = DEFAULT_GENERATED_FRAMES,
) -> list[Command]:
    """テキストや図形をタイムラインへ置く

    素材を持たないので、置く先は必ず映像トラック 既存のクリップと重ならない
    よう、指定位置に空きが無ければ新しいトラックを作る テロップは元の映像に
    重ねたいのが普通で、既存クリップを避けて後ろへ並べるのは意図と違う
    """
    commands: list[Command] = []
    start = project.duration if at_frame is None else max(0, at_frame)
    track = _free_video_track(project, start, duration, commands)
    commands.append(
        AddClip(
            track.id,
            Clip(timeline_start=start, duration=duration, source=source),
        )
    )
    return commands


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
    track = Track(kind=TrackKind.VIDEO, name=f"V{index}")
    commands.append(AddTrack(track))
    return track
