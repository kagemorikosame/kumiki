"""ヘルプメニューと、互換性レポートやテンプレートの注意書きを報告に写す口

どれも困った人が受け口へ辿り着くための道 壊れても編集は続けられるので
気付きにくく、気付くのは報告が届かなくなってから
"""

from __future__ import annotations

import ctypes
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

from sashimono import __version__
from sashimono.compat.aviutl import catalog as catalog_module
from sashimono.compat.aviutl.catalog import ScriptCatalog
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.catalog import TemplateCatalog, TemplateEntry
from sashimono.core import userdirs
from sashimono.links import MANUAL_URL, REPORT_URL
from sashimono.ui import report_masking
from sashimono.ui.compat_dialog import CompatibilityDialog, report_text
from sashimono.ui.main_window import MainWindow, about_text
from sashimono.ui.report_masking import HOME_PLACEHOLDER, mask_user_folders, user_folders
from sashimono.ui.template_dialog import TemplateDialog, notes_text
from tests.compat.test_ymm4 import template as ymm4_template
from tests.compat.test_ymm4 import text_item, write_ymmt


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


def _clear_folder_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """伏せる置き場を決める環境変数を全部消す

    ``user_folders`` は OS で分けずに全部を読む 走らせる機械で XDG などが立って
    いると、伏せる置き場の数や伏せた結果が機械ごとに変わり、比べる試験が落ちる
    一覧から消すのは、置き場を足したときに試験の側が取り残されないようにするため
    """
    for variable, _placeholder in report_masking._FOLDER_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


