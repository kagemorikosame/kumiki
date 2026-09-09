"""互換性の穴を記録する。

AviUtl の ``obj`` API は広く、全部を一度に実装することはできない。大事なのは
「動かない」ことではなく、**何が足りなくて動かないのかが分かること**。

スクリプトが未対応の関数を呼んだら、落とさずに記録して先へ進む。あとで一覧を
見れば、実際に使われている API が使用回数つきで並ぶので、次に何を実装すべきかを
勘ではなくデータで決められる。
"""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field

__all__ = ["CompatibilityReport", "ScriptFailure", "global_report"]


@dataclass(frozen=True, slots=True)
class ScriptFailure:
    """スクリプトの実行が失敗した記録。"""

    script: str
    message: str


@dataclass
class CompatibilityReport:
    """未対応 API と失敗の記録。

    複数のスクリプトが描画スレッドから同時に触るので、数える処理だけ錠を掛ける。
    """

    #: 呼ばれた未対応 API の名前と回数。
    missing: Counter[str] = field(default_factory=Counter)
    #: 解釈できなかった制御文字。
    controls: Counter[str] = field(default_factory=Counter)
    #: 実行時の失敗。同じものが何度も出るので、最初の 1 件だけ残す。
    failures: dict[str, ScriptFailure] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def note_missing(self, name: str) -> None:
        with self._lock:
            self.missing[name] += 1

    def note_control(self, line: str) -> None:
        with self._lock:
            self.controls[line] += 1

    def note_failure(self, script: str, message: str) -> None:
        with self._lock:
            self.failures.setdefault(script, ScriptFailure(script, message))

    def clear(self) -> None:
        with self._lock:
            self.missing.clear()
            self.controls.clear()
            self.failures.clear()

    @property
    def is_empty(self) -> bool:
        return not (self.missing or self.controls or self.failures)

    def summary(self) -> str:
        """1 行の要約。ステータスバーに出す。"""
        if self.is_empty:
            return "未対応の呼び出しはありません"
        parts = []
        if self.missing:
            parts.append(f"未実装 API {len(self.missing)} 種")
        if self.controls:
            parts.append(f"未対応の制御文字 {len(self.controls)} 種")
        if self.failures:
            parts.append(f"失敗 {len(self.failures)} 件")
        return "、".join(parts)

    def lines(self) -> tuple[str, ...]:
        """一覧に出す行。多い順に並べる。"""
        with self._lock:
            rows = [f"{name} — {count} 回" for name, count in self.missing.most_common()]
            rows.extend(f"{line} — {count} 回" for line, count in self.controls.most_common())
            rows.extend(f"{f.script}: {f.message}" for f in self.failures.values())
        return tuple(rows)


#: アプリ全体で 1 つ。スクリプトはどこから走っても同じ場所へ記録する。
global_report = CompatibilityReport()
