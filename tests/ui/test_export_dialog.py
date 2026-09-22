"""書き出しの画面に並ぶコーデック（#67）

何も選ばずに書き出したときの動きがここで決まる 開けない物が既定になると、
書き出しを押しただけで失敗する
"""

from __future__ import annotations

from collections.abc import Iterator

import av.error
import pytest

from sashimono.core.model import Project, ProjectSettings
from sashimono.engine.encode import exporter
from sashimono.ui.export_dialog import AUTO_CODEC, ExportDialog


@pytest.fixture
def qsv_does_not_open(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """この開発機と同じく、QSV は入っているが開けない機械を真似る"""

    def fake(name: str) -> None:
        if name == "h264_qsv":
            raise av.error.ArgumentError(22, "Invalid argument")

    # 開けるかの答えはプロセスの中で覚えている 前後で忘れないと、偽の答えが他の試験へ漏れる
    exporter._opens.cache_clear()
    monkeypatch.setattr(exporter, "_open_encoder", fake)
    monkeypatch.setattr(exporter, "VIDEO_CODEC_PREFERENCE", ("h264_qsv", "libx264"))
    yield
    exporter._opens.cache_clear()


def test_a_qsv_that_does_not_open_is_not_listed(qsv_does_not_open: None) -> None:
    """開けない QSV を並べない

    並べると、開けない QSV を選んで書き出しを押し、失敗してから別の物を選び直すことになる
    """
    dialog = ExportDialog(Project.create(ProjectSettings()))
    try:
        codecs = [dialog._codec.itemData(index) for index in range(dialog._codec.count())]
        assert codecs == [AUTO_CODEC, "libx264"]
    finally:
        dialog.deleteLater()


def test_the_default_leaves_the_choice_to_the_exporter(qsv_does_not_open: None) -> None:
    """既定のまま書き出すと、コーデックを名指ししない

    先頭の候補を名指しで渡すと、試しには開けても作品の大きさで断られたとき
    （NVENC は幅 4096 まで）に次の候補へ落ちられず、画面から書き出すときだけ失敗する
    """
    dialog = ExportDialog(Project.create(ProjectSettings()))
    try:
        settings = dialog._settings()
        assert settings is not None
        assert settings.video_codec is None
    finally:
        dialog.deleteLater()


def test_a_codec_picked_by_hand_is_passed_by_name(qsv_does_not_open: None) -> None:
    """手で選んだコーデックは名指しで渡す 書き出し側が勝手に別の物へ変えない"""
    dialog = ExportDialog(Project.create(ProjectSettings()))
    try:
        dialog._codec.setCurrentIndex(dialog._codec.findData("libx264"))
        settings = dialog._settings()
        assert settings is not None
        assert settings.video_codec == "libx264"
    finally:
        dialog.deleteLater()
