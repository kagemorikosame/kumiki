"""案内する先と、Issue の雛形が、中身と食い違っていないか

どちらも文字で書いた案内で、名前や置き場を変えたときに一緒に直さないと
嘘の案内が残る 残っても編集は続けられるので、報告が届かなくなるまで気付かない
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

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

    def test_the_menu_paths_in_the_templates_exist(self) -> None:
        # 雛形は〔メニュー〕→〔項目〕の形で操作を案内する 項目の名前を変えたら
        # 雛形も直さないと、案内どおりに探しても見つからない
        window_source = (ROOT / "src" / "sashimono" / "ui" / "main_window.py").read_text(
            encoding="utf-8"
        )
        for template in TEMPLATES.glob("*.yml"):
            text = template.read_text(encoding="utf-8")
            for menu, item in re.findall(r"〔([^〕]+)〕→〔([^〕]+)〕", text):
                assert f'self._menu("{menu}")' in window_source, (template.name, menu)
                assert f'"{item}"' in window_source, (template.name, item)
