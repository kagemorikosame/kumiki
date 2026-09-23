"""README の写真を撮る道具（tools/shots.py）

写真は手で撮ると、撮った人の画面配置や本人のファイル名が写り、画面が変わった
ときに同じ絵を作り直せない この道具はそこを引き受けているので、壊れると
README の写真が古いまま残る

GPU が要る検査は ``gpu`` フィクスチャを付けてある GPU の無い所（CI）では飛ぶ
"""

from __future__ import annotations

import importlib.util
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QLabel, QTreeWidget, QWidget

from sashimono.compat.aviutl import catalog as catalog_module
from sashimono.compat.aviutl.catalog import ScriptCatalog, script_catalog, set_script_catalog
from sashimono.compat.catalog import TemplateCatalog
from sashimono.core import userdirs
from sashimono.effects.definition import registry
from tests.media_fixtures import libx264_available

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
        # 名指しの配布物が無い機械でも、文字を持つ物の先頭に落ちること 落ちないと
        # 棚の写真は何も選ばれていない状態で写り、着せた写真は撮られずに飛ぶ
        (tmp_path / "あ.object").write_text(SAMPLE_ALIAS, encoding="utf-8")
        entries = TemplateCatalog().scan((tmp_path,))

        chosen = shots._choose(entries, shots.PREFERRED_ALIAS, shots._has_text)
        assert chosen is not None
        assert chosen.name == "あ"


class TestSettling:
    def test_it_really_waits(
        self, shots: ModuleType, qt_application: QApplication, tmp_path: Path
    ) -> None:
        """撮る前の待ちが、本当に時間を使っていること

        ``processEvents`` に時間を渡しても、処理するイベントが尽きれば
        すぐ戻る 待っているつもりで待っていないと、解析の反映（250ms ごと）を
        1 度も通さないまま撮り、波形とサムネイルの無いタイムラインが写る
        """
        del tmp_path, qt_application
        widget = QWidget()
        rounds = 6
        started = time.monotonic()
        shots.settle(widget, rounds=rounds)
        elapsed = (time.monotonic() - started) * 1000

        # 端数で落ちないよう 8 割で見る 待っていなければ 1 ミリ秒も経たない
        assert elapsed >= rounds * shots.SETTLE_MS * 0.8


class TestPasting:
    def test_it_ignores_the_scaling_of_the_target(
        self, shots: ModuleType, qt_application: QApplication
    ) -> None:
        """拡大率の付いた画像へも、渡した画素の位置に貼れること

        ``QPainter`` は相手の QImage が持つ devicePixelRatio で座標を変換する
        1.0 に戻さずに貼ると、拡大率 125% の機械では GL の面が二重に拡大されて
        画面の外へ出て、プレビューの場所には何も写らない写真ができる
        """
        del qt_application
        image = QImage(200, 200, QImage.Format.Format_RGB32)
        image.fill(QColor("black"))
        image.setDevicePixelRatio(2.0)
        source = QImage(10, 10, QImage.Format.Format_RGB32)
        source.fill(QColor("red"))

        shots.paste(image, QRect(100, 100, 40, 40), source)

        # 渡したのは画素そのものの座標 2 倍に変換されると (200, 200) は画像の外で、
        # ここは黒いままになる
        assert image.pixelColor(120, 120) == QColor("red")


class TestSampleScript:
    def test_the_sample_script_becomes_a_settings_panel(
        self, shots: ModuleType, tmp_path: Path
    ) -> None:
        """制御行が設定欄になる これが AviUtl の写真で見せている所そのもの

        壊れると、README の AviUtl の写真に実際とは違う設定欄が写る
        """
        from sashimono.effects.definition import registry

        kind = shots.install_sample_script(tmp_path / "scripts")
        definition = registry.require(kind)
        assert [spec.label for spec in definition.parameters] == ["振れ幅", "速さ", "横に揺れる"]


