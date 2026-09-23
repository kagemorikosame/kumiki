"""素材の読み込みで画面を固めないことと、裏の処理の進み具合を画面へ出すこと

窓は表示しない（表示すると GL の初期化が走り、画面が点滅する） 素材を調べる所は
差し替えて、調べている最中の状態を狙って作る 本物の素材では、調べ終わる前に
確かめられるかどうかが機械の速さ次第になる
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from sashimono.core.commands import insert_media
from sashimono.core.model import MediaId, MediaItem, Project, ProjectSettings, new_media_id
from sashimono.core.timebase import FrameRate
from sashimono.engine.cache.progress import JobBoard, ProgressSnapshot
from sashimono.engine.decode import ProbeError
from sashimono.ui import main_window as main_window_module
from sashimono.ui.main_window import MainWindow
from sashimono.ui.progress_display import describe_background, row_notes
from sashimono.ui.workspace import Preferences, PreferenceStore


@pytest.fixture
def window(qt_application: QApplication, monkeypatch: pytest.MonkeyPatch) -> Iterator[MainWindow]:
    del qt_application
    created = MainWindow(
        Project.create(ProjectSettings(width=320, height=240, frame_rate=FrameRate(30))),
        confirm_unsaved=False,
    )
    # 置いた素材は本物のファイルではない 解析と控えを頼むと、裏で「開けない」が数えられて
    # この試験が見たい読み込みの表示に混ざる 進み具合の表示は別の試験で数を直に入れて見る
    monkeypatch.setattr(created, "_request_proxy", lambda _media: None)
    monkeypatch.setattr(created._analyzer, "request", lambda _media, **_kwargs: None)
    yield created
    created.close()


class _Gate:
    """素材を調べる所の差し替え 開けるまで調べ終わらない

    調べた所のスレッドを覚える 画面のスレッドで調べていたら、それが固まる原因
    """

    def __init__(self, template: MediaItem, broken: tuple[str, ...] = ()) -> None:
        self.template = template
        self.broken = broken
        self.release = threading.Event()
        self.threads: list[threading.Thread] = []
        self.started = threading.Event()

    def __call__(self, path: Path) -> MediaItem:
        self.threads.append(threading.current_thread())
        self.started.set()
        assert self.release.wait(20.0), "試験が開け忘れた"
        if path.name in self.broken:
            raise ProbeError(f"素材を開けない: {path}")
        # 素材ごとに別の id にする 同じ id だと 2 本目が 1 本目と同じ素材として扱われる
        return replace(self.template, path=path, id=new_media_id())


def _names(window: MainWindow) -> list[str]:
    """一覧に入った素材のファイル名 置いた順"""
    return [media.path.name for media in window.document.project.media]


def _spin(application: QApplication, until: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if until():
            return True
        time.sleep(0.005)
    return False


class TestImportDoesNotFreeze:
    def test_it_returns_before_the_files_are_read(
        self,
        window: MainWindow,
        video_media: MediaItem,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        qt_application: QApplication,
    ) -> None:
        # 前は素材を 1 本ずつ画面のスレッドで調べ、調べ終わるまで返らなかった
        # （1080p の mp4 で 1 本 55ms 24 本で 1.4 秒固まる） ここで待たされる形に
        # 戻ると、門が開かないまま 20 秒待って試験が落ちる
        gate = _Gate(video_media)
        monkeypatch.setattr(main_window_module, "probe_media", gate)

        started = time.monotonic()
        window.import_media([tmp_path / "a.mp4", tmp_path / "b.mp4"])
        assert time.monotonic() - started < 1.0
        assert window.importing
        assert gate.started.wait(5.0)
        assert all(thread is not threading.main_thread() for thread in gate.threads)

        # 調べている間も画面のイベントが回る（タイマーが鳴る）
        ticked: list[bool] = []
        QTimer.singleShot(0, lambda: ticked.append(True))
        assert _spin(qt_application, lambda: bool(ticked))
        assert _names(window) == []

        gate.release.set()
        assert window.wait_for_imports()
        assert _names(window) == ["a.mp4", "b.mp4"]

    def test_the_progress_is_shown_while_reading(
        self,
        window: MainWindow,
        video_media: MediaItem,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        qt_application: QApplication,
    ) -> None:
        gate = _Gate(video_media)
        monkeypatch.setattr(main_window_module, "probe_media", gate)
        window.import_media([tmp_path / "a.mp4", tmp_path / "b.mp4"])
        try:
            indicator = window._import_indicator
            assert _spin(qt_application, lambda: not indicator.isHidden())
            assert indicator.text == "素材を調べている 0/2 本"
            assert indicator.cancel_button is not None
        finally:
            gate.release.set()
        assert window.wait_for_imports()
        # 終わったら消す 出したままだと、終わったのか止まったのか分からない
        assert window._import_indicator.isHidden()
        assert window.statusBar().currentMessage() == "2 件を読み込んだ"

    def test_cancel_places_nothing(
        self,
        window: MainWindow,
        video_media: MediaItem,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        gate = _Gate(video_media)
        monkeypatch.setattr(main_window_module, "probe_media", gate)
        window.import_media([tmp_path / "a.mp4", tmp_path / "b.mp4"])
        window.import_media([tmp_path / "c.mp4"])
        assert gate.started.wait(5.0)

        cancel = window._import_indicator.cancel_button
        assert cancel is not None
        cancel.click()
        gate.release.set()
        assert not window.importing
        assert window.wait_for_imports()
        # 取り消した後に調べ終わった分を置きに来ると、取り消しが効いていない
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        assert _names(window) == []
        assert window.document.history_labels == ()
        assert window._import_indicator.isHidden()
        assert window.statusBar().currentMessage() == "素材の読み込みを取り消した"

    def test_one_undo_takes_the_whole_import_back(
        self,
        window: MainWindow,
        video_media: MediaItem,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # 裏へ移しても 1 回の操作にまとめる 1 本ずつ置くと、10 本読み込んで 10 回取り消すことになる
        gate = _Gate(video_media)
        gate.release.set()
        monkeypatch.setattr(main_window_module, "probe_media", gate)
        window.import_media([tmp_path / f"{index}.mp4" for index in range(5)])
        assert window.wait_for_imports()
        assert len(window.document.project.media) == 5
        assert len(window.document.history_labels) == 1

        window.undo()
        assert _names(window) == []
        assert window.document.project.timeline.tracks == () or all(
            not track.clips for track in window.document.project.timeline.tracks
        )

    def test_a_second_import_waits_for_the_first(
        self,
        window: MainWindow,
        video_media: MediaItem,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        qt_application: QApplication,
    ) -> None:
        # 並べて走らせると、後から頼んだ方が先に置かれ、置く順が頼んだ順と食い違う
        gate = _Gate(video_media)
        monkeypatch.setattr(main_window_module, "probe_media", gate)
        window.import_media([tmp_path / "先.mp4"])
        window.import_media([tmp_path / "後.mp4"])
        assert _spin(qt_application, lambda: "ほかに 1 回分" in window._import_indicator.text)
        gate.release.set()
        assert window.wait_for_imports()
        assert _names(window) == ["先.mp4", "後.mp4"]
        # 頼んだ回ごとに 1 手 まとめて 1 手にすると、後の分だけ戻せない
        assert len(window.document.history_labels) == 2

    def test_a_broken_file_is_reported_and_the_rest_placed(
        self,
        window: MainWindow,
        video_media: MediaItem,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        gate = _Gate(video_media, broken=("壊れた.mp4",))
        gate.release.set()
        monkeypatch.setattr(main_window_module, "probe_media", gate)
        window.import_media([tmp_path / "壊れた.mp4", tmp_path / "良い.mp4"])
        assert window.wait_for_imports()
        assert _names(window) == ["良い.mp4"]
        assert "壊れた.mp4" in window.statusBar().currentMessage()

    def test_a_drop_on_the_pool_goes_the_same_way(
        self,
        window: MainWindow,
        video_media: MediaItem,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # 一覧への落とし込みとボタンは import_requested を通る 別の道で読むと、
        # そちらだけ画面が固まったまま残る
        gate = _Gate(video_media)
        monkeypatch.setattr(main_window_module, "probe_media", gate)
        window._media_pool.import_requested.emit([tmp_path / "落とした.mp4"])
        assert window.importing
        gate.release.set()
        assert window.wait_for_imports()
        assert _names(window) == ["落とした.mp4"]


class TestBackgroundProgress:
    """控えと解析の進み具合 数は裏の係の数え板へ直に入れる

    本物の変換は機械の速さで終わる時刻が変わり、途中の割合を狙って見られない
    """

    def _place(self, window: MainWindow, media: MediaItem) -> None:
        window.execute_all(insert_media(window.view_project, media), "置く")

    def test_the_status_bar_and_the_row_show_it(
        self, window: MainWindow, video_media: MediaItem
    ) -> None:
        self._place(window, video_media)
        board = window._analyzer._board
        board.start(("waveform", video_media.id), video_media.id)
        board.start(("filmstrip", video_media.id), video_media.id)
        board.report(("waveform", video_media.id), 1.0)

        window._flush_analysis()
        indicator = window._background_indicator
        assert not indicator.isHidden()
        assert indicator.text == "波形とサムネイル 0/2 件 50%"
        assert indicator.value == 500
        row = window._media_pool.row_text(video_media.id)
        assert row is not None and row.endswith("[解析 50%]")

    def test_the_end_and_the_failure_are_told(
        self, window: MainWindow, video_media: MediaItem
    ) -> None:
        self._place(window, video_media)
        board = window._analyzer._board
        key = ("waveform", video_media.id)
        board.start(key, video_media.id)
        window._flush_analysis()
        board.finish(key, "波形を作れなかった: 素材を開けない")
        window._flush_analysis()

        assert window._background_indicator.isHidden()
        message = window.statusBar().currentMessage()
        assert "失敗 1 件" in message
        assert "素材を開けない" in message
        row = window._media_pool.row_text(video_media.id)
        assert row is not None and row.endswith("[解析できなかった]")

    def test_a_quiet_finish_says_nothing(self, window: MainWindow, video_media: MediaItem) -> None:
        # キャッシュから一瞬で済んだ分まで「終わった」と出すと、開くたびに文言が出る
        board = window._analyzer._board
        key = ("waveform", video_media.id)
        board.start(key, video_media.id)
        board.finish(key)
        window.statusBar().clearMessage()
        window._flush_analysis()
        assert window.statusBar().currentMessage() == ""

    def test_the_proxy_is_counted_apart(self, window: MainWindow, video_media: MediaItem) -> None:
        # 読み込んだ直後にプレビューが重い理由が、控えを作っている最中だからなのかを見て分かるように
        board = window._proxies._board
        board.start(video_media.id, video_media.id)
        board.report(video_media.id, 0.4)
        window._flush_analysis()
        assert window._background_indicator.text == "控え 0/1 本 40%"

    def test_turning_the_rows_off_leaves_the_status_bar(
        self, window: MainWindow, video_media: MediaItem
    ) -> None:
        # 切っても行に割合が残るなら、設定がある方が質が悪い
        self._place(window, video_media)
        window._apply_preferences(Preferences(pool_progress=False))
        board = window._analyzer._board
        board.start(("waveform", video_media.id), video_media.id)
        board.report(("waveform", video_media.id), 0.5)
        window._flush_analysis()
        row = window._media_pool.row_text(video_media.id)
        assert row is not None and "[" not in row
        assert not window._background_indicator.isHidden()

    def test_turning_the_rows_off_clears_them_at_once(
        self, window: MainWindow, video_media: MediaItem
    ) -> None:
        self._place(window, video_media)
        board = window._analyzer._board
        board.start(("waveform", video_media.id), video_media.id)
        window._flush_analysis()
        window._apply_preferences(Preferences(pool_progress=False))
        row = window._media_pool.row_text(video_media.id)
        assert row is not None and "[" not in row


class TestWording:
    def test_both_kinds_are_listed(self) -> None:
        media = MediaId("m")
        proxy, analysis = JobBoard(), JobBoard()
        proxy.start("p", media)
        analysis.start("a", media)
        analysis.finish("a", "開けない")
        analysis.start("b", media)
        text = describe_background(proxy.poll(), analysis.poll())
        assert text == "控え 0/1 本 0%・波形とサムネイル 1/2 件 50% 失敗 1 件"

    def test_a_row_without_work_has_no_note(self) -> None:
        assert row_notes(ProgressSnapshot(), ProgressSnapshot()) == {}


class TestPreference:
    def test_it_is_on_by_default(self) -> None:
        # 既定は知らない人が困らない側 どの素材の控えを作っているのかが見える側
        assert Preferences().pool_progress is True

    def test_it_comes_back(self, tmp_path: Path) -> None:
        store = PreferenceStore(tmp_path / "preferences.json")
        store.save(Preferences(pool_progress=False))
        assert store.load().pool_progress is False

    def test_a_broken_value_falls_back(self, tmp_path: Path) -> None:
        path = tmp_path / "preferences.json"
        path.write_text('{"pool_progress": "off"}', encoding="utf-8")
        assert PreferenceStore(path).load().pool_progress is True

    def test_the_dialog_shows_and_returns_it(self, qt_application: QApplication) -> None:
        del qt_application
        from sashimono.ui.preferences_dialog import PreferencesDialog

        dialog = PreferencesDialog(Preferences(pool_progress=False))
        assert dialog.preferences().pool_progress is False
        dialog._pool_progress.setChecked(True)
        assert dialog.preferences().pool_progress is True
