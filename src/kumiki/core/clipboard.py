"""クリップのコピー・切り取り・貼り付け

Qt のクリップボードは使わない 中身は ID とリンクを持ったモデルのままで、文字や
画像にしてから戻すと、リンクや素材の参照を作り直すことになる アプリの中だけで
行き来する

どれもコマンドの列を返すだけで、実行はしない 呼び出し側がチェックポイントで
括れば、貼り付けは何本でも 1 回の Undo で戻る
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from kumiki.core.commands import AddClip, AddTrack, Command, RemoveClip
from kumiki.core.model import (
    Clip,
    ClipId,
    GroupId,
    Project,
    Track,
    TrackId,
    TrackKind,
    new_clip_id,
    new_group_id,
)

__all__ = ["ClipboardContent", "CopiedClip", "copy_clips", "cut_commands", "paste_commands"]


@dataclass(frozen=True, slots=True)
class CopiedClip:
    """コピーしたクリップと、元にいたトラック"""

    track_id: TrackId
    kind: TrackKind
    clip: Clip


@dataclass(frozen=True, slots=True)
class ClipboardContent:
    """コピーした中身 開始位置の順に並ぶ"""

    clips: tuple[CopiedClip, ...]

    @property
    def origin(self) -> int:
        """いちばん早い開始位置 貼り付けではここを再生ヘッドに合わせる"""
        return min(copied.clip.timeline_start for copied in self.clips)


def copy_clips(project: Project, clip_ids: Iterable[ClipId]) -> ClipboardContent:
    """クリップをコピーする リンクした相手（映像なら音声）も一緒に入れる

    相手を置いていくと、貼り付けた映像に音が付いてこない 画面で選んでいるのは
    片方だけでも、編集の単位は組のほう
    """
    found: dict[ClipId, CopiedClip] = {}
    timeline = project.timeline
    for clip_id in clip_ids:
        located = timeline.locate_clip(clip_id)
        if located is None:
            continue
        track, clip = located
        members = (
            list(timeline.linked_clips(clip.link_group))
            if clip.link_group is not None
            else [(track, clip)]
        )
        for member_track, member in members:
            found.setdefault(member.id, CopiedClip(member_track.id, member_track.kind, member))
    ordered = sorted(found.values(), key=lambda copied: copied.clip.timeline_start)
    return ClipboardContent(tuple(ordered))


def cut_commands(project: Project, content: ClipboardContent) -> list[Command]:
    """コピーしたものを消すコマンド 詰めはしない（隙間はそのまま残す）

    リンクした組は :class:`RemoveClip` 1 つで両方消える 組の両方に出すと、
    2 つ目が「見つからない」で失敗する
    """
    commands: list[Command] = []
    groups: set[GroupId] = set()
    for copied in content.clips:
        clip = copied.clip
        if project.timeline.locate_clip(clip.id) is None:
            continue
        if clip.link_group is not None:
            if clip.link_group in groups:
                continue
            groups.add(clip.link_group)
        commands.append(RemoveClip(clip.id))
    return commands


def paste_commands(project: Project, content: ClipboardContent, at_frame: int) -> list[Command]:
    """``at_frame`` を先頭にして貼り付けるコマンド

    並びの間隔はコピーしたときのまま 行き先は元のトラックを優先し、塞がって
    いれば同じ種類の別のトラック、それも無ければ新しいトラックを作る 重ねて置く
    ことはできない（:class:`Track` はクリップの重なりを許さない）

    リンクは新しいグループに付け替える 元のままだと、貼ったものと元のものが
    同じ組になり、片方を動かすともう片方まで動く
    """
    if not content.clips:
        return []
    offset = max(0, at_frame) - content.origin
    groups: dict[GroupId, GroupId] = {}
    #: 束ね（グループ）も新しく付け替える 元のままだと、貼ったものを選ぶと元の
    #: クリップまで一緒に選ばれて動く
    bundles: dict[GroupId, GroupId] = {}
    taken: dict[TrackId, list[tuple[int, int]]] = {}
    commands: list[Command] = []

    for copied in content.clips:
        clip = copied.clip
        if clip.media_id is not None and project.find_media(clip.media_id) is None:
            raise ValueError("コピーした素材がプロジェクトから外されているので貼り付けられない")
        start = clip.timeline_start + offset
        end = start + clip.duration
        track = _landing_track(project, copied, start, end, taken, commands)
        taken.setdefault(track.id, []).append((start, end))
        group = (
            groups.setdefault(clip.link_group, new_group_id())
            if clip.link_group is not None
            else None
        )
        bundle = (
            bundles.setdefault(clip.group_id, new_group_id()) if clip.group_id is not None else None
        )
        pasted = replace(
            clip, id=new_clip_id(), timeline_start=start, link_group=group, group_id=bundle
        )
        commands.append(AddClip(track.id, pasted))
    return commands


def _landing_track(
    project: Project,
    copied: CopiedClip,
    start: int,
    end: int,
    taken: dict[TrackId, list[tuple[int, int]]],
    commands: list[Command],
) -> Track:
    """``[start, end)`` が空いている行き先 この貼り付けで先に置いた分も数える"""
    created = [c.track for c in commands if isinstance(c, AddTrack)]
    same_kind = [t for t in (*project.timeline.tracks, *created) if t.kind is copied.kind]
    ordered = sorted(same_kind, key=lambda track: track.id != copied.track_id)

    for track in ordered:
        if track.locked:
            continue
        busy = any(clip.overlaps(start, end) for clip in track.clips) or any(
            s < end and start < e for s, e in taken.get(track.id, [])
        )
        if not busy:
            return track

    prefix = "V" if copied.kind is TrackKind.VIDEO else "A"
    track = Track(kind=copied.kind, name=f"{prefix}{len(same_kind) + 1}")
    commands.append(AddTrack(track))
    return track
