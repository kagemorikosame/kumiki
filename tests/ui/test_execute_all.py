"""まとめて実行するコマンドが断られたことを、呼び出し側が知れるか

テンプレートの配置は素材の登録と配置を 1 回の Undo にまとめる 途中で断られると
全部戻るので、戻った後に「置いた」と出したり、戻した素材の解析を頼んだりしてはいけない
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from sashimono.compat.mapped import MappedObject
from sashimono.core.commands import AddClip, AddMedia, insert_generated
from sashimono.core.model import Clip, GeneratedSource, MediaItem, Project, TrackId
from sashimono.ui import template_dialog
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
    # 通ったのに偽を返すと、テンプレートを置いても素材の解析と控えの作成に進まず、
    # 「置いた」とも出ない
    media = MediaItem(path=Path("C:/素材/効果音.mp3"))
    assert window.execute_all([AddMedia(media)], "素材を追加") is True
    assert window.document.project.media == (media,)


class _Chosen:
    """テンプレートの棚の代わり 開くとすぐ、決めておいた選択で閉じる"""

    choice: tuple[str, list[MappedObject]] = ("place", [])

    def __init__(self, parent: object = None) -> None:
        del parent
        self.origin: Path | None = None

    def exec(self) -> QDialog.DialogCode:
        return QDialog.DialogCode.Accepted


def _refuse_everything(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, choice: tuple[str, list[MappedObject]]
) -> None:
    monkeypatch.setattr(_Chosen, "choice", choice)
    monkeypatch.setattr(template_dialog, "TemplateDialog", _Chosen)
    monkeypatch.setattr(window, "execute_all", lambda *_args, **_kw: False)


def test_a_refused_restyle_does_not_claim_success(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 断られて戻ったのに「適用した」と出すと、何も変わっていないことに気付けない
    text = GeneratedSource(kind="text", params={"text": "字幕"})
    for command in insert_generated(window.document.project, text, at_frame=0):
        window.execute(command)
    clip = window.document.project.timeline.tracks[0].clips[0]
    window.select_clip(clip.id)
    template = MappedObject(clip=Clip(timeline_start=0, duration=30, source=text), layer=1)
    _refuse_everything(window, monkeypatch, ("restyle", [template]))
    window.statusBar().clearMessage()

    window.show_templates()
    assert "適用した" not in window.statusBar().currentMessage()


def test_a_refused_place_does_not_analyze_the_rolled_back_media(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 戻した素材の解析や控えを頼むと、一覧に無い素材のために裏で重い処理が走る
    picture = tmp_path / "絵.png"
    picture.write_bytes(b"")
    item = MappedObject(
        clip=Clip(timeline_start=0, duration=30), layer=1, media_path=str(picture), kind="画像"
    )
    monkeypatch.setattr(
        "sashimono.ui.main_window._probe_or_none", lambda path: MediaItem(path=path)
    )
    requested: list[MediaItem] = []
    analyzed: list[MediaItem] = []
    monkeypatch.setattr(window, "_request_proxy", requested.append)
    monkeypatch.setattr(
        window._analyzer, "request", lambda media, **_kwargs: analyzed.append(media)
    )
    _refuse_everything(window, monkeypatch, ("place", [item]))
    window.statusBar().clearMessage()

    window.show_templates()
    assert analyzed == []
    assert requested == []
    assert "置いた" not in window.statusBar().currentMessage()