class TestCompatibilityCopy:
    def test_the_copied_text_hides_the_user_name(self, tmp_path: Path) -> None:
        # 壊れると、公開の Issue に貼った記録からユーザー名が読める
        home = tmp_path / "kagemori"
        report = CompatibilityReport()
        report.note_failure("ゆらゆら.anm2", f"開けない: {home / 'scripts' / 'ゆらゆら.anm2'}")
        text = report_text(report, 3, user_folders(home))
        # 場所の文字列が無いだけでは、一部だけが置き換わって名前が残っても通る
        # 名前そのものが消えたことを見る
        assert "kagemori" not in text.casefold()
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
        assert "kagemori" not in text.casefold()
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
        _clear_folder_variables(monkeypatch)
        monkeypatch.setenv("APPDATA", roaming)
        text = mask_user_folders(
            rf"開けない: {roaming.lower()}\Sashimono\scripts\a.anm2", user_folders(tmp_path)
        )
        assert text == r"開けない: %APPDATA%\Sashimono\scripts\a.anm2"

    def test_the_xdg_folders_outside_the_home_are_hidden(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Windows 以外では設定と退避の置き場が XDG の環境変数で決まる ホームの外
        # （ネットワークの置き場など）へ向けた機械では、ホームだけを伏せると名前が残る
        state = "/mnt/share/kagemori/state"
        _clear_folder_variables(monkeypatch)
        monkeypatch.setenv("XDG_STATE_HOME", state)
        text = mask_user_folders(
            f"開けない: {state}/Sashimono/recovery/a.sme", user_folders(tmp_path)
        )
        assert text == "開けない: $XDG_STATE_HOME/Sashimono/recovery/a.sme"

    def test_the_short_8dot3_name_of_the_home_is_hidden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 古い API や DLL は場所を 8.3 形式で返す 短い形にも名前の頭が残るので、
        # 長い形だけを伏せると公開の Issue に名前が出る 実機の 8.3 の有無に依らないよう、
        # 短い形を求める所を差し替える
        long_home = r"C:\Users\kagemori"
        shorts = {long_home: r"C:\Users\KAGEMO~1"}
        _clear_folder_variables(monkeypatch)
        monkeypatch.setattr(report_masking, "short_path", shorts.get)
        text = mask_user_folders(
            r"開けない: c:\users\kagemo~1\scripts\a.anm2", user_folders(Path(long_home))
        )
        assert "kagemo" not in text.casefold()
        assert text == r"開けない: %USERPROFILE%\scripts\a.anm2"

    def test_a_short_name_equal_to_the_long_one_is_not_added_twice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 8.3 を切ってある置き場では、短い形を聞いても同じ形が返る
        _clear_folder_variables(monkeypatch)
        monkeypatch.setattr(report_masking, "short_path", lambda folder: folder.upper())
        assert user_folders(Path(r"C:\Users\kagemori")) == [
            (r"C:\Users\kagemori", HOME_PLACEHOLDER)
        ]

    def test_without_the_windows_api_only_the_long_names_are_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Windows 以外では windll が無い 求められないからとコピーまで落ちると、報告できない
        monkeypatch.delattr(ctypes, "windll", raising=False)
        assert report_masking.short_path(r"C:\Users\kagemori") is None
        assert report_text(CompatibilityReport(), 0)

    def test_a_failing_call_falls_back_to_the_long_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 呼び出しが失敗しても、長い形だけで続ける 短い形が取れないときに長い形まで
        # 伏せ損ねると、公開の文に名前が残る なので報告の文まで通して確かめる
        def broken(*_arguments: object) -> int:
            raise OSError("呼べない")

        fake = SimpleNamespace(kernel32=SimpleNamespace(GetShortPathNameW=broken))
        monkeypatch.setattr(ctypes, "windll", fake, raising=False)
        _clear_folder_variables(monkeypatch)
        assert report_masking.short_path(r"C:\Users\kagemori") is None
        home = Path(r"C:\Users\kagemori")
        assert user_folders(home) == [(str(home), HOME_PLACEHOLDER)]
        report = CompatibilityReport()
        report.note_failure("a.anm2", r"開けない: c:/users/KAGEMORI\scripts\a.anm2")
        text = report_text(report, 1, user_folders(home))
        assert "kagemori" not in text.casefold()
        assert r"開けない: %USERPROFILE%\scripts\a.anm2" in text

    def test_every_script_folder_in_the_list_is_hidden(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 探索先を読点でつなぐと、場所の直後に読点が来て、最後以外の探索先が伏せられない
        # （読点は名前の途中に来うるので、伏せる側は場所の終わりと認めない）
        _clear_folder_variables(monkeypatch)
        roots = [r"D:\kagemori\aviutl\Script", r"E:\kagemori2\scripts", r"F:\山田\素材"]
        text = report_text(CompatibilityReport(), 0, user_folders(tmp_path), roots=roots)
        assert "探索先:\n  <探索先1>\n  <探索先2>\n  <探索先3>" in text
        for name in ("kagemori", "山田"):
            assert name not in text.casefold()

    @pytest.mark.parametrize("after", ["）", "」", "』", "】", "〕", "　"])
    def test_a_closing_bracket_ends_the_folder(self, after: str) -> None:
        # 文の中で場所を括弧で囲むと、場所の直後に閉じ括弧が来る そこで伏せ損ねると名前が残る
        text = mask_user_folders(
            rf"（C:\Users\kagemori{after}ほか", [(r"C:\Users\kagemori", "%USERPROFILE%")]
        )
        assert text == f"（%USERPROFILE%{after}ほか"

    @pytest.mark.parametrize("comma", ["、", "，", "．"])
    def test_a_name_that_goes_on_after_a_comma_is_left_whole(self, comma: str) -> None:
        # 読点などは名前の途中に来うる 終わりと認めると、別の人の場所の頭だけを伏せ、
        # 残りの「太郎」から名前が読める 本人の場所ではないので丸ごと残す
        written = rf"C:\Users\山田{comma}太郎\a"
        text = mask_user_folders(written, [(r"C:\Users\山田", "%USERPROFILE%")])
        assert text == written

    def test_a_middle_dot_is_part_of_the_name(self) -> None:
        # カタカナの名前では「・」の後ろに名前の続きが来る 頭だけを伏せると残りが読める
        text = mask_user_folders(
            r"C:\Users\ジョン・スミス\a", [(r"C:\Users\ジョン", "%USERPROFILE%")]
        )
        assert text == r"C:\Users\ジョン・スミス\a"

    def test_a_missing_folder_has_no_short_name(self, tmp_path: Path) -> None:
        # 無い場所では API が 0 を返す それを空の名前として足すと、何も伏せない行が混ざる
        assert report_masking.short_path(str(tmp_path / "無い")) is None

    def test_the_settings_folder_is_named_rather_than_the_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ホームから先に伏せると %USERPROFILE%\AppData\Roaming になり、設定の置き場だと読めない
        _clear_folder_variables(monkeypatch)
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

    def test_a_script_folder_outside_the_home_is_hidden(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 探索先は本人が決めた場所で、利用者名を含みうる 読めなかったスクリプトの
        # 記録には絶対パスが入るので、ホームの外の探索先はそれだけで名前が漏れる
        _clear_folder_variables(monkeypatch)
        outside = r"D:\kagemori\aviutl\Script"
        report = CompatibilityReport()
        report.note_failure("ゆらゆら.anm2", rf"開けない: [Errno 13] {outside}\ゆらゆら.anm2")
        text = report_text(report, 1, user_folders(tmp_path), roots=[outside])
        assert "kagemori" not in text.casefold()
        assert r"ゆらゆら.anm2: 開けない: [Errno 13] <探索先1>\ゆらゆら.anm2" in text
        assert "探索先:\n  <探索先1>" in text

    def test_a_script_folder_under_the_settings_keeps_its_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 設定の置き場の下の探索先は、印ではなく %APPDATA% の名前で伏せる
        # 印にすると、どの置き場の話なのかが報告から読めなくなる
        _clear_folder_variables(monkeypatch)
        monkeypatch.setenv("APPDATA", r"C:\Users\kagemori\AppData\Roaming")
        inside = r"C:\Users\kagemori\AppData\Roaming\Sashimono\scripts"
        text = report_text(CompatibilityReport(), 0, user_folders(tmp_path), roots=[inside])
        assert "探索先:\n  %APPDATA%\\Sashimono\\scripts" in text
        assert "<探索先" not in text

    def test_the_dialog_tells_which_marker_is_which(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 貼った文の印が何を指すかは、聞かれたときに本人が画面で答えられるようにする
        _clear_folder_variables(monkeypatch)
        outside = Path(r"D:\kagemori\aviutl\Script")
        # 前の一覧は ``_catalog`` から直に取る ``script_catalog()`` で取ると、一覧が
        # まだ無いときに既定の探索先（本物の AviUtl2 の Script など）を走査して登録する
        monkeypatch.setattr(catalog_module, "_catalog", ScriptCatalog(roots=(outside,)))
        dialog = CompatibilityDialog(CompatibilityReport())
        try:
            assert f"  <探索先1> {outside}" in dialog._scripts.text().splitlines()
            dialog.copy_to_clipboard()
            assert "kagemori" not in QApplication.clipboard().text().casefold()
        finally:
            dialog.close()

    def test_the_same_folder_written_differently_shows_its_marker_twice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r"""まとめる側と画面で引く側の見分け方が違うと、`D:\kagemori\Script` と
        `d:/kagemori/script` が並んだとき、後の方の行に印が付かず、本人が印の指す
        場所を画面で答えられない
        """
        _clear_folder_variables(monkeypatch)
        first = Path(r"D:\kagemori\Script")
        second = "d:/kagemori/script"
        monkeypatch.setattr(catalog_module, "_catalog", ScriptCatalog(roots=(first, Path(second))))
        dialog = CompatibilityDialog(CompatibilityReport())
        try:
            lines = [line.strip() for line in dialog._scripts.text().splitlines()]
            marked = [line for line in lines if line.startswith("<探索先1>")]
            assert len(marked) == 2
        finally:
            dialog.close()

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


def test_a_search_folder_listed_twice_gets_one_marker() -> None:
    """同じ探索先が 2 度並ぶと印が 2 つ付き、画面と貼る文で違う印が出て、
    報告者が印の指す場所を確かめられない
    """
    from sashimono.ui.report_masking import root_markers

    markers = root_markers([r"D:\山田\Script", r"d:/山田/script", r"E:\別\Script"], [])
    # 同じ場所の 2 つの書き方は同じ印 別の場所は次の番号
    assert [marker for _root, marker in markers] == ["<探索先1>", "<探索先1>", "<探索先2>"]


def test_every_spelling_of_the_same_folder_is_hidden() -> None:
    r"""同じ場所の別の書き方をまとめるときに片方を落とすと、そちらが伏せる相手から
    外れ、`D:\work\..\kagemori\Script` のような書き方で貼る文に名前が残る
    """
    from sashimono.ui.report_masking import mask_user_folders, root_markers

    plain = r"D:\kagemori\Script"
    winding = r"D:\work\..\kagemori\Script"
    markers = root_markers([plain, winding], [])
    text = mask_user_folders(f"開けない: {winding}\\a.anm2\n開けない: {plain}\\b.anm2", markers)
    assert "kagemori" not in text.casefold()
    assert {marker for _root, marker in markers} == {"<探索先1>"}


def test_a_rooted_and_a_relative_folder_are_not_merged() -> None:
    r"""頭の区切りを捨てて比べると、`\山田\Script` と `山田\Script` を同じ場所とみなし、
    片方に印が付かないまま貼る文に場所が残る
    """
    from sashimono.ui.report_masking import root_markers

    markers = root_markers([r"\山田\Script", r"山田\Script", r"D:\山田\Script"], [])
    assert len(markers) == 3


#: 見本のエイリアス 中身（見本の文字・フォント）は貼る文に入ってはいけない印を兼ねる
#: 未対応のフィルタを 1 つ積んで、注意書きが 1 行出るようにしてある
_ALIAS = "\n".join(
    [
        "[Object]",
        "frame=0,89",
        "[Object.0]",
        "effect.name=テキスト",
        "フォント=Dela Gothic One",
        "テキスト=見本の秘密の文字",
        "[Object.1]",
        "effect.name=謎のフィルタ",
        "",
    ]
)


def _shelf(root: Path) -> Path:
    """AviUtl のエイリアス 1 本と、YMM4 の 2 本入りのテンプレートを置いた棚"""
    (root / "字幕").mkdir(parents=True)
    (root / "字幕" / "強調.object").write_text(_ALIAS, "utf-8")
    unknown = {"$type": "Example.UnknownEffect, Example", "IsEnabled": True}
    write_ymmt(
        root / "束.ymmt",
        ymm4_template("見出し", text_item()),
        ymm4_template("動き/揺れ", text_item(VideoEffects=[unknown])),
    )
    return root


def _copied(dialog: TemplateDialog, name: str) -> str:
    """棚で ``name`` を選んで〔内容をコピー〕を押し、クリップボードの中身を返す"""
    tree = dialog._tree
    for index in range(tree.topLevelItemCount()):
        group = tree.topLevelItem(index)
        assert group is not None
        for row in range(group.childCount()):
            item = group.child(row)
            if item is not None and item.text(0) == name:
                tree.setCurrentItem(item)
                dialog._copy_button.click()
                return QApplication.clipboard().text()
    raise AssertionError(f"棚に無い: {name}")


class TestTemplateNotesCopy:
    def test_the_button_puts_the_notes_on_the_clipboard(self, tmp_path: Path) -> None:
        # 壊れると、注意書きを 1 行ずつ手で打ち写すことになり、写し漏れがそのまま届く
        dialog = TemplateDialog(TemplateCatalog(), roots=(_shelf(tmp_path / "棚"),))
        try:
            lines = _copied(dialog, "強調").splitlines()
        finally:
            dialog.close()
        assert lines[0] == f"Sashimono Edit {__version__} テンプレートの注意書き"
        assert "名前: 強調" in lines
        assert "種類: AviUtl のエイリアス" in lines
        assert "  フィルタ: 謎のフィルタ — 1 回" in lines

    def test_the_template_inside_a_ymm4_file_is_told_apart(self, tmp_path: Path) -> None:
        # YMM4 は 1 ファイルに何本も入っている 何本目かが無いと、受けた側が
        # 100 本を超える中から同じ物を探すことになる
        dialog = TemplateDialog(TemplateCatalog(), roots=(_shelf(tmp_path / "棚"),))
        try:
            lines = _copied(dialog, "動き/揺れ").splitlines()
        finally:
            dialog.close()
        assert "種類: YMM4 のアイテムテンプレート（ファイルの 2 本目）" in lines
        assert "  YMM4 の映像エフェクト: UnknownEffect — 1 回" in lines

    def test_the_distributed_template_itself_is_left_out(self, tmp_path: Path) -> None:
        # 配布物の中身は再配布の条件が作者ごとに違う 貼る文に混ざると、公開の Issue に
        # 本人の知らないうちに他人の作品を貼ることになる
        dialog = TemplateDialog(TemplateCatalog(), roots=(_shelf(tmp_path / "棚"),))
        try:
            texts = [_copied(dialog, name) for name in ("強調", "見出し", "動き/揺れ")]
        finally:
            dialog.close()
        for text in texts:
            for inside in (
                "見本の秘密の文字",
                "Dela Gothic One",
                "effect.name",
                "サンプルテキスト",
                "Noto Sans JP Black",
                "$type",
                "#FF2B9FE2",
            ):
                assert inside not in text

    def test_a_shelf_outside_the_home_is_hidden(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 棚の置き場は本人が決めた場所で、ホームの外なら利用者名を含みうる ファイルの
        # 場所は絶対パスで入るので、そこを伏せないと公開の Issue に名前が出る
        _clear_folder_variables(monkeypatch)
        # ホームを棚と関係ない場所へ向ける 一時フォルダは本物のホームの下にあるので、
        # 向けないとホームとして伏せられ、探索先を伏せる側を確かめられない
        home = tmp_path / "someone"
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("HOME", str(home))
        root = _shelf(tmp_path / "kagemori" / "棚")
        dialog = TemplateDialog(TemplateCatalog(), roots=(root,))
        try:
            text = _copied(dialog, "強調")
            tip = dialog._copy_button.toolTip()
        finally:
            dialog.close()
        # 場所の文字列が無いだけでは、一部だけが置き換わって名前が残っても通る
        # 名前そのものが消えたことを見る
        assert "kagemori" not in text.casefold()
        assert str(tmp_path).casefold() not in text.casefold()
        assert "ファイル: <探索先1>\字幕\強調.object" in text.replace("/", "\\")
        assert "探索先:\n  <探索先1>" in text
        # どの印がどの場所かは、聞かれたときに本人が画面で答えられるようにする
        assert f"  <探索先1> {root}" in tip.splitlines()

    def test_a_shelf_under_the_settings_keeps_its_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # YMM4 の置き場は %LOCALAPPDATA% の下 印にすると、YMM4 の棚を見に行って
        # いるかが報告から読めなくなる
        _clear_folder_variables(monkeypatch)
        local = r"\server\profiles\kagemori\AppData\Local"
        monkeypatch.setenv("LOCALAPPDATA", local)
        root = rf"{local}\YukkuriMovieMaker\ItemTemplate"
        entry = TemplateEntry(
            name="見出し", path=Path(rf"{root}\束.ymmt"), folder="束", source="ymm4"
        )
        text = notes_text(
            entry, "1 オブジェクト（text）", [], [root], user_folders(Path(r"C:\Users\x"))
        )
        assert "kagemori" not in text.casefold()
        assert r"ファイル: %LOCALAPPDATA%\YukkuriMovieMaker\ItemTemplate\束.ymmt" in text
        assert "<探索先" not in text

    def test_a_template_that_fails_to_load_can_still_be_copied(self, tmp_path: Path) -> None:
        # 読めない理由こそ報告に要る 読めなかったときに写せないと、いちばん困った
        # 人が手で打ち写すことになる
        entry = TemplateEntry(name="壊れ", path=tmp_path / "壊れ.object")
        text = notes_text(entry, "読み込めません: 形が違う", [], [tmp_path])
        assert "読んだ結果: 読み込めません: 形が違う" in text.splitlines()
        assert "注意書き: （なし）" in text.splitlines()

    def test_a_broken_ymm4_file_can_be_chosen_and_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 壊れた .ymmt が棚から消えると、読めない理由を画面でも貼る文でも見られず、
        # いちばん報告してほしい物が報告できない
        _clear_folder_variables(monkeypatch)
        home = tmp_path / "someone"
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("HOME", str(home))
        root = tmp_path / "kagemori" / "棚"
        root.mkdir(parents=True)
        (root / "壊れ.ymmt").write_text("{これは JSON ではない", "utf-8")
        dialog = TemplateDialog(TemplateCatalog(), roots=(root,))
        try:
            text = _copied(dialog, "壊れ（読めません）")
            detail = dialog._detail.text()
            placeable = dialog._place_button.isEnabled()
            restylable = dialog._restyle_button.isEnabled()
        finally:
            dialog.close()
        assert detail.startswith("読み込めません: 壊れ.ymmt: JSON として読めない")
        assert "読んだ結果: 読み込めません: 壊れ.ymmt: JSON として読めない" in text
        assert "ファイル: <探索先1>\\壊れ.ymmt" in text.replace("/", "\\")
        assert "kagemori" not in text.casefold()
        # 読めないファイルは本数が分からない 「1 本目」と書くと、残りは読めたと読まれる
        assert "種類: YMM4 のアイテムテンプレート（ファイルを読めず、本数は不明）" in text
        assert "本目" not in text
        # 読めない物は置くことも着せることもできない
        assert not placeable
        assert not restylable

    def test_nothing_is_copied_without_a_selection(self, tmp_path: Path) -> None:
        # 選んでいないのに押せると、空の文や前に選んだ物の文が写り、別の物の報告になる
        dialog = TemplateDialog(TemplateCatalog(), roots=(tmp_path,))
        try:
            assert not dialog._copy_button.isEnabled()
        finally:
            dialog.close()
