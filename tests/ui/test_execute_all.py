"""まとめて実行するコマンドが断られたことを、呼び出し側が知れるか

テンプレートの配置は素材の登録と配置を 1 回の Undo にまとめる 途中で断られると
全部戻るので、戻った後に「置いた」と出したり、戻した素材の解析を頼んだりしてはいけない
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from sashimono.core.commands import AddClip, AddMedia
from sashimono.core.model import Clip, MediaItem, Project, TrackId
from sashimono.ui.main_window import MainWindow


@pytest.fixture
def window(qt_application: QApplication) -> Iterator[MainWindow]:
    del qt_application
    created = MainWindow(Project.create(), confirm_unsaved=False)
    yield created
    created.close()


def test_a_refused_batch_reports_failure_and_rolls_back(window: MainWindow) -> None:
    # 無いトラックへ置くコマンドで断らせる 先に足した素材も一緒に戻る
    media = MediaItem(path=Path("C:/素材/効果音.mp3"))
    refused = window.execute_all(
        [AddMedia(media), AddClip(TrackId("無いトラック"), Clip(timeline_start=0, duration=10))],
        "テンプレートを配置",
    )
    assert refused is False
    assert window.document.project.media == ()


def test_a_successful_batch_reports_success(window: MainWindow) -> None:
    media = MediaItem(path=Path("C:/素材/効果音.mp3"))
    assert window.execute_all([AddMedia(media)], "素材を追加") is True
    assert window.document.project.media == (media,)
