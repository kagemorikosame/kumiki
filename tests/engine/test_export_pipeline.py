"""書き出しの合成と書き込みを重ねる所（``_WritePipeline``）

GPU を使わない 受け渡しと、失敗したときに止まらないことだけを見る
ここが壊れると、書き出しが黙って途中で終わるか、永久に戻らなくなる
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from sashimono.engine.encode.exporter import _WritePipeline


def _image(value: int) -> np.ndarray:
    return np.full((2, 2, 4), value, dtype=np.uint8)


class TestPipelining:
    def test_composing_does_not_wait_for_the_writer(self) -> None:
        # 重ねる意味が無くなると、書き込みが終わるまで次の合成が始まらない
        # 直列に戻ると 1 枚あたりの時間が色変換とエンコードのぶんだけ増える
        release = threading.Event()
        written: list[int] = []

        def write(index: int, frame_number: int, image: np.ndarray) -> None:
            release.wait(5)
            written.append(index)

        with _WritePipeline(write, depth=2) as pipeline:
            # 1 枚目は書き込みスレッドが抱え、2 枚目はキューに入る
            # どちらも書き込みの終わりを待たずに戻らなければならない
            pipeline.reserve()
            pipeline.submit(0, 0, _image(0))
            pipeline.reserve()
            pipeline.submit(1, 1, _image(1))
            assert written == []
            release.set()

        assert written == [0, 1]

    def test_the_seat_holds_the_producer_back_before_composing(self) -> None:
        # 席を取らずに描いてから渡すと、キューの分と書き込み中の分に加えて
        # 手元の 1 枚が余分に残る 4K なら 1 枚 33MB で、選んだ枚数より多く抱える
        release = threading.Event()
        started = threading.Event()

        def write(index: int, frame_number: int, image: np.ndarray) -> None:
            started.set()
            release.wait(5)

        pipeline = _WritePipeline(write, depth=1)
        with pipeline:
            # 1 枚目は書き込み中、2 枚目はキュー 深さ 1 ならここで席が尽きる
            pipeline.reserve()
            pipeline.submit(0, 0, _image(0))
            assert started.wait(5)
            pipeline.reserve()
            pipeline.submit(1, 1, _image(1))
            blocked = threading.Thread(target=pipeline.reserve)
            blocked.start()
            blocked.join(0.3)
            # 3 枚目は席が空くまで**描き始められない**
            assert blocked.is_alive()
            release.set()
            blocked.join(5)
            assert not blocked.is_alive()

    def test_the_order_is_kept(self) -> None:
        # 順番が入れ替わると、フレームの並びが崩れた動画ができる
        written: list[int] = []
        with _WritePipeline(lambda index, _f, _i: written.append(index), depth=4) as pipeline:
            for index in range(10):
                pipeline.reserve()
                pipeline.submit(index, index, _image(index))
        assert written == list(range(10))

    def test_depth_zero_writes_in_the_calling_thread(self) -> None:
        # 設定で切れることを押さえる 切っても裏でスレッドが動くなら、
        # 設定がある方が質が悪い
        threads: list[int] = []
        with _WritePipeline(
            lambda _i, _f, _img: threads.append(threading.get_ident()), depth=0
        ) as pipeline:
            pipeline.reserve()
            pipeline.submit(0, 0, _image(0))
        assert threads == [threading.get_ident()]


class TestFailures:
    def test_a_failure_in_the_writer_is_raised_again(self) -> None:
        # 握り潰すと、書き出しが成功したことになって壊れたファイルが残る
        def write(index: int, frame_number: int, image: np.ndarray) -> None:
            raise RuntimeError("書き込みに失敗")

        pipeline = _WritePipeline(write, depth=2)
        with pytest.raises(RuntimeError, match="書き込みに失敗"), pipeline:
            for index in range(4):
                pipeline.reserve()
                pipeline.submit(index, index, _image(index))

    def test_a_failure_does_not_block_the_producer(self) -> None:
        # 失敗したら受け取りをやめる作りにすると、キューがいっぱいのまま
        # 合成の側が put で永久に止まる（書き出しが固まって中止も効かない）
        def write(index: int, frame_number: int, image: np.ndarray) -> None:
            raise RuntimeError("書き込みに失敗")

        pipeline = _WritePipeline(write, depth=1)
        done = threading.Event()

        def produce() -> None:
            try:
                with pipeline:
                    for index in range(50):
                        pipeline.reserve()
                        pipeline.submit(index, index, _image(index))
            except RuntimeError:
                pass
            done.set()

        worker = threading.Thread(target=produce)
        worker.start()
        assert done.wait(10), "書き込みが失敗した後、合成の側が戻ってこない"
        worker.join(5)

    def test_the_failure_is_visible_before_closing(self) -> None:
        # 合成を続けても捨てられるだけ 気付けないと、失敗が最後まで遠ざかる
        failed = threading.Event()

        def write(index: int, frame_number: int, image: np.ndarray) -> None:
            failed.set()
            raise RuntimeError("書き込みに失敗")

        pipeline = _WritePipeline(write, depth=1)
        try:
            pipeline.__enter__()
            pipeline.reserve()
            pipeline.submit(0, 0, _image(0))
            assert failed.wait(5)
            # 投げた直後に旗が立つとは限らない 書き込みスレッドが例外を捕まえて
            # 覚えるまでの間だけ待つ
            deadline = time.monotonic() + 5
            while not pipeline.failed and time.monotonic() < deadline:
                time.sleep(0.01)
            assert pipeline.failed
        finally:
            with pytest.raises(RuntimeError, match="書き込みに失敗"):
                pipeline.close()

    def test_an_exception_while_composing_still_stops_the_thread(self) -> None:
        # スレッドが残ったまま書きかけのファイルを消しに行くと、同じコンテナを
        # 取り合って FFmpeg の中で落ちる
        pipeline = _WritePipeline(lambda _i, _f, _img: None, depth=2)
        with pytest.raises(ValueError, match="合成に失敗"), pipeline:
            pipeline.reserve()
            pipeline.submit(0, 0, _image(0))
            raise ValueError("合成に失敗")
        assert threading.active_count() >= 1
        assert all(t.name != "sashimono-export-writer" for t in threading.enumerate())
