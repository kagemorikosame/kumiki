r"""タイムラインの描画の速さを測る

    .venv\Scripts\python.exe tools\bench_timeline.py
    .venv\Scripts\python.exe tools\bench_timeline.py --clips 5000 --tracks 10

性能目標「スクロール / ズームが 60fps」（1 回の描画が 16.7ms 以内）を確かめる
測らずに最適化しない、の測る側

ウィジェットを画面に出さず、同じ大きさの画像へ描かせて時間を取る 画面への転送は
Qt と GPU の仕事で、ここで見たいのは自前の描画（クリップを並べる計算と塗り）
"""

from __future__ import annotations

import argparse
import io
import os
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 開発者本人の設定やキャッシュに触らない
_base = Path(tempfile.mkdtemp(prefix="kumiki-bench-"))
os.environ["APPDATA"] = str(_base / "roaming")
os.environ["LOCALAPPDATA"] = str(_base / "local")

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from kumiki.core.model import Clip, Project, Track, TrackKind  # noqa: E402
from kumiki.effects.sources import TEXT  # noqa: E402
from kumiki.engine.cache import MediaAnalyzer  # noqa: E402
from kumiki.ui.timeline import TimelineView  # noqa: E402
from kumiki.ui.timeline.layout import TimelineLayout  # noqa: E402

#: 60fps の 1 コマ（ミリ秒）
BUDGET_MS = 1000 / 60


def build_project(clips: int, tracks: int, length: int) -> Project:
    """クリップを隙間なく並べたプロジェクト 映像と音声を半分ずつ"""
    # 余りも配る 切り捨てると、表示した本数より少ない数で測ったことになる
    per_track, remainder = divmod(clips, tracks)
    built: list[Track] = []
    for index in range(tracks):
        kind = TrackKind.VIDEO if index % 2 == 0 else TrackKind.AUDIO
        row = tuple(
            Clip(timeline_start=n * length, duration=length, source=TEXT.create())
            for n in range(per_track + (1 if index < remainder else 0))
        )
        built.append(Track(kind, f"{'V' if kind is TrackKind.VIDEO else 'A'}{index + 1}", row))
    base = Project.create()
    return base.with_timeline(replace(base.timeline, tracks=tuple(built)))


def measure(view: TimelineView, layouts: list[TimelineLayout], repeat: int) -> list[float]:
    """各表示状態で描いた時間（ミリ秒） 1 回目は温まっていないので捨てる"""
    image = QImage(view.size(), QImage.Format.Format_ARGB32_Premultiplied)
    times: list[float] = []
    for layout in layouts:
        view._layout = layout
        for attempt in range(repeat + 1):
            painter = QPainter(image)
            started = time.perf_counter()
            view.render(painter, QPoint())
            elapsed = (time.perf_counter() - started) * 1000
            painter.end()
            if attempt:
                times.append(elapsed)
    return times


def report(name: str, times: list[float]) -> bool:
    worst = max(times)
    p95 = statistics.quantiles(times, n=20)[18] if len(times) >= 20 else worst
    ok = p95 <= BUDGET_MS
    print(
        f"{name:<14} 中央 {statistics.median(times):6.2f} ms  95% {p95:6.2f} ms  "
        f"最悪 {worst:6.2f} ms  {'OK' if ok else '予算超え'}"
    )
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clips", type=int, default=3000, help="クリップの総数")
    parser.add_argument("--tracks", type=int, default=8, help="トラックの数")
    parser.add_argument("--length", type=int, default=90, help="1 クリップの長さ（フレーム）")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=420)
    parser.add_argument("--repeat", type=int, default=5, help="1 つの表示状態で描く回数")
    arguments = parser.parse_args(argv)

    application = QApplication.instance() or QApplication(sys.argv)
    del application
    project = build_project(arguments.clips, arguments.tracks, arguments.length)
    analyzer = MediaAnalyzer(sample_rate=48000, channels=2)
    view = TimelineView(project, analyzer)
    view.resize(arguments.width, arguments.height)

    duration = project.duration
    fit = TimelineLayout(pixels_per_frame=(arguments.width - 132) / max(1, duration))
    steps = 60
    scroll = [TimelineLayout(scroll_frame=duration * n / steps) for n in range(steps)]
    zoom = [TimelineLayout(pixels_per_frame=fit.pixels_per_frame * (1.25**n)) for n in range(40)]

    print(
        f"クリップ {arguments.clips} 本 / トラック {arguments.tracks} 本 / "
        f"長さ {duration} フレーム / 画面 {arguments.width}x{arguments.height}"
    )
    print(f"予算 {BUDGET_MS:.1f} ms（60fps）  判定は 95 パーセンタイル")
    results = [
        report("全体を表示", measure(view, [fit], arguments.repeat * 4)),
        report("スクロール", measure(view, scroll, arguments.repeat)),
        report("ズーム", measure(view, zoom, arguments.repeat)),
    ]
    analyzer.close()
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
