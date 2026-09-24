"""プロジェクト設定の組み合わせを名前を付けて保存・呼び出し・削除する（Issue #27）

保存した物は決まった解像度と同じ一覧に「保存: 」付きで並び、次に開いた画面でも選べる
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import QApplication

from sashimono.core.model import Blending, ProjectSettings
from sashimono.core.timebase import FrameRate
from sashimono.ui.project_presets import ProjectPreset, ProjectPresetStore
from sashimono.ui.project_settings_dialog import (
    FRAME_RATE_PRESETS,
    RESOLUTION_PRESETS,
    SAVED_PREFIX,
    ProjectSettingsDialog,
)
from sashimono.ui.workspace import config_root


def _texts(dialog: ProjectSettingsDialog) -> list[str]:
    combo = dialog._preset
    return [combo.itemText(i) for i in range(combo.count()) if combo.itemText(i)]


def _choose_rate(dialog: ProjectSettingsDialog, rate: FrameRate) -> None:
    assert dialog._rate is not None
    dialog._rate.setCurrentIndex([r for _, r in FRAME_RATE_PRESETS].index(rate))


def _new_dialog(store: ProjectPresetStore) -> ProjectSettingsDialog:
    return ProjectSettingsDialog(ProjectSettings(), new=True, presets=store)


class TestStore:
    def test_by_default_it_lives_with_the_other_settings(self) -> None:
        """置き場は本人の設定の所 プロジェクトの隣に置くと、別の作品で呼び出せない"""
        assert ProjectPresetStore().path.parent == config_root()

    def test_a_round_trip_keeps_exact_rates(self, tmp_path: Path) -> None:
        # 29.97 を小数で書くと、読み戻したときに 30000/1001 へ戻らず、選びと合わなくなる
        store = ProjectPresetStore(tmp_path / "p.json")
        preset = ProjectPreset("放送", 1920, 1080, FrameRate(30000, 1001), Blending.LINEAR)
        store.put(preset)
        assert ProjectPresetStore(tmp_path / "p.json").load() == [preset]

    def test_a_broken_entry_is_skipped_not_the_whole_list(self, tmp_path: Path) -> None:
        """1 つ壊れただけで全部を捨てない 作り直す手間が大きい"""
        path = tmp_path / "p.json"
        good = {"name": "縦", "width": 1080, "height": 1920, "frame_rate": [60, 1]}
        good["blending"] = "srgb"
        broken = [
            {**good, "name": "奇数", "width": 1081},
            {**good, "name": "型違い", "height": "1920"},
            {**good, "name": "真偽", "frame_rate": [True, 1]},
            {**good, "name": "知らない混ぜ方", "blending": "hdr"},
            {**good, "name": ""},
            "文字列",
        ]
        path.write_text(json.dumps({"version": 1, "presets": [*broken, good]}), encoding="utf-8")
        assert [p.name for p in ProjectPresetStore(path).load()] == ["縦"]

    def test_an_unreadable_file_gives_an_empty_list(self, tmp_path: Path) -> None:
        # 起動を止めると、テンプレートのために編集そのものができなくなる
        path = tmp_path / "p.json"
        path.write_text("{壊れた", encoding="utf-8")
        assert ProjectPresetStore(path).load() == []


class TestDialog:
    def test_a_saved_preset_is_listed_after_the_builtins_and_marked(
        self, qt_application: QApplication, tmp_path: Path
    ) -> None:
        """保存した物は決まった物の後ろに「保存: 」付きで並ぶ

        印が無いと、どれが消せる物（本人が作った物）なのか見分けられない
        """
        del qt_application
        store = ProjectPresetStore(tmp_path / "p.json")
        dialog = _new_dialog(store)
        dialog._width.setValue(1080)
        dialog._height.setValue(1920)
        _choose_rate(dialog, FrameRate(60))
        assert dialog.save_preset("ショート 60") is not None

        texts = _texts(dialog)
        builtin = [label for label, _, _ in RESOLUTION_PRESETS]
        assert texts[: len(builtin)] == builtin
        saved = [text for text in texts if text.startswith(SAVED_PREFIX)]
        assert len(saved) == 1
        assert "ショート 60" in saved[0]
        assert "60 fps" in saved[0]
        # 保存した直後は、その物を選んだ状態になっている 同じ縦横の決まった物に移ると、
        # 保存できたのかが分からない
        assert dialog._preset.currentText() == saved[0]

    def test_a_saved_preset_is_offered_next_time_and_applies_all_values(
        self, qt_application: QApplication, tmp_path: Path
    ) -> None:
        """次に開いた画面で選ぶと、縦横・フレームレート・重ね合わせがすべて戻る"""
        del qt_application
        store = ProjectPresetStore(tmp_path / "p.json")
        first = _new_dialog(store)
        first._width.setValue(2560)
        first._height.setValue(1440)
        _choose_rate(first, FrameRate(60000, 1001))
        first._blending.setCurrentIndex(first._blending.findData(Blending.LINEAR))
        first.save_preset("WQHD")

        second = _new_dialog(store)
        second._preset.setCurrentIndex(second._preset.findText(_saved_text(second, "WQHD")))
        settings = second.settings()
        assert (settings.width, settings.height) == (2560, 1440)
        assert settings.frame_rate == FrameRate(60000, 1001)
        assert settings.blending == Blending.LINEAR
        assert "WQHD" in second._preset.currentText()

    def test_an_existing_project_keeps_its_rate_and_says_so(
        self, qt_application: QApplication, tmp_path: Path
    ) -> None:
        """作ったあとの設定画面で選ぶと、フレームレートは変えずに理由を出す

        変えると全クリップの時刻がずれる 黙って飛ばすと、60fps の物を選んだのに
        30fps のままの理由が分からない
        """
        del qt_application
        store = ProjectPresetStore(tmp_path / "p.json")
        store.put(ProjectPreset("実況", 1280, 720, FrameRate(60), Blending.SRGB))
        dialog = ProjectSettingsDialog(ProjectSettings(), presets=store)
        dialog._preset.setCurrentIndex(dialog._preset.findText(_saved_text(dialog, "実況")))
        assert dialog.settings().frame_rate == FrameRate(30)
        assert dialog.resolution() == (1280, 720)
        assert "60 fps" in dialog._warning.text()
        assert "実況" in dialog._preset.currentText()

    def test_delete_removes_it_from_the_list_and_the_file(
        self, qt_application: QApplication, tmp_path: Path
    ) -> None:
        del qt_application
        store = ProjectPresetStore(tmp_path / "p.json")
        store.put(ProjectPreset("消す", 1280, 720, FrameRate(30), Blending.SRGB))
        dialog = _new_dialog(store)
        dialog._preset.setCurrentIndex(dialog._preset.findText(_saved_text(dialog, "消す")))
        assert dialog._delete_button.isEnabled()
        assert dialog.delete_preset("消す")
        assert not any(text.startswith(SAVED_PREFIX) for text in _texts(dialog))
        assert store.load() == []
        # 数字は残る 消しただけで今の入力まで変わると、作りかけの設定を失う
        assert dialog.resolution() == (1280, 720)

    def test_builtins_cannot_be_deleted(self, qt_application: QApplication, tmp_path: Path) -> None:
        # 押せると、押しても何も消えず、壊れているように見える
        del qt_application
        dialog = _new_dialog(ProjectPresetStore(tmp_path / "p.json"))
        assert "フル HD" in dialog._preset.currentText()
        assert not dialog._delete_button.isEnabled()

    def test_odd_sizes_are_not_saved(self, qt_application: QApplication, tmp_path: Path) -> None:
        """奇数の縦横は保存しない 呼び出すたびに書き出せない設定が入る"""
        del qt_application
        store = ProjectPresetStore(tmp_path / "p.json")
        dialog = _new_dialog(store)
        dialog._width.setValue(1919)
        assert not dialog._save_button.isEnabled()
        assert dialog.save_preset("奇数") is None
        assert store.load() == []

    def test_the_same_name_replaces_instead_of_doubling(
        self, qt_application: QApplication, tmp_path: Path
    ) -> None:
        # 同じ名前が 2 つ並ぶと、どちらを選んだのか、どちらが消えるのかが分からない
        del qt_application
        store = ProjectPresetStore(tmp_path / "p.json")
        dialog = _new_dialog(store)
        dialog.save_preset("いつもの")
        dialog._width.setValue(1280)
        dialog._height.setValue(720)
        dialog.save_preset("いつもの")
        assert [(p.name, p.width) for p in store.load()] == [("いつもの", 1280)]


def _saved_text(dialog: ProjectSettingsDialog, name: str) -> str:
    return next(text for text in _texts(dialog) if text.startswith(SAVED_PREFIX + name))
