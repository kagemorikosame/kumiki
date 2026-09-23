"""裏で走る処理（控え・波形・サムネイル）の進み具合を数える

数えるのはワーカースレッド、読むのは画面のスレッドのタイマー 画面の部品は
ワーカースレッドから触ると Qt が落ちるので、ここでは数を持つだけにして、
画面の側が決まった間隔で :meth:`JobBoard.poll` で読みに来る

Qt に依存しない 裏の処理の係（:class:`~sashimono.engine.cache.proxy.ProxyBuilder`
と :class:`~sashimono.engine.cache.analyzer.MediaAnalyzer`）が持つ
"""

from __future__ import annotations

import threading
from collections.abc import Hashable, Mapping
from dataclasses import dataclass, field

from sashimono.core.model import MediaId

__all__ = ["JobBoard", "ProgressSnapshot"]


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """ある時点の進み具合 画面へ出すためだけに使う"""

    #: ひと続きの仕事として頼まれた数 前のひと続きを画面が見終えると 0 から数え直す
    total: int = 0
    #: そのうち終わった数（失敗も数える）
    finished: int = 0
    #: そのうち失敗した数
    failed: int = 0
    #: 走っている仕事の途中の分の合計 解析は 1 つの素材に 2 つの仕事があるので、
    #: 素材ごとの平均（``running``）から足し直すと全体の割合がずれる
    partial: float = 0.0
    #: 走っている（か待っている）素材ごとの進み具合 0..1
    running: Mapping[MediaId, float] = field(default_factory=dict)
    #: 失敗した素材と理由 作り直しに成功するか素材を外すまで残す
    #: 行ごとの表示は、ひと続きが終わった後も失敗を出し続けたいので数え直しで消さない
    failures: Mapping[MediaId, str] = field(default_factory=dict)

    @property
    def busy(self) -> bool:
        return self.finished < self.total

    @property
    def fraction(self) -> float:
        """全体の進み具合 0..1 走っている物の途中の分も足す

        終わった本数だけで数えると、長い素材 1 本の控えを作る間ずっと 0% のまま動かない
        """
        if self.total <= 0:
            return 1.0
        return min(1.0, (self.finished + self.partial) / self.total)


class JobBoard:
    """仕事ごとの進み具合と、ひと続きの本数を数える

    仕事は鍵（``Hashable``）で区別し、どの素材の仕事かも一緒に持つ 解析は 1 つの
    素材に波形とサムネイルの 2 つの仕事があるので、素材と鍵を分けておかないと
    行ごとの表示で片方が片方を上書きする
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[Hashable, tuple[MediaId, float]] = {}
        #: 失敗は仕事の鍵ごとに持つ 素材ごとに持つと、波形が失敗した素材で
        #: サムネイルを頼み直しただけで波形の失敗まで消える
        self._failures: dict[Hashable, tuple[MediaId, str]] = {}
        self._total = 0
        self._finished = 0
        self._failed = 0

    def start(self, key: Hashable, media_id: MediaId) -> None:
        """仕事を 1 つ数え始める 同じ鍵が走っていれば何もしない"""
        with self._lock:
            if key in self._active:
                return
            self._active[key] = (media_id, 0.0)
            self._total += 1
            # 作り直しを頼んだ仕事の前の失敗は消す 残すと、作り直している最中も
            # 「作れなかった」と出続ける
            self._failures.pop(key, None)

    def report(self, key: Hashable, value: float) -> None:
        with self._lock:
            entry = self._active.get(key)
            if entry is not None:
                self._active[key] = (entry[0], max(0.0, min(1.0, value)))

    def finish(self, key: Hashable, failure: str | None = None) -> None:
        """仕事が終わった ``failure`` があれば失敗として数える"""
        with self._lock:
            entry = self._active.pop(key, None)
            if entry is None:
                return
            self._finished += 1
            if failure is not None:
                self._failed += 1
                self._failures[key] = (entry[0], failure)

    def drop(self, key: Hashable) -> None:
        """止めた仕事を数から外す

        終わった数に入れると、素材を外しただけで「3 本のうち 3 本」と
        仕事をしたように見え、失敗に入れると外しただけで失敗が出る
        """
        with self._lock:
            if self._active.pop(key, None) is not None:
                self._total -= 1

    def forget(self, media_id: MediaId) -> None:
        """外した素材の失敗を捨てる 一覧に無い素材の失敗を数え続けない"""
        with self._lock:
            for key in [key for key, (owner, _) in self._failures.items() if owner == media_id]:
                del self._failures[key]

    def poll(self) -> ProgressSnapshot:
        """今の進み具合を返す 何も走っていなければ、返したあとで本数を数え直す

        数え直すのは画面が読んだとき 仕事が終わった時点で数え直すと、次の
        ひと続きが間隔の間に始まったとき、画面は「終わった」を 1 度も見ないまま
        前の失敗の数ごと見失う 読むのは画面のタイマー 1 か所だけという前提
        """
        with self._lock:
            running: dict[MediaId, list[float]] = {}
            for media_id, value in self._active.values():
                running.setdefault(media_id, []).append(value)
            failures: dict[MediaId, list[str]] = {}
            for media_id, reason in self._failures.values():
                failures.setdefault(media_id, []).append(reason)
            snapshot = ProgressSnapshot(
                total=self._total,
                finished=self._finished,
                failed=self._failed,
                partial=sum(value for _media, value in self._active.values()),
                running={key: sum(values) / len(values) for key, values in running.items()},
                failures={key: "\n".join(reasons) for key, reasons in failures.items()},
            )
            if not self._active:
                self._total = self._finished = self._failed = 0
            return snapshot
