"""字幕起こし バックエンド抽象・実行環境の導入・整形

字幕は素材に紐付き、タイムライン上の位置は投影で決まる
（:mod:`kumiki.core.projection`） ここが返すのはソース時刻だけで、
タイムラインのことは何も知らない
"""

from kumiki.asr.backend import (
    MODELS,
    AsrError,
    ModelInfo,
    Progress,
    ShouldCancel,
    TranscribeOptions,
    TranscriptionBackend,
)
from kumiki.asr.cleanup import (
    DEFAULT_FILLERS,
    EXTRA_FILLERS,
    CleanupOptions,
    clean_text,
    clean_transcript,
    wrap_text,
)
from kumiki.asr.environment import (
    ASR_PACK,
    PackStatus,
    activate_runtime,
    install_command,
    install_runtime,
    runtime_status,
)
from kumiki.asr.service import Job, JobEvent, JobKind, TranscriptionService

__all__ = [
    "ASR_PACK",
    "DEFAULT_FILLERS",
    "EXTRA_FILLERS",
    "MODELS",
    "AsrError",
    "CleanupOptions",
    "Job",
    "JobEvent",
    "JobKind",
    "ModelInfo",
    "PackStatus",
    "Progress",
    "ShouldCancel",
    "TranscribeOptions",
    "TranscriptionBackend",
    "TranscriptionService",
    "activate_runtime",
    "clean_text",
    "clean_transcript",
    "install_command",
    "install_runtime",
    "runtime_status",
    "wrap_text",
]


def default_backend() -> TranscriptionBackend:
    """既定のバックエンド

    import はここで初めて行う faster-whisper 本体はさらに遅らせてあるので、
    未導入の環境でもこの関数は成功する
    """
    from kumiki.asr.whisper import FasterWhisperBackend

    return FasterWhisperBackend()
