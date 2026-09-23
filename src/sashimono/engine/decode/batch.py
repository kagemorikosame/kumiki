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

    ``ThreadPoolExecutor`` は使わない あちらのスレッドは Python の終わりに
    待ち合わされるので、応答しないネットワーク越しの素材を 1 本調べている間は
    ソフトを閉じても終われない 自前のスレッドを daemon にして、誰も待たない
    （調べるのは読むだけで、途中で切れても書きかけの物が残らない）
    """

    def __init__(
        self,
        paths: Sequence[Path],
        probe: Callable[[Path], MediaItem],
        *,
        workers: int = PROBE_WORKERS,
    ) -> None:
        self._paths = tuple(paths)
        self._probe = probe
        self._lock = threading.Lock()
        self._next = 0
        self._done = 0
        self._cancelled = False
        #: 頼んだ順の結果 調べ終わるまでは ``None``
        self._outcomes: list[MediaItem | BaseException | None] = [None] * len(self._paths)
        self._threads = [
            threading.Thread(target=self._work, name=f"sashimono-probe-{index}", daemon=True)
            for index in range(max(1, min(workers, len(self._paths))))
        ]
        for thread in self._threads:
            thread.start()

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
        """全部を調べ終えた 取り消した後は、調べ終えていない物が残るので真にならない"""
        with self._lock:
            return self._done == len(self._paths)

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        """まだ始まっていない分を捨てる 待たずに返る

        調べている最中の 1 本は止められない（PyAV は途中で止める口を持たない）
        終わっても結果を使わないだけ スレッドは daemon なので、応答しない素材を
        調べたまま残っても、ソフトの終わりを止めない
        """
        with self._lock:
            self._cancelled = True

    def results(self) -> list[MediaItem | ProbeError]:
        """頼んだ順の結果 調べられなかった素材は :class:`ProbeError` で返す

        :attr:`finished` が真になってから呼ぶ 前に呼ぶと :class:`RuntimeError`
        :class:`ProbeError` 以外の例外はそのまま投げる 知らない失敗を
        「開けない素材」として数えると、直すべき不具合が 1 行の文言に紛れる
        """
        with self._lock:
            outcomes = list(self._outcomes)
            if self._done != len(self._paths):
                raise RuntimeError("まだ調べ終わっていない")
        results: list[MediaItem | ProbeError] = []
        for outcome in outcomes:
            if isinstance(outcome, (MediaItem, ProbeError)):
                results.append(outcome)
            elif isinstance(outcome, BaseException):
                raise outcome
        return results

    def _work(self) -> None:
        while True:
            with self._lock:
                if self._cancelled or self._next >= len(self._paths):
                    return
                index = self._next
                self._next += 1
            outcome: MediaItem | BaseException
            try:
                outcome = self._probe(self._paths[index])
            # BaseException まで受けて結果に残す 数えずに抜けると finished が真にならず、
            # 読み込みの表示が消えないまま、後に待つ読み込みも始まらない
            except BaseException as exc:  # 投げ直すのは results（画面のスレッド）の側
                outcome = exc
            with self._lock:
                self._outcomes[index] = outcome
                self._done += 1
