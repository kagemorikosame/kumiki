"""プロジェクトへの変更を表すコマンドと、その履歴

UI からの操作も AI エージェントからの操作も、必ずここを通る 同じ入口にすることで
Undo の対象から漏れる変更が存在しなくなり、AI の一連の操作をまとめて取り消す
チェックポイントも自然に成立する
"""

from kumiki.core.commands.base import Command
from kumiki.core.commands.edit import (
    AddClip,
    AddMedia,
    AddTrack,
    MoveClip,
    RemoveClip,
    RemoveMedia,
    RemoveTrack,
    RenameProject,
    RippleCut,
    SetTranscript,
    SplitClip,
    TrimClip,
)
from kumiki.core.commands.effects import (
    AddEffect,
    ClearKeyframes,
    MoveEffect,
    MoveKeyframe,
    ParamPath,
    ParamTarget,
    RemoveEffect,
    RemoveKeyframe,
    SetClipProperty,
    SetEffectEnabled,
    SetKeyframe,
    SetParam,
    SetSource,
    resolve_param,
)
from kumiki.core.commands.history import Document, HistoryEntry
from kumiki.core.commands.insert import (
    DEFAULT_GENERATED_FRAMES,
    DEFAULT_STILL_FRAMES,
    insert_generated,
    insert_media,
)
from kumiki.core.commands.subtitle import (
    AddSegment,
    MergeWithNext,
    RemoveSegment,
    RetimeSegment,
    SetSegmentText,
    SplitSegment,
    burn_subtitles,
)

__all__ = [
    "DEFAULT_GENERATED_FRAMES",
    "DEFAULT_STILL_FRAMES",
    "AddClip",
    "AddEffect",
    "AddMedia",
    "AddSegment",
    "AddTrack",
    "ClearKeyframes",
    "Command",
    "Document",
    "HistoryEntry",
    "MergeWithNext",
    "MoveClip",
    "MoveEffect",
    "MoveKeyframe",
    "ParamPath",
    "ParamTarget",
    "RemoveClip",
    "RemoveEffect",
    "RemoveKeyframe",
    "RemoveMedia",
    "RemoveSegment",
    "RemoveTrack",
    "RenameProject",
    "RetimeSegment",
    "RippleCut",
    "SetClipProperty",
    "SetEffectEnabled",
    "SetKeyframe",
    "SetParam",
    "SetSegmentText",
    "SetSource",
    "SetTranscript",
    "SplitClip",
    "SplitSegment",
    "TrimClip",
    "burn_subtitles",
    "insert_generated",
    "insert_media",
    "resolve_param",
]
