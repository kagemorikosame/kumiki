"""プロジェクトのデータモデル。

すべて frozen dataclass で、変更は :func:`dataclasses.replace` による差し替えで表現する。
木の一部だけを作り直し、残りは共有される（構造共有）ので、Undo は「古いルートを持っておく」
だけで済む。逆操作コマンドを一つずつ書く方式に比べて、取り消しの取りこぼしが原理的に起きない。

この方針の代償は、1 クリップの変更でそのトラックのクリップ列を作り直す点だが、
トラックあたり数千クリップの規模では問題にならない。
"""

from novaedit.core.model.effect import (
    AnimatedValue,
    Effect,
    Interpolation,
    Keyframe,
    ParamValue,
)
from novaedit.core.model.ids import (
    ClipId,
    EffectId,
    GroupId,
    MediaId,
    SegmentId,
    TrackId,
    new_clip_id,
    new_effect_id,
    new_group_id,
    new_media_id,
    new_segment_id,
    new_track_id,
)
from novaedit.core.model.media import AudioStreamInfo, MediaItem, VideoStreamInfo
from novaedit.core.model.project import Project, ProjectSettings
from novaedit.core.model.timeline import Clip, Marker, Timeline, Track, TrackKind
from novaedit.core.model.transcript import Transcript, TranscriptSegment, Word

__all__ = [
    "AnimatedValue",
    "AudioStreamInfo",
    "Clip",
    "ClipId",
    "Effect",
    "EffectId",
    "GroupId",
    "Interpolation",
    "Keyframe",
    "Marker",
    "MediaId",
    "MediaItem",
    "ParamValue",
    "Project",
    "ProjectSettings",
    "SegmentId",
    "Timeline",
    "Track",
    "TrackId",
    "TrackKind",
    "Transcript",
    "TranscriptSegment",
    "VideoStreamInfo",
    "Word",
    "new_clip_id",
    "new_effect_id",
    "new_group_id",
    "new_media_id",
    "new_segment_id",
    "new_track_id",
]
