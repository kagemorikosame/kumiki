"""起こしに渡す音の時刻を、素材の原点にそろえる（Issue #125）

faster-whisper にパスを渡すと、音の最初のサンプルを 0 秒として数える 素材の中の時刻は
映像の頭（素材の原点）から数えるので、音が映像より早く始まる素材では、起こした字幕が
その差の分だけずれる ここでは音が映像より 0.5 秒早い素材を作り、起こしへ渡した音が
原点から数えた音になっていることを見る faster-whisper は入れずに、渡された物を受け取る
代わりのモデルで確かめる
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sashimono.asr.backend import TranscribeOptions
from sashimono.asr.whisper import FasterWhisperBackend
from sashimono.engine.decode import AudioDecoder
from tests.media_fixtures import SampleMedia

#: faster-whisper が受け取る音のサンプリングレート（モデルが決めている値）
WHISPER_SAMPLE_RATE = 16000

#: 映像を遅らせる秒数 音はこの分だけ映像より早く始まる
LEAD = 0.5


def _sound_first(directory: Path, source: Path) -> Path:
    """映像だけを ``LEAD`` 秒遅らせ、音が映像より早く始まる素材を作る 画素と音は写すだけ"""
    path = directory / "sound-first.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-itsoffset",
            str(LEAD),
            "-i",
            str(source),
            "-map",
            "1:v",
            "-map",
            "0:a",
            "-c",
            "copy",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


class _Segment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text
        self.words: list[Any] = []


class _Info:
    duration = 1.0
    language = "ja"


class _Model:
    """渡された音を覚え、決まった 1 文を返す代わりのモデル"""

    def __init__(self) -> None:
        self.audio: Any = None

    def transcribe(self, audio: Any, **_: Any) -> tuple[list[_Segment], _Info]:
        self.audio = audio
        return [_Segment(1.0, 1.5, "あ")], _Info()


def _transcribe(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[_Model, Any]:
    backend = FasterWhisperBackend()
    model = _Model()
    monkeypatch.setattr(backend, "_ensure_model", lambda options: model)
    transcript = backend.transcribe(path, TranscribeOptions())
    return model, transcript


def test_the_sound_given_to_whisper_is_counted_from_the_media_origin(
    sample_av: SampleMedia, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # パスを渡すと音の頭（映像の 0.5 秒前）が 0 秒になり、起こした字幕がすべて 0.5 秒後ろへずれる
    path = _sound_first(tmp_path, sample_av.path)
    model, transcript = _transcribe(path, monkeypatch)

    assert isinstance(model.audio, np.ndarray)
    assert model.audio.dtype == np.float32
    assert model.audio.ndim == 1
    # 原点（映像の頭）の音は、元の素材の 0.5 秒目の音
    with AudioDecoder(sample_av.path, sample_rate=WHISPER_SAMPLE_RATE, channels=1) as decoder:
        expected = decoder.read(int(LEAD * WHISPER_SAMPLE_RATE), WHISPER_SAMPLE_RATE)[:, 0]
    given = model.audio[:WHISPER_SAMPLE_RATE]
    signal = float(np.sqrt(np.mean(expected**2)))
    assert signal > 0.01
    error = float(np.sqrt(np.mean((given - expected) ** 2)))
    assert error / signal < 0.05, f"相対 RMS 誤差 {error / signal:.3%}"
    # 起こした時刻は受け取ったまま使う 渡した音の 0 秒が素材の原点なので足し引きは要らない
    assert transcript is not None
    assert [(float(s.start), float(s.end)) for s in transcript.segments] == [(1.0, 1.5)]


def test_a_cancel_while_reading_the_sound_stops_before_the_model(
    sample_av: SampleMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 長い素材の音を読む間に止められないと、止めても読み終わるまで待たされる
    backend = FasterWhisperBackend()
    model = _Model()
    monkeypatch.setattr(backend, "_ensure_model", lambda options: model)
    calls = iter([False, True])
    result = backend.transcribe(
        sample_av.path, TranscribeOptions(), should_cancel=lambda: next(calls, True)
    )
    assert result is None
    assert model.audio is None


def test_a_cancel_during_the_last_read_stops_before_the_model(
    sample_av: SampleMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2 秒の素材は 1 回で読み切る 読み始めにしか見ないと、読む間に止めてもモデルへ渡す
    backend = FasterWhisperBackend()
    model = _Model()
    monkeypatch.setattr(backend, "_ensure_model", lambda options: model)
    calls = iter([False, False])
    result = backend.transcribe(
        sample_av.path, TranscribeOptions(), should_cancel=lambda: next(calls, True)
    )
    assert result is None
    assert model.audio is None
