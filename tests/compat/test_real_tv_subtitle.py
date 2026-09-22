"""このマシンに置かれたテレビ字幕（配布物）を、実際の DLL ごと描く

テレビ字幕は ``obj.module("TVSubtitle").scan`` で文字の行ごとの四角を受け取り、
その裏へ板を描く ``scan`` の中身は DLL（``TVSubtitle.mod2``）で公開されていない
推測で書き直さず、**利用者の AviUtl2 に入っている DLL をそのまま呼んでいる**ので、
ここではその道が最後まで通って板が出ることを確かめる

配布物も DLL もリポジトリには入れていないので、無ければ黙って飛ばす
（``%PROGRAMDATA%\\aviutl2`` に AviUtl2 と配布物が入っている機械でだけ走る）
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from sashimono.compat.aviutl import catalog as catalog_module
from sashimono.compat.aviutl import native
from sashimono.compat.aviutl.catalog import ScriptCatalog, set_script_catalog
from sashimono.compat.aviutl.exo import load_exo
from sashimono.compat.aviutl.mapping import map_object
from sashimono.compat.catalog import place
from sashimono.core.model import Project, ProjectSettings
from sashimono.core.timebase import FrameRate
from sashimono.engine.render import FrameRenderer

pytestmark = pytest.mark.usefixtures("gpu")

#: 配布物の注意警告の板 ``背景色=ffd400``
YELLOW = (255, 212, 0)


def _installed() -> tuple[Path, Path] | None:
    program_data = os.environ.get("PROGRAMDATA")
    if not program_data:
        return None
    root = Path(program_data) / "aviutl2"
    alias = root / "Alias" / "YouTube字幕デザイン" / "07_注意警告_黄プレート.object"
    module = root / "Script" / "テレビ字幕" / "TVSubtitle.mod2"
    if not (alias.is_file() and module.is_file()):
        return None
    return alias, module


def _render(alias: Path) -> np.ndarray:
    settings = ProjectSettings(width=1920, height=1080, frame_rate=FrameRate(60))
    objects = [o for obj in load_exo(alias).objects if (o := map_object(obj, settings.frame_rate))]
    project = Project.create(settings)
    for command in place(objects, project, at_frame=0):
        project = command.apply(project)
    renderer = FrameRenderer(project)
    try:
        return renderer.render(project.duration // 2)
    finally:
        renderer.close()


@pytest.fixture
def installed() -> Iterator[tuple[Path, Path]]:
    """配布物と DLL があれば、本物のスクリプト一覧を用意する

    共有の一覧はほかの試験が差し替えることがある（見本だけの一覧にして戻さない）
    その一覧にはテレビ字幕が無いので、ここで本物を読み、終わったら戻す
    """
    found = _installed()
    if found is None:
        pytest.skip("テレビ字幕の配布物か DLL がこのマシンに無い")
    native.set_enabled(True)
    saved = catalog_module._catalog
    real = ScriptCatalog()
    real.scan()
    set_script_catalog(real)
    yield found
    catalog_module._catalog = saved
    if saved is not None:
        saved.register_all()


class TestTheYellowPlate:
    def test_the_plate_is_drawn_behind_the_text(self, installed: tuple[Path, Path]) -> None:
        """黄色の板が出て、文字はその内側にある

        板が出ないときに疑う所は 3 つ DLL を呼べていない（``scan`` が nil）、
        板の色を読めていない（黒の上に黒）、図形を 0〜1 で貼れていない（透明を貼る）
        どれも以前は実際に起きていた
        """
        alias, _module = installed
        image = _render(alias)[..., :3].astype(int)
        plate = np.all(np.abs(image - YELLOW) <= 2, axis=2)
        assert plate.sum() > 10_000, "黄色の板が出ていない"

        rows, columns = np.nonzero(plate)
        top, bottom, left, right = rows.min(), rows.max(), columns.min(), columns.max()
        # 文字（暗い色）は板の内側にある 板が文字からずれて描かれていない
        text = (image.sum(axis=2) < 150) & (image.sum(axis=2) > 30)
        text_rows, text_columns = np.nonzero(text)
        assert top <= text_rows.min() and text_rows.max() <= bottom
        assert left <= text_columns.min() and text_columns.max() <= right

    def test_turned_off_the_plate_is_not_drawn(self, installed: tuple[Path, Path]) -> None:
        """切ったら DLL を呼ばない 板は出ない（文字だけは出る）"""
        alias, _module = installed
        native.set_enabled(False)
        try:
            image = _render(alias)[..., :3].astype(int)
        finally:
            native.set_enabled(True)
        plate = np.all(np.abs(image - YELLOW) <= 2, axis=2)
        assert plate.sum() == 0
        # 文字は出ていること 板が無いだけでなく、真っ黒な絵でも上の確かめは通ってしまう
        # （DLL を読まないとスクリプトは失敗し、そのオブジェクトは素通しで出る）
        text = (image.sum(axis=2) > 30) & (image.sum(axis=2) < 150)
        assert text.sum() > 1_000, "板どころか文字まで消えている"
