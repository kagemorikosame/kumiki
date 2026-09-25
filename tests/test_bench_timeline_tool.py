"""タイムラインの速さを測る道具（tools/bench_timeline.py）の、実素材を並べる所

テキストだけを並べて測ると、サムネイルと波形を描く道を通らず、実素材を並べたときに
60fps に収まるかが分からない（#201） 素材はその場で ffmpeg で作る
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QImage, QPainter

from sashimono.core.model import MediaItem, TrackKind
from sashimono.engine.cache import MediaAnalyzer
from sashimono.engine.cache.store import CacheStore
from sashimono.ui.timeline import TimelineView
from tests.media_fixtures import encoder_available, libx264_available

ROOT = Path(__file__).resolve().parent.parent

# 動画は libx264、音は aac で作る 片方だけの ffmpeg では素材を作る所で落ち、
# 飛ばすはずの試験がどれも取り付け口のエラーになる
pytestmark = pytest.mark.skipif(
    not (libx264_available() and encoder_available("aac")),
    reason="ffmpeg に libx264 か aac が無いので実素材を作れない",
)


@pytest.fixture
def tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    # 道具は読み込んだ時に APPDATA と LOCALAPPDATA を一時フォルダへ向ける 試験の間だけに
    # とどめる（monkeypatch が元へ戻す） 戻さないと後の試験が別の置き場を読む
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    spec = importlib.util.spec_from_file_location(
        "bench_timeline", ROOT / "tools" / "bench_timeline.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def media(tool: ModuleType, tmp_path: Path) -> tuple[MediaItem, MediaItem]:
    video, audio = tool.make_media(tmp_path / "media", seconds=4)
    assert isinstance(video, MediaItem) and isinstance(audio, MediaItem)
    return video, audio


def test_the_media_bench_places_clips_that_point_at_the_made_media(
    tool: ModuleType, media: tuple[MediaItem, MediaItem]
) -> None:
    """映像のトラックには testsrc の動画、音声のトラックには sine の音を並べる

    素材を指さないクリップを並べると、サムネイルも波形も描かれず、測っても今の表と同じになる
    """
    video, audio = media
    assert video.has_video and audio.has_audio and not audio.has_video
    project = tool.build_project(10, 4, 30, media=media)
    assert {item.id for item in project.media} == {video.id, audio.id}
    clips = [(track, clip) for track in project.timeline.tracks for clip in track.clips]
    assert len(clips) == 10
    for track, clip in clips:
        expected = video if track.kind is TrackKind.VIDEO else audio
        assert clip.media_id == expected.id


def test_the_media_bench_waits_until_thumbnails_and_waveforms_are_ready(
    tool: ModuleType, media: tuple[MediaItem, MediaItem], tmp_path: Path
) -> None:
    """解析が済む前に測ると、サムネイルと波形の無い速さを測ってしまう"""
    video, audio = media
    analyzer = MediaAnalyzer(CacheStore(tmp_path / "cache"))
    try:
        tool.analyze(analyzer, media)
        assert analyzer.filmstrip(video) is not None
        assert analyzer.waveform(audio) is not None
    finally:
        analyzer.close()


def _render(view: TimelineView) -> bytes:
    image = QImage(view.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    view.render(painter, QPoint())
    painter.end()
    return bytes(image.constBits())


def test_the_plain_analyzer_draws_the_same_timeline_without_the_contents(
    tool: ModuleType, media: tuple[MediaItem, MediaItem], tmp_path: Path
) -> None:
    """重い所を分けて測るため、サムネイルと波形だけを描かない解析を持つ

    絵が同じなら、描かない方にしたつもりで同じ物を 2 度測っている
    """
    project = tool.build_project(4, 2, 30, media=media)
    real = MediaAnalyzer(CacheStore(tmp_path / "cache"))
    plain = tool.PlainAnalyzer(CacheStore(tmp_path / "plain"))
    try:
        tool.analyze(real, media)
        drawn, bare = TimelineView(project, real), TimelineView(project, plain)
        for view in (drawn, bare):
            view.resize(640, 240)
        assert _render(drawn) != _render(bare)
        assert plain.filmstrip(media[0]) is None
        assert plain.waveform(media[1]) is None
    finally:
        real.close()
        plain.close()


def test_the_media_bench_runs_end_to_end_and_splits_out_the_contents(
    tool: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """素材を並べたときは、サムネイルだけ・波形だけ・どちらも描かない速さも並べて出す

    予算を超えたときに、サムネイルと波形のどちらが重いのか、それ以外なのかを分けられる
    """
    code = tool.main(
        ["--media", "--clips", "4", "--tracks", "2", "--repeat", "1", "--width", "640"]
    )
    assert code in (0, 1)
    out = capsys.readouterr().out
    for title in ("サムネイルだけ", "波形だけ", "サムネイルと波形を描かない"):
        assert title in out
    assert out.count("全体を表示") == 4


@pytest.mark.parametrize(
    "stuck",
    [
        subprocess.TimeoutExpired(["ffmpeg"], 1.0),
        subprocess.CalledProcessError(1, ["ffmpeg"]),
    ],
)
def test_a_stuck_or_failed_ffmpeg_stops_with_guidance(
    tool: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stuck: Exception,
) -> None:
    """素材を作る ffmpeg が止まっても落ちても、トレースバックでなく案内を出して終了コード 2

    TimeoutExpired は OSError の仲間でも CalledProcessError の仲間でもない 受けないと
    待つ上限を付けても、上限に掛かったときにトレースバックで終わる（#204）
    """

    def fail(*args: object, **kwargs: object) -> None:
        raise stuck

    monkeypatch.setattr(tool, "make_media", fail)
    assert tool.main(["--media", "--clips", "2", "--tracks", "2", "--repeat", "1"]) == 2
    assert "素材を作れない" in capsys.readouterr().out


def test_an_analysis_that_never_finishes_stops_with_guidance(
    tool: ModuleType,
    media: tuple[MediaItem, MediaItem],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """解析が待つ上限を超えたら、案内を出して終了コード 2 トレースバックで終わらない（#204）"""

    def slow(*args: object, **kwargs: object) -> None:
        raise TimeoutError("120 秒待ってもサムネイルと波形が揃わない")

    monkeypatch.setattr(tool, "make_media", lambda *args, **kwargs: media)
    monkeypatch.setattr(tool, "analyze", slow)
    assert tool.main(["--media", "--clips", "2", "--tracks", "2", "--repeat", "1"]) == 2
    assert "揃わない" in capsys.readouterr().out


