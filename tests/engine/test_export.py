"""書き出し

実際にファイルを作って、読み直せることまで確かめる 書き出しは最後の工程なので、
ここが壊れていると編集作業がまるごと無駄になる
"""

from __future__ import annotations

import threading
from fractions import Fraction
from pathlib import Path

import av
import pytest

from sashimono.core.commands import Document, insert_media
from sashimono.core.model import Project, ProjectSettings
from sashimono.core.timebase import FrameRate
from sashimono.engine.decode import probe_media
from sashimono.engine.encode import (
    ExportError,
    ExportSettings,
    available_video_codecs,
    export_project,
)
from tests.media_fixtures import SampleMedia, make_sample

# 書き出しは内部で GL コンテキストを作って合成する GPU の無い環境では
# 作れても使えないので、失敗ではなく飛ばす
pytestmark = pytest.mark.usefixtures("gpu")


@pytest.fixture
def ready_project(sample_av: SampleMedia) -> Project:
    """映像 + 音声を 1 本置いた 320x240 / 30fps のプロジェクト"""
    document = Document(
        Project.create(ProjectSettings(width=320, height=240, frame_rate=FrameRate(30)))
    )
    for command in insert_media(document.project, probe_media(sample_av.path)):
        document.execute(command)
    return document.project


@pytest.fixture
def silent_project(sample_long: SampleMedia) -> Project:
    """映像のみのプロジェクト 音声ストリームを作らない経路の確認用"""
    document = Document(
        Project.create(ProjectSettings(width=320, height=240, frame_rate=FrameRate(30)))
    )
    for command in insert_media(document.project, probe_media(sample_long.path)):
        document.execute(command)
    return document.project


class TestCodecs:
    def test_at_least_one_codec_is_available(self) -> None:
        assert available_video_codecs(), "書き出せるコーデックが 1 つも無い"

    def test_preference_order_is_respected(self) -> None:
        # GPU コーデックが使えるなら先に来る CPU より圧倒的に速い
        codecs = available_video_codecs()
        if "h264_nvenc" in codecs and "libx264" in codecs:
            assert codecs.index("h264_nvenc") < codecs.index("libx264")


class TestExport:
    @pytest.mark.parametrize("codec", ["libx264", "h264_nvenc"])
    def test_produces_a_playable_file(
        self, codec: str, ready_project: Project, tmp_path: Path
    ) -> None:
        if codec not in available_video_codecs():
            pytest.skip(f"{codec} が使えない")

        output = tmp_path / "out.mp4"
        export_project(ready_project, ExportSettings(path=output, video_codec=codec))

        assert output.exists()
        assert output.stat().st_size > 0

        item = probe_media(output)
        assert item.has_video
        assert item.has_audio, "音声トラックがあるのに無音のファイルになっている"
        stream = item.video_streams[0]
        assert (stream.width, stream.height) == (320, 240)
        assert stream.frame_rate == FrameRate(30)

    def test_frame_count_matches_the_timeline(self, ready_project: Project, tmp_path: Path) -> None:
        output = tmp_path / "out.mp4"
        export_project(ready_project, ExportSettings(path=output, video_codec="libx264"))

        with av.open(str(output)) as container:
            frames = sum(1 for _ in container.decode(video=0))
        assert frames == ready_project.duration

    def test_video_only_project(self, silent_project: Project, tmp_path: Path) -> None:
        output = tmp_path / "silent.mp4"
        export_project(silent_project, ExportSettings(path=output, video_codec="libx264"))

        item = probe_media(output)
        assert item.has_video
        assert not item.has_audio

    def test_frame_range(self, ready_project: Project, tmp_path: Path) -> None:
        output = tmp_path / "range.mp4"
        export_project(
            ready_project,
            ExportSettings(path=output, video_codec="libx264", frame_range=(10, 25)),
        )
        with av.open(str(output)) as container:
            frames = sum(1 for _ in container.decode(video=0))
        assert frames == 15

    def test_reports_progress(self, ready_project: Project, tmp_path: Path) -> None:
        seen: list[float] = []
        export_project(
            ready_project,
            ExportSettings(path=tmp_path / "p.mp4", video_codec="libx264"),
            progress=seen.append,
        )
        assert seen
        assert seen[-1] == pytest.approx(1.0)
        assert seen == sorted(seen)

    def test_cancel_leaves_no_file(self, ready_project: Project, tmp_path: Path) -> None:
        # 中途半端なファイルが残ると、書き出せたのかどうか分からなくなる
        output = tmp_path / "cancelled.mp4"
        stop = threading.Event()

        def should_cancel() -> bool:
            stop.set()
            return True

        with pytest.raises(ExportError, match="中止"):
            export_project(
                ready_project,
                ExportSettings(path=output, video_codec="libx264"),
                should_cancel=should_cancel,
            )
        assert not output.exists()

    def test_failure_leaves_no_file(self, ready_project: Project, tmp_path: Path) -> None:
        output = tmp_path / "broken.mp4"
        with pytest.raises(Exception):  # noqa: B017 - コーデック側の例外種別は問わない
            export_project(
                ready_project,
                ExportSettings(path=output, video_codec="存在しないコーデック"),
            )
        assert not output.exists()

    def test_empty_timeline_is_refused(self, tmp_path: Path) -> None:
        empty = Project.create(ProjectSettings(width=320, height=240))
        with pytest.raises(ExportError, match="範囲が空"):
            export_project(empty, ExportSettings(path=tmp_path / "empty.mp4"))

    def test_creates_missing_directories(self, ready_project: Project, tmp_path: Path) -> None:
        output = tmp_path / "深い" / "階層" / "out.mp4"
        export_project(ready_project, ExportSettings(path=output, video_codec="libx264"))
        assert output.exists()


class TestRoundTrip:
    def test_exported_file_can_be_edited_again(
        self, ready_project: Project, tmp_path: Path
    ) -> None:
        # 書き出したものを読み込み直せること 中間ファイルとして使う経路
        output = tmp_path / "again.mp4"
        export_project(ready_project, ExportSettings(path=output, video_codec="libx264"))

        reimported = probe_media(output)
        document = Document(Project.create(ProjectSettings(width=320, height=240)))
        for command in insert_media(document.project, reimported):
            document.execute(command)

        assert document.project.duration > 0
        assert len(list(document.project.timeline.video_tracks())) == 1
        assert len(list(document.project.timeline.audio_tracks())) == 1

    def test_audio_survives(self, media_dir: Path, tmp_path: Path) -> None:
        # 音が無音になっていないこと 書き出し経路で最も見落としやすい
        loud = make_sample(media_dir, "loud.mp4", duration=1.5, tone_hz=440)
        document = Document(
            Project.create(ProjectSettings(width=320, height=240, frame_rate=FrameRate(30)))
        )
        for command in insert_media(document.project, probe_media(loud.path)):
            document.execute(command)

        output = tmp_path / "audio.mp4"
        export_project(document.project, ExportSettings(path=output, video_codec="libx264"))

        from sashimono.engine.decode import AudioDecoder

        with AudioDecoder(output, sample_rate=48000) as decoder:
            samples = decoder.read_seconds(Fraction(1, 4), Fraction(1, 2))
        assert float(abs(samples).max()) > 0.005, "書き出したファイルが無音"
