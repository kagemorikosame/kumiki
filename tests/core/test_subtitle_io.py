"""字幕ファイルの書き出し。

出すのは投影後の時刻。素材が持っている生の時刻をそのまま出すと、編集前の動画に
しか合わない字幕になる。
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from novaedit.core.commands import AddClip, RippleCut, SetTranscript
from novaedit.core.io import save_subtitles, to_srt, to_text, to_vtt
from novaedit.core.model import MediaItem, Project, Transcript
from novaedit.core.projection import project_timeline
from novaedit.core.timebase import FrameRate
from tests.conftest import make_clip


@pytest.fixture
def placed(project: Project, video_media: MediaItem, transcript: Transcript) -> Project:
    with_transcript = SetTranscript(video_media.id, transcript).apply(project)
    track = with_transcript.timeline.tracks[0]
    return AddClip(track.id, make_clip(0, 300, video_media)).apply(with_transcript)


class TestSrt:
    def test_numbering_and_timestamps(self, placed: Project) -> None:
        srt = to_srt(project_timeline(placed), placed.rate)
        assert srt.startswith("1\n00:00:01,000 --> 00:00:03,000\n今日は\n")
        assert "3\n00:00:07,000 --> 00:00:09,000\n作ります\n" in srt

    def test_the_cut_moves_the_times(self, placed: Project) -> None:
        # 冒頭 1 秒を切ると、字幕もその分だけ前へ来る。書き出しに追従処理は要らない。
        cut = RippleCut(((0, 30),)).apply(placed)
        srt = to_srt(project_timeline(cut), cut.rate)
        assert srt.startswith("1\n00:00:00,000 --> 00:00:02,000\n今日は\n")

    def test_empty_text_is_skipped(self, project: Project, video_media: MediaItem) -> None:
        from novaedit.core.model import TranscriptSegment

        blank = Transcript(
            (
                TranscriptSegment(Fraction(0), Fraction(1), "  "),
                TranscriptSegment(Fraction(2), Fraction(3), "本題"),
            )
        )
        placed = SetTranscript(video_media.id, blank).apply(project)
        placed = AddClip(placed.timeline.tracks[0].id, make_clip(0, 300, video_media)).apply(placed)
        assert to_srt(project_timeline(placed), placed.rate).count("-->") == 1

    def test_no_subtitles_gives_an_empty_string(self, project: Project) -> None:
        assert to_srt(project_timeline(project), project.rate) == ""


class TestVtt:
    def test_header_and_period_separator(self, placed: Project) -> None:
        vtt = to_vtt(project_timeline(placed), placed.rate)
        assert vtt.startswith("WEBVTT\n")
        assert "00:00:01.000 --> 00:00:03.000" in vtt


class TestText:
    def test_only_the_body_survives(self, placed: Project) -> None:
        assert to_text(project_timeline(placed)) == "今日は\n編集ソフトを\n作ります\n"

    def test_line_breaks_inside_a_subtitle_become_spaces(
        self, project: Project, video_media: MediaItem
    ) -> None:
        from novaedit.core.model import TranscriptSegment

        wrapped = Transcript((TranscriptSegment(Fraction(0), Fraction(2), "上の行\n下の行"),))
        placed = SetTranscript(video_media.id, wrapped).apply(project)
        placed = AddClip(placed.timeline.tracks[0].id, make_clip(0, 300, video_media)).apply(placed)
        assert to_text(project_timeline(placed)) == "上の行 下の行\n"


class TestTimestamps:
    def test_fractional_frame_rates_use_real_time(self, placed: Project) -> None:
        # 29.97fps では、フレーム番号をそのまま秒にするとずれる。ミリ秒で出す。
        from dataclasses import replace

        ntsc_rate = FrameRate(30000, 1001)
        ntsc = replace(
            placed,
            settings=replace(placed.settings, frame_rate=ntsc_rate),
            timeline=replace(placed.timeline, rate=ntsc_rate),
        )
        srt = to_srt(project_timeline(ntsc), ntsc.rate)
        # 素材の 1 秒は 29 フレーム目（切り捨て）。その実時間は 29 x 1001/30000 で
        # 0.968 秒。フレーム番号を 30 で割った 0.967 秒でも、1.000 秒でもない。
        assert "00:00:00,968 --> " in srt


class TestSaveSubtitles:
    def test_the_extension_decides_the_format(self, placed: Project, tmp_path: Path) -> None:
        srt = save_subtitles(placed, tmp_path / "a.srt").read_text(encoding="utf-8")
        assert srt.startswith("1\n")
        assert (
            save_subtitles(placed, tmp_path / "a.vtt")
            .read_text(encoding="utf-8")
            .startswith("WEBVTT")
        )
        assert save_subtitles(placed, tmp_path / "a.txt").read_text(encoding="utf-8") == (
            "今日は\n編集ソフトを\n作ります\n"
        )

    def test_an_unknown_extension_falls_back_to_srt(self, placed: Project, tmp_path: Path) -> None:
        written = save_subtitles(placed, tmp_path / "a.unknown")
        assert written.read_text(encoding="utf-8").startswith("1\n")

    def test_written_without_a_byte_order_mark(self, placed: Project, tmp_path: Path) -> None:
        # BOM 付きだと、古い再生機で 1 枚目の番号が読めず字幕全体が出ないことがある。
        raw = save_subtitles(placed, tmp_path / "a.srt").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
