"""頭の時刻が 0 でない素材を、ふつうに置いて再生する（Issue #123）

分割して書き出した物や放送の録画（MPEG-TS）は、最初のフレームの PTS が 0 より後ろにある
素材の時刻を PTS そのままで数えると、置いたクリップ（``source_in`` 0・長さは素材の長さ）が
読む 0〜2 秒はすべて最初のフレームより前になり、頭の絵が止まったまま動かず、音は
クリップの外（5 秒から先）にあって鳴らない

ここでは ``-output_ts_offset 5`` で頭だけをずらした複製を、元の素材と同じ手順で置き、
同じ絵と音になることを見る 画素は ``-c copy`` で写しているので、映像は 1 画素まで同じになる
"""

from __future__ import annotations

from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import av
import numpy as np
import pytest

from sashimono.core.commands import Document, insert_media
from sashimono.core.model import Project, ProjectSettings, TrackKind
from sashimono.core.timebase import FrameRate
from sashimono.engine.audio import AudioMixer, analyze_waveform
from sashimono.engine.cache.thumbnails import build_filmstrip
from sashimono.engine.decode import probe_media
from sashimono.engine.decode.probe import _container_duration, _stream_end, media_origin
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer
from tests.media_fixtures import SampleMedia, make_delayed

#: 素材の頭をずらす秒数 2 秒の素材より長くして、PTS そのままの時刻ではクリップの中に
#: 素材の絵も音も 1 つも入らないようにする
DELAY = 5.0


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    """オフスクリーンの GL コンテキスト 作れない環境ではテストを飛ばす"""
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


@pytest.fixture
def late(sample_av: SampleMedia, tmp_path: Path) -> Path:
    return make_delayed(tmp_path, "late.mp4", sample_av.path, DELAY)


def _inserted(path: Path) -> Project:
    """素材を読み、画面から置くときと同じ ``insert_media`` でタイムラインへ置く"""
    document = Document(
        Project.create(ProjectSettings(width=320, height=240, frame_rate=FrameRate(30)))
    )
    for command in insert_media(document.project, probe_media(path)):
        document.execute(command)
    return document.project


def _durations(project: Project) -> list[int]:
    return [
        clip.duration
        for kind in (TrackKind.VIDEO, TrackKind.AUDIO)
        for track in project.timeline.tracks
        if track.kind is kind
        for clip in track.clips
    ]


def _window_levels(sound: np.ndarray, rate: int, step: float) -> np.ndarray:
    """``step`` 秒ごとの二乗平均の平方根 位相のずれに左右されない量で比べるため"""
    size = round(rate * step)
    count = len(sound) // size
    windows = sound[: count * size].reshape(count, size, -1)
    return np.asarray(np.sqrt((windows.astype(np.float64) ** 2).mean(axis=(1, 2))))


def test_the_clip_is_as_long_as_the_original(sample_av: SampleMedia, late: Path) -> None:
    """頭をずらしただけの素材は、元と同じ長さのクリップになる

    コンテナの長さは、映像より 24ms 早く始まる AAC の前置き（プライミング）を含む
    それをそのまま長さにすると 1 フレーム長いクリップになり、最後の 1 フレームは
    映像の終わりの後なので何も映らない
    """
    assert _durations(_inserted(late)) == _durations(_inserted(sample_av.path)) == [60, 60]


def test_the_picture_plays_from_the_clip_head(
    sample_av: SampleMedia, late: Path, gl_context: OffscreenGLContext
) -> None:
    """クリップのどのフレームも、元の素材を置いたときと同じ絵になる

    PTS そのままで数えると、クリップが読む 0〜2 秒は最初のフレーム（5 秒）より前なので
    60 フレームすべてが最初の絵のまま止まる
    """
    original = FrameRenderer(_inserted(sample_av.path), context=gl_context)
    shifted = FrameRenderer(_inserted(late), context=gl_context)
    try:
        for frame in range(60):
            expected = original.render(frame)
            actual = shifted.render(frame)
            assert np.array_equal(actual, expected), f"{frame} フレーム目が元の素材と違う"
    finally:
        original.close()
        shifted.close()


def test_the_sound_plays_from_the_clip_head(sample_av: SampleMedia, late: Path) -> None:
    """クリップの頭から、元の素材と同じ大きさの音が鳴る

    PTS そのままで数えると、音は 5 秒から先にあってクリップ（0〜2 秒）の外なので、
    クリップの間はずっと無音になる 映像と同じ原点で数えるので、映像との食い違いも出ない
    前置きの分（1ms 未満）だけ位相がずれるので、波形ではなく 0.1 秒ごとの大きさで比べる
    """
    rate = 48000
    original = AudioMixer(_inserted(sample_av.path))
    shifted = AudioMixer(_inserted(late))
    try:
        expected = _window_levels(original.render_frames(0, 60), rate, 0.1)
        actual = _window_levels(shifted.render_frames(0, 60), rate, 0.1)
    finally:
        original.close()
        shifted.close()
    assert expected.min() > 0.01, "元の素材の音が鳴っていない 比べる相手になっていない"
    np.testing.assert_allclose(actual, expected, rtol=0.05)


