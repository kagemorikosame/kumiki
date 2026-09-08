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
from novaedit.core.commands.history import Document, HistoryEntry

__all__ = [
    "AddClip",
    "AddMedia",
    "AddTrack",
    "Command",
    "Document",
    "HistoryEntry",
    "MoveClip",
    "RemoveClip",
    "RemoveMedia",
    "RemoveTrack",
    "RenameProject",
    "SetTranscript",
    "SplitClip",
    "TrimClip",
]
