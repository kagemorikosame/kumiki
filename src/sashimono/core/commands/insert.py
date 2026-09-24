"""素材をタイムラインへ入れる、という一連の操作

「読み込んで置く」は 1 つのコマンドではなく、素材の登録・トラックの用意・
映像と音声のクリップ配置・両者のリンク、の組み合わせになる UI からも AI からも
同じ手順を踏みたいので、ここに置いてコマンドの列として返す

コマンドを返すだけで実行はしない 呼び出し側がチェックポイントで括れば、
まとめて 1 回の Undo で戻せる
"""

from __future__ import annotations

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
    TrackKind,
    new_group_id,
)
from sashimono.core.timebase import Rounding, seconds_to_frame

__all__ = [
    "DEFAULT_GENERATED_FRAMES",
    "DEFAULT_STILL_FRAMES",
    "insert_filter",
    "insert_generated",
    "insert_media",
]

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
    commands: list[Command] = []
    if project.find_media(media.id) is None:
        commands.append(AddMedia(media))

    duration = _timeline_duration(project, media)
    if duration <= 0:
        return commands

    start = project.duration if at_frame is None else max(0, at_frame)
    # 映像と音声の両方があるときだけリンクする 1 本しかないのにグループを
    # 付けると、あとで別の素材と誤って連動する余地を残すことになる
    group = new_group_id() if media.has_video and media.has_audio else None

    if media.has_video or media.is_still:
        track = _find_or_create(project, TrackKind.VIDEO, commands)
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
        track = _find_or_create(project, TrackKind.AUDIO, commands)
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


def insert_filter(
    project: Project,
    *,
    at_frame: int | None = None,
    duration: int = DEFAULT_GENERATED_FRAMES,
    effects: tuple[Effect, ...] = (),
) -> list[Command]:
    """フィルタのクリップ（:data:`~sashimono.core.model.FILTER_KIND`）を置く

    フィルタはそれより**下**のトラックにしか効かない テキストや図形と同じく下から
    空きを探すと、範囲にある絵より下へ入り、置いたのに何も変わらないことがある
    範囲に絵のある一番上のトラックより上で空いているトラックを使い、無ければ一番上に作る

    見るのは描かれるトラック（:meth:`~sashimono.core.model.Timeline.active_tracks`）だけ
    ミュートしたトラックやソロの外のトラックへ置くと、置いたのにプレビューにも書き出しにも
    効かない 絵の有無も、描かれないトラックの物は数えない（見えない絵より上に置く理由が無い）
    ソロで絞っている間に新しく作るトラックは、ソロを付けて作る 付けないと作った所で外れる
    """
    commands: list[Command] = []
    start = project.duration if at_frame is None else max(0, at_frame)
    end = start + duration
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
        track = Track(kind=TrackKind.VIDEO, name=f"V{len(video) + 1}", solo=soloed)
        # 末尾へ足す 映像トラックの重ね順は並びの順なので、末尾が一番上になる
        commands.append(AddTrack(track))
    commands.append(
        AddClip(
            track.id,
            Clip(
                timeline_start=start,
                duration=duration,
                source=GeneratedSource(kind=FILTER_KIND),
                effects=effects,
            ),
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
