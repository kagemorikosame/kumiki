"""書き出し

実際にファイルを作って、読み直せることまで確かめる 書き出しは最後の工程なので、
ここが壊れていると編集作業がまるごと無駄になる
"""

from __future__ import annotations

import threading
from fractions import Fraction
from pathlib import Path
from typing import cast

import av
import av.error
import av.video.codeccontext
import pytest
from av.video.reformatter import ColorPrimaries, ColorRange, ColorTrc

from kumiki.core.commands import Document, insert_media
from kumiki.core.model import Project, ProjectSettings
from kumiki.core.timebase import FrameRate
from kumiki.engine.decode import VideoDecoder, probe_media
from kumiki.engine.encode import (
    COLOR_OPTIONS,
    ExportError,
    ExportSettings,
    available_video_codecs,
    export_project,
)
from tests.color_bars import (
    AVCOL_SPC_BT709,
    BT709_YUV,
    COLORS,
    assert_close,
    bars,
    rgb_at_bars,
    yuv_at_bars,
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

        from kumiki.engine.decode import AudioDecoder

        with AudioDecoder(output, sample_rate=48000) as decoder:
            samples = decoder.read_seconds(Fraction(1, 4), Fraction(1, 2))
        assert float(abs(samples).max()) > 0.005, "書き出したファイルが無音"


@pytest.fixture
def bars_project(tmp_path: Path) -> Project:
    """原色と灰色の帯の静止画を 1 枚置いたプロジェクト 行列の違いがいちばん大きく出る"""
    still = tmp_path / "bars.png"
    image = av.VideoFrame.from_ndarray(bars(320, 240), format="rgb24")
    with av.open(str(still), mode="w", format="image2") as container:
        stream = container.add_stream("png")
        stream.width = 320
        stream.height = 240
        stream.pix_fmt = "rgb24"
        container.mux(stream.encode(image))
        container.mux(stream.encode(None))

    document = Document(
        Project.create(ProjectSettings(width=320, height=240, frame_rate=FrameRate(30)))
    )
    for command in insert_media(document.project, probe_media(still)):
        document.execute(command)
    return document.project


#: GPU が無いと開けないエンコーダ この失敗だけは環境の都合として飛ばす
HARDWARE_CODECS = frozenset({"h264_nvenc", "h264_qsv"})


def skip_unless_it_opens(codec: str) -> None:
    """エンコーダを書き出しと同じ条件で開いてみて、開けなければ飛ばす

    QSV はコーデックとしては入っていても、Intel の GPU が無い機械では開けない
    飛ばすのはここで開けないときだけ 書き出し全体の失敗まで飛ばすと、開けたあとの
    回帰（変換・エンコード・多重化）が「飛ばした」に紛れる
    """
    # create は種類の union を返す 映像の属性を触るので、ここで型を確定させる
    context = cast("av.video.codeccontext.VideoCodecContext", av.CodecContext.create(codec, "w"))
    context.width = 320
    context.height = 240
    context.pix_fmt = "yuv420p"
    context.time_base = Fraction(1, 30)
    # 開いた文脈は捨てるだけでよい PyAV は参照が切れたときに閉じる
    try:
        context.open()
    except av.error.FFmpegError as exc:
        pytest.skip(f"{codec} を開けない: {exc}")


class TestColor:
    """書き出しの色は BT.709 / limited で、同じ値のタグが付く（#61）

    直す前は swscale の既定の BT.601 で変換し、タグも無かった 赤の Y が 63 ではなく 81 に
    なり、再生側は行列を推測で読むしかなかった
    """

    @pytest.mark.parametrize("codec", ["libx264", "h264_nvenc", "h264_qsv"])
    def test_values_and_tags_are_bt709(
        self, codec: str, bars_project: Project, tmp_path: Path
    ) -> None:
        if codec not in available_video_codecs():
            pytest.skip(f"{codec} が使えない")
        if codec in HARDWARE_CODECS:
            skip_unless_it_opens(codec)
        # 書き出しそのものは囲まない 開けると分かったあとの失敗（変換・エンコード・
        # 多重化）は、どのコーデックでも本物の回帰
        output = tmp_path / f"{codec}.mp4"
        export_project(
            bars_project, ExportSettings(path=output, video_codec=codec, frame_range=(0, 3))
        )

        with av.open(str(output)) as container:
            stream = container.streams.video[0]
            context = stream.codec_context
            # 開き直して読むのは、コンテナとビットストリームに残ったタグ
            assert context.color_primaries == ColorPrimaries.BT709
            assert context.color_trc == ColorTrc.BT709
            assert context.colorspace == AVCOL_SPC_BT709
            assert context.color_range == ColorRange.MPEG
            frame = next(container.decode(stream))
        # ハードウェアのエンコーダは非可逆なので少し幅を持たせる BT.601 との差は 10 以上ある
        assert_close(yuv_at_bars(frame), BT709_YUV, tolerance=3)

    @pytest.mark.parametrize("name", sorted(COLOR_OPTIONS))
    def test_color_options_are_refused(
        self, name: str, bars_project: Project, tmp_path: Path
    ) -> None:
        # options はコーデックを開くときに効くので、付けたタグを後から書き換えてしまう
        # 画素は BT.709 のまま、タグだけ bt470bg などになったファイルができる
        output = tmp_path / "out.mp4"
        settings = ExportSettings(
            path=output, video_codec="libx264", frame_range=(0, 3), options={name: "bt470bg"}
        )
        with pytest.raises(ExportError, match=name):
            export_project(bars_project, settings)
        assert not output.exists()

    @pytest.mark.parametrize(
        ("name", "value", "culprit"),
        [
            ("x264-params", "colorprim=bt470bg:colormatrix=bt470bg", "colorprim"),
            ("x264-params", "keyint=60:Transfer=smpte170m", "transfer"),
            ("x264opts", "keyint=60,fullrange=on", "fullrange"),
            ("x265-params", "range=full", "range"),
        ],
    )
    def test_color_params_inside_encoder_options_are_refused(
        self, name: str, value: str, culprit: str, bars_project: Project, tmp_path: Path
    ) -> None:
        # x264-params の中の色はビットストリームの VUI だけを書き換える MP4 の colr は
        # bt709 のまま残り、生の H.264 を読むと bt470bg、というファイルになっていた
        output = tmp_path / "out.mp4"
        settings = ExportSettings(
            path=output, video_codec="libx264", frame_range=(0, 3), options={name: value}
        )
        with pytest.raises(ExportError, match=culprit):
            export_project(bars_project, settings)
        assert not output.exists()

    def test_other_encoder_params_pass(self, bars_project: Project, tmp_path: Path) -> None:
        # 色と関係の無いまとめ書きまで断ると、プリセットや品質の指定が通せなくなる
        output = tmp_path / "out.mp4"
        settings = ExportSettings(
            path=output,
            video_codec="libx264",
            frame_range=(0, 3),
            options={"x264-params": "keyint=30:bframes=0"},
        )
        export_project(bars_project, settings)
        assert output.exists()

    def test_colors_survive_a_round_trip(self, bars_project: Project, tmp_path: Path) -> None:
        # 書いたタグどおりに読み直せば、元の色へ戻る 変換とタグのどちらかが壊れると、
        # 書き出したものを素材として読み込み直したときに原色がずれる（中間ファイルの色が変わる）
        output = tmp_path / "again.mp4"
        export_project(
            bars_project,
            ExportSettings(path=output, video_codec="libx264", frame_range=(0, 3)),
        )
        with VideoDecoder(output) as decoder:
            image = decoder.frame_at(Fraction(0))
        assert image is not None
        assert_close(rgb_at_bars(image), COLORS, tolerance=4)
