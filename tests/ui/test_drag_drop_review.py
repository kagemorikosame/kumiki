"""ドラッグ＆ドロップと素材一覧の、レビューで見つかった穴（PR #144）

- 新しく作るトラックの仮の行は、実際に足される位置（映像なら上、音声なら下）に出す
- 読み込みの最中にシーンを切り替えても、落としたシーンへ置く
- 壊れた中身のドラッグで例外を出さない
- 3 色や灰色の絵でも、サムネイルが行の外を読まない
- 同じ素材 ID のままファイルが差し替わったら、サムネイルを作り直す
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication

from sashimono.core.model import (
    MediaItem,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
    new_media_id,
)
from sashimono.core.timebase import FrameRate
from sashimono.engine.cache import MediaAnalyzer
from sashimono.ui import main_window as main_window_module
from sashimono.ui.main_window import MainWindow
from sashimono.ui.media_icons import GRID_ICON_SIZE, thumbnail_icon
from sashimono.ui.media_pool import MEDIA_MIME, MediaPoolWidget, media_ids_in
from sashimono.ui.timeline import TimelineView
from tests.conftest import make_clip


def _occupied(video: MediaItem, audio: MediaItem) -> Project:
    """V1 と A1 の頭が埋まったプロジェクト 素材を頭へ落とすと、両方とも新しいトラックが要る"""
    base = Project.create(
        ProjectSettings(width=320, height=240, frame_rate=FrameRate(30)), media=(video, audio)
    )
    tracks = (
        Track(TrackKind.VIDEO, "V1", (make_clip(0, 300, video),)),
        Track(TrackKind.AUDIO, "A1", (make_clip(0, 300, audio),)),
    )
    return base.with_timeline(replace(base.timeline, tracks=tracks))


def _move(view: TimelineView, mime: QMimeData, position: QPoint) -> QDragMoveEvent:
    event = QDragMoveEvent(
        position,
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.dragMoveEvent(event)
    return event


def _drop(view: TimelineView, mime: QMimeData, position: QPointF) -> QDropEvent:
    event = QDropEvent(
        position,
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.dropEvent(event)
    return event


def _media_mime(media: MediaItem) -> QMimeData:
    mime = QMimeData()
    mime.setData(MEDIA_MIME, str(media.id).encode("utf-8"))
    return mime


@pytest.fixture
def analyzer(qt_application: QApplication) -> Iterator[MediaAnalyzer]:
    del qt_application
    created = MediaAnalyzer(sample_rate=48000, channels=2)
    yield created
    created.close()


class TestNewTracksShowWhereTheyGo:
    def test_the_video_row_is_on_top_and_the_audio_row_at_the_bottom(
        self, analyzer: MediaAnalyzer, video_media: MediaItem, audio_media: MediaItem
    ) -> None:
        # 前は仮の行を並びの一番下にまとめて出していた 映像のトラックは一番上に足されるので、
        # 落とした後に出てくる位置と違い、どこへ入ったのか探すことになる
        view = TimelineView(_occupied(video_media, audio_media), analyzer)
        view.resize(900, 400)
        band = view._layout.bands(view.project.timeline)[0]
        # 引く前の、一番上の行の空いた所 仮の行はここより明るく描かれるはず
        plain = view.grab().toImage().pixelColor(880, band.top + band.height // 2)
        mime = _media_mime(video_media)
        _move(view, mime, QPoint(int(view._layout.frame_to_x(30)), band.top + band.height // 2))
        preview = view.drop_preview
        assert preview is not None
        assert len(preview.new_tracks) == 2
        shown = view._layout.bands(preview.timeline)
        # 置いたあとのタイムラインと同じ並び 上から V2 V1 A1 A2
        assert [b.track.name for b in shown] == ["V2", "V1", "A1", "A2"]
        assert shown[0].track.id in preview.new_tracks
        assert shown[-1].track.id in preview.new_tracks
        # 絵でも確かめる 一番上の行が仮の行になり、地が元より明るい
        assert shown[0].top == band.top
        ghost = view.grab().toImage().pixelColor(880, shown[0].top + shown[0].height // 2)
        assert ghost.lightness() > plain.lightness()

    def test_dropping_on_the_new_row_does_not_hit_the_row_it_pushed_down(
        self, analyzer: MediaAnalyzer, video_media: MediaItem, audio_media: MediaItem
    ) -> None:
        # 見えている並び（仮の行を入れた並び）で落ちる先を決める 本物の並びで決めると、
        # 仮の行へ落としたのに、その下へ押し下げられて見えている V1 のつもりにされる
        view = TimelineView(_occupied(video_media, audio_media), analyzer)
        view.resize(900, 400)
        band = view._layout.bands(view.project.timeline)[0]
        mime = _media_mime(video_media)
        point = QPoint(int(view._layout.frame_to_x(30)), band.top + band.height // 2)
        _move(view, mime, point)
        received: list[str] = []
        view.media_dropped.connect(lambda _ids, _frame, track: received.append(track))
        _drop(view, mime, QPointF(point))
        assert received == [""]
        assert view.drop_preview is None


class TestBrokenDrags:
    def test_broken_bytes_are_refused_quietly(self, analyzer: MediaAnalyzer) -> None:
        # 同じ形式の名前で壊れた中身を引いてくるアプリがある 例外になると、ドラッグの
        # 知らせの途中で止まり、落とせない印も出ない
        mime = QMimeData()
        mime.setData(MEDIA_MIME, b"\xff\xfe\xfa")
        assert media_ids_in(mime) == []
        base = Project.create()
        view = TimelineView(base, analyzer)
        view.resize(600, 300)
        assert not _move(view, mime, QPoint(300, 100)).isAccepted()
        assert not _drop(view, mime, QPointF(300, 100)).isAccepted()


class TestThumbnailShapes:
    @pytest.mark.parametrize("channels", [1, 3])
    def test_fewer_channels_still_make_a_picture(self, channels: int) -> None:
        # 幅×4 バイトの行だと思って 3 色や 1 色の絵を読むと、行の外まで読んで絵が崩れる
        tile = np.full((72, 128, channels), 180, dtype=np.uint8)
        image = thumbnail_icon(tile).pixmap(GRID_ICON_SIZE).toImage()
        center = image.pixelColor(image.width() // 2, image.height() // 2)
        assert (center.red(), center.green(), center.blue(), center.alpha()) == (180, 180, 180, 255)

    def test_a_flat_grey_picture_is_taken(self) -> None:
        tile = np.full((72, 128), 90, dtype=np.uint8)
        image = thumbnail_icon(tile).pixmap(GRID_ICON_SIZE).toImage()
        assert image.pixelColor(64, 36).red() == 90


class TestReplacedSource:
    def test_a_new_file_gets_a_new_thumbnail(
        self, qt_application: QApplication, video_media: MediaItem
    ) -> None:
        # 同じ ID のままファイルだけ差し替わったのに前の絵が残ると、中身と絵が食い違う
        del qt_application
        project = Project.create(media=(video_media,))
        pool = MediaPoolWidget(project)
        colors = {"本編.mp4": (200, 30, 40), "差し替え.mp4": (20, 160, 60)}

        def tile(media: MediaItem) -> np.ndarray:
            picture = np.zeros((72, 128, 4), dtype=np.uint8)
            picture[:, :, :3] = colors[media.path.name]
            picture[:, :, 3] = 255
            return picture

        pool.set_thumbnail_source(tile)
        image = pool._list.item(0).icon().pixmap(GRID_ICON_SIZE).toImage()
        assert image.pixelColor(64, 36).name() == "#c81e28"
        swapped = replace(video_media, path=video_media.path.with_name("差し替え.mp4"))
        pool.set_project(project.replace_media(swapped))
        image = pool._list.item(0).icon().pixmap(GRID_ICON_SIZE).toImage()
        assert image.pixelColor(64, 36).name() == "#14a03c"


class _GatedProbe:
    """開けるまで調べ終わらない 調べている間にシーンを切り替えるため"""

    def __init__(self, template: MediaItem) -> None:
        self.template = template
        self.release = threading.Event()

    def __call__(self, path: Path) -> MediaItem:
        assert self.release.wait(20.0), "試験が開け忘れた"
        return replace(self.template, path=path, id=new_media_id())


@pytest.fixture
def window(qt_application: QApplication, monkeypatch: pytest.MonkeyPatch) -> Iterator[MainWindow]:
    del qt_application
    base = Project.create(ProjectSettings(width=320, height=240, frame_rate=FrameRate(30)))
    created = MainWindow(base, confirm_unsaved=False)
    monkeypatch.setattr(created, "_request_proxy", lambda _media: None)
    monkeypatch.setattr(created._analyzer, "request", lambda _media, **_kwargs: None)
    created._timeline.resize(900, 300)
    yield created
    created.close()


def _file_mime(path: Path) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    return mime


class TestSceneSwitchWhileReading:
    def test_it_lands_in_the_scene_it_was_dropped_on(
        self,
        window: MainWindow,
        audio_media: MediaItem,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # 調べ終えた時点で開いているシーン（ここではメイン）へ置くと、落としていない
        # タイムラインに素材が入る
        probe = _GatedProbe(audio_media)
        monkeypatch.setattr(main_window_module, "probe_media", probe)
        scene_id = window.create_scene("オープニング")
        assert scene_id is not None
        _drop(window._timeline, _file_mime(tmp_path / "声.wav"), QPointF(400, 120))
        window.open_scene(None)
        probe.release.set()
        assert window.wait_for_imports()
        root = window.document.project
        assert all(track.clips == () for track in root.timeline.tracks)
        placed = [c for t in root.require_scene(scene_id).timeline.tracks for c in t.clips]
        assert len(placed) == 1
        # 取り消し 1 回で戻る
        window.undo()
        assert window.document.project.media == ()

    def test_a_drop_on_main_stays_in_main_after_opening_a_scene(
        self,
        window: MainWindow,
        audio_media: MediaItem,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # 逆向き メインへ落としてからシーンを開いても、シーンへ入れない
        probe = _GatedProbe(audio_media)
        monkeypatch.setattr(main_window_module, "probe_media", probe)
        _drop(window._timeline, _file_mime(tmp_path / "声.wav"), QPointF(400, 120))
        scene_id = window.create_scene("あとで開いた")
        assert scene_id is not None and window.active_scene == scene_id
        probe.release.set()
        assert window.wait_for_imports()
        root = window.document.project
        assert [len(t.clips) for t in root.timeline.tracks] == [1]
        assert all(t.clips == () for t in root.require_scene(scene_id).timeline.tracks)
        window.undo()
        assert window.document.project.media == ()

    def test_a_removed_scene_only_fills_the_pool(
        self,
        window: MainWindow,
        audio_media: MediaItem,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # 落とした先のシーンが消えたら、ほかのタイムラインへは置かない 素材だけ一覧へ入れて伝える
        probe = _GatedProbe(audio_media)
        monkeypatch.setattr(main_window_module, "probe_media", probe)
        scene_id = window.create_scene("消える")
        assert scene_id is not None
        _drop(window._timeline, _file_mime(tmp_path / "声.wav"), QPointF(400, 120))
        window.remove_active_scene()
        assert window.document.project.find_scene(scene_id) is None
        probe.release.set()
        assert window.wait_for_imports()
        root = window.document.project
        assert [media.path.name for media in root.media] == ["声.wav"]
        assert all(track.clips == () for track in root.timeline.tracks)
        assert "シーンが無くなった" in window.statusBar().currentMessage()
