"""案内する先と、Issue の雛形が、中身と食い違っていないか

どちらも文字で書いた案内で、名前や置き場を変えたときに一緒に直さないと
嘘の案内が残る 残っても編集は続けられるので、報告が届かなくなるまで気付かない
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from sashimono.core import userdirs
from sashimono.core.userdirs import APP_FOLDER
from sashimono.links import MANUAL_URL, REPORT_URL, REPOSITORY_URL

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"


def _headings(markdown: str) -> set[str]:
    return {match.group(1).strip() for match in re.finditer(r"^#+\s+(.+)$", markdown, re.M)}


class TestLinks:
    def test_the_manual_points_at_a_heading_that_exists(self) -> None:
        # Wiki を公開するまでは README の節へ飛ばす 節の名前を変えると、GitHub は
        # README の頭を出すだけで、何も言わずに案内先を失う
        parts = urlsplit(MANUAL_URL)
        if parts.fragment:
            readme = (ROOT / "README.md").read_text(encoding="utf-8")
            assert parts.fragment in _headings(readme)

    def test_every_link_stays_in_the_repository(self) -> None:
        # 壊れると、リポジトリを移したときに 1 つだけ古い先へ飛ばし続ける
        for url in (MANUAL_URL, REPORT_URL):
            assert url.startswith(REPOSITORY_URL)


class TestIssueTemplates:
    def test_the_contact_links_point_into_the_repository(self) -> None:
        # 壊れると、雛形を選ぶ画面の「質問はこちら」が別のリポジトリへ飛ぶ
        config = (TEMPLATES / "config.yml").read_text(encoding="utf-8")
        urls = re.findall(r"^\s*url:\s*(\S+)", config, re.M)
        assert urls
        assert all(url.startswith(REPOSITORY_URL) for url in urls)

    def test_the_bug_report_names_the_real_folders(self) -> None:
        # 置き場の名前は userdirs が決める 変えたのに雛形が古いままだと、
        # 報告する人は無いフォルダを探すことになる
        text = (TEMPLATES / "bug_report.yml").read_text(encoding="utf-8")
        assert f"%APPDATA%\\{APP_FOLDER}" in text
        assert f"%LOCALAPPDATA%\\{APP_FOLDER}\\recovery" in text

    def test_the_bug_report_names_the_folders_outside_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 動かし方に「ソースから」、OS に「その他」を選べる Windows の場所だけを書くと、
        # それ以外の機械の人は無いフォルダを探すことになる 場所は userdirs から求める
        for name in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            monkeypatch.delenv(name, raising=False)
        text = (TEMPLATES / "bug_report.yml").read_text(encoding="utf-8")
        for root in (userdirs.config_root(), userdirs.state_root()):
            assert f"~/{root.relative_to(Path.home()).as_posix()}" in text
        assert "XDG_CONFIG_HOME" in text
        assert "XDG_STATE_HOME" in text

    def test_the_bug_report_uses_the_labels_of_the_about_box(self) -> None:
        # 雛形は置き場を〔バージョン情報…〕の見出しで指す 見出しを変えたら雛形も直さないと、
        # 案内された見出しが画面に無い
        from sashimono.ui.main_window import about_text

        text = (TEMPLATES / "bug_report.yml").read_text(encoding="utf-8")
        for label in ("設定・スクリプト・テンプレート", "退避・バックアップ"):
            assert f"{label}:" in about_text()
            assert f"「{label}」" in text

    def test_the_menu_paths_in_the_templates_exist(self, menu_paths: set[str]) -> None:
        # 雛形は〔メニュー〕→〔項目〕の形で操作を案内する 項目の名前を変えたり、
        # 別のメニューへ移したりしたら雛形も直さないと、案内どおりに探しても見つからない
        guided = {
            f"{template.name}: {path}"
            for template in TEMPLATES.glob("*.yml")
            for path in _guided_paths(template.read_text(encoding="utf-8"))
            if path not in menu_paths
        }
        assert guided == set()

    def test_an_item_under_another_menu_is_caught(self, menu_paths: set[str]) -> None:
        # メニューと項目がそれぞれ在るだけで通すと、〔互換〕→〔設定…〕のような
        # 取り違えた道を案内し続けても気付けない
        assert "表示/設定…" in menu_paths
        assert _guided_paths("〔互換〕→〔設定…〕") == ["互換/設定…"]
        assert "互換/設定…" not in menu_paths


def _guided_paths(text: str) -> list[str]:
    """雛形が案内する〔メニュー〕→〔項目〕の道 画面の操作の名前と同じ「メニュー/項目」の形"""
    return [f"{menu}/{item}" for menu, item in re.findall(r"〔([^〕]+)〕→〔([^〕]+)〕", text)]


@pytest.fixture
def menu_paths() -> Iterator[set[str]]:
    """編集画面に実際にある「メニュー/項目」

    ソースの文字を探すのではなく、窓を組み立てて確かめる 文字で探すと、項目が
    どのメニューの下に足されたかまでは分からない ショートカットの設定が使う名前と
    同じ物なので、メニューの組み立てを変えても追える
    """
    from sashimono.ui.main_window import MainWindow

    window = MainWindow(confirm_unsaved=False)
    try:
        yield set(window._actions)
    finally:
        window.close()
