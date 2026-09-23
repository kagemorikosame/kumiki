"""YMM4 の音の設定を写し、ミキサーを通して鳴り方を YMM4 の実測と比べる（#89）

写した値だけを見ても、エフェクトの側が左右を逆に解いていれば気付けない
YMM4 に書き出させて測った表（2026-09-23 YMM4 4.56.1.1 docs/development.md の
「音を測る」）と同じ比になるかを、鳴らした結果で確かめる

素材は 2 秒・440Hz・振幅 0.5 の正弦波を標準ライブラリの ``wave`` で書く
ffmpeg の組み立て方に依らずに走らせるため
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.catalog import gather_media, place
from sashimono.compat.ymm4.template import map_template
from sashimono.core.model import Project, ProjectSettings
from sashimono.core.timebase import FrameRate
from sashimono.engine.audio import AudioMixer
from sashimono.engine.decode import probe_media

SAMPLE_RATE = 48000
FPS = 30
#: 素材の長さ（秒） YMM4 を測ったときと同じ
SOURCE_SECONDS = 2
#: 枠の長さ（フレーム） 素材の倍 等倍なら後ろ半分が無音になる
FRAME_LENGTH = 4 * FPS
AMPLITUDE = 0.5


@pytest.fixture(scope="module")
def tone(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("ymm4_sound") / "tone.wav"
    t = np.arange(SOURCE_SECONDS * SAMPLE_RATE) / SAMPLE_RATE
    wave_form = AMPLITUDE * np.sin(2 * np.pi * 440.0 * t)
    frames = np.round(np.repeat(wave_form[:, None], 2, axis=1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(frames.tobytes())
    return path


def _still(amount: float) -> dict[str, Any]:
    return {"Values": [{"Value": amount}], "Span": 0.0, "AnimationType": "なし"}


def _mixed(tone: Path, **values: Any) -> np.ndarray:
    """音声アイテム 1 つを写して置き、枠の長さだけ鳴らす"""
    item: dict[str, Any] = {
        "$type": "YukkuriMovieMaker.Project.Items.AudioItem, YukkuriMovieMaker",
        "FilePath": str(tone),
        "Volume": _still(100.0),
        "Pan": _still(0.0),
        "PlaybackRate": 100.0,
        "ContentOffset": "00:00:00",
        "Frame": 0,
        "Layer": 0,
        "Length": FRAME_LENGTH,
    }
    item.update(values)
    objects = map_template([item], report=CompatibilityReport())
    project = Project.create(
        ProjectSettings(width=320, height=240, frame_rate=FrameRate(FPS), sample_rate=SAMPLE_RATE)
    )
    plan = gather_media(objects, project, probe_media)
    assert plan.missing == ()
    for command in [*plan.commands, *place(objects, project, media=plan.media)]:
        project = command.apply(project)
    mixer = AudioMixer(project)
    try:
        return mixer.render(0, FRAME_LENGTH * SAMPLE_RATE // FPS)
    finally:
        mixer.close()


def _peaks(samples: np.ndarray) -> tuple[float, float]:
    return float(np.abs(samples[:, 0]).max()), float(np.abs(samples[:, 1]).max())


@pytest.fixture(scope="module")
def reference(tone: Path) -> tuple[float, float]:
    """基準の枠（Volume 100・Pan 0・PlaybackRate 100）の左右の最大振幅"""
    left, right = _peaks(_mixed(tone))
    assert left > 0.4
    assert right > 0.4
    return left, right


def test_volume_50_halves_the_amplitude(tone: Path, reference: tuple[float, float]) -> None:
    # YMM4 は 50 で 0.501 倍 dB や二乗と読み違えると 0.5 から外れる
    left, right = _peaks(_mixed(tone, Volume=_still(50.0)))
    assert left / reference[0] == pytest.approx(0.5, abs=0.01)
    assert right / reference[1] == pytest.approx(0.5, abs=0.01)


@pytest.mark.parametrize(
    ("pan", "expected"),
    [(-100.0, (1.0, 0.0)), (-50.0, (1.0, 0.5)), (50.0, (0.5, 1.0)), (100.0, (0.0, 1.0))],
)
def test_the_pan_does_not_swap_left_and_right(
    tone: Path, reference: tuple[float, float], pan: float, expected: tuple[float, float]
) -> None:
    """YMM4 の実測と同じ比になる -100 で右が無音・50 で左が半分

    符号を取り違えると左右が入れ替わり、遠い側を 0 にする向きまで逆になる
    """
    left, right = _peaks(_mixed(tone, Pan=_still(pan)))
    assert left / reference[0] == pytest.approx(expected[0], abs=0.01)
    assert right / reference[1] == pytest.approx(expected[1], abs=0.01)


def test_a_rate_of_zero_makes_no_sound(tone: Path) -> None:
    # YMM4 は 0 で無音 等倍へ読み替えると、止めたはずの音が鳴る
    assert np.all(_mixed(tone, PlaybackRate=0.0) == 0.0)


def test_half_rate_fills_the_frame_without_growing_it(tone: Path) -> None:
    """50 で 2 秒の素材が 4 秒の枠いっぱいに鳴り、最大振幅は変わらない

    YMM4 は 3.98 秒鳴った 等倍のまま写すと 2 秒で切れ、後ろ半分が無音になる
    """
    block = _mixed(tone, PlaybackRate=50.0)
    last_half_second = block[-SAMPLE_RATE // 2 :]
    assert float(np.abs(last_half_second).max()) > 0.4
    left, _ = _peaks(block)
    assert left == pytest.approx(AMPLITUDE, abs=0.01)
