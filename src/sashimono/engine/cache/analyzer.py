"""波形とサムネイルをバックグラウンドで用意する

素材を読み込んだ直後に UI が固まるのが一番まずい 解析は必ず別スレッドで走らせ、
できたものから順に通知する

キャッシュがあれば解析せずに即返す プロジェクトを開き直すたびに数十秒待つのは
実用に耐えない
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

from sashimono.core.model import MediaId, MediaItem
from sashimono.engine.audio.waveform import Waveform, analyze_waveform

from .progress import JobBoard, ProgressSnapshot
from .store import CacheStore
from .thumbnails import (
    DEFAULT_INTERVAL,
    THUMBNAIL_HEIGHT,
    Filmstrip,
    build_filmstrip,
    filmstrip_key,
    load_filmstrip,
    save_filmstrip,
)
from .waveform_cache import load_waveform, save_waveform, waveform_key

__all__ = ["MediaAnalyzer"]

#: 失敗したときに画面へ出す名前
_KIND_NAMES = {"waveform": "波形", "filmstrip": "サムネイル"}

#: 同時に走らせる解析の数 増やしすぎるとディスクの取り合いで全体が遅くなる
MAX_WORKERS = 2


class MediaAnalyzer:
    """素材の波形とサムネイルを非同期に用意する

    結果はメモリにも保持するので、2 度目以降はディスクも読まない
    """

    def __init__(
        self,
        store: CacheStore | None = None,
        *,
        sample_rate: int = 48000,
        channels: int = 2,
        max_workers: int = MAX_WORKERS,
    ) -> None:
        self._store = store if store is not None else CacheStore()
        self._sample_rate = sample_rate
        self._channels = channels
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="sashimono-analyze"
        )
        self._lock = threading.Lock()
        self._waveforms: dict[MediaId, Waveform] = {}
        self._filmstrips: dict[MediaId, Filmstrip] = {}
        #: 実行中の解析 Future ではなく鍵の集合で持つ Future を辞書に
        #: 入れ直す形にすると、投入前に完了した場合に消し損ねる
        self._running: set[tuple[str, MediaId]] = set()
        self._cancelled: set[tuple[str, MediaId]] = set()
        self._closed = False
        #: 画面へ出す進み具合 数えるのは裏のスレッド、読むのは画面のタイマー
        self._board = JobBoard()

    def poll(self) -> ProgressSnapshot:
        """画面へ出す進み具合 画面のスレッドのタイマーから呼ぶ（:meth:`JobBoard.poll`）"""
        return self._board.poll()

    def waveform(self, media: MediaItem) -> Waveform | None:
        """すでに用意できていれば返す 無ければ ``None``

        描画のたびに呼ばれるので、ここでは決してブロックしない
        """
        with self._lock:
            return self._waveforms.get(media.id)

    def filmstrip(self, media: MediaItem) -> Filmstrip | None:
        with self._lock:
            return self._filmstrips.get(media.id)

    def request(
        self, media: MediaItem, *, on_ready: Callable[[MediaId], None] | None = None
    ) -> None:
        """素材の解析を予約する すでにあるもの・処理中のものは無視する"""
        if media.has_audio:
            self._submit("waveform", media, self._analyze_waveform, on_ready)
        if media.has_video:
            self._submit("filmstrip", media, self._analyze_filmstrip, on_ready)

    def forget(self, media_id: MediaId) -> None:
        """素材を外したときに、結果と進行中の解析を捨てる"""
        with self._lock:
            self._waveforms.pop(media_id, None)
            self._filmstrips.pop(media_id, None)
            for kind in ("waveform", "filmstrip"):
                key = (kind, media_id)
                if key in self._running:
                    self._cancelled.add(key)
        self._board.forget(media_id)

    def close(self) -> None:
        # 投入（_submit）と同じロックの中で止める ロックの外で止めると、投入側が
        # 「まだ止まっていない」と見た直後に止まり、停止済みの executor へ投げて
        # RuntimeError になる 投入したキーも _running に残ったままになる
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancelled.update(self._running)
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _submit(
        self,
        kind: str,
        media: MediaItem,
        work: Callable[[MediaItem, Callable[[float], None]], bool],
        on_ready: Callable[[MediaId], None] | None,
    ) -> None:
        key = (kind, media.id)
        with self._lock:
            if self._closed:
                return
            done = self._waveforms if kind == "waveform" else self._filmstrips
            if media.id in done or key in self._running:
                return
            self._cancelled.discard(key)
            self._running.add(key)
            self._board.start(key, media.id)

        def report(value: float) -> None:
            self._board.report(key, value)

        def run() -> None:
            produced = False
            failure: str | None = None
            try:
                produced = work(media, report)
            except Exception as exc:  # 裏のスレッドの例外は誰にも見えずに消える
                # 投げ直さずに失敗として数える 前は executor の中で黙って消え、
                # 波形が出ないまま理由も分からなかった
                failure = f"{_KIND_NAMES[kind]}を作れなかった: {exc}"
            finally:
                with self._lock:
                    stopped = self._closed or key in self._cancelled
                    self._running.discard(key)
                    self._cancelled.discard(key)
            if stopped:
                self._board.drop(key)
            elif produced:
                self._board.finish(key)
            else:
                self._board.finish(key, failure or f"{_KIND_NAMES[kind]}を作れなかった")
            if produced and on_ready is not None:
                on_ready(media.id)

        with self._lock:
            # close と同じロックの中で投げる（close の説明を参照）
            if self._closed:
                self._running.discard(key)
                self._board.drop(key)
                return
            self._executor.submit(run)

    def _publish(self, kind: str, media_id: MediaId, result: Waveform | Filmstrip) -> bool:
        """結果を登録する 取り消されていたら登録しない

        解析は時間が掛かるので、走っている間に素材が外される（forget）ことがある
        確かめずに登録すると、外した素材の波形やサムネイルが復活する
        確かめるのと登録するのを同じロックの中で行う
        """
        with self._lock:
            if self._closed or (kind, media_id) in self._cancelled:
                return False
            if isinstance(result, Waveform):
                self._waveforms[media_id] = result
            else:
                self._filmstrips[media_id] = result
            return True

    def _is_cancelled(self, kind: str, media_id: MediaId) -> bool:
        with self._lock:
            return self._closed or (kind, media_id) in self._cancelled

    def _analyze_waveform(self, media: MediaItem, report: Callable[[float], None]) -> bool:
        key = waveform_key(media.path, self._sample_rate, self._channels)
        waveform = load_waveform(self._store, key)

        if waveform is None:
            waveform = analyze_waveform(
                media.path,
                sample_rate=self._sample_rate,
                channels=self._channels,
                progress=report,
                should_cancel=lambda: self._is_cancelled("waveform", media.id),
            )
            if waveform is None:
                return False
            save_waveform(self._store, key, waveform)

        return self._publish("waveform", media.id, waveform)

    def _analyze_filmstrip(self, media: MediaItem, report: Callable[[float], None]) -> bool:
        interval = _interval_for(media.duration)
        key = filmstrip_key(media.path, interval, THUMBNAIL_HEIGHT)
        filmstrip = load_filmstrip(self._store, key)

        if filmstrip is None:
            filmstrip = build_filmstrip(
                media.path,
                interval=interval,
                height=THUMBNAIL_HEIGHT,
                progress=report,
                should_cancel=lambda: self._is_cancelled("filmstrip", media.id),
            )
            if filmstrip is None:
                return False
            save_filmstrip(self._store, key, filmstrip)

        return self._publish("filmstrip", media.id, filmstrip)


def _interval_for(duration: Fraction) -> Fraction:
    """素材の長さに応じたサムネイル間隔

    短い素材は細かく、長い素材は粗く 一定にすると、1 時間の素材で
    7200 枚を作ることになる
    """
    if duration <= 0:
        return DEFAULT_INTERVAL
    if duration <= 60:
        return DEFAULT_INTERVAL
    if duration <= 600:
        return Fraction(2)
    return Fraction(5)
