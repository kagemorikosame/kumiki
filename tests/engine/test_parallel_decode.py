"""レイヤーごとの並列デコード（Issue #56 の 2）

重ねたクリップの映像デコードを、素材ごとに分けて同時に走らせる
壊れ方は 3 通りあり、どれも絵に出るまで気付きにくい

- 並んでいない 重ねた枚数だけ待ちが直列に並び、直す前と同じ速さに戻る
- 同じデコーダを 2 スレッドが触る コンテナの読み位置が食い違い、絵が飛ぶ
- 先読みの失敗を誰も受け取らない 読めない素材が黒いまま書き出される
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from sashimono.core.commands import AddClip, AddMedia, AddTrack
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    Effect,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.engine.decode import probe_media
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer
from tests.media_fixtures import SampleMedia, make_sample

pytestmark = pytest.mark.usefixtures("gpu")


@pytest.fixture(scope="session")
def two_sources(media_dir: Path) -> tuple[SampleMedia, SampleMedia]:
    """別々のファイルの素材 2 本 同じファイルだとデコーダを使い回して並ばない"""
    return (
        make_sample(media_dir, "layer-a.mp4", duration=2.0, audio=False),
        make_sample(media_dir, "layer-b.mp4", duration=2.0, audio=False, pattern="smptebars"),
    )


def _stacked(sources: tuple[SampleMedia, ...], frames: int = 8) -> Project:
    """素材を 1 枚ずつ別のトラックへ重ねたプロジェクト"""
    project = Project.create(
        ProjectSettings(width=sources[0].width, height=sources[0].height, frame_rate=FrameRate(30))
    )
    for index, sample in enumerate(sources):
        media = probe_media(sample.path)
        project = AddMedia(media).apply(project)
        track = Track(kind=TrackKind.VIDEO, name=f"V{index + 1}")
        project = AddTrack(track).apply(project)
        project = AddClip(
            track.id,
            Clip(
                timeline_start=0,
                duration=frames,
                media_id=media.id,
                # 下の絵も描かせる 不透明のままだと、隠れた層を飛ばす作りになったときに
                # 重ねた意味が無くなる
                opacity=AnimatedValue(0.7),
            ),
        ).apply(project)
    return project


class _Blocking:
    """2 本そろうまで返さないデコーダの代役

    直列にデコードしていると 1 本目が待ち続けて待ち合わせが成立せず、
    :class:`threading.BrokenBarrierError` で落ちる
    """

    def __init__(self, barrier: threading.Barrier, image: np.ndarray) -> None:
        self._barrier = barrier
        self._image = image
        self.closed = False

    def frame_at(self, seconds: Fraction) -> np.ndarray:
        self._barrier.wait(timeout=5)
        return self._image

    def close(self) -> None:
        self.closed = True


class _Failing:
    """必ず投げるデコーダの代役 先読みの失敗が呼ぶ側まで届くかを見る"""

    def frame_at(self, seconds: Fraction) -> np.ndarray:
        raise RuntimeError("素材を読めない")

    def close(self) -> None:
        return None


@pytest.fixture
def context() -> Iterator[OffscreenGLContext]:
    """テストごとのオフスクリーン GL コンテキスト 作れない環境では飛ばす"""
    try:
        made = OffscreenGLContext()
    except GLContextError as exc:  # pragma: no cover - GPU の無い環境
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield made
    made.release()


class TestParallelDecode:
    def test_two_layers_decode_at_the_same_time(
        self, two_sources: tuple[SampleMedia, SampleMedia], context: OffscreenGLContext
    ) -> None:
        # 並んでいなければ、1 本目のデコードが待ち合わせで止まったまま 2 本目が始まらない
        # 直列に戻ると重ねた枚数だけ待ちが積み上がり、直す前の速さへ戻る
        project = _stacked(two_sources)
        renderer = FrameRenderer(project, context=context, decode_threads=4)
        barrier = threading.Barrier(2)
        image = np.zeros((16, 16, 4), dtype=np.uint8)
        try:
            for key in list(renderer._decoders):
                renderer._decoders[key].close()
            renderer._decoders.clear()
            for media in project.media:
                renderer._decoders[(media.id, 0)] = _Blocking(barrier, image)  # type: ignore[assignment]
            with context:
                renderer.compose(0)
        finally:
            renderer.close()

    def test_one_thread_setting_really_stops_the_parallel_decode(
        self, two_sources: tuple[SampleMedia, SampleMedia], context: OffscreenGLContext
    ) -> None:
        # 切っても並んだままなら、設定がある方が質が悪い 待ち合わせは成立してはいけない
        project = _stacked(two_sources)
        renderer = FrameRenderer(project, context=context, decode_threads=1)
        barrier = threading.Barrier(2)
        image = np.zeros((16, 16, 4), dtype=np.uint8)
        try:
            for key in list(renderer._decoders):
                renderer._decoders[key].close()
            renderer._decoders.clear()
            for media in project.media:
                renderer._decoders[(media.id, 0)] = _Blocking(barrier, image)  # type: ignore[assignment]
            with context, pytest.raises(threading.BrokenBarrierError):
                renderer.compose(0)
        finally:
            renderer.close()

    def test_a_failing_prefetch_is_raised_instead_of_being_swallowed(
        self, two_sources: tuple[SampleMedia, SampleMedia], context: OffscreenGLContext
    ) -> None:
        # 受け取らずに捨てると、読めない素材が黒いまま書き出されて誰も気付かない
        project = _stacked(two_sources)
        renderer = FrameRenderer(project, context=context, decode_threads=4)
        try:
            for key in list(renderer._decoders):
                renderer._decoders[key].close()
            renderer._decoders.clear()
            for media in project.media:
                renderer._decoders[(media.id, 0)] = _Failing()  # type: ignore[assignment]
            with context, pytest.raises(RuntimeError, match="素材を読めない"):
                renderer.compose(0)
        finally:
            renderer.close()

    def test_the_leftover_prefetch_does_not_stay_behind(
        self, two_sources: tuple[SampleMedia, SampleMedia], context: OffscreenGLContext
    ) -> None:
        # 1 コマ描き終えて先読みが残っていると、次のコマの先読みと同じデコーダで重なる
        project = _stacked(two_sources)
        renderer = FrameRenderer(project, context=context, decode_threads=4)
        try:
            with context:
                renderer.compose(0)
                assert renderer._decoding == {}
                renderer.compose(1)
                assert renderer._decoding == {}
        finally:
            renderer.close()

    def test_the_same_frames_come_out_with_and_without_parallel_decode(
        self, two_sources: tuple[SampleMedia, SampleMedia], context: OffscreenGLContext
    ) -> None:
        # 並べたせいで 1 画素でも変わったら、書き出した絵が変わっている
        project = _stacked(two_sources)
        serial = FrameRenderer(project, context=context, decode_threads=1)
        try:
            expected = [serial.render(number) for number in range(6)]
        finally:
            serial.close()
        parallel = FrameRenderer(project, context=context, decode_threads=4)
        try:
            produced = [parallel.render(number) for number in range(6)]
        finally:
            parallel.close()

        for number, (left, right) in enumerate(zip(expected, produced, strict=True)):
            assert np.array_equal(left, right), f"{number} コマ目の絵が違う"


class TestWhatIsNotPrefetched:
    """先読みに出してはいけないクリップ 出すと読み直しとシークが増える"""

    def test_a_clip_with_an_after_image_is_left_alone(
        self, two_sources: tuple[SampleMedia, SampleMedia], context: OffscreenGLContext
    ) -> None:
        # 残像は同じデコーダへ前のフレームを先に頼む 今のフレームを先読みすると、
        # 受け取った絵を捨てたうえに戻る向きのシークまで増える
        project = _stacked(two_sources)
        track = project.timeline.tracks[0]
        clip = track.clips[0]
        trailed = replace(clip, effects=(Effect(kind="after_image"),))
        project = project.with_timeline(
            replace(
                project.timeline,
                tracks=(replace(track, clips=(trailed,)), *project.timeline.tracks[1:]),
            )
        )
        renderer = FrameRenderer(project, context=context, decode_threads=4)
        try:
            assert renderer._decode_request(trailed, 4, project.rate) is None
            plain = project.timeline.tracks[1].clips[0]
            assert renderer._decode_request(plain, 4, project.rate) is not None
        finally:
            renderer.close()

    def test_a_generated_clip_is_left_alone(
        self, two_sources: tuple[SampleMedia, SampleMedia], context: OffscreenGLContext
    ) -> None:
        # 絵を素材から取らないクリップまで先読みへ出すと、デコーダを開くだけ開いて空振りする
        project = _stacked(two_sources)
        renderer = FrameRenderer(project, context=context, decode_threads=4)
        try:
            drawn = Clip(timeline_start=0, duration=4)
            assert renderer._decode_request(drawn, 0, project.rate) is None
        finally:
            renderer.close()


class TestDecoderLifetime:
    def test_closing_waits_for_the_prefetch_before_freeing_the_decoders(
        self, two_sources: tuple[SampleMedia, SampleMedia], context: OffscreenGLContext
    ) -> None:
        # 走っているスレッドの手元でコンテナを閉じると、解放済みを触って落ちる
        project = _stacked(two_sources)
        renderer = FrameRenderer(project, context=context, decode_threads=4)
        started = threading.Event()
        release = threading.Event()

        class _Slow:
            def frame_at(self, seconds: Fraction) -> np.ndarray:
                started.set()
                release.wait(5)
                return np.zeros((16, 16, 4), dtype=np.uint8)

            def close(self) -> None:
                assert release.is_set(), "先読みが走っている最中にデコーダを閉じた"

        try:
            for key in list(renderer._decoders):
                renderer._decoders[key].close()
            renderer._decoders.clear()
            for media in project.media:
                renderer._decoders[(media.id, 0)] = _Slow()  # type: ignore[assignment]
            pool = renderer._pool()
            first = next(iter(renderer._decoders))
            renderer._decoding[first] = (
                Fraction(0),
                pool.submit(renderer._decoders[first].frame_at, Fraction(0)),
            )
            assert started.wait(5)
            release.set()
        finally:
            renderer.close()
