"""字幕パネル

パネルは自分ではプロジェクトを書き換えない 出てくるのはコマンドだけなので、
ここでは「どの操作でどのコマンドが出るか」を見る
"""

from __future__ import annotations

from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QFileDialog

from kumiki.core.commands import (
    AddClip,
    Command,
    RenameProject,
    RippleCut,
    SetSegmentText,
    SetTranscript,
)
from kumiki.core.model import MediaItem, Project, Transcript
from kumiki.engine.audio.waveform import BASE_SAMPLES_PER_PEAK, PeakLevel, Waveform
from kumiki.engine.cache import MediaAnalyzer
from kumiki.ui.subtitle import SubtitlePanel
from kumiki.ui.subtitle.dialogs import JetCutDialog
from tests.conftest import make_clip


class StubAnalyzer(MediaAnalyzer):
    """波形を持っているふりをする解析器"""

    def __init__(self, waveform: Waveform | None = None) -> None:
        self._stub = waveform

    def waveform(self, media: MediaItem) -> Waveform | None:
        del media
        return self._stub

    def close(self) -> None:
        return None


def make_waveform(pattern: list[tuple[float, int]]) -> Waveform:
    blocks = []
    for amplitude, count in pattern:
        block = np.zeros((count, 1, 2), dtype=np.float32)
        block[:, :, 0] = -amplitude
        block[:, :, 1] = amplitude
        blocks.append(block)
    peaks = np.concatenate(blocks, axis=0)
    return Waveform(
        sample_rate=48000,
        channels=1,
        total_samples=peaks.shape[0] * BASE_SAMPLES_PER_PEAK,
        levels=(PeakLevel(BASE_SAMPLES_PER_PEAK, peaks),),
    )


@pytest.fixture
def placed(project: Project, video_media: MediaItem, transcript: Transcript) -> Project:
    with_transcript = SetTranscript(video_media.id, transcript).apply(project)
    track = with_transcript.timeline.tracks[0]
    return AddClip(track.id, make_clip(0, 300, video_media)).apply(with_transcript)


@pytest.fixture
def panel(
    qt_application: QApplication, placed: Project
) -> Iterator[tuple[SubtitlePanel, list[tuple[list[Command], str]]]]:
    del qt_application
    created = SubtitlePanel(placed, StubAnalyzer())
    issued: list[tuple[list[Command], str]] = []
    created.commands_requested.connect(lambda commands, label: issued.append((commands, label)))
    yield created, issued
    created.deleteLater()


class TestListing:
    def test_only_media_with_audio_is_offered(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]], video_media: MediaItem
    ) -> None:
        widget, _ = panel
        assert widget.media_id == video_media.id

    def test_rows_show_timeline_positions_not_source_times(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]]
    ) -> None:
        widget, _ = panel
        # 素材の 1 秒は 30 フレーム目 30fps なので 00:00:01:00
        assert widget._table.rowCount() == 3
        item = widget._table.item(0, 0)
        assert item is not None
        assert item.text() == "00:00:01:00"

    def test_the_cut_moves_the_shown_times(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]], placed: Project
    ) -> None:
        widget, _ = panel
        widget.set_project(RippleCut(((0, 30),)).apply(placed))
        item = widget._table.item(0, 0)
        assert item is not None
        assert item.text() == "00:00:00:00"

    def test_subtitles_outside_the_timeline_are_marked(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]], placed: Project
    ) -> None:
        widget, _ = panel
        # クリップを冒頭 2 秒だけにすると、後ろ 2 枚はどこにも出なくなる
        trimmed = placed.timeline.tracks[0].clips[0]
        from dataclasses import replace

        shortened = placed.with_timeline(
            placed.timeline.replace_track(
                placed.timeline.tracks[0].with_clips((replace(trimmed, duration=60),))
            )
        )
        widget.set_project(shortened)
        item = widget._table.item(2, 0)
        assert item is not None
        assert item.text() == "—"


class TestRebuilding:
    """一覧の作り直しは**編集のたび**に通る 字幕が多いと、そこが重さになる"""

    def test_it_skips_when_nothing_changed(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]], placed: Project
    ) -> None:
        """中身が前と同じなら作り直さない

        字幕 2000 本で 70ms 掛かる所 字幕に関係のない編集（プロジェクト名を
        変えるなど）でそれを払うのは無駄 作り直していないことは、表の中身が
        同じ物のままかどうかで見る
        """
        widget, _ = panel
        before = widget._table.item(0, 1)
        widget.set_project(RenameProject("別の名前").apply(placed))
        assert widget._table.item(0, 1) is before, "作り直している"

    def test_it_rebuilds_when_the_text_changes(
        self,
        panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]],
        placed: Project,
        video_media: MediaItem,
        transcript: Transcript,
    ) -> None:
        """字幕を直したら作り直す 飛ばすと、直した文字が画面に出ない"""
        widget, _ = panel
        from dataclasses import replace

        first = transcript.segments[0]
        edited = Transcript(
            segments=(replace(first, text="書き直した字幕"), *transcript.segments[1:])
        )
        widget.set_project(SetTranscript(video_media.id, edited).apply(placed))
        item = widget._table.item(0, 1)
        assert item is not None
        assert item.text() == "書き直した字幕"

    def test_the_time_column_does_not_measure_every_row(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]]
    ) -> None:
        """時刻の列は**固定幅** 中身に合わせると本数の 2 乗で遅くなる

        Qt は幅を中身に合わせるとき、1 行足すたびに全部の行を測り直す
        字幕 2000 本では作り直しに 6 秒掛かっていた
        """
        from PySide6.QtWidgets import QHeaderView

        widget, _ = panel
        header = widget._table.horizontalHeader()
        assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.Fixed
        assert header.sectionSize(0) > 0, "幅が 0 だと時刻が読めない"