def test_the_filmstrip_matches_the_original(sample_av: SampleMedia, late: Path) -> None:
    """タイムラインに並ぶ絵も、元の素材と同じ

    PTS そのままで数えると、並ぶ絵がすべて最初の 1 枚になり、クリップの中身が見分けられない
    """
    expected = build_filmstrip(sample_av.path)
    actual = build_filmstrip(late)
    assert expected is not None
    assert actual is not None
    assert actual.interval == expected.interval
    assert np.array_equal(actual.sheet, expected.sheet)


def test_the_waveform_starts_at_the_clip_head(sample_av: SampleMedia, late: Path) -> None:
    """タイムラインの波形も、頭から音がある

    PTS そのままで数えると、素材の長さ（2 秒）ぶん読む間はずっと無音で、波形が平らになる
    無音を切る機能（ジェットカット）はこの波形から無音を探すので、素材全体を無音と見なす
    """
    expected = analyze_waveform(sample_av.path)
    actual = analyze_waveform(late)
    assert expected is not None
    assert actual is not None
    per_second = 48000 // expected.levels[0].samples_per_peak
    loudest = float(np.abs(expected.levels[0].peaks[:per_second]).max())
    heard = float(np.abs(actual.levels[0].peaks[:per_second]).max())
    assert loudest > 0.01
    assert heard == pytest.approx(loudest, rel=0.05)


# 頭の時刻が欠けていたり負だったりする素材（#124 のレビュー） 実物をこの形に作るのは
# 難しいので、コンテナと道の書いてある値だけを持つ代わりの物で長さの決め方を見る

#: PyAV のコンテナの時刻の刻み（マイクロ秒）
MICRO = 1_000_000


def _stream(start: Fraction | None, length: Fraction, base: Fraction) -> Any:
    """道の代わり 頭と長さを ``base`` の刻みで持つ"""
    return SimpleNamespace(
        start_time=None if start is None else int(start / base),
        duration=int(length / base),
        time_base=base,
        # 印の無い普通の道 カバー画像（attached_pic）は映像に数えないので、解析が印を見る
        disposition=av.stream.Disposition(0),
    )


def _container(
    start: Fraction | None,
    length: Fraction | None,
    video: list[Any],
    audio: list[Any],
) -> Any:
    return SimpleNamespace(
        start_time=None if start is None else int(start * MICRO),
        duration=None if length is None else int(length * MICRO),
        streams=SimpleNamespace(video=video, audio=audio),
    )


def test_a_negative_sound_head_is_not_counted_into_the_length() -> None:
    # 音の前置きが -0.02 秒・映像の頭が 0.1 秒 負の頭を 0 に丸めると、クリップが
    # 0.02 秒長くなり、終わりに絵も音も無い区間ができる
    base = Fraction(1, 1000)
    video = _stream(Fraction(1, 10), Fraction(2), base)
    audio = _stream(Fraction(-1, 50), Fraction(212, 100), base)
    container = _container(Fraction(-1, 50), Fraction(212, 100), [video], [audio])
    origin = media_origin(container)
    assert origin == Fraction(1, 10)
    assert _container_duration(container, origin) == Fraction(2)


def test_an_unknown_container_head_does_not_shrink_the_media_away() -> None:
    # コンテナが頭を書いていないのに原点だけ引くと、長さが 0 まで縮み、置いても
    # クリップができない
    base = Fraction(1, 1000)
    video = _stream(Fraction(5), Fraction(2), base)
    container = _container(None, Fraction(2), [video], [])
    assert _container_duration(container, media_origin(container)) == Fraction(2)
    nothing = _container(None, Fraction(2), [_stream(None, Fraction(2), base)], [])
    assert _container_duration(nothing, Fraction(5)) == Fraction(2)


def test_a_zero_origin_keeps_the_old_length() -> None:
    # 上の 2 つを直すときに崩しやすい所の押さえ（前の作りでも通る）
    # B フレームの並べ替えで頭が負の素材は原点 0 で、この修正の前から正しく映っていた
    # 負の区間を引くと、そういう素材の長さがすべて縮む
    base = Fraction(1, 1000)
    video = _stream(Fraction(-1, 25), Fraction(2), base)
    container = _container(Fraction(-1, 25), Fraction(2), [video], [])
    assert media_origin(container) == 0
    assert _container_duration(container, Fraction(0)) == Fraction(2)


def test_a_video_stream_without_a_head_ends_at_its_own_length() -> None:
    # 道の頭が無いのに 0 から始まると見て原点を引くと、終わりが原点の分だけ早まり、
    # 最後のフレームより前から絵が出なくなる 終わりを捨てて素材の長さで見ると、
    # 音の方が長い素材で映像の後も最後の絵が残る 原点から始まるものと見て道の長さで切る
    base = Fraction(1, 1000)
    headless = _stream(None, Fraction(2), base)
    assert _stream_end(headless, Fraction(1)) == Fraction(2)
    assert _stream_end(headless, Fraction(0)) == Fraction(2)
    assert _stream_end(_stream(Fraction(5), Fraction(2), base), Fraction(5)) == Fraction(2)
