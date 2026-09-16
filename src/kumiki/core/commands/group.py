"""クリップを束ねる・束ねを解く

束ねたクリップは、画面でクリック 1 回でまとめて選ばれ、一緒に動く 位置や長さは
それぞれのクリップが持ったままなので、束ねても解いても絵は変わらない
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from kumiki.core.commands.base import Command
from kumiki.core.model import ClipId, GroupId, Project, new_group_id

__all__ = ["GroupClips", "UngroupClips"]


def _set_group(project: Project, clip_ids: tuple[ClipId, ...], group: GroupId | None) -> Project:
    timeline = project.timeline
    wanted = set(clip_ids)
    found: set[ClipId] = set()
    for track in timeline.tracks:
        if not any(clip.id in wanted for clip in track.clips):
            continue
        clips = []
        for clip in track.clips:
            if clip.id in wanted:
                found.add(clip.id)
                clip = replace(clip, group_id=group)
            clips.append(clip)
        timeline = timeline.replace_track(replace(track, clips=tuple(clips)))
    missing = wanted - found
    if missing:
        raise KeyError(f"クリップが見つからない: {', '.join(sorted(missing))}")
    return project.with_timeline(timeline)


@dataclass(frozen=True, slots=True)
class GroupClips(Command):
    """2 本以上のクリップを 1 つに束ねる すでに別の束ねにいたものも、新しい束ねへ移る

    ``group_id`` を省くと、コマンドを作る時点で新しく決める ``apply`` のたびに
    作ると、同じコマンドを当て直したときに ID と、ID から決まる色が変わる
    """

    clip_ids: tuple[ClipId, ...]
    group_id: GroupId = field(default_factory=new_group_id)

    @property
    def label(self) -> str:
        return f"{len(self.clip_ids)} 本をグループ化"

    def apply(self, project: Project) -> Project:
        if len(set(self.clip_ids)) < 2:
            # 1 本だけの束ねは、選択を広げる意味が無いのに解除の手間だけが残る
            raise ValueError("グループ化には 2 本以上のクリップが要る")
        return _set_group(project, self.clip_ids, self.group_id)


@dataclass(frozen=True, slots=True)
class UngroupClips(Command):
    """束ねを解く 渡したクリップが属する束ねは、ほかのメンバーごと解く

    一部だけを外すと、残ったメンバーが 1 本だけの束ねになることがある
    """

    clip_ids: tuple[ClipId, ...]

    @property
    def label(self) -> str:
        return "グループ解除"

    def apply(self, project: Project) -> Project:
        timeline = project.timeline
        groups: set[GroupId] = set()
        for clip_id in self.clip_ids:
            located = timeline.locate_clip(clip_id)
            if located is None:
                raise KeyError(f"クリップが見つからない: {clip_id}")
            if located[1].group_id is not None:
                groups.add(located[1].group_id)
        if not groups:
            raise ValueError("グループに入っているクリップがない")
        members = tuple(
            clip.id for track in timeline.tracks for clip in track.clips if clip.group_id in groups
        )
        return _set_group(project, members, None)