class TestEditing:
    def test_changing_a_cell_issues_a_command(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]], video_media: MediaItem
    ) -> None:
        widget, issued = panel
        item = widget._table.item(1, 1)
        assert item is not None
        item.setText("直した")

        commands, label = issued[-1]
        assert label == "字幕を編集"
        assert isinstance(commands[0], SetSegmentText)
        assert commands[0].text == "直した"
        assert commands[0].media_id == video_media.id

    def test_rebuilding_the_list_does_not_issue_commands(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]], placed: Project
    ) -> None:
        # 作り直しのたびに編集コマンドが飛ぶと、履歴が埋まる
        widget, issued = panel
        widget.set_project(placed)
        assert issued == []


class TestPlayheadFollowing:
    def test_the_row_under_the_playhead_is_selected(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]]
    ) -> None:
        widget, _ = panel
        widget.set_frame(150)  # 5 秒目 2 枚目（4..6 秒）の範囲
        assert widget._table.currentRow() == 1

    def test_clicking_a_row_asks_to_seek(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]]
    ) -> None:
        widget, _ = panel
        seeks: list[int] = []
        widget.seek_requested.connect(seeks.append)
        widget._table.selectRow(2)
        assert seeks == [210]

    def test_following_the_playhead_does_not_seek_back(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]]
    ) -> None:
        # 再生ヘッドに追従して行を選んだだけで移動を要求すると、再生が引っ掛かる
        widget, _ = panel
        seeks: list[int] = []
        widget.seek_requested.connect(seeks.append)
        widget.set_frame(150)
        assert seeks == []


class TestJetCut:
    def test_without_a_waveform_it_says_so(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]]
    ) -> None:
        widget, issued = panel
        messages: list[str] = []
        widget.status_message.connect(messages.append)
        widget.jet_cut()
        assert issued == []
        assert "波形" in messages[0]

    def test_silence_becomes_a_ripple_cut(
        self,
        qt_application: QApplication,
        placed: Project,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del qt_application
        # 先頭 200 ピーク（約 1.07 秒）だけ無音の素材
        analyzer = StubAnalyzer(make_waveform([(0.0, 400), (0.5, 1500)]))
        widget = SubtitlePanel(placed, analyzer)
        issued: list[tuple[list[Command], str]] = []
        widget.commands_requested.connect(lambda commands, label: issued.append((commands, label)))

        monkeypatch.setattr(JetCutDialog, "exec", lambda self: 1)
        widget.jet_cut()

        commands, label = issued[-1]
        assert isinstance(commands[0], RippleCut)
        # 無音は 0..2.13 秒 余白 0.1 秒で内側へ寄り、さらに 1 秒から始まる字幕を
        # 守るので 0.1..0.9 秒 フレームへ落として 3..27 波形が静かでも、
        # 起こせている区間は切らない
        assert commands[0].ranges == ((3, 27),)
        assert "無音カット" in label
        widget.deleteLater()


class TestBurnAndExport:
    def test_burning_places_text_clips(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]]
    ) -> None:
        widget, issued = panel
        widget.burn()
        commands, label = issued[-1]
        assert label == "字幕を焼き込み"
        # トラックを 1 本足して、字幕 3 枚をテキストとして置く
        assert len(commands) == 4

    def test_export_writes_the_file(
        self,
        panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        widget, _ = panel
        target = tmp_path / "出力.srt"
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(target), "")
        )
        messages: list[str] = []
        widget.status_message.connect(messages.append)

        widget.export_file()
        assert target.read_text(encoding="utf-8").startswith("1\n00:00:01,000")
        assert "書き出した" in messages[0]

    def test_cancelling_the_dialog_writes_nothing(
        self,
        panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        widget, _ = panel
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: ("", ""))
        messages: list[str] = []
        widget.status_message.connect(messages.append)
        widget.export_file()
        assert messages == []


class TestSourceTime:
    def test_the_playhead_maps_back_into_the_media(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]], video_media: MediaItem
    ) -> None:
        widget, _ = panel
        assert widget._source_time(video_media, 90) == Fraction(3)

    def test_a_frame_outside_every_clip_has_no_source_time(
        self, panel: tuple[SubtitlePanel, list[tuple[list[Command], str]]], video_media: MediaItem
    ) -> None:
        widget, _ = panel
        assert widget._source_time(video_media, 9999) is None
