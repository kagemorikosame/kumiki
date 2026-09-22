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

from sashimono.core.commands import AddClip, AddTrack
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    GeneratedSource,
    ParamValue,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.engine.audio_shapes import (
    SPECTRUM_SIZE,
    WAVEFORM_LEAD,
    cell_mask,
    spectrum_levels,
)
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer
from sashimono.engine.sources import render_source, waveform_points

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
        # 音量が振れ幅に掛からないと、音量 50 でも実物の倍の高さの波形が出る
        points = waveform_points(np.array([0.5, 0.5]), 2.0, 400.0, 50.0)
        assert points[0, 1] == pytest.approx(1.0 + 50.0)


def _source(**extra: ParamValue) -> GeneratedSource:
    params: dict[str, ParamValue] = {
        "shape": "waveform",
        "width": AnimatedValue(400.0),
        "height": AnimatedValue(200.0),
    }
    params.update(extra)
    return GeneratedSource(kind="shape", params=params)


def test_silence_is_a_flat_line_below_the_centre() -> None:
    # 無音の所は中心の下 2 行（実物の絵のまま）
    image = render_source(
        _source(), WIDTH, HEIGHT, audio=np.zeros(WAVEFORM_LEAD + 400, dtype=np.float32)
    )
    assert image is not None
    assert list(_lit_rows(image, WIDTH // 2)) == [0, 1]
    # 横幅の外には出ない
    assert image[:, WIDTH // 2 - 205, 3].max() == 0


def test_nothing_is_drawn_without_sound() -> None:
    # 音が読めないとき 平らな線を出すと、鳴っていない音が鳴っているように見える
    image = render_source(_source(), WIDTH, HEIGHT)
    assert image is not None
    assert image[:, :, 3].max() == 0


class TestGrid:
    def test_cells_fill_the_box_with_gaps_at_the_edges(self) -> None:
        # 横 16 升・スペース 4 は、幅 50 の升の境目に 2 画素のすき間（実物のまま）
        mask = cell_mask(np.ones((16, 16), dtype=bool), 800, 400, 4.0, 4.0)
        row = mask[12]
        assert not row[49] and not row[50]
        assert row[48] and row[51]
        # 縦は高さ 25 の升に 1 画素
        assert int((~mask[:, 25]).sum()) == 16

    def test_a_coarse_grid_draws_the_line_two_cells_thick(self) -> None:
        # 縦 16 升では、無音の線が中心をまたぐ 2 升に乗る（高さ 200 なら 1 升 12.5 画素）
        source = _source(wave_rows=AnimatedValue(16.0))
        audio = np.zeros(WAVEFORM_LEAD + 400, dtype=np.float32)
        image = render_source(source, WIDTH, HEIGHT, audio=audio)
        assert image is not None
        rows = _lit_rows(image, WIDTH // 2)
        assert rows.min() == pytest.approx(-12.5, abs=1.0)
        assert rows.max() == pytest.approx(11.5, abs=1.0)


class TestSpectrum:
    def test_a_tone_rises_at_its_frequency(self) -> None:
        # 横軸は 40Hz〜20kHz の対数 1kHz の音なら左から 0.51 の所が一番高い
        rate = 44100
        time = np.arange(SPECTRUM_SIZE) / rate
        levels = spectrum_levels(np.sin(2 * np.pi * 1000.0 * time), 800, rate, 100.0)
        peak = int(np.argmax(levels))
        assert peak / 800 == pytest.approx(np.log(1000 / 40) / np.log(20000 / 40), abs=0.01)

    def test_silence_draws_nothing(self) -> None:
        # 実物も音の無い頭の 25 フレームは何も出さなかった
        source = _source(wave_spectrum=True)
        audio = np.zeros(WAVEFORM_LEAD + SPECTRUM_SIZE, dtype=np.float32)
        image = render_source(source, WIDTH, HEIGHT, audio=audio)
        assert image is not None
        assert image[:, :, 3].max() == 0

    def test_bars_grow_from_the_bottom(self) -> None:
        # 下の辺から上へ塗る 中心から塗ると、実物の絵の上半分に棒が出る
        rate = 44100
        time = np.arange(WAVEFORM_LEAD + SPECTRUM_SIZE) / rate
        audio = (0.5 * np.sin(2 * np.pi * 200.0 * time)).astype(np.float32)
        image = render_source(_source(wave_spectrum=True), WIDTH, HEIGHT, audio=audio)
        assert image is not None
        lit = np.nonzero(image[:, :, 3].max(axis=1) > 128)[0] - HEIGHT // 2
        assert lit.max() == pytest.approx(99, abs=1)


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


def _project(
    source: GeneratedSource,
    *,
    duration: int = 60,
    sample_rate: int = RATE,
    stream_index: int = 0,
    screen: tuple[int, int] = (WIDTH, HEIGHT),
) -> Project:
    settings = ProjectSettings(
        width=screen[0], height=screen[1], frame_rate=FrameRate(60), sample_rate=sample_rate
    )
    project = Project.create(settings)
    track = Track(kind=TrackKind.VIDEO, name="V1")
    project = AddTrack(track).apply(project)
    placed = Clip(timeline_start=0, duration=duration, source=source, stream_index=stream_index)
    return AddClip(track.id, placed).apply(project)


def _render(
    context: OffscreenGLContext,
    source: GeneratedSource,
    frame: int,
    *,
    duration: int = 60,
    sample_rate: int = RATE,
    stream_index: int = 0,
    screen: tuple[int, int] = (WIDTH, HEIGHT),
) -> np.ndarray:
    project = _project(
        source,
        duration=duration,
        sample_rate=sample_rate,
        stream_index=stream_index,
        screen=screen,
    )
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


def _write_wav(path: Path, value: float) -> None:
    pcm = (np.full(RATE, value) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(pcm.tobytes())


@pytest.fixture(scope="module")
def two_streams(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """音声を 2 本持つ素材 1 本目は +0.5、2 本目は -0.5 がずっと続く"""
    av = pytest.importorskip("av")
    path = tmp_path_factory.mktemp("streams") / "two.mka"
    with av.open(str(path), "w", format="matroska") as container:
        streams = []
        for _ in range(2):
            stream = container.add_stream("pcm_s16le", rate=RATE)
            stream.layout = "mono"
            streams.append(stream)
        for stream, value in zip(streams, (0.5, -0.5), strict=True):
            pcm = (np.full((1, RATE), value) * 32767).astype("<i2")
            frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
            frame.sample_rate = RATE
            for packet in stream.encode(frame):
                container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
    return path


class TestWhatTheReviewFound:
    def test_the_chosen_audio_stream_is_drawn(
        self, gl_context: OffscreenGLContext, two_streams: Path
    ) -> None:
        # 2 本目の音声を選んだクリップに 1 本目の波形が出ていた（デコーダを道だけで持っていた）
        source = _source(audio_path=str(two_streams))
        first = _render(gl_context, source, 10, stream_index=0)
        second = _render(gl_context, source, 10, stream_index=1)
        assert _line_row(first, WIDTH // 2) == pytest.approx(51.0, abs=1.5)
        assert _line_row(second, WIDTH // 2) == pytest.approx(-49.0, abs=1.5)

    def test_a_removed_waveform_lets_go_of_its_file(
        self, gl_context: OffscreenGLContext, tmp_path: Path
    ) -> None:
        # 波形のクリップを消しても、レンダラを閉じるまで音声ファイルを開いたままだった
        # 開いたままでは Windows で消すことも差し替えることもできない
        path = tmp_path / "held.wav"
        _write_wav(path, 0.5)
        project = _project(_source(audio_path=str(path)))
        renderer = FrameRenderer(project, context=gl_context)
        try:
            renderer.render(10)
            renderer.set_project(Project.create(project.settings))
            path.unlink()
        finally:
            renderer.close()
        assert not path.exists()

    def test_a_file_put_back_later_is_read(
        self, gl_context: OffscreenGLContext, tmp_path: Path
    ) -> None:
        # 開けなかった記録をずっと覚えていて、後から置いた素材を読まなかった
        path = tmp_path / "later.wav"
        project = _project(_source(audio_path=str(path)))
        renderer = FrameRenderer(project, context=gl_context)
        try:
            assert renderer.render(10)[:, :, :3].max() == 0
            _write_wav(path, 0.5)
            renderer.set_project(project)
            image = renderer.render(10)
        finally:
            renderer.close()
        assert _line_row(image, WIDTH // 2) == pytest.approx(51.0, abs=1.5)

    def test_a_broken_width_does_not_stop_drawing(self) -> None:
        # 横幅が無限大でも round で落ちず、描ける範囲に収める
        source = _source(width=AnimatedValue(float("inf")), wave_rows=AnimatedValue(1e12))
        audio = np.zeros(WAVEFORM_LEAD + 800, dtype=np.float32)
        assert render_source(source, WIDTH, HEIGHT, audio=audio) is not None

    def test_a_plain_number_width_reads_enough_sound(
        self, gl_context: OffscreenGLContext, stepped_audio: Path
    ) -> None:
        # 横幅を素の数で持つと、読む音を既定の 800 サンプル（スペクトラムの窓と合わせて
        # 1536）に切っていた 横幅 2000 なら 1537 サンプル目より先が平らな線になる
        source = _source(audio_path=str(stepped_audio), width=2000)
        image = _render(gl_context, source, 0, screen=(2000, HEIGHT))
        assert _line_row(image, 1900) == pytest.approx(51.0, abs=1.5)


def test_each_renderer_keeps_its_own_trail_paths(gl_context: OffscreenGLContext) -> None:
    # 道の置き場をプロセスで 1 つ共有すると、プレビューを閉じたときに書き出しの道まで消えた
    project = Project.create(ProjectSettings(width=WIDTH, height=HEIGHT))
    first = FrameRenderer(project, context=gl_context)
    second = FrameRenderer(project, context=gl_context)
    try:
        assert first._trail_paths is not second._trail_paths
        second._trail_paths.get("kept", lambda a, b: np.zeros((b - a, 2)), 10)
        first.set_project(project)
        assert len(second._trail_paths._paths) == 1
    finally:
        first.close()
        second.close()
