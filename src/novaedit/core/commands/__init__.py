"""プロジェクトへの変更を表すコマンドと、その履歴。

UI からの操作も AI エージェントからの操作も、必ずここを通る。同じ入口にすることで
Undo の対象から漏れる変更が存在しなくなり、AI の一連の操作をまとめて取り消す
チェックポイントも自然に成立する。
"""

from novaedit.core.commands.base import Command
from novaedit.core.commands.edit import (
    AddClip,
    AddMedia,
    AddTrack,
    MoveClip,
    RemoveClip,
    RemoveMedia,
    RemoveTrack,
    RenameProject,
    SetTranscript,
    SplitClip,
    TrimClip,
)
from novaedit.core.commands.effects import (
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
from novaedit.core.commands.history import Document, HistoryEntry
from novaedit.core.commands.insert import (
    DEFAULT_GENERATED_FRAMES,
    DEFAULT_STILL_FRAMES,
    insert_generated,
    insert_media,
)

__all__ = [
    "DEFAULT_GENERATED_FRAMES",
    "DEFAULT_STILL_FRAMES",
    "AddClip",
    "AddEffect",
    "AddMedia",
    "AddTrack",
    "ClearKeyframes",
    "Command",
    "Document",
    "HistoryEntry",
    "MoveClip",
    "MoveEffect",
    "MoveKeyframe",
    "ParamPath",
    "ParamTarget",
    "RemoveClip",
    "RemoveEffect",
    "RemoveKeyframe",
    "RemoveMedia",
    "RemoveTrack",
    "RenameProject",
    "SetClipProperty",
    "SetEffectEnabled",
    "SetKeyframe",
    "SetParam",
    "SetSource",
    "SetTranscript",
    "SplitClip",
    "TrimClip",
    "insert_generated",
    "insert_media",
    "resolve_param",
]
