"""音声波形（AviUtl2 の ``音声波形表示``）を映像として描く

絵を描く側（レンダラ）が、そのフレームの音のサンプルを読むのはここが初めて
決まりは AviUtl2 の書き出しを素材のサンプルと突き合わせて読んだ

* 1 画素に 1 サンプル（プロジェクトの音のレート） 窓の頭はそのフレームの時刻
* 振幅 1 で高さの半分 **正の値が下へ出る**
* クリップの終わりより先は 0（平らな線） 再生範囲を過ぎたら何も描かない
"""

from __future__ import annotations

import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from kumiki.core.commands import AddClip, AddTrack
from kumiki.core.model import (
    AnimatedValue,
    Clip,
    GeneratedSource,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from kumiki.core.timebase import FrameRate
from kumiki.engine.gpu import GLContextError, OffscreenGLContext
from kumiki.engine.render import FrameRenderer
from kumiki.engine.sources import render_source, waveform_points

RATE = 44100
WIDTH, HEIGHT = 640, 360


def _lit_rows(image: np.ndarray, column: int) -> np.ndarray:
    rows: np.ndarray = np.nonzero(image[:, column, 3] > 128)[0] - HEIGHT // 2
    return rows


class TestPoints:
    def test_one_sample_per_pixel(self) -> None:
        # 窓を画面の幅に引き伸ばすと、AviUtl2 と同じ時間の波が横に倍にも半分にもなる
        points = waveform_points(np.zeros(800), 800.0, 400.0, 100.0)
        assert len(points) == 800
        assert points[1, 0] - points[0, 0] == pytest.approx(1.0)

    def test_a_positive_sample_goes_down(self) -> None:
        # 実物と突き合わせると下向きで相関 0.99 上向きに描くと上下逆さまの波形になる
        points = waveform_points(np.array([0.5, 0.5]), 2.0, 400.0, 100.0)
        assert points[0, 1] == pytest.approx(1.0 + 100.0)

    def test_the_volume_scales_the_swing(self) -> None:
        points = waveform_points(np.array([0.5, 0.5]), 2.0, 400.0, 50.0)
        assert points[0, 1] == pytest.approx(1.0 + 50.0)


def _source(**extra: object) -> GeneratedSource:
    params: dict[str, object] = {
        "shape": "waveform",
        "width": AnimatedValue(400.0),
        "height": AnimatedValue(200.0),
    }
    params.update(extra)
    return GeneratedSource(kind="shape", params=params)  # type: ignore[arg-type]


def test_silence_is_a_flat_line_below_the_centre() -> None:
    # 無音の所は中心の下 2 行（実物の絵のまま）
    image = render_source(_source(), WIDTH, HEIGHT, audio=np.zeros(400, dtype=np.float32))
    assert image is not None
    assert list(_lit_rows(image, WIDTH // 2)) == [0, 1]
    # 横幅の外には出ない
    assert image[:, WIDTH // 2 - 205, 3].max() == 0


def test_nothing_is_drawn_without_sound() -> None:
    # 音が読めないとき 平らな線を出すと、鳴っていない音が鳴っているように見える
    image = render_source(_source(), WIDTH, HEIGHT)
    assert image is not None
    assert image[:, :, 3].max() == 0


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


@pytest.fixture(scope="module")
def stepped_audio(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """0.5 秒まで +0.5、そこから -0.5 の 1 秒の音 どの時刻を読んだかが値で分かる"""
    path = tmp_path_factory.mktemp("wave") / "step.wav"
    samples = np.full(RATE, 0.5)
    samples[RATE // 2 :] = -0.5
    pcm = (samples * 32767).astype("<i2")
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(pcm.tobytes())
    return path


def _render(
    context: OffscreenGLContext,
    source: GeneratedSource,
    frame: int,
    *,
    duration: int = 60,
    sample_rate: int = RATE,
) -> np.ndarray:
    settings = ProjectSettings(
        width=WIDTH, height=HEIGHT, frame_rate=FrameRate(60), sample_rate=sample_rate
    )
    project = Project.create(settings)
    track = Track(kind=TrackKind.VIDEO, name="V1")
    project = AddTrack(track).apply(project)
    placed = Clip(timeline_start=0, duration=duration, source=source)
    project = AddClip(track.id, placed).apply(project)
    renderer = FrameRenderer(project, context=context)
    try:
        image: np.ndarray = renderer.render(frame)
    finally:
        renderer.close()
    return image


def _line_row(image: np.ndarray, column: int) -> float:
    lit = np.nonzero(image[:, column, :3].max(axis=1) > 128)[0]
    assert len(lit), column
    return float(lit.mean()) - HEIGHT / 2


class TestRenderer:
    def test_it_reads_the_sound_at_the_frame_time(
        self, gl_context: OffscreenGLContext, stepped_audio: Path
    ) -> None:
        # 頭では +0.5（下へ 50） 0.5 秒を過ぎると -0.5（上へ 50）
        # 音を読まないレンダラでは線がまったく出なかった
        source = _source(audio_path=str(stepped_audio))
        early = _render(gl_context, source, 0)
        late = _render(gl_context, source, 40)
        assert _line_row(early, WIDTH // 2) == pytest.approx(51.0, abs=1.5)
        assert _line_row(late, WIDTH // 2) == pytest.approx(-49.0, abs=1.5)

    def test_the_line_is_flat_after_the_clip_ends(
        self, gl_context: OffscreenGLContext, stepped_audio: Path
    ) -> None:
        # 最後のフレームは 1 フレームぶん（22.05kHz なら 368 サンプル）より先が 0
        # 実物もクリップの終わりから先を平らに描いた 続きの素材を読むと、鳴らない音が描かれる
        source = _source(audio_path=str(stepped_audio))
        image = _render(gl_context, source, 9, duration=10, sample_rate=22050)
        assert _line_row(image, WIDTH // 2 - 100) == pytest.approx(51.0, abs=1.5)
        assert _line_row(image, WIDTH // 2 + 190) == pytest.approx(1.0, abs=1.5)

    def test_nothing_after_the_playback_range(
        self, gl_context: OffscreenGLContext, stepped_audio: Path
    ) -> None:
        # 再生範囲を 0 秒にした見本（10,10）は AviUtl2 で何も出なかった
        source = _source(audio_path=str(stepped_audio), audio_end_ms=0)
        image = _render(gl_context, source, 10)
        assert image[:, :, :3].max() == 0
