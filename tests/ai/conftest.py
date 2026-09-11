"""AI 層のテスト用のホスト

:class:`~kumiki.ai.host.EditorHost` を満たす偽物を用意する ウィジェットを一切
作らずにツールの挙動を確かめられるのは、AI 層が Qt を知らない作りにしてあるため
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from kumiki.ai.host import ToolError
from kumiki.core.commands import AddClip, Command, Document
from kumiki.core.model import (
    ClipId,
    MediaId,
    MediaItem,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
    Transcript,
)
from kumiki.core.timebase import FrameRate
from kumiki.engine.audio.waveform import Waveform
from tests.conftest import make_clip

RATE_30 = FrameRate(30)


class FakeHost:
    """編集ソフトのふりをする"""

    def __init__(self, project: Project) -> None:
        self._document = Document(project)
        self.frame = 0
        self.selected: ClipId | None = None
        self.stopped = 0
        self.rendered: list[tuple[int, int]] = []
        self.analyzed: list[MediaId] = []
        self.probed: list[Path] = []
        self.stub_waveform: Waveform | None = None
        self.probe_result: MediaItem | None = None
        self.transcription = "起こしは走っていません"

    @property
    def document(self) -> Document:
        return self._document

    @property
    def playhead(self) -> int:
        return self.frame

    def seek(self, frame: int) -> None:
        self.frame = frame

    @property
    def selected_clip(self) -> ClipId | None:
        return self.selected

    def select_clip(self, clip_id: ClipId | None) -> None:
        self.selected = clip_id

    def apply_commands(self, commands: list[Command], label: str) -> None:
        if not commands:
            return
        try:
            with self._document.checkpoint(label):
                for command in commands:
                    self._document.execute(command)
        except (ValueError, KeyError) as exc:
            raise ToolError(str(exc)) from exc

    def stop_playback(self) -> None:
        self.stopped += 1

    def render_png(self, frame: int, *, width: int) -> bytes:
        self.rendered.append((frame, width))
        # PNG の識別子だけ本物にしておく 中身は誰も見ない
        return b"\x89PNG\r\n\x1a\n" + f"{frame}".encode()

    def probe(self, path: Path) -> MediaItem:
        self.probed.append(path)
        if self.probe_result is None:
            raise ToolError(f"読み込めません: {path}")
        return replace(self.probe_result, path=path)

    def analyze(self, media: MediaItem) -> None:
        self.analyzed.append(media.id)

    def waveform(self, media: MediaItem) -> Waveform | None:
        del media
        return self.stub_waveform

    def start_transcription(self, media_id: MediaId, model: str) -> str:
        self.transcription = f"{model} で開始"
        return f"{media_id} の起こしを始めました"

    def transcription_status(self) -> str:
        return self.transcription


def make_loaded(video_media: MediaItem, transcript: Transcript) -> Project:
    """10 秒の素材を 1 本置き、字幕を付けたプロジェクトを組む

    fixture ではなく関数にしてあるのは、別のフォルダのテストからも使うため
    conftest の fixture は、そのフォルダの下からしか見えない
    """
    with_transcript = replace(video_media, transcript=transcript)
    base = Project.create(ProjectSettings(frame_rate=RATE_30), media=(with_transcript,))
    track = Track(kind=TrackKind.VIDEO, name="V1")
    base = base.with_timeline(replace(base.timeline, tracks=(track,)))
    return AddClip(track.id, make_clip(0, 300, with_transcript)).apply(base)


@pytest.fixture
def loaded(video_media: MediaItem, transcript: Transcript) -> Project:
    return make_loaded(video_media, transcript)


@pytest.fixture
def host(loaded: Project, video_media: MediaItem) -> FakeHost:
    created = FakeHost(loaded)
    created.probe_result = MediaItem(
        path=Path("C:/素材/追加.mp4"),
        duration=Fraction(5),
        video_streams=video_media.video_streams,
        audio_streams=video_media.audio_streams,
    )
    return created
