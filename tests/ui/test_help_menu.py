"""ヘルプメニューと、互換性レポートを報告に写す口

どちらも困った人が受け口へ辿り着くための道 壊れても編集は続けられるので
気付きにくく、気付くのは報告が届かなくなってから
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

from sashimono import __version__
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.core import userdirs
from sashimono.links import MANUAL_URL, REPORT_URL
from sashimono.ui.compat_dialog import (
    HOME_PLACEHOLDER,
    CompatibilityDialog,
    mask_user_folders,
    report_text,
    user_folders,
)
from sashimono.ui.main_window import MainWindow, about_text


@pytest.fixture
def window(qt_application: QApplication) -> Iterator[MainWindow]:
    del qt_application
    created = MainWindow(confirm_unsaved=False)
    yield created
    created.close()


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """ブラウザを本当に開かずに、開こうとした先を覚える"""
    urls: list[str] = []

    def fake_open(url: QUrl) -> bool:
        urls.append(url.toString())
        return True

    monkeypatch.setattr(QDesktopServices, "openUrl", fake_open)
    return urls


class TestHelpMenu:
    def test_the_manual_opens_the_one_manual_url(
        self, window: MainWindow, opened: list[str]
    ) -> None:
        # 壊れると、F1 を押しても何も起きないか、定数と違う古い先へ飛ぶ
        action, default = window._actions["ヘルプ/使い方"]
        action.trigger()
        assert opened == [QUrl(MANUAL_URL).toString()]
        assert default == "F1"

    def test_the_report_item_opens_the_template_chooser(
        self, window: MainWindow, opened: list[str]
    ) -> None:
        # 壊れると、困った人がソフトの中から受け口へ辿り着けない
        window._actions["ヘルプ/不具合・要望を送る"][0].trigger()
        assert opened == [QUrl(REPORT_URL).toString()]

    def test_a_missing_browser_leaves_the_url_on_screen(
        self, window: MainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 壊れると、ブラウザの無い機械では押しても黙って何も起きない
        monkeypatch.setattr(QDesktopServices, "openUrl", lambda _url: False)
        window.open_report_page()
        assert REPORT_URL in window.statusBar().currentMessage()

    def test_the_about_item_exists(self, window: MainWindow) -> None:
        # 報告の雛形が〔ヘルプ〕→〔バージョン情報…〕を案内している 無いと案内が嘘になる
        assert "ヘルプ/バージョン情報…" in window._actions

    def test_the_about_text_has_what_a_report_asks_for(self) -> None:
        # 壊れると、報告の「版」の欄を埋めるのに zip の名前を探させることになる
        text = about_text()
        assert text.splitlines()[0] == f"Sashimono Edit {__version__}"
        assert str(userdirs.config_root()) in text
        assert str(userdirs.state_root()) in text


class TestCompatibilityCopy:
    def test_the_copied_text_hides_the_user_name(self, tmp_path: Path) -> None:
        # 壊れると、公開の Issue に貼った記録からユーザー名が読める
        home = tmp_path / "kagemori"
        report = CompatibilityReport()
        report.note_failure("ゆらゆら.anm2", f"開けない: {home / 'scripts' / 'ゆらゆら.anm2'}")
        text = report_text(report, 3, user_folders(home))
        assert str(home) not in text
        assert HOME_PLACEHOLDER in text

    @pytest.mark.parametrize(
        "written",
        [
            r"C:\Users\Kagemori\scripts\a.anm2",
            r"c:\users\kagemori\scripts\a.anm2",
            "C:/Users/kagemori/scripts/a.anm2",
            r"C:\\Users\\KAGEMORI/scripts\a.anm2",
        ],
    )
    def test_other_spellings_of_the_home_are_hidden_too(self, written: str) -> None:
        # Windows の場所は大文字小文字も区切りも揺れる 完全一致で探すと、OS の文言に
        # 入った別の書き方が素通りし、公開の Issue に利用者名が残る
        text = mask_user_folders(f"開けない: {written}", [(r"C:\Users\kagemori", "%USERPROFILE%")])
        assert "kagemori" not in text.lower()
        assert text.startswith("開けない: %USERPROFILE%")
        assert text.endswith("a.anm2")

    def test_a_longer_name_that_shares_the_start_is_left_whole(self) -> None:
        # 名前の頭だけを伏せると ``%USERPROFILE%mori`` のように、残りから名前が読める
        # 別の人の場所として丸ごと残すほうがまし（そもそも本人の名前ではない）
        text = mask_user_folders(r"C:\Users\kagemori\a", [(r"C:\Users\kage", "%USERPROFILE%")])
        assert text == r"C:\Users\kagemori\a"

    def test_the_app_folders_outside_the_home_are_hidden(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 移動プロファイルなどで設定の置き場がホームの外にあると、ホームだけを伏せても
        # その場所の利用者名が残る
        roaming = r"\\server\profiles\kagemori\AppData\Roaming"
        monkeypatch.setenv("APPDATA", roaming)
        text = mask_user_folders(
            rf"開けない: {roaming.lower()}\Sashimono\scripts\a.anm2", user_folders(tmp_path)
        )
        assert text == r"開けない: %APPDATA%\Sashimono\scripts\a.anm2"

    def test_the_settings_folder_is_named_rather_than_the_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ホームから先に伏せると %USERPROFILE%\AppData\Roaming になり、設定の置き場だと読めない
        monkeypatch.setenv("APPDATA", r"C:\Users\kagemori\AppData\Roaming")
        text = mask_user_folders(
            r"C:\Users\kagemori\AppData\Roaming\Sashimono", user_folders(Path(r"C:\Users\kagemori"))
        )
        assert text == r"%APPDATA%\Sashimono"

    def test_the_copied_text_carries_the_version_and_every_line(self) -> None:
        # 壊れると、貼られた記録がどの版のものか分からず、直ったかどうかを判断できない
        report = CompatibilityReport()
        report.note_missing("obj.getpixeldata")
        report.note_missing("obj.getpixeldata")
        report.note_control("--dialog 謎の欄")
        lines = report_text(report, 11).splitlines()
        assert __version__ in lines[0]
        assert "読み込み済みのスクリプト 11 本" in lines
        assert "obj.getpixeldata — 2 回" in lines
        assert "--dialog 謎の欄 — 1 回" in lines

    def test_the_button_puts_the_text_on_the_clipboard(self, qt_application: QApplication) -> None:
        # 壊れると、一覧を 1 行ずつしか選べず、報告に貼れない
        report = CompatibilityReport()
        report.note_missing("obj.putpixeldata")
        dialog = CompatibilityDialog(report)
        try:
            dialog.copy_to_clipboard()
            assert "obj.putpixeldata — 1 回" in qt_application.clipboard().text()
        finally:
            dialog.close()
