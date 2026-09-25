"""音声ストリームが 2 本ある実ファイルを置いて、2 本目の音まで鳴ること（Issue #27 の流れ）

ffmpeg で 1 本目を無音・2 本目を 440Hz にした動画を作る 1 本目しか置かないと、
混合の方式でも分ける方式でも、置いた動画は無音になる
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from sashimono.core.commands import Command, insert_media
from sashimono.core.model import LayerMode, Project, ProjectSettings, TrackKind
from sashimono.core.timebase import FrameRate
from sashimono.engine.audio import AudioMixer
from sashimono.engine.decode import probe_media
from tests.media_fixtures import libx264_available


@pytest.fixture(scope="module")
def two_voices(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """映像 1 本と音声 2 本（1 本目は無音、2 本目は 440Hz）の 2 秒の動画"""
    if not libx264_available():
        pytest.skip("ffmpeg に libx264 が無いので実素材のテストを飛ばす")
    path = tmp_path_factory.mktemp("multi_audio") / "two_voices.mkv"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=30:duration=2",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2:sample_rate=48000,volume=10dB",
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map",
        "2:a",
        "-t",
        "2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ac",
        "2",
        str(path),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return path


def _placed(path: Path, mode: str, *, split_audio: bool = True) -> Project:
    settings = ProjectSettings(width=320, height=240, frame_rate=FrameRate(30), layer_mode=mode)
    project = Project.create(settings)
    media = probe_media(path)
    assert len(media.audio_streams) == 2, "素材の作り方が崩れていて、確かめる意味が無い"
    commands: list[Command] = insert_media(project, media, split_audio=split_audio)
    for command in commands:
        project = command.apply(project)
    return project


def _peak(project: Project) -> float:
    mixer = AudioMixer(project)
    try:
        return float(np.abs(mixer.render(0, 48000)).max())
    finally:
        mixer.close()


def test_the_second_voice_sounds_on_layers(two_voices: Path) -> None:
    # 1 本目だけを置くと、2 本目（ここでは唯一鳴っている音）がタイムラインに無く無音になる
    project = _placed(two_voices, LayerMode.MIXED)
    assert [t.kind for t in project.timeline.tracks] == [TrackKind.MIXED] * 3
    assert _peak(project) > 0.05


def test_the_second_voice_sounds_on_audio_tracks(two_voices: Path) -> None:
    # 分ける方式でも 2 本目の音声トラックが無いと、同じ動画を置いたのに無音になる
    project = _placed(two_voices, LayerMode.SEPARATED)
    kinds = [t.kind for t in project.timeline.tracks]
    assert kinds == [TrackKind.VIDEO, TrackKind.AUDIO, TrackKind.AUDIO]
    assert _peak(project) > 0.05


def test_the_first_only_setting_leaves_the_second_voice_out(two_voices: Path) -> None:
    # 設定で 1 本目だけにしても 2 本目が鳴るなら、設定が効いていない
    project = _placed(two_voices, LayerMode.MIXED, split_audio=False)
    assert len(project.timeline.tracks) == 1
    assert _peak(project) < 1e-3
