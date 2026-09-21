r"""字幕パネルの描き直しと、高 DPI での表示を測る

    .venv\Scripts\python.exe tools\bench_subtitles.py
    .venv\Scripts\python.exe tools\bench_subtitles.py --segments 4000 --scale 2

字幕パネルは**編集のたびに作り直す**（``set_project``）ので、字幕の本数が
増えるとそこが重くなる 1 時間の動画に焼き込む字幕は 1000〜2000 本になる

高 DPI（150%・200%）では、同じ見た目を描くのに画素が 2.25〜4 倍になる
Qt の拡大は ``QT_SCALE_FACTOR`` で真似できるので、倍率を変えて測る

画面の無い環境でも ``offscreen`` で動く GUI を開かないので、そのまま走らせてよい
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
from fractions import Fraction
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 開発者本人の設定に触らない
_base = Path(tempfile.mkdtemp(prefix="kumiki-subtitle-bench-"))
os.environ["APPDATA"] = str(_base / "roaming")
os.environ["LOCALAPPDATA"] = str(_base / "local")
# 画面を開かない 測るのは描き直しの計算で、実際の画面への転送ではない
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


#: 拡大率は QApplication を作る前に決める あとから変えても効かない
def _early_scale(argv: list[str]) -> str | None:
    """``--scale`` の値を argparse より先に読む 値が無ければ ``None``

    拡大率は ``QApplication`` を作る前に環境変数で決める あとから変えても効かない
    書き方が壊れていてもここでは何も言わない argparse に任せた方が、
    使い方の案内がそろう
    """
    for index, value in enumerate(argv):
        if value.startswith("--scale="):
            return value.split("=", 1)[1]
        if value == "--scale" and index + 1 < len(argv):
            return argv[index + 1]
    return None


_scale = _early_scale(sys.argv[1:])
if _scale is not None:
    os.environ["QT_SCALE_FACTOR"] = _scale

from PySide6.QtWidgets import QApplication  # noqa: E402

from kumiki.core.commands import (  # noqa: E402
    AddClip,
    AddMedia,
    AddTrack,
    RenameProject,
    SetTranscript,
)
from kumiki.core.model import (  # noqa: E402
    AudioStreamInfo,
    Clip,
    MediaItem,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
    Transcript,
    TranscriptSegment,
    VideoStreamInfo,
)
from kumiki.core.timebase import FrameRate  # noqa: E402
from kumiki.engine.cache import MediaAnalyzer  # noqa: E402
from kumiki.ui.subtitle import SubtitlePanel  # noqa: E402

#: 30fps の 1 コマ（ミリ秒） 再生中に毎フレーム通る所の予算
FRAME_BUDGET_MS = 1000 / 30

#: 編集のたびに通る所の予算 押してから画面が変わるまでが 100ms を超えると、
#: 人は「待たされた」と感じる（操作の応答として広く使われている目安）
EDIT_BUDGET_MS = 100.0


def _project(segments: int, seconds: float) -> Project:
    """字幕を ``segments`` 本持つ素材を 1 本置いたプロジェクト"""
    step = Fraction(int(seconds * 1000), max(1, segments)) / 1000
    media = MediaItem(
        path=Path("長い動画.mp4"),
        duration=Fraction(int(seconds)),
        video_streams=(
            VideoStreamInfo(
                index=0,
                width=1920,
                height=1080,
                frame_rate=FrameRate(30),
                time_base=Fraction(1, 30),
                codec="h264",
            ),
        ),
        # 字幕のパネルは音のある素材だけを並べる 音が無いと 1 本も出ない
        audio_streams=(
            AudioStreamInfo(
                index=1, sample_rate=48000, channels=2, time_base=Fraction(1, 48000), codec="aac"
            ),
        ),
        transcript=Transcript(
            segments=tuple(
                TranscriptSegment(
                    start=step * index,
                    end=step * index + step * Fraction(9, 10),
                    text=f"字幕の {index + 1} 本目 ここに読み上げた文が入る",
                )
                for index in range(segments)
            )
        ),
    )
    project = Project.create(ProjectSettings(width=1920, height=1080, frame_rate=FrameRate(30)))
    project = AddMedia(media).apply(project)
    track = Track(kind=TrackKind.VIDEO, name="V1")
    project = AddTrack(track).apply(project)
    clip = Clip(timeline_start=0, duration=int(seconds * 30), media_id=media.id)
    return AddClip(track.id, clip).apply(project)


def _percentile95(times: list[float]) -> float:
    """95 パーセンタイル 標本の外側へ外挿しない（inclusive）

    番号で取り出す形（``ordered[int(len * 0.95)]``）だと、標本が 5 個のときに
    一番大きい値を選ぶ 外れ値 1 つで予算超えと出てしまう
    """
    if len(times) < 2:
        return max(times)
    return statistics.quantiles(times, n=20, method="inclusive")[18]


def _report(name: str, times: list[float], budget: float) -> bool:
    ordered = sorted(times)
    p95 = _percentile95(ordered)
    ok = p95 <= budget
    print(
        f"  {'○' if ok else '×'} {name:<28} 中央 {statistics.median(ordered):7.2f} ms  "
        f"95% {p95:7.2f} ms  予算 {budget:.1f} ms"
    )
    return ok


def _positive(value: str) -> int:
    """1 以上の整数 0 を受けると測るものが無いまま結果を出そうとして落ちる"""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"1 以上を指定する: {value}")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segments", type=int, default=2000, help="字幕の本数")
    parser.add_argument("--seconds", type=float, default=3600.0, help="素材の長さ（秒）")
    parser.add_argument("--repeats", type=_positive, default=5, help="作り直しを測る回数")
    parser.add_argument("--frames", type=_positive, default=200, help="再生を測るフレーム数")
    parser.add_argument("--scale", default="1", help="Qt の拡大率（1 / 1.5 / 2）")
    arguments = parser.parse_args()

    application = QApplication.instance() or QApplication([])
    ratio = os.environ.get("QT_SCALE_FACTOR", "1")
    print(f"字幕 {arguments.segments} 本 / 拡大率 {ratio} 倍")

    project = _project(arguments.segments, arguments.seconds)
    analyzer = MediaAnalyzer(sample_rate=48000, channels=2)
    panel = SubtitlePanel(project, analyzer)
    panel.resize(480, 900)
    panel.show()
    try:
        # 作り直し 字幕かクリップが変わったときに通る
        # **本物の編集**で変える 字幕を 1 本書き直す（SetTranscript）
        # 印を手で消して作り直させると、実際の編集では通らない道を測ることになる
        media = project.media[0]
        transcript = media.transcript
        assert transcript is not None
        rebuilds: list[float] = []
        current = project
        for index in range(arguments.repeats):
            edited = Transcript(
                segments=(
                    replace(transcript.segments[0], text=f"書き直した {index}"),
                    *transcript.segments[1:],
                )
            )
            current = SetTranscript(media.id, edited).apply(current)
            started = time.perf_counter()
            panel.set_project(current)
            application.processEvents()
            rebuilds.append((time.perf_counter() - started) * 1000)

        # 変わっていないときの素通り 編集のたびに通る所
        # 字幕に関係のない編集（プロジェクト名を変える）を**本物の命令**で行う
        skips: list[float] = []
        for index in range(arguments.repeats):
            current = RenameProject(f"測定 {index}").apply(current)
            started = time.perf_counter()
            panel.set_project(current)
            application.processEvents()
            skips.append((time.perf_counter() - started) * 1000)

        # 再生 毎フレーム通る（いま出ている字幕を選び直す）
        playback: list[float] = []
        for frame in range(arguments.frames):
            started = time.perf_counter()
            panel.set_frame(frame * 7)
            application.processEvents()
            playback.append((time.perf_counter() - started) * 1000)
    finally:
        panel.close()
        analyzer.close()

    passed = _report("作り直し（字幕が変わったとき）", rebuilds, EDIT_BUDGET_MS)
    passed &= _report("素通り（字幕が変わらない編集）", skips, EDIT_BUDGET_MS)
    passed &= _report("再生中の 1 フレーム", playback, FRAME_BUDGET_MS)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