class TestCompatibilityShot:
    def test_the_report_shot_hides_where_the_sample_lives(
        self,
        shots: ModuleType,
        qt_application: QApplication,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """見本のスクリプトは一時フォルダに置く その場所を出したまま撮ると、
        撮った人のユーザー名を含むフォルダが Wiki の写真に写る
        """
        del qt_application
        # ホームを試験の中で決める 見本はその下に置くので、撮った人の名前の代わりに
        # この名前が写っていないかで見られる
        home = tmp_path / "ホーム名は写らない"
        home.mkdir()
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("HOME", str(home))
        context = shots.Context(
            media=None, alias_root=None, ymm4_root=None, script_root=home / "scripts"
        )
        with _restored_scripts():
            dialog = shots.compatibility_dialog(context)
            try:
                shown = "\n".join(label.text() for label in dialog.findChildren(QLabel))
            finally:
                dialog.close()
        assert str(tmp_path) not in shown
        assert str(Path.home()) not in shown
        # 場所の文字列が無いだけでは、一部だけが写って名前が残っても通る 名前そのものも見る
        # ホームの名前は試験の中で決めた物（一時フォルダの名前）を使う 走らせる機械の
        # ホームの名前は、空（ホームが `/`）や `1` のように短いことがあり、写っていなくても
        # 画面の決まった文（「スクリプト 1 本」など）と一致してしまう
        assert home.name.casefold() not in shown.casefold()
        assert "スクリプト 1 本" in shown

    def test_scripts_in_the_default_folder_are_not_kept(
        self, qt_application: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """後始末の基準に、既定の探索先のスクリプトを混ぜない

        一覧がまだ無い所で基準を取ると、既定の探索先を走査して登録した定義が基準に
        入り、試験のあとも登録簿に残る
        """
        del qt_application, tmp_path
        # 探索先は設定の置き場（conftest が一時フォルダへ向けている）だけにする
        monkeypatch.delenv("PROGRAMDATA", raising=False)
        monkeypatch.setattr(catalog_module, "_catalog", None)
        scripts = userdirs.config_root() / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "既定の置き場の見本.anm2").write_text("--track0:量,0,100,50\n", "utf-8")
        # 手助けに入るだけで何もしない 見るのは、手助け自身が基準を取るときに既定の
        # 探索先を走査しないか（前の手助けは走査して、見本を基準に入れて残していた）
        with _restored_scripts():
            pass
        assert not [d.kind for d in registry.all() if "既定の置き場の見本" in d.kind]
        # 置いた見本が本当に走査される物か 走査されない物なら、上の確かめは何も言っていない
        try:
            script_catalog()
            assert [d.kind for d in registry.all() if "既定の置き場の見本" in d.kind]
        finally:
            for definition in registry.all():
                if "既定の置き場の見本" in definition.kind:
                    registry.unregister(definition.kind)
            monkeypatch.setattr(catalog_module, "_catalog", None)

    def test_the_report_shot_leaves_no_sample_behind(
        self, shots: ModuleType, qt_application: QApplication, tmp_path: Path
    ) -> None:
        """見本のスクリプトの定義を登録簿に残さない

        残すと、同じモジュールで後に走る試験が走る順番しだいで見本を見てしまう
        （``forget_scripts`` が片付けるのはモジュールの終わり）
        """
        del qt_application
        context = shots.Context(
            media=None, alias_root=None, ymm4_root=None, script_root=tmp_path / "scripts"
        )
        kinds = {definition.kind for definition in registry.all()}
        with _restored_scripts():
            shots.compatibility_dialog(context).close()
        assert {definition.kind for definition in registry.all()} == kinds


@contextmanager
def _restored_scripts() -> Iterator[None]:
    """写真の道具が差し替える物を、試験の前の形へ戻す

    道具はアプリ全体の一覧を見本の物と差し替え、見本のスクリプトを登録簿に足す
    一覧を戻すだけでは足した定義が残るので、増えた種類も外す
    モジュールの終わりにまとめて外す ``forget_scripts`` を待つと、同じモジュールの
    後の試験が見本を見る
    """
    # 基準を取る前に一覧を空の物にしておく 一覧がまだ無いまま ``script_catalog()`` を
    # 呼ぶと既定の探索先を走査して定義を登録し、それが基準に入って片付けから漏れる
    # 前の一覧は ``_catalog`` から直に取る 関数で取ると、同じ走査が起きる
    previous = catalog_module._catalog
    set_script_catalog(ScriptCatalog(roots=()))
    kinds = {definition.kind for definition in registry.all()}
    try:
        yield
    finally:
        for definition in registry.all():
            if definition.kind not in kinds:
                registry.unregister(definition.kind)
        # 前の一覧の定義は基準を取る前から登録されているので、差し戻すだけでよい
        catalog_module._catalog = previous


@pytest.mark.usefixtures("gpu")
class TestTakingTheEditorShot:
    def test_the_editor_shot_shows_the_preview(
        self, shots: ModuleType, qt_application: QApplication, tmp_path: Path
    ) -> None:
        """撮れた絵の**プレビューの場所**が真っ黒でないこと

        ``QWidget.grab()`` は環境によって GL の中身を拾わない 拾えていないと、
        気付かないまま真っ黒なプレビューの写真が README に載る
        窓全体の明るさで見ると、メニューやタイムラインが明るいだけで通ってしまう
        ので、プレビューの占める範囲だけを切って見る
        """
        del qt_application
        if not libx264_available():
            pytest.skip("libx264 の入った ffmpeg が無いので見本の素材を作れない")

        context = shots.Context(
            media=shots.make_sample_media(tmp_path / "media"),
            alias_root=None,
            ymm4_root=None,
            script_root=tmp_path / "scripts",
        )
        with shots.editor(shots.sample_project()) as window:
            shots.build_sample_timeline(window, context)
            image = shots.take_editor_shot(window)
            rect = shots.preview_rect(window)

        assert (image.width(), image.height()) == shots.WINDOW_SIZE
        assert rect is not None
        assert shots.brightest(image, rect) > shots.BLACK_LEVEL
