"""読み込む素材をまとめて裏で調べる

素材を 1 本調べる（:func:`~sashimono.engine.decode.probe_media`）のは 1080p の mp4 で
55ms ほど 画面のスレッドで順に調べると、24 本で 1.4 秒画面が固まる
ネットワーク越しの素材ではもっと延びる 調べるのは裏のスレッドに任せ、
画面は :meth:`ProbeBatch.progress` を決まった間隔で読みに来るだけにする

Qt に依存しない 結果を 1 回の操作としてプロジェクトへ入れるのは画面の側
（:meth:`sashimono.ui.main_window.MainWindow.import_media`）
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from sashimono.core.model import MediaItem
from sashimono.engine.decode.probe import ProbeError

__all__ = ["PROBE_WORKERS", "ProbeBatch"]

#: 同時に調べる本数 1080p の mp4 24 本で、1 本ずつなら 1.4 秒、4 本なら 0.43 秒、
#: 8 本なら 0.29 秒（2026-09-24 に測った） 8 本まで増やしても縮む幅は小さく、
#: ネットワーク越しの素材では相手の機械へ同時に掛ける読み出しが増えるだけなので 4 で止める
PROBE_WORKERS = 4


class ProbeBatch:
    """素材の一覧を裏で調べる 結果は頼んだ順に返す

    順を保つのは、置く順が選んだ順と変わると、読み込むたびに並びが入れ替わるため
    （調べ終わった順は素材の大きさと、たまたまの速さで決まる）
    """

    def __init__(
        self,
        paths: Sequence[Path],
        probe: Callable[[Path], MediaItem],
        *,
        workers: int = PROBE_WORKERS,
    ) -> None:
        self._paths = tuple(paths)
        self._lock = threading.Lock()
        self._done = 0
        self._cancelled = False
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="sashimono-probe"
        )
        self._futures: list[Future[MediaItem]] = []
        for path in self._paths:
            future = self._executor.submit(probe, path)
            future.add_done_callback(self._count)
            self._futures.append(future)
        # 投げ終えたら閉じる 閉じずに置くと、終わった後もスレッドが残る
        self._executor.shutdown(wait=False)

    @property
    def paths(self) -> tuple[Path, ...]:
        return self._paths

    @property
    def total(self) -> int:
        return len(self._paths)

    def progress(self) -> int:
        """調べ終わった本数 画面のタイマーから呼ぶので、ここではブロックしない"""
        with self._lock:
            return self._done

    @property
    def finished(self) -> bool:
        return all(future.done() for future in self._futures)

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        """まだ始まっていない分を捨てる

        調べている最中の 1 本は止められない（PyAV は途中で止める口を持たない）
        終わっても結果を使わないだけ 待たずに返るので、画面は固まらない
        """
        with self._lock:
            self._cancelled = True
        for future in self._futures:
            future.cancel()

    def results(self) -> list[MediaItem | ProbeError]:
        """頼んだ順の結果 調べられなかった素材は :class:`ProbeError` で返す

        :attr:`finished` が真になってから呼ぶ 前に呼ぶと、終わるまでここで待つ
        :class:`ProbeError` 以外の例外はそのまま投げる 知らない失敗を
        「開けない素材」として数えると、直すべき不具合が 1 行の文言に紛れる
        """
        outcome: list[MediaItem | ProbeError] = []
        for future in self._futures:
            try:
                outcome.append(future.result())
            except ProbeError as exc:
                outcome.append(exc)
        return outcome

    def _count(self, future: Future[MediaItem]) -> None:
        del future
        with self._lock:
            self._done += 1
