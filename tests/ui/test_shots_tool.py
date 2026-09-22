"""README の写真を撮る道具（tools/shots.py）

写真は手で撮ると、撮った人の画面配置や本人のファイル名が写り、画面が変わった
ときに同じ絵を作り直せない この道具はそこを引き受けているので、壊れると
README の写真が古いまま残る

GPU が要る検査は ``gpu`` フィクスチャを付けてある GPU の無い所（CI）では飛ぶ
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QTreeWidget

from sashimono.compat.catalog import TemplateCatalog
from tests.media_fixtures import ffmpeg_available

ROOT = Path(__file__).resolve().parents[2]

#: 見本のエイリアス 実配布物はリポジトリに入れないので、同じ書き方の最小の物を作る
SAMPLE_ALIAS = "\n".join(
    (
        "[Object]",
        "frame=0,89",
        "[Object.0]",
        "effect.name=テキスト",
        "サイズ=64.00",
        "文字色=ffee00",
        "テキスト=見本の字幕",
        "",
    )
)


@pytest.fixture(scope="module")
def shots() -> ModuleType:
    """道具を 1 つのモジュールとして読み込む インストールされた包みではない"""
    spec = importlib.util.spec_from_file_location("shots", ROOT / "tools" / "shots.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestShelfRoots:
    """棚の置き場を差し替えられること

    差し替えられないと、撮った機械に入っている配布物が丸ごと写真に写る
    """

    def test_the_dialog_lists_only_the_given_roots(
        self, shots: ModuleType, qt_application: QApplication, tmp_path: Path
    ) -> None:
        del shots, qt_application
        from sashimono.ui.template_dialog import TemplateDialog

        (tmp_path / "見本のエイリアス.object").write_text(SAMPLE_ALIAS, encoding="utf-8")
        dialog = TemplateDialog(TemplateCatalog(), roots=(tmp_path,))
        try:
            tree = dialog.findChild(QTreeWidget)
            assert tree is not None
            names: list[str] = []
            for index in range(tree.topLevelItemCount()):
                group = tree.topLevelItem(index)
                assert group is not None
                for position in range(group.childCount()):
                    child = group.child(position)
                    assert child is not None
                    names.append(child.text(0))
            assert names == ["見本のエイリアス"]
        finally:
            dialog.close()


class TestChoosingTemplates:
    def test_the_named_template_wins(self, shots: ModuleType, tmp_path: Path) -> None:
        # README の本文が名前を引き合いに出している どれが写るか分からないと、
        # 本文と絵が食い違う
        (tmp_path / "あ.object").write_text(SAMPLE_ALIAS, encoding="utf-8")
        (tmp_path / "13_金ピカテキスト.object").write_text(SAMPLE_ALIAS, encoding="utf-8")
        entries = TemplateCatalog().scan((tmp_path,))

        chosen = shots._choose(entries, shots.PREFERRED_ALIAS, shots._has_text)
        assert chosen is not None
        assert chosen.name == "13_金ピカテキスト"

    def test_without_the_named_one_it_falls_back(self, shots: ModuleType, tmp_path: Path) -> None:
        (tmp_path / "あ.object").write_text(SAMPLE_ALIAS, encoding="utf-8")
        entries = TemplateCatalog().scan((tmp_path,))

        chosen = shots._choose(entries, shots.PREFERRED_ALIAS, shots._has_text)
        assert chosen is not None
        assert chosen.name == "あ"


class TestSampleScript:
    def test_the_sample_script_becomes_a_settings_panel(
        self, shots: ModuleType, tmp_path: Path
    ) -> None:
        """制御行が設定欄になる これが AviUtl の写真で見せている所そのもの"""
        from sashimono.effects.definition import registry

        kind = shots.install_sample_script(tmp_path / "scripts")
        definition = registry.require(kind)
        assert [spec.label for spec in definition.parameters] == ["振れ幅", "速さ", "横に揺れる"]


@pytest.mark.usefixtures("gpu")
class TestTakingTheEditorShot:
    def test_the_editor_shot_shows_the_preview(
        self, shots: ModuleType, qt_application: QApplication, tmp_path: Path
    ) -> None:
        """撮れた絵のプレビューが真っ黒でないこと

        ``QWidget.grab()`` は環境によって GL の中身を拾わない 拾えていないと、
        気付かないまま真っ黒なプレビューの写真が README に載る
        """
        del qt_application
        if not ffmpeg_available():
            pytest.skip("ffmpeg が PATH に無いので見本の素材を作れない")

        context = shots.Context(
            media=shots.make_sample_media(tmp_path / "media"),
            alias_root=None,
            ymm4_root=None,
            script_root=tmp_path / "scripts",
        )
        image = shots.shot_editor(context)

        assert (image.width(), image.height()) == shots.WINDOW_SIZE
        whole = QRect(0, 0, image.width(), image.height())
        assert shots.brightest(image, whole) > shots.BLACK_LEVEL