def test_the_command_ends_without_waiting_for_a_stuck_analysis(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令として走らせたときは、終了コードを返したらそのまま終わる

    解析のワーカーは Python が終わるときに待たれる FFmpeg の 1 回のデコードの中で
    止まったワーカーは取り消しの印を見られず、解析の上限で終了コード 2 を返しても
    プロセスが終わらない（#204 の Qodo） 止まったワーカーは待たずに終える
    """
    stuck = threading.Event()
    worker = threading.Thread(target=stuck.wait, daemon=True)
    worker.start()
    ended: list[int] = []

    def end(code: int) -> None:
        ended.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(tool, "main", lambda argv=None: 2)
    monkeypatch.setattr(tool.os, "_exit", end)
    try:
        with pytest.raises(SystemExit):
            tool.run()
    finally:
        stuck.set()
    assert ended == [2]


def test_the_ffmpeg_calls_have_an_upper_limit(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """応答しない ffmpeg に当たっても、素材を作る所で待ち続けない（#204）"""
    seen: list[object] = []

    def record(*args: object, **kwargs: object) -> None:
        seen.append(kwargs.get("timeout"))
        raise subprocess.CalledProcessError(1, ["ffmpeg"])

    monkeypatch.setattr(tool.subprocess, "run", record)
    with pytest.raises(subprocess.CalledProcessError):
        tool.make_media(tmp_path / "media")
    assert seen == [tool.FFMPEG_TIMEOUT]


def test_the_partial_analyzer_shows_only_what_was_chosen(
    tool: ModuleType, media: tuple[MediaItem, MediaItem], tmp_path: Path
) -> None:
    """サムネイルだけ・波形だけを選べる 選んでいない方まで返すと、分けて測れない"""
    video, audio = media
    real = MediaAnalyzer(CacheStore(tmp_path / "cache"))
    pictures = tool.PlainAnalyzer(CacheStore(tmp_path / "p"), real, filmstrips=True)
    sounds = tool.PlainAnalyzer(CacheStore(tmp_path / "s"), real, waveforms=True)
    try:
        tool.analyze(real, media)
        assert pictures.filmstrip(video) is not None and pictures.waveform(audio) is None
        assert sounds.filmstrip(video) is None and sounds.waveform(audio) is not None
    finally:
        for analyzer in (real, pictures, sounds):
            analyzer.close()
