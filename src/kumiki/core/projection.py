"""素材に紐付いた字幕を、タイムライン上の位置へ投影する

字幕はソース時刻でしか持っていない タイムライン上のどこに出るかは、その素材を
参照しているクリップごとに毎回ここで計算する

この方式にしているのは「同期を取らない」ため 字幕にタイムライン位置を持たせると、
カット・トリム・移動・速度変更・複製のすべてに追従処理が必要になり、どれか 1 つ
漏れた瞬間にずれる 位置を持たせなければ、ずれようがない
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction

from kumiki.core.model import (
    Clip,
    ClipId,
    MediaItem,
    Project,
    TrackId,
    TranscriptSegment,
)
from kumiki.core.timebase import FrameRate, Rounding, seconds_to_frame

__all__ = ["ProjectedSubtitle", "project_clip", "project_timeline"]


@dataclass(frozen=True, slots=True)
class ProjectedSubtitle:
    """タイムライン上に現れた字幕 1 枚"""

    segment: TranscriptSegment
    clip_id: ClipId
    track_id: TrackId
    #: タイムライン上の表示範囲（フレーム） ``end_frame`` は含まない
    start_frame: int
    end_frame: int
    #: クリップの端で切り詰められたか UI で「続きがある」表示に使う
    clipped_head: bool = False
    clipped_tail: bool = False

    @property
    def duration(self) -> int:
        return self.end_frame - self.start_frame


def project_clip(
    clip: Clip, media: MediaItem, rate: FrameRate, track_id: TrackId
) -> Iterator[ProjectedSubtitle]:
    """1 つのクリップに現れる字幕を返す

    クリップが使っているソース範囲に重なるセグメントだけが対象で、はみ出した分は
    クリップの端で切り詰められる
    """
    transcript = media.transcript
    if transcript is None:
        return

    source_in = clip.source_in
    source_out = clip.source_out(rate)

    for segment in transcript.overlapping(source_in, source_out):
        visible_start = max(segment.start, source_in)
        visible_end = min(segment.end, source_out)

        start_offset = _source_to_clip_frame(visible_start, clip, rate, Rounding.FLOOR)
        end_offset = _source_to_clip_frame(visible_end, clip, rate, Rounding.CEIL)

        start_offset = max(0, min(start_offset, clip.duration - 1))
        # 表示は最低 1 フレーム 丸めの結果 0 フレームになると画面に出ない
        end_offset = max(start_offset + 1, min(end_offset, clip.duration))

        yield ProjectedSubtitle(
            segment=segment,
            clip_id=clip.id,
            track_id=track_id,
            start_frame=clip.timeline_start + start_offset,
            end_frame=clip.timeline_start + end_offset,
            clipped_head=segment.start < source_in,
            clipped_tail=segment.end > source_out,
        )


def project_timeline(project: Project) -> Iterator[ProjectedSubtitle]:
    """タイムライン全体に現れる字幕を、開始位置順に返す

    同じ素材を複数回置けば、字幕もその回数だけ現れる これは意図した挙動で、
    素材を使い回したときに字幕が片方にしか出ないことの方が驚きが大きい
    """
    projected: list[ProjectedSubtitle] = []
    for track in project.timeline.tracks:
        for clip in track.clips:
            if clip.media_id is None:
                continue
            media = project.find_media(clip.media_id)
            if media is None or media.transcript is None:
                continue
            projected.extend(project_clip(clip, media, project.rate, track.id))

    projected.sort(key=lambda p: (p.start_frame, p.end_frame))
    yield from projected


def _source_to_clip_frame(
    source_time: Fraction, clip: Clip, rate: FrameRate, rounding: Rounding
) -> int:
    """ソース秒を、クリップ先頭からの相対フレームへ

    速度変更を掛けたクリップでは、ソース時間の進みとタイムライン時間の進みが
    ``clip.speed`` 倍だけ違う
    """
    elapsed = (source_time - clip.source_in) / clip.speed
    return seconds_to_frame(elapsed, rate, rounding)
