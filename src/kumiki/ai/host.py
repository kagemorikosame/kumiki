"""AI 層が編集ソフトに求めること

AI の操作は最終的に UI と同じ :class:`~kumiki.core.commands.Document` を通る
ただし AI 層はウィジェットを知らないでいたいので、必要な操作だけをここに列挙して
おき、:class:`~kumiki.ui.main_window.MainWindow` がそれを満たす

この分離のおかげで、テストではウィジェットを一切作らずに、偽のホストへ差し替えて
ツールの挙動を確かめられる
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from kumiki.core.commands import Command, Document
from kumiki.core.model import ClipId, MediaId, MediaItem
from kumiki.engine.audio.waveform import Waveform

__all__ = ["EditorHost", "ToolError"]


class ToolError(RuntimeError):
    """ツールの実行に失敗した

    この例外の文面はそのまま Claude へ返る 「どうすれば直るか」が分かる書き方を
    すること Claude は次の手をこの文面だけで決める
    """


class EditorHost(Protocol):
    """AI からの操作を受け付ける編集ソフト側の窓口

    ここのメソッドは**すべて UI スレッドで呼ばれる**（:mod:`kumiki.ai.bridge` が
    そう仕向ける） 実装側でスレッドを気にする必要はない
    """

    @property
    def document(self) -> Document:
        """編集中のプロジェクトと履歴"""
        ...

    @property
    def playhead(self) -> int: ...

    def seek(self, frame: int) -> None: ...

    @property
    def selected_clip(self) -> ClipId | None: ...

    def select_clip(self, clip_id: ClipId | None) -> None: ...

    def apply_commands(self, commands: list[Command], label: str) -> None:
        """コマンドをまとめて実行する 失敗したら :class:`ToolError` を投げること

        まとめるのは、1 つの指示による編集を 1 回の Undo で戻せるようにするため
        """
        ...

    def stop_playback(self) -> None:
        """再生を止める フレームを描く前に呼ぶ"""
        ...

    def render_png(self, frame: int, *, width: int) -> bytes:
        """そのフレームを合成して PNG で返す"""
        ...

    def probe(self, path: Path) -> MediaItem:
        """素材を読み、メディアプールに入れられる形にする"""
        ...

    def analyze(self, media: MediaItem) -> None:
        """波形とサムネイルの用意を予約する"""
        ...

    def waveform(self, media: MediaItem) -> Waveform | None:
        """用意できていれば波形を返す 無ければ ``None``"""
        ...

    def start_transcription(self, media_id: MediaId, model: str) -> str:
        """起こしを始める 戻り値は画面に出す短い文言"""
        ...

    def transcription_status(self) -> str:
        """走っている起こしの様子"""
        ...
