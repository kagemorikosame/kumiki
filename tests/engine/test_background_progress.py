"""裏の処理の進み具合を数える所と、読み込む素材をまとめて裏で調べる所

画面に出す前の数が合っていないと、ステータスバーは「3 本のうち 5 本」のような
あり得ない数を出す 画面を組み立てずに数だけを確かめる
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from sashimono.core.model import MediaId, MediaItem
from sashimono.engine.cache import analyzer as analyzer_module
from sashimono.engine.cache import proxy as proxy_module
from sashimono.engine.cache.analyzer import MediaAnalyzer
from sashimono.engine.cache.progress import JobBoard, ProgressSnapshot
from sashimono.engine.cache.proxy import ProxyBuilder, ProxyStore
from sashimono.engine.cache.store import CacheStore
from sashimono.engine.decode import ProbeError
from sashimono.engine.decode.batch import ProbeBatch

NL = chr(10)

A = MediaId("a")
B = MediaId("b")


def _wait(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _tall(media: MediaItem) -> MediaItem:
    """控えを作る値打ちのある大きさ（4K）にする 1080p までは控えを頼まない"""
    stream = media.video_streams[0]
    return replace(media, video_streams=(replace(stream, width=3840, height=2160),))


class TestJobBoard:
    def test_it_counts_the_finished_and_the_failed(self) -> None:
        # 失敗を終わった数と理由に入れないと、開けない素材があっても画面に何も出ず気付けない
        board = JobBoard()
        board.start("x", A)
        board.start("y", B)
        board.finish("x")
        board.finish("y", "開けない")
        snapshot = board.poll()
        assert (snapshot.total, snapshot.finished, snapshot.failed) == (2, 2, 1)
        assert snapshot.failures == {B: "開けない"}
        assert not snapshot.busy

    def test_the_fraction_counts_the_running_part(self) -> None:
        # 終わった本数だけで数えると、長い 1 本の控えを作る間ずっと 0% のまま動かない
        board = JobBoard()
        board.start("x", A)
        board.report("x", 0.5)
        assert board.poll().fraction == pytest.approx(0.5)

    def test_two_jobs_of_one_media_share_the_row(self) -> None:
        # 波形とサムネイルは同じ素材の 2 つの仕事 行には平均を 1 つだけ出す
        board = JobBoard()
        board.start(("waveform", A), A)
        board.start(("filmstrip", A), A)
        board.report(("waveform", A), 1.0)
        snapshot = board.poll()
        assert snapshot.running == {A: pytest.approx(0.5)}
        assert snapshot.fraction == pytest.approx(0.5)

    def test_a_stopped_job_is_not_counted(self) -> None:
        # 終わった数に入れると、素材を外しただけで仕事をしたように見える
        board = JobBoard()
        board.start("x", A)
        board.start("y", B)
        board.drop("x")
        board.finish("y")
        snapshot = board.poll()
        assert (snapshot.total, snapshot.finished, snapshot.failed) == (1, 1, 0)

    def test_reading_does_not_restart_the_count(self) -> None:
        # 読んだだけで数え直すと、控えが止まった時点で控えの失敗が消える 解析が
        # まだ走っていれば、終わりの知らせに控えの失敗が出ず、全体の割合も巻き戻る
        board = JobBoard()
        board.start("x", A)
        board.finish("x", "開けない")
        first = board.poll()
        second = board.poll()
        assert (second.total, second.finished, second.failed) == (1, 1, 1)
        assert first == second

    def test_settling_restarts_the_count(self) -> None:
        # 数え直さないと、前のひと続きの本数が次のひと続きに残り、「n 本のうち m 本」が膨らむ
        board = JobBoard()
        board.start("x", A)
        board.finish("x", "開けない")
        assert board.settle(board.poll())
        snapshot = board.poll()
        assert (snapshot.total, snapshot.finished, snapshot.failed) == (0, 0, 0)
        # 行の失敗は数え直しても残す 終わった後に一覧を見て理由が分からないと困る
        assert snapshot.failures == {A: "開けない"}

    def test_settling_keeps_what_moved_after_reading(self) -> None:
        # 読んだ後に始まって終わった仕事まで消すと、画面はその失敗を 1 度も見ない
        board = JobBoard()
        board.start("x", A)
        board.finish("x")
        seen = board.poll()
        board.start("y", B)
        board.finish("y", "開けない")
        assert not board.settle(seen)
        assert board.poll().failed == 1

    def test_settling_while_running_keeps_the_count(self) -> None:
        # 走っている最中に数え直すと、走っている 1 本だけの割合になり、全体の割合が巻き戻って
        # 「n 本のうち m 本」の数も崩れる
        board = JobBoard()
        board.start("x", A)
        assert not board.settle(board.poll())
        assert board.poll().total == 1

    def test_a_failure_stays_on_the_row_until_asked_again(self) -> None:
        # 頼み直しても前の失敗を残すと、作り直している最中の素材に「作れなかった」が出続ける
        board = JobBoard()
        board.start("x", A)
        board.finish("x", "開けない")
        assert board.poll().failures == {A: "開けない"}
        board.start("x", A)
        snapshot = board.poll()
        assert snapshot.failures == {}
        # 頼み直した回だけを数える 前の回の終わりも数えると「1 本のうち 2 本」になる
        assert (snapshot.total, snapshot.finished, snapshot.failed) == (1, 0, 0)

    def test_asking_one_job_again_keeps_the_other_failure(self) -> None:
        # 素材ごとに消すと、サムネイルを頼み直しただけで波形の失敗まで消える
        board = JobBoard()
        board.start(("waveform", A), A)
        board.finish(("waveform", A), "波形を作れなかった")
        board.start(("filmstrip", A), A)
        assert board.poll().failures == {A: "波形を作れなかった"}

    def test_forgetting_a_media_drops_its_failures(self) -> None:
        # 素材を外した後も失敗を残すと、もう無い素材の失敗が数だけ知らせに残り、理由は出ない
        board = JobBoard()
        board.start("x", A)
        board.finish("x", "開けない")
        board.forget(A)
        assert board.poll().failures == {}

    def test_forgetting_a_media_takes_back_its_counts(self) -> None:
        # 理由だけ消して数を残すと、「失敗 1 件」と出るのに何が失敗したのかがどこにも出ない
        board = JobBoard()
        board.start("x", A)
        board.finish("x", "開けない")
        board.start("y", B)
        board.finish("y")
        board.start("z", A)
        board.forget(A)
        snapshot = board.poll()
        assert (snapshot.total, snapshot.finished, snapshot.failed) == (1, 1, 0)
        assert snapshot.running == {}
        # 外した後に止まった仕事が終わりを知らせに来ても数えない
        board.finish("z", "止めた")
        assert board.poll().failed == 0

    def test_nothing_asked_is_complete(self) -> None:
        # 何も頼んでいないのに終わっていない扱いだと、読み込み直後から進み具合の棒が出たまま消えない
        assert ProgressSnapshot().fraction == 1.0
        assert not ProgressSnapshot().busy


class TestProxyProgress:
    def test_the_progress_and_the_end_are_counted(
        self, video_media: MediaItem, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 控えの割合が板へ届かないと、4K を読み込んだ直後に重い理由が画面から分からない
        release = threading.Event()

        def fake(source: Path, target: Path, **kwargs: object) -> Path:
            report = kwargs["progress"]
            assert callable(report)
            report(0.25)
            release.wait(10.0)
            return target

        monkeypatch.setattr(proxy_module, "create_proxy", fake)
        builder = ProxyBuilder(ProxyStore(CacheStore(tmp_path), height=120))
        media = _tall(video_media)
        try:
            builder.request(media)
            assert _wait(lambda: builder.poll().running.get(media.id) == 0.25)
            snapshot = builder.poll()
            assert (snapshot.total, snapshot.finished) == (1, 0)
            release.set()
            assert _wait(lambda: not builder.poll().busy)
        finally:
            release.set()
            builder.close()

    def test_an_exception_is_counted_as_a_failure(
        self, video_media: MediaItem, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 前は executor の中で黙って消え、控えが無いまま理由も出なかった
        def broken(source: Path, target: Path, **kwargs: object) -> Path:
            raise RuntimeError("符号化器が無い")

        monkeypatch.setattr(proxy_module, "create_proxy", broken)
        builder = ProxyBuilder(ProxyStore(CacheStore(tmp_path), height=120))
        media = _tall(video_media)
        try:
            builder.request(media)
            assert _wait(lambda: not builder.poll().busy)
        finally:
            builder.close()
        assert builder.poll().failed == 1
        assert "符号化器が無い" in builder.poll().failures[media.id]

    def test_nothing_made_is_a_failure(
        self, video_media: MediaItem, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 作れなかった（None が返った）控えを失敗と数えないと、控えが無いまま「終わった」と出る
        monkeypatch.setattr(proxy_module, "create_proxy", lambda *_args, **_kwargs: None)
        builder = ProxyBuilder(ProxyStore(CacheStore(tmp_path), height=120))
        media = _tall(video_media)
        try:
            builder.request(media)
            assert _wait(lambda: media.id in builder.poll().failures)
        finally:
            builder.close()

    def test_a_forgotten_media_is_not_a_failure(
        self, video_media: MediaItem, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 素材を外しただけで「作れなかった」と出ると、壊れたように見える
        started = threading.Event()

        def stoppable(source: Path, target: Path, **kwargs: object) -> Path | None:
            started.set()
            should_cancel = kwargs["should_cancel"]
            assert callable(should_cancel)
            while not should_cancel():
                time.sleep(0.01)
            return None

        monkeypatch.setattr(proxy_module, "create_proxy", stoppable)
        builder = ProxyBuilder(ProxyStore(CacheStore(tmp_path), height=120))
        media = _tall(video_media)
        try:
            builder.request(media)
            assert started.wait(10.0)
            builder.forget(media.id)
            assert _wait(lambda: builder.progress(media.id) is None)
            snapshot = builder.poll()
        finally:
            builder.close()
        assert snapshot.failed == 0
        assert snapshot.failures == {}


class TestAnalysisProgress:
    def test_the_progress_reaches_the_board(
        self, video_media: MediaItem, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 解析の割合が板へ届かないと、波形とサムネイルを作っている間ずっと 0% のまま出る
        release = threading.Event()

        def slow(path: Path, **kwargs: object) -> None:
            report = kwargs["progress"]
            assert callable(report)
            report(0.5)
            release.wait(10.0)
            return None

        monkeypatch.setattr(analyzer_module, "analyze_waveform", slow)
        monkeypatch.setattr(analyzer_module, "build_filmstrip", slow)
        analyzer = MediaAnalyzer(CacheStore(tmp_path))
        try:
            analyzer.request(video_media)
            assert _wait(lambda: analyzer.poll().running.get(video_media.id) == 0.5)
            assert analyzer.poll().total == 2
        finally:
            release.set()
            analyzer.close()

    def test_an_unreadable_media_is_a_failure(
        self, video_media: MediaItem, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 開けない素材を失敗と数えないと、波形が出ない理由が画面に出ない
        def unreadable(path: Path, **kwargs: object) -> None:
            raise ProbeError(f"素材を開けない: {path}")

        monkeypatch.setattr(analyzer_module, "analyze_waveform", unreadable)
        monkeypatch.setattr(analyzer_module, "build_filmstrip", lambda *_a, **_k: None)
        analyzer = MediaAnalyzer(CacheStore(tmp_path))
        try:
            analyzer.request(video_media)
            assert _wait(lambda: not analyzer.poll().busy and video_media.id in _failures(analyzer))
            reason = analyzer.poll().failures[video_media.id]
        finally:
            analyzer.close()
        assert "波形を作れなかった" in reason
        assert "サムネイルを作れなかった" in reason


def _failures(analyzer: MediaAnalyzer) -> dict[MediaId, str]:
    return dict(analyzer.poll().failures)


class TestProbeBatch:
    def test_the_results_keep_the_asked_order(self, tmp_path: Path) -> None:
        # 調べ終わった順に置くと、読み込むたびに並びが入れ替わる
        first_done = threading.Event()

        def probe(path: Path) -> MediaItem:
            if path.name == "1.mp4":
                # 1 本目は 2 本目より後に終わらせる
                first_done.wait(10.0)
            else:
                first_done.set()
            return MediaItem(path=path)

        paths = [tmp_path / "1.mp4", tmp_path / "2.mp4"]
        batch = ProbeBatch(paths, probe)
        assert _wait(lambda: batch.finished)
        results = batch.results()
        assert [item.path for item in results if isinstance(item, MediaItem)] == paths

    def test_it_runs_off_the_calling_thread(self, tmp_path: Path) -> None:
        # 呼んだスレッドで調べると、素材を読み込む間また画面が固まる（この PR で直したい所そのもの）
        seen: list[threading.Thread] = []

        def probe(path: Path) -> MediaItem:
            seen.append(threading.current_thread())
            return MediaItem(path=path)

        batch = ProbeBatch([tmp_path / "1.mp4"], probe)
        assert _wait(lambda: batch.finished)
        assert seen and seen[0] is not threading.current_thread()

    def test_an_unreadable_file_is_returned_not_raised(self, tmp_path: Path) -> None:
        # 1 本開けないだけで読み込み全体を止めない 前と同じく数えて知らせる
        def probe(path: Path) -> MediaItem:
            if path.name == "壊れた.mp4":
                raise ProbeError("素材を開けない")
            return MediaItem(path=path)

        batch = ProbeBatch([tmp_path / "壊れた.mp4", tmp_path / "良い.mp4"], probe)
        assert _wait(lambda: batch.finished)
        broken, good = batch.results()
        assert isinstance(broken, ProbeError)
        assert isinstance(good, MediaItem)
        assert batch.progress() == 2

    def test_an_unknown_error_is_not_hidden(self, tmp_path: Path) -> None:
        # 知らない失敗を「開けない素材」に数えると、直すべき不具合が 1 行の文言に紛れる
        def probe(path: Path) -> MediaItem:
            raise ZeroDivisionError("こわれた")

        batch = ProbeBatch([tmp_path / "1.mp4"], probe)
        assert _wait(lambda: batch.finished)
        with pytest.raises(ZeroDivisionError):
            batch.results()

    def test_a_base_exception_still_finishes_the_batch(self, tmp_path: Path) -> None:
        # Exception だけを受けると、BaseException（試験の pytest.fail など）で係が数えずに
        # 抜け、finished がずっと偽のまま 読み込みの表示が消えず、後に待つ読み込みも始まらない
        class Stop(BaseException):
            pass

        def probe(path: Path) -> MediaItem:
            raise Stop

        batch = ProbeBatch([tmp_path / "1.mp4"], probe)
        assert _wait(lambda: batch.finished)
        with pytest.raises(Stop):
            batch.results()

    def test_cancel_skips_what_has_not_started(self, tmp_path: Path) -> None:
        # 取り消した後もまだ始まっていない素材を調べ続けると、取り消しても待たされ、裏で無駄に開く
        release = threading.Event()
        probed: list[Path] = []

        def probe(path: Path) -> MediaItem:
            probed.append(path)
            release.wait(10.0)
            return MediaItem(path=path)

        paths = [tmp_path / f"{index}.mp4" for index in range(6)]
        batch = ProbeBatch(paths, probe, workers=1)
        assert _wait(lambda: len(probed) == 1)
        batch.cancel()
        release.set()
        assert _wait(lambda: batch.progress() == 1)
        time.sleep(0.1)
        assert probed == paths[:1], "取り消したのに、始まっていない素材まで調べた"
        assert batch.cancelled
        # 取り消した読み込みは調べ終わらない 終わったと見て置きに来ないように
        assert not batch.finished

    def test_cancel_does_not_wait_for_a_hung_file(self, tmp_path: Path) -> None:
        # ネットワーク越しの素材が応答しないと、調べる所は戻ってこない 取り消しが
        # それを待つと、取り消すボタンを押した所で画面が固まる
        hang = threading.Event()
        started = threading.Event()

        def probe(path: Path) -> MediaItem:
            started.set()
            hang.wait(30.0)
            return MediaItem(path=path)

        batch = ProbeBatch([tmp_path / "1.mp4"], probe)
        try:
            assert started.wait(5.0)
            began = time.monotonic()
            batch.cancel()
            assert time.monotonic() - began < 0.5
        finally:
            hang.set()

    def test_a_hung_file_does_not_keep_the_app_from_exiting(self) -> None:
        # ThreadPoolExecutor のスレッドは Python の終わりに待ち合わされる 応答しない
        # 素材を調べている間にソフトを閉じると、閉じたのに process が残り続ける
        script = (
            "import threading, time"
            + NL
            + "from pathlib import Path"
            + NL
            + "from sashimono.engine.decode.batch import ProbeBatch"
            + NL
            + "started = threading.Event()"
            + NL
            + "def probe(path):"
            + NL
            + "    started.set()"
            + NL
            + "    threading.Event().wait()"
            + NL
            + "batch = ProbeBatch([Path('net.mp4')], probe)"
            + NL
            + "assert started.wait(10)"
            + NL
            + "batch.cancel()"
            + NL
        )
        # 待ち合わせで止まると、ここで TimeoutExpired になって落ちる
        completed = subprocess.run(
            [sys.executable, "-c", script], timeout=30, capture_output=True, check=False
        )
        assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
