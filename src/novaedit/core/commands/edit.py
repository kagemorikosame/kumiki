"""基本的な編集コマンド。

どれも純関数で、失敗するときは例外を投げる。トラック内でクリップが重ならないことなどの
不変条件は :class:`~novaedit.core.model.Track` 側で検査されるので、ここで作った
おかしな状態はモデル構築時点で弾かれる。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from novaedit.core.commands.base import Command
from novaedit.core.model import (
    Clip,
    ClipId,
    MediaId,
    MediaItem,
    Project,
    Track,
    TrackId,
    TrackKind,
    Transcript,
    new_clip_id,
    new_group_id,
)

__all__ = [
    "AddClip",
    "AddMedia",
    "AddTrack",
    "MoveClip",
    "RemoveClip",
    "RemoveMedia",
    "RemoveTrack",
    "RenameProject",
    "SetTranscript",
    "SplitClip",
    "TrimClip",
]


@dataclass(frozen=True, slots=True)
class AddMedia(Command):
    """メディアプールに素材を追加する。"""

    item: MediaItem

    @property
    def label(self) -> str:
        return f"素材を追加: {self.item.name}"

    def apply(self, project: Project) -> Project:
        if project.find_media(self.item.id) is not None:
            raise ValueError(f"すでに登録されている素材: {self.item.id}")
        return project.with_media((*project.media, self.item))


@dataclass(frozen=True, slots=True)
class RemoveMedia(Command):
    """素材をメディアプールから外す。

    その素材を使っているクリップが 1 つでもあれば失敗する。参照だけ残して
    素材を消すと、あとから原因の分からない再生エラーになるため。
    """

    media_id: MediaId

    @property
    def label(self) -> str:
        return "素材を削除"

    def apply(self, project: Project) -> Project:
        item = project.require_media(self.media_id)
        in_use = [
            clip.id
            for track in project.timeline.tracks
            for clip in track.clips
            if clip.media_id == self.media_id
        ]
        if in_use:
            raise ValueError(f"素材 {item.name!r} はタイムラインで {len(in_use)} 箇所使われている")
        remaining = tuple(m for m in project.media if m.id != self.media_id)
        return project.with_media(remaining)


@dataclass(frozen=True, slots=True)
class SetTranscript(Command):
    """素材の字幕起こし結果を差し替える。

    字幕はトラックではなく素材に紐付くので、この 1 操作でその素材を使っている
    すべての箇所の字幕が同時に変わる。
    """

    media_id: MediaId
    transcript: Transcript | None

    @property
    def label(self) -> str:
        return "字幕を更新" if self.transcript is not None else "字幕を削除"

    def apply(self, project: Project) -> Project:
        item = project.require_media(self.media_id)
        return project.replace_media(item.with_transcript(self.transcript))


@dataclass(frozen=True, slots=True)
class AddTrack(Command):
    """トラックを追加する。``index`` が ``None`` なら末尾。"""

    track: Track
    index: int | None = None

    @property
    def label(self) -> str:
        return f"トラックを追加: {self.track.name or self.track.kind.value}"

    def apply(self, project: Project) -> Project:
        timeline = project.timeline
        if timeline.find_track(self.track.id) is not None:
            raise ValueError(f"すでに存在するトラック: {self.track.id}")
        tracks = list(timeline.tracks)
        tracks.insert(len(tracks) if self.index is None else self.index, self.track)
        return project.with_timeline(replace(timeline, tracks=tuple(tracks)))


@dataclass(frozen=True, slots=True)
class RemoveTrack(Command):
    """トラックを、載っているクリップごと削除する。"""

    track_id: TrackId

    @property
    def label(self) -> str:
        return "トラックを削除"

    def apply(self, project: Project) -> Project:
        timeline = project.timeline
        if timeline.find_track(self.track_id) is None:
            raise KeyError(f"トラックが見つからない: {self.track_id}")
        tracks = tuple(t for t in timeline.tracks if t.id != self.track_id)
        return project.with_timeline(replace(timeline, tracks=tracks))


@dataclass(frozen=True, slots=True)
class AddClip(Command):
    """トラックにクリップを置く。既存のクリップと重なる場合は失敗する。"""

    track_id: TrackId
    clip: Clip

    @property
    def label(self) -> str:
        return "クリップを追加"

    def apply(self, project: Project) -> Project:
        timeline = project.timeline
        track = _require_track(project, self.track_id)
        if track.locked:
            raise ValueError(f"トラック {track.name!r} はロックされている")
        _validate_clip_media(project, track, self.clip)
        updated = track.with_clips((*track.clips, self.clip))
        return project.with_timeline(timeline.replace_track(updated))


@dataclass(frozen=True, slots=True)
class RemoveClip(Command):
    """クリップを削除する。

    ``ripple`` が真なら、同じトラックの後続クリップを詰める。リンクされた
    映像・音声も同時に削除される。
    """

    clip_id: ClipId
    ripple: bool = False

    @property
    def label(self) -> str:
        return "クリップを削除（詰める）" if self.ripple else "クリップを削除"

    def apply(self, project: Project) -> Project:
        located = project.timeline.locate_clip(self.clip_id)
        if located is None:
            raise KeyError(f"クリップが見つからない: {self.clip_id}")
        _, clip = located

        targets = _linked_group(project, clip)
        timeline = project.timeline
        for track_id, target in targets:
            track = _require_track(project, track_id)
            remaining = [c for c in track.clips if c.id != target.id]
            if self.ripple:
                remaining = [
                    c.moved_to(c.timeline_start - target.duration)
                    if c.timeline_start >= target.timeline_end
                    else c
                    for c in remaining
                ]
            timeline = timeline.replace_track(track.with_clips(tuple(remaining)))
            project = project.with_timeline(timeline)
        return project


@dataclass(frozen=True, slots=True)
class MoveClip(Command):
    """クリップを別の位置、必要なら別のトラックへ動かす。"""

    clip_id: ClipId
    timeline_start: int
    track_id: TrackId | None = None

    @property
    def label(self) -> str:
        return "クリップを移動"

    def apply(self, project: Project) -> Project:
        located = project.timeline.locate_clip(self.clip_id)
        if located is None:
            raise KeyError(f"クリップが見つからない: {self.clip_id}")
        source_track, clip = located
        if self.timeline_start < 0:
            raise ValueError(f"開始位置が負: {self.timeline_start}")

        target_track = (
            source_track if self.track_id is None else _require_track(project, self.track_id)
        )
        if source_track.locked or target_track.locked:
            raise ValueError("ロックされたトラックのクリップは動かせない")
        _validate_clip_media(project, target_track, clip)

        timeline = project.timeline
        without = tuple(c for c in source_track.clips if c.id != clip.id)
        timeline = timeline.replace_track(source_track.with_clips(without))

        # 同一トラック内の移動では、直前の replace_track で反映済みのトラックを
        # 取り直さないと、取り除いたはずのクリップが復活する。
        destination = timeline.find_track(target_track.id)
        if destination is None:
            raise KeyError(f"トラックが見つからない: {target_track.id}")
        moved = clip.moved_to(self.timeline_start)
        timeline = timeline.replace_track(destination.with_clips((*destination.clips, moved)))
        return project.with_timeline(timeline)


@dataclass(frozen=True, slots=True)
class SplitClip(Command):
    """クリップを ``frame`` の位置で 2 つに割る。

    左側は元の ID を保ち、右側が新しい ID を得る。リンクされた映像・音声も
    同じ位置で割られるので、片方だけずれることはない。
    """

    clip_id: ClipId
    frame: int

    @property
    def label(self) -> str:
        return "クリップを分割"

    def apply(self, project: Project) -> Project:
        located = project.timeline.locate_clip(self.clip_id)
        if located is None:
            raise KeyError(f"クリップが見つからない: {self.clip_id}")
        _, clip = located
        if not (clip.timeline_start < self.frame < clip.timeline_end):
            raise ValueError(
                f"分割位置がクリップの内側にない: {self.frame} は "
                f"[{clip.timeline_start}, {clip.timeline_end}) の外"
            )

        rate = project.rate
        timeline = project.timeline
        # 右側は新しいリンクグループにする。元のままだと、分割してできた左右が
        # 同じグループに残り、片方を削除するともう片方まで消える。新しいグループを
        # 映像・音声の右側どうしで共有するので、分割後もリンクは保たれる。
        right_group = new_group_id() if clip.link_group is not None else None

        for track_id, target in _linked_group(project, clip):
            if not target.contains(self.frame):
                # リンク先の長さが違う場合。片方だけ割ると同期が崩れるので何もしない。
                continue
            track = timeline.find_track(track_id)
            if track is None:
                continue
            left_duration = self.frame - target.timeline_start
            left = replace(target, duration=left_duration)
            right = replace(
                target,
                id=new_clip_id(),
                timeline_start=self.frame,
                duration=target.timeline_end - self.frame,
                # 右側は、左側が消費したソース時間の分だけ後ろから始まる。
                source_in=target.source_in + left_duration * rate.frame_duration * target.speed,
                link_group=right_group,
            )
            others = tuple(c for c in track.clips if c.id != target.id)
            timeline = timeline.replace_track(track.with_clips((*others, left, right)))
        return project.with_timeline(timeline)


@dataclass(frozen=True, slots=True)
class TrimClip(Command):
    """クリップの端を動かす。

    ``head`` を動かすとソース範囲の開始位置も一緒にずれる（素材の中身は動かない）。
    ``tail`` は長さだけを変える。
    """

    clip_id: ClipId
    #: 先頭を動かす量（フレーム）。正で短く、負で長くなる。
    head_delta: int = 0
    #: 末尾を動かす量（フレーム）。正で長く、負で短くなる。
    tail_delta: int = 0

    @property
    def label(self) -> str:
        return "クリップをトリム"

    def apply(self, project: Project) -> Project:
        located = project.timeline.locate_clip(self.clip_id)
        if located is None:
            raise KeyError(f"クリップが見つからない: {self.clip_id}")
        track, clip = located

        duration = clip.duration - self.head_delta + self.tail_delta
        if duration <= 0:
            raise ValueError(f"トリム後の長さが 0 以下: {duration}")

        source_in = clip.source_in + self.head_delta * project.rate.frame_duration * clip.speed
        if source_in < 0:
            raise ValueError("素材の先頭より前はトリムできない")

        trimmed = replace(
            clip,
            timeline_start=clip.timeline_start + self.head_delta,
            duration=duration,
            source_in=source_in,
        )
        if trimmed.timeline_start < 0:
            raise ValueError("タイムラインの先頭より前へは動かせない")

        others = tuple(c for c in track.clips if c.id != clip.id)
        updated = track.with_clips((*others, trimmed))
        return project.with_timeline(project.timeline.replace_track(updated))


@dataclass(frozen=True, slots=True)
class RenameProject(Command):
    """プロジェクト名を変える。"""

    name: str

    @property
    def label(self) -> str:
        return "プロジェクト名を変更"

    def apply(self, project: Project) -> Project:
        return project.renamed(self.name)


def _require_track(project: Project, track_id: TrackId) -> Track:
    track = project.timeline.find_track(track_id)
    if track is None:
        raise KeyError(f"トラックが見つからない: {track_id}")
    return track


def _validate_clip_media(project: Project, track: Track, clip: Clip) -> None:
    """クリップの素材がトラックの種類に合っているかを確かめる。

    映像トラックに音声しか持たない素材を置くと、再生時に何も出ない無音の穴になる。
    置いた時点で気付ける方がよい。
    """
    if clip.media_id is None:
        return
    item = project.require_media(clip.media_id)
    if track.kind is TrackKind.VIDEO and not (item.has_video or item.is_still):
        raise ValueError(f"素材 {item.name!r} に映像が無いので映像トラックには置けない")
    if track.kind is TrackKind.AUDIO and not item.has_audio:
        raise ValueError(f"素材 {item.name!r} に音声が無いので音声トラックには置けない")


def _linked_group(project: Project, clip: Clip) -> list[tuple[TrackId, Clip]]:
    """リンクされたクリップをまとめて返す。リンクが無ければ自分だけ。"""
    if clip.link_group is None:
        located = project.timeline.locate_clip(clip.id)
        if located is None:
            return []
        track, found = located
        return [(track.id, found)]
    return [(track.id, found) for track, found in project.timeline.linked_clips(clip.link_group)]
