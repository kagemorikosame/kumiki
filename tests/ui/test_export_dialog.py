"""書き出しの画面に並ぶコーデック（#67）

画面の先頭が、何も選ばずに書き出したときのコーデックになる ここに開けない物が
来ると、書き出しを押しただけで失敗する
"""

from __future__ import annotations

from collections.abc import Iterator

import av.error
import pytest

from sashimono.core.model import Project, ProjectSettings
from sashimono.engine.encode import exporter
from sashimono.ui.export_dialog import ExportDialog


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


def test_a_qsv_that_does_not_open_is_neither_listed_nor_the_default(
    qsv_does_not_open: None,
) -> None:
    """開けない QSV を並べず、既定（先頭）は libx264 になる

    並べると、開けない QSV を選んで書き出しを押し、失敗してから別の物を選び直すことになる
    """
    dialog = ExportDialog(Project.create(ProjectSettings()))
    try:
        codecs = [dialog._codec.itemData(index) for index in range(dialog._codec.count())]
        assert codecs == ["libx264"]
        assert dialog._codec.currentData() == "libx264"
    finally:
        dialog.deleteLater()
