"""音声が何本もある動画の波形と音が、ストリームごとに別になること

利用者の画面では、音声 4 本の動画を混合の方式で置くと、4 本の音のレイヤーが
どれも同じ波形になっていた 波形の解析と控えが素材だけを鍵にしていて、
どの音のクリップにも 1 本目の波形を出していた

ffmpeg で中身の違う 4 本の音（無音・大きい 440Hz・小さい 880Hz・中くらいの 220Hz）を
持つ動画を作り、波形・控え・鳴る音・タイムラインが引く波形のどれもが、
クリップが鳴らすストリームのものになることを確かめる
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from sashimono.core.commands import Command, SetTrackState, insert_media
from sashimono.core.model import (
    LayerMode,
    MediaItem,
    Project,
    ProjectSettings,
    TrackKind,
    heard_stream,
)
from sashimono.core.timebase import FrameRate
from sashimono.engine.audio import AudioMixer, Waveform
from sashimono.engine.cache import CacheStore, MediaAnalyzer, load_waveform, waveform_key
from sashimono.engine.decode import probe_media
from sashimono.ui.timeline import TimelineView
from tests.media_fixtures import libx264_available

#: 音ごとの大きさ（dB） 無音は ``None`` 並びは素材の音声ストリームの順
_VOICES: tuple[tuple[str, float | None], ...] = (
    ("anullsrc=r=48000:cl=stereo", None),
    ("sine=frequency=440:duration=2:sample_rate=48000", 0.0),
    ("sine=frequency=880:duration=2:sample_rate=48000", -24.0),
    ("sine=frequency=220:duration=2:sample_rate=48000", -10.0),
)


@pytest.fixture(scope="module")
def four_voices(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """映像 1 本と中身の違う音声 4 本の 2 秒の動画"""
    if not libx264_available():
        pytest.skip("ffmpeg に libx264 が無いので実素材のテストを飛ばす")
    path = tmp_path_factory.mktemp("four_voices") / "four_voices.mkv"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=30:duration=2",
    ]
    for source, gain in _VOICES:
        filtered = source if gain is None else f"{source},volume={gain}dB"
        command += ["-f", "lavfi", "-i", filtered]
    command += ["-map", "0:v"]
    for number in range(len(_VOICES)):
        command += ["-map", f"{number + 1}:a"]
    command += ["-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2"]
    subprocess.run([*command, str(path)], check=True, capture_output=True)
    return path


@pytest.fixture
def media(four_voices: Path) -> MediaItem:
    item = probe_media(four_voices)
    assert len(item.audio_streams) == len(_VOICES), "素材の作り方が崩れていて、確かめる意味が無い"
    return item


@pytest.fixture
def analyzer(tmp_path: Path) -> Iterator[MediaAnalyzer]:
    created = MediaAnalyzer(CacheStore(tmp_path / "cache"))
    yield created
    created.close()


def _wait(predicate: Callable[[], bool], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("解析が終わらない")


def _loudness(waveform: Waveform) -> float:
    peaks = waveform.levels[0].peaks
    return float(np.abs(peaks).max()) if peaks.size else 0.0


def _placed(media: MediaItem) -> Project:
    settings = ProjectSettings(
        width=160, height=120, frame_rate=FrameRate(30), layer_mode=LayerMode.MIXED
    )
    project = Project.create(settings)
    commands: list[Command] = insert_media(project, media, split_audio=True)
    for command in commands:
        project = command.apply(project)
    return project


def _expected_order() -> list[int]:
    """大きい順の並び（素材の音の番号）"""
    loud = [(gain if gain is not None else -1000.0, n) for n, (_, gain) in enumerate(_VOICES)]
    return [n for _, n in sorted(loud, reverse=True)]


def test_each_voice_gets_its_own_waveform(media: MediaItem, analyzer: MediaAnalyzer) -> None:
    # 素材だけで波形を持つと、4 本とも 1 本目（無音）の波形になる
    analyzer.request(media)
    streams = [s.index for s in media.audio_streams]
    _wait(lambda: all(analyzer.waveform(media, s) is not None for s in streams))
    loudness = []
    for stream in streams:
        waveform = analyzer.waveform(media, stream)
        assert waveform is not None
        loudness.append(_loudness(waveform))
    assert loudness[0] < 1e-3, "1 本目は無音のはず"
    assert sorted(range(len(streams)), key=lambda n: -loudness[n]) == _expected_order()


def test_the_first_voice_keeps_the_old_cache_key(
    media: MediaItem, analyzer: MediaAnalyzer, tmp_path: Path
) -> None:
    # 1 本目の控えの鍵を変えると、ストリームを分ける前に作った控えが全部作り直しになる
    # 2 本目以降は番号入りの鍵に入り、1 本目の控えを上書きしない
    analyzer.request(media)
    streams = [s.index for s in media.audio_streams]
    _wait(lambda: all(analyzer.waveform(media, s) is not None for s in streams))
    store = CacheStore(tmp_path / "cache")
    first = load_waveform(store, waveform_key(media.path, 48000, 2))
    second = load_waveform(store, waveform_key(media.path, 48000, 2, stream=streams[1]))
    assert first is not None and second is not None
    assert _loudness(first) < 1e-3 < _loudness(second)


def test_an_unknown_stream_falls_back_to_the_first(
    media: MediaItem, analyzer: MediaAnalyzer
) -> None:
    # デコーダは無い番号を 1 本目として開く 波形も同じにしないと、無い番号で引いたとき
    # 解析の終わらない空の波形のまま残る
    analyzer.request(media)
    first = media.audio_streams[0].index
    _wait(lambda: analyzer.waveform(media, first) is not None)
    assert analyzer.waveform(media, 99) is analyzer.waveform(media, first)
    assert analyzer.waveform(media) is analyzer.waveform(media, first)


def test_each_layer_sounds_its_own_voice(media: MediaItem) -> None:
    # 鳴らす音が波形と別の決まりで選ばれていると、波形と違う音が鳴る
    project = _placed(media)
    sound_layers = [
        track
        for track in project.timeline.tracks
        if track.kind is TrackKind.MIXED
        and track.clips
        and heard_stream(track, track.clips[0]) is not None
    ]
    assert len(sound_layers) == len(_VOICES)
    peaks = []
    for kept in sound_layers:
        alone = project
        for track in project.timeline.tracks:
            if track.id != kept.id:
                alone = SetTrackState(track.id, muted=True).apply(alone)
        mixer = AudioMixer(alone)
        try:
            peaks.append(float(np.abs(mixer.render(0, 48000)).max()))
        finally:
            mixer.close()
    # レイヤーは素材の音の順に並ぶ
    streams = [heard_stream(track, track.clips[0]) for track in sound_layers]
    assert streams == [s.index for s in media.audio_streams]
    assert peaks[0] < 1e-3
    assert sorted(range(len(peaks)), key=lambda n: -peaks[n]) == _expected_order()


class _Recording(MediaAnalyzer):
    """タイムラインが波形を引くときの番号を覚える"""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[int | None] = []

    def waveform(self, media: MediaItem, stream: int | None = None) -> Waveform | None:
        self.asked.append(stream)
        return None


def test_the_timeline_asks_for_the_voice_each_clip_plays(
    qt_application: QApplication, media: MediaItem
) -> None:
    # 素材だけで引くと、どのレイヤーの音のクリップにも 1 本目の波形が出る
    del qt_application
    project = _placed(media)
    recording = _Recording()
    view = TimelineView(project, recording)
    try:
        view.resize(1200, 600)
        view.zoom_to_fit()
        view.grab()
        QApplication.processEvents()
        asked = {stream for stream in recording.asked if stream is not None}
        assert asked == {s.index for s in media.audio_streams}
    finally:
        view.deleteLater()
        recording.close()
