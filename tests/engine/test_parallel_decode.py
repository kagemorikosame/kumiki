"""レイヤーごとの並列デコード（Issue #56 の 2）

重ねたクリップの映像デコードを、素材ごとに分けて同時に走らせる
壊れ方は 4 通りあり、どれも絵に出るまで気付きにくい

- 並んでいない 重ねた枚数だけ待ちが直列に並び、直す前と同じ速さに戻る
- 同じデコーダを 2 スレッドが触る コンテナの読み位置が食い違い、絵が飛ぶ
- 読んでいる最中のデコーダを閉じる 解放済みのコンテナを触って落ちる
- 先読みの失敗を誰も受け取らない 読めない素材が黒いまま書き出される
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import cast

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
from sashimono.engine.decode import VideoDecoder, probe_media
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer
from sashimono.engine.render.renderer import MAX_OPEN_DECODERS
from tests.media_fixtures import SampleMedia, make_sample

pytestmark = pytest.mark.usefixtures("gpu")

#: 代役が返す絵 中身は見ないので、小さくて構わない
_DUMMY = np.zeros((16, 16, 4), dtype=np.uint8)


@pytest.fixture(scope="session")
def two_sources(media_dir: Path) -> tuple[SampleMedia, SampleMedia]:
    """別々のファイルの素材 2 本 同じファイルだとデコーダを使い回して並ばない"""
    return (
        make_sample(media_dir, "layer-a.mp4", duration=2.0, audio=False),
        make_sample(media_dir, "layer-b.mp4", duration=2.0, audio=False, pattern="smptebars"),
    )


@pytest.fixture(scope="session")
def many_sources(media_dir: Path) -> tuple[SampleMedia, ...]:
    """開けるデコーダの上限を 1 本超える本数の素材 追い出しの道を通すため"""
    return tuple(
        make_sample(media_dir, f"many-{index}.mp4", width=64, height=48, duration=1.0, audio=False)
        for index in range(MAX_OPEN_DECODERS + 1)
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


def _use_stubs(renderer: FrameRenderer, project: Project, make: Callable[[], object]) -> None:
    """レンダラが開いたデコーダを、試験用の代役へ差し替える

    代役は ``frame_at`` と ``close`` しか持たない 先読みと描画が触るのはこの 2 つだけ
    なので、型の食い違いはここ 1 か所だけで抑える 代役の側に増やすと、
    :class:`VideoDecoder` の約束から外れたときに、外した理由が読み取れなくなる
    """
    for key in list(renderer._decoders):
        renderer._decoders[key].close()
    renderer._decoders.clear()
    for media in project.media:
        renderer._decoders[(media.id, 0)] = cast("VideoDecoder", make())


class _Blocking:
    """2 本そろうまで返さないデコーダの代役

    直列にデコードしていると 1 本目が待ち続けて待ち合わせが成立せず、
    :class:`threading.BrokenBarrierError` で落ちる
    """

    def __init__(self, barrier: threading.Barrier) -> None:
        self._barrier = barrier

    def frame_at(self, seconds: Fraction) -> np.ndarray:
        self._barrier.wait(timeout=5)
        return _DUMMY

    def close(self) -> None:
        return None


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
        try:
            _use_stubs(renderer, project, lambda: _Blocking(barrier))
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
        try:
            _use_stubs(renderer, project, lambda: _Blocking(barrier))
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
            _use_stubs(renderer, project, _Failing)
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


class _Idle:
    """何もしないデコーダの代役 開いているか閉じたかだけを見るとき用"""

    def __init__(self) -> None:
        self.closed = False

    def frame_at(self, seconds: Fraction) -> np.ndarray:
        assert not self.closed, "閉じたデコーダから読もうとした"
        return _DUMMY

    def close(self) -> None:
        self.closed = True


class TestTooManyLayers:
    """開けるデコーダの上限を超える本数を重ねたとき（#98 のレビュー）"""

    def test_a_decoder_opened_now_is_never_the_one_evicted(
        self, many_sources: tuple[SampleMedia, ...], context: OffscreenGLContext
    ) -> None:
        # 先に開いた分が全部先読み中だと、追い出す相手が「いま開いたもの」しか残らない
        # そこを外さないと、開いた直後に閉じたデコーダを呼ぶ側へ返してしまい、
        # 9 本目のレイヤーのデコードがそのフレームごと失敗する
        project = _stacked(many_sources)
        renderer = FrameRenderer(project, context=context, decode_threads=MAX_OPEN_DECODERS)
        items = list(project.media)
        try:
            pool = renderer._pool()
            for item in items[:MAX_OPEN_DECODERS]:
                key = (item.id, 0)
                renderer._decoders[key] = cast("VideoDecoder", _Idle())
                renderer._decoding[key] = (Fraction(0), pool.submit(lambda: _DUMMY))
            extra = (items[MAX_OPEN_DECODERS].id, 0)
            opened = renderer._decoder_for(*extra)
            assert opened is not None
            assert renderer._decoders.get(extra) is opened, "開いた直後に追い出された"
            # 閉じたコンテナから読むと PyAV が投げる ここを通れば閉じられていない
            assert opened.frame_at(Fraction(0)) is not None
        finally:
            renderer._decoding.clear()
            renderer.close()

    def test_the_overflow_is_released_once_the_prefetch_is_taken(
        self, many_sources: tuple[SampleMedia, ...], context: OffscreenGLContext
    ) -> None:
        # 先読みを守るために上限を超えたまま返すことがある 受け取り終えた所で
        # 減らさないと、同じ素材を描き続ける間は誰も減らさず、超えた分の
        # コンテナ・スレッド・ファイルハンドルがレンダラを閉じるまで残る
        project = _stacked(many_sources)
        renderer = FrameRenderer(project, context=context, decode_threads=MAX_OPEN_DECODERS)
        items = list(project.media)
        try:
            pool = renderer._pool()
            for item in items[:MAX_OPEN_DECODERS]:
                key = (item.id, 0)
                renderer._decoders[key] = cast("VideoDecoder", _Idle())
                renderer._decoding[key] = (Fraction(0), pool.submit(lambda: _DUMMY))
            renderer._decoder_for(items[MAX_OPEN_DECODERS].id, 0)
            assert len(renderer._decoders) == MAX_OPEN_DECODERS + 1, "超えた所を作れていない"
            renderer._drain_decodes()
            assert len(renderer._decoders) <= MAX_OPEN_DECODERS, "超えた分が残ったまま"
        finally:
            renderer.close()

    def test_a_decoder_is_never_handed_out_after_being_closed(
        self, many_sources: tuple[SampleMedia, ...], context: OffscreenGLContext
    ) -> None:
        # 先に開いた分が全部先読み中だと、追い出す相手が「いま開いたもの」しか
        # 残らない 開いた直後に閉じて呼ぶ側へ返すと、そのクリップのデコードが
        # 失敗して 9 本目のレイヤーから先が描けなくなる
        project = _stacked(many_sources)
        renderer = FrameRenderer(project, context=context, decode_threads=4)
        try:
            with context:
                renderer.render(0)
                renderer.render(1)
        finally:
            renderer.close()

    def test_every_layer_still_shows_up(
        self, many_sources: tuple[SampleMedia, ...], context: OffscreenGLContext
    ) -> None:
        # 並べた場合と並べない場合で絵が変わるなら、どこかのレイヤーが抜けている
        project = _stacked(many_sources)
        serial = FrameRenderer(project, context=context, decode_threads=1)
        try:
            expected = [serial.render(number) for number in range(3)]
        finally:
            serial.close()
        parallel = FrameRenderer(project, context=context, decode_threads=4)
        try:
            produced = [parallel.render(number) for number in range(3)]
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
        # 代役は自分から止まらず時間で返す 止める合図を外から出すと、閉じる前に
        # 先読みが終わってしまい、待たない作りに戻しても試験が通る
        project = _stacked(two_sources)
        renderer = FrameRenderer(project, context=context, decode_threads=4)
        finished = threading.Event()

        class _Slow:
            def frame_at(self, seconds: Fraction) -> np.ndarray:
                time.sleep(0.3)
                finished.set()
                return _DUMMY

            def close(self) -> None:
                assert finished.is_set(), "先読みが走っている最中にデコーダを閉じた"

        _use_stubs(renderer, project, _Slow)
        pool = renderer._pool()
        first = next(iter(renderer._decoders))
        renderer._decoding[first] = (
            Fraction(0),
            pool.submit(renderer._decoders[first].frame_at, Fraction(0)),
        )
        assert not finished.is_set(), "代役が待たずに返った 試験になっていない"
        renderer.close()
        assert finished.is_set()
