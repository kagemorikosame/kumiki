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
from sashimono.ui.compat_dialog import HOME_PLACEHOLDER, CompatibilityDialog, report_text
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
        text = report_text(report, 3, home=home)
        assert str(home) not in text
        assert HOME_PLACEHOLDER in text

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
