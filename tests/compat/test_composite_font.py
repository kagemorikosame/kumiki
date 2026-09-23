"""合成フォント（AviUtl2 の汎用プラグイン ``comfont.aux2``）まわり

配布エイリアス「合成フォントテキスト」は 3 つが揃って初めて絵になる

1. ``obj.getfont()`` が大きさと字間を返す
2. ``obj.module("compositefont")`` が汎用プラグインの登録した表を返す
3. テキスト欄の中の ``obj.mes`` が本文を書き出す

どれが欠けてもオブジェクトは真っ黒のまま出る 実物のプラグインは
リポジトリに入れられないので、**実物を使う試験は無ければ飛ばし**、
仕組みの方は偽のプラグインと偽の登録で確かめる
"""

from __future__ import annotations

import gc
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sashimono.compat.aviutl import native, plugin
from sashimono.compat.aviutl.objapi import ObjApi, ObjectState
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.aviutl.runtime import LuaScriptRuntime, blank_image
from sashimono.core.model import AnimatedValue
from sashimono.engine.render.scripts import text_font
from tests import conftest
from tests.conftest import real_comfont_missing, touches_real_aviutl2


@pytest.fixture(autouse=True)
def allowed() -> Iterator[None]:
    """試験のたびに設定を戻す 切ったままにすると、後の試験が読めなくなる"""
    native.set_enabled(True)
    plugin.forget()
    yield
    native.set_enabled(True)
    plugin.forget()


def _state() -> ObjectState:
    return ObjectState(image=blank_image(4, 4))


class TestGetFont:
    """``obj.getfont`` AviUtl2 の lua.txt が定める並びで返す"""

    def test_returns_what_setfont_was_given(self) -> None:
        """往復で値が変わると、書体を取って組み直すスクリプトが別の絵を出す"""
        api = ObjApi(_state())
        api.lua_setfont("Yu Mincho", 72, 3, 0x112233, 0x445566, True, False, 4, 8)
        assert api.lua_getfont() == ("Yu Mincho", 72.0, 3, 0x112233, 0x445566, True, False, 4, 8)

    def test_name_is_empty_when_no_font_was_chosen(self) -> None:
        """既定の書体名を返すと、合成フォントが AviUtl2 と違う組み方をする"""
        name, size, *_ = ObjApi(_state()).lua_getfont()
        assert name == ""
        assert size == 48.0

    def test_short_calls_are_filled_to_nine_values(self) -> None:
        """短い組を返すと、受け側の ``local a,…,h = obj.getfont()`` に nil が並ぶ"""
        api = ObjApi(_state())
        api.lua_setfont("Meiryo", 30)
        values = api.lua_getfont()
        assert len(values) == 9
        assert values[:2] == ("Meiryo", 30.0)
        assert values[7:] == (0.0, 0.0)

    def test_lua_can_call_it(self) -> None:
        """Lua から呼べないと、配布エイリアスの 1 行目で落ちて真っ黒になる"""
        runtime = LuaScriptRuntime()
        state = _state()
        state.font = text_font(
            {"size": AnimatedValue(64.0), "letter_spacing": AnimatedValue(3.0)}, 0
        )
        text = runtime.expand_text(
            "<?local f,s,_,_,_,_,_,sp = obj.getfont() obj.mes(s..'/'..sp..'/'..f..'.')?>", state
        )
        assert text == "64/3/."


class TestTextFont:
    """テキストオブジェクトの設定欄を ``obj.getfont`` の並びへ"""

    def test_size_and_spacing_come_from_the_object(self) -> None:
        """設定欄を読まないと、大きさも字間もいじれない字幕になる"""
        font = text_font(
            {
                "size": AnimatedValue(64.0),
                "letter_spacing": AnimatedValue(2.5),
                "line_spacing": AnimatedValue(6.0),
                "color": (1.0, 0.0, 0.5, 1.0),
                "bold": 1,
            },
            0,
        )
        name, size, style, color, _, bold, italic, letter, line = font["given"]
        assert (name, size, style) == ("", 64.0, 0)
        assert color == 0xFF0080
        assert (bold, italic) == (True, False)
        assert (letter, line) == (2.5, 6.0)

    def test_font_name_stays_empty_when_unset(self) -> None:
        """空でない名前を作ると、合成フォントが書体を引きに行って別の組みになる"""
        assert text_font({}, 0)["given"][0] == ""


class TestEmbeddedMes:
    """テキスト欄の中の ``obj.mes``"""

    def test_writes_the_body_instead_of_drawing(self) -> None:
        """絵にしてしまうと 1x1 の作業用の絵へ描いて捨て、本文が空＝真っ黒になる"""
        runtime = LuaScriptRuntime()
        assert runtime.expand_text("<?obj.mes('あいう')?>", _state()) == "あいう"

    def test_values_are_written_the_lua_way(self) -> None:
        """Python の ``str`` で文字にすると、``true`` が ``True``、``nil`` が ``None``
        と出て、同じテキスト欄の ``mes`` と書き方で結果が変わる
        """
        runtime = LuaScriptRuntime()
        source = "<?obj.mes(true) mes('/') obj.mes(nil) mes('/') obj.mes(1.5)?>"
        assert runtime.expand_text(source, _state()) == "true/nil/1.5"

    def test_scripts_outside_the_text_box_still_draw(self) -> None:
        """書き出しへ回し続けると、``obj.mes`` で字を描くスクリプトが何も描かなくなる"""
        drawn: list[str] = []

        def record(kind: str, params: dict[str, Any], width: int, height: int) -> Any:
            del kind
            drawn.append(str(params["text"]))
            return blank_image(width, height)

        api = ObjApi(_state(), render_source=record)
        api.lua_mes("かきく")
        assert drawn == ["かきく"]


class TestPluginModules:
    """汎用プラグイン（``.aux2``）の読み込み"""

    def test_empty_folder_gives_no_modules(self, tmp_path: Path) -> None:
        """置き場が空で落ちると、AviUtl2 の入っていない機械で互換層ごと使えなくなる"""
        assert plugin.script_modules(roots=(tmp_path,)) == {}

    def test_missing_folder_gives_no_modules(self, tmp_path: Path) -> None:
        """無いフォルダで落ちると、AviUtl2 を入れていない人が起動できない"""
        assert plugin.script_modules(roots=(tmp_path / "無い",)) == {}

    def test_broken_file_is_skipped(self, tmp_path: Path) -> None:
        """1 つの壊れたプラグインで残りを諦めると、無関係な物のせいで合成フォントが消える"""
        (tmp_path / "こわれ.aux2").write_bytes("MZ これは DLL ではない".encode())
        assert plugin.script_modules(roots=(tmp_path,)) == {}

    def test_broken_file_is_recorded(self, tmp_path: Path) -> None:
        """黙って飛ばすと「なぜか合成フォントが効かない」で終わり、原因を追えない"""
        (tmp_path / "こわれ.aux2").write_bytes("MZ これは DLL ではない".encode())
        report = CompatibilityReport()
        plugin.script_modules(roots=(tmp_path,), report=report)
        assert any("こわれ.aux2" in line for line in report.lines())

    def test_plugins_one_folder_down_are_found(self, tmp_path: Path) -> None:
        """直下しか見ないと、フォルダごと配られたプラグインが 1 つも見つからない
        （手元の AviUtl2 では AIEdit も SrtImporter も 1 段下にある）
        """
        (tmp_path / "直下.aux2").write_bytes(b"MZ")
        nested = tmp_path / "なにか"
        nested.mkdir()
        (nested / "一段下.aux2").write_bytes(b"MZ")
        (nested / "さらに").mkdir()
        (nested / "さらに" / "二段下.aux2").write_bytes(b"MZ")
        found = [path.name for path in plugin.plugin_files(tmp_path)]
        # 直下が先 AviUtl2 の一覧と同じ並びにする
        assert found == ["直下.aux2", "一段下.aux2"]

    def test_the_same_plugin_is_not_listed_twice(self, tmp_path: Path) -> None:
        """2 度読むと、同じプラグインの初期化が二重になる"""
        (tmp_path / "ひとつ.aux2").write_bytes(b"MZ")
        assert len(plugin.plugin_files(tmp_path)) == 1

    def test_a_name_registered_twice_keeps_the_first_one(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """後から上書きすると、1 段下のプラグインが同じ名前を登録しただけで
        obj.module に別の実装が渡る 直下（先に並ぶ方）を残し、重なりは記録する
        """
        (tmp_path / "直下.aux2").write_bytes(b"MZ")
        nested = tmp_path / "なにか"
        nested.mkdir()
        (nested / "一段下.aux2").write_bytes(b"MZ")
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(plugin, "_load", lambda path: {"compositefont": path.name})
        report = CompatibilityReport()
        # 偽の読み込みは名前の代わりにファイル名を返す 型は NativeModule ではない
        found: dict[str, Any] = plugin._scan((tmp_path,), report)
        assert found == {"compositefont": "直下.aux2"}
        assert any("一段下.aux2" in line for line in report.lines())

    def test_a_refusal_reaches_every_report_once(self, tmp_path: Path, monkeypatch: Any) -> None:
        """読んだ結果は覚えて使い回すので、理由を最初の器にしか書かないと、
        2 つ目からのスクリプトには「見つかりません」だけが残って原因を追えない
        同じ器へ描くたびに書き直すと、回数が膨らんで多い順の並びが狂う
        """
        (tmp_path / "comfont.aux2").write_bytes(b"MZ")
        monkeypatch.setattr(plugin, "default_plugin_roots", lambda: (tmp_path,))
        monkeypatch.setattr("sys.platform", "win32")

        def refuse(path: Path) -> dict[str, Any]:
            raise native.NativeModuleError(f"{path.name} は本体の版 9999999 を求めている")

        monkeypatch.setattr(plugin, "_load", refuse)
        plugin.forget()
        try:
            first, second = CompatibilityReport(), CompatibilityReport()
            plugin.script_module("compositefont", report=first)
            plugin.script_module("compositefont", report=second)
            plugin.script_module("compositefont", report=second)
            for report in (first, second):
                lines = [line for line in report.missing if "comfont.aux2" in line]
                assert len(lines) == 1
                assert report.missing[lines[0]] == 1
            # 記録の画面で消したあとも、次に探したときに理由が戻ること
            # 戻らないと「見つかりません」だけが残って原因を追えない
            second.clear()
            plugin.script_module("compositefont", report=second)
            assert any("comfont.aux2" in line for line in second.missing)
        finally:
            plugin.forget()

    def test_a_reason_found_later_reaches_a_report_already_told(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """名前ごとに探すので、理由は後から増える 伝え済みの器へ増えた分を
        書かないと、2 つ目の名前の失敗が「見つかりません」だけになる 前の分まで
        書き直すと、1 つ目の理由の回数が膨らむ
        """
        (tmp_path / "comfont.aux2").write_bytes(b"MZ")
        (tmp_path / "second.aux2").write_bytes(b"MZ")
        providers = {**plugin.KNOWN_PROVIDERS, "second": ("second.aux2",)}
        monkeypatch.setattr(plugin, "KNOWN_PROVIDERS", providers)
        monkeypatch.setattr(plugin, "default_plugin_roots", lambda: (tmp_path,))
        monkeypatch.setattr("sys.platform", "win32")

        def refuse(path: Path) -> dict[str, Any]:
            raise native.NativeModuleError(f"{path.name} は読めない")

        monkeypatch.setattr(plugin, "_load", refuse)
        report = CompatibilityReport()
        plugin.script_module("compositefont", report=report)
        plugin.script_module("second", report=report)
        counts = {
            name: sum(count for line, count in report.missing.items() if name in line)
            for name in ("comfont.aux2", "second.aux2")
        }
        assert counts == {"comfont.aux2": 1, "second.aux2": 1}

    def test_a_forgotten_report_is_not_remembered(self, monkeypatch: Any) -> None:
        """器が消えても伝えた回数だけ残ると、一時の器を作るたびに覚える量が増える"""
        monkeypatch.setattr(plugin, "default_plugin_roots", lambda: ())
        plugin.forget()
        try:
            for _ in range(50):
                plugin.script_module("compositefont", report=CompatibilityReport())
            gc.collect()
            assert len(plugin._told_at) <= 1
        finally:
            plugin.forget()

    def test_a_module_that_could_not_be_used_is_recorded(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """プラグインは読めても中のモジュールが使えないとき、黙って捨てると
        利用者には「モジュールが見つかりません」しか見えず、原因を追えない
        """
        (tmp_path / "半分.aux2").write_bytes(b"MZ")
        monkeypatch.setattr("sys.platform", "win32")

        def half(path: Path) -> dict[str, Any]:
            plugin._problems[path.resolve()] = [
                f"汎用プラグイン {path.name} のモジュール x を使えない"
            ]
            return {}

        monkeypatch.setattr(plugin, "_load", half)
        try:
            report = CompatibilityReport()
            plugin._scan((tmp_path,), report)
            assert any("半分.aux2 のモジュール x" in line for line in report.missing)
        finally:
            plugin._problems.pop((tmp_path / "半分.aux2").resolve(), None)

    def test_an_unnamed_registration_is_counted(self) -> None:
        """名前の無い登録は引けないので使わないが、数えずに捨てると、
        それを使うスクリプトが止まったときに原因を追えない
        """
        host = plugin._Host()
        host._register_unnamed(0x1234)
        host._register_unnamed(None)
        assert host.unnamed == 1

    def test_switched_off_gives_no_modules(self, tmp_path: Path) -> None:
        """切っても読むなら、設定に意味が無い"""
        native.set_enabled(False)
        assert plugin.script_modules(roots=(tmp_path,)) == {}

    def test_obj_module_prefers_the_registered_one(self, monkeypatch: Any) -> None:
        """名前で登録されたモジュールは、ファイルとして存在しない
        先に見ないと「見つかりません」で終わり、エイリアスが真っ黒になる
        """

        class _Fake:
            path = Path("偽.aux2")
            names = ("api_version",)

            def call(self, name: str, args: Any) -> list[Any]:
                del name, args
                return [10]

        monkeypatch.setattr(
            plugin, "script_module", lambda name, **kwargs: {"compositefont": _Fake()}.get(name)
        )
        runtime = LuaScriptRuntime()
        text = runtime.expand_text(
            "<?obj.mes(obj.module('compositefont').api_version())?>", _state()
        )
        assert text == "10"


class TestOnlyTheNeededPlugins:
    """``obj.module`` で初期化する汎用プラグインを、要る物だけに絞る（Issue #135）

    前は名前が何であれ ``Plugin`` フォルダの全部を初期化していた テレビ字幕
    （中身は ``.mod2`` のファイル）を描くだけで WhisperAutoSub が Python を起動し、
    本人の AviUtl2 の置き場へ設定と一時ファイルを書いていた
    """

    @pytest.fixture
    def started(self, tmp_path: Path, monkeypatch: Any) -> Iterator[list[str]]:
        """合成フォントと、関係の無いプラグインを 1 つずつ置き、初期化した物を数える"""
        (tmp_path / "comfont.aux2").write_bytes(b"MZ")
        (tmp_path / "よそ").mkdir()
        (tmp_path / "よそ" / "よそ.aux2").write_bytes(b"MZ")
        monkeypatch.setattr(plugin, "default_plugin_roots", lambda: (tmp_path,))
        monkeypatch.setattr("sys.platform", "win32")
        loaded: list[str] = []

        def record(path: Path) -> dict[str, Any]:
            loaded.append(path.name)
            return {}

        monkeypatch.setattr(plugin, "_load", record)
        yield loaded
        plugin.set_scan_all(False)

    def test_a_file_module_starts_no_plugin(self, started: list[str]) -> None:
        """ファイルの ``.mod2`` を引くだけで全部を初期化すると、テレビ字幕を
        描いただけで本人の AviUtl2 の置き場が書き換わる
        """
        LuaScriptRuntime().expand_text("<?local m = obj.module('TVSubtitle')?>", _state())
        assert started == []

    def test_composite_font_starts_only_its_plugin(self, started: list[str]) -> None:
        """合成フォントのためにほかのプラグインまで初期化すると、関係の無い DLL が
        ウィンドウを作ったり Python を起動したりする
        """
        LuaScriptRuntime().expand_text("<?local m = obj.module('compositefont')?>", _state())
        assert started == ["comfont.aux2"]

    def test_a_name_is_searched_once(self, started: list[str]) -> None:
        """見つからない名前を引くたびに探し直すと、毎コマ DLL の読み込みを試す"""
        for _ in range(3):
            plugin.script_module("compositefont")
        assert started == ["comfont.aux2"]

    def test_scan_all_reads_every_plugin(self, started: list[str]) -> None:
        """入れても全部を読まないなら、表に無いプラグインのモジュールを使えない"""
        plugin.set_scan_all(True)
        plugin.script_module("なにか")
        assert started == ["comfont.aux2", "よそ.aux2"]

    def test_switching_back_forgets_what_scan_all_found(
        self, started: list[str], monkeypatch: Any
    ) -> None:
        """切ったあとも全部を読んだときの結果を返すと、切った意味が無い"""
        del started
        found = object()
        monkeypatch.setattr(
            plugin, "_load", lambda path: {"なにか": found} if path.name == "よそ.aux2" else {}
        )
        plugin.set_scan_all(True)
        assert plugin.script_module("なにか") is found
        plugin.set_scan_all(False)
        assert plugin.script_module("なにか") is None

    def test_a_missing_module_points_at_the_setting(self, started: list[str]) -> None:
        """読まずにおいたプラグインが出す物かもしれないと書かないと、AviUtl2 では
        動くのに Sashimono では動かない理由に辿り着けない
        """
        report = CompatibilityReport()
        runtime = LuaScriptRuntime(report=report)
        runtime.expand_text("<?local m = obj.module('なにか')?>", _state())
        assert any("全部読んで探す" in line for line in report.missing)
        assert started == []

    def test_no_hint_when_everything_was_read(self, started: list[str]) -> None:
        """全部を読んでも無い物に設定を勧めると、入れても直らない案内になる"""
        del started
        plugin.set_scan_all(True)
        report = CompatibilityReport()
        runtime = LuaScriptRuntime(report=report)
        runtime.expand_text("<?local m = obj.module('なにか')?>", _state())
        assert not any("全部読んで探す" in line for line in report.missing)


class TestTheRealFolderIsOutOfReach:
    """試験から本人の AviUtl2 の汎用プラグインを読ませない（Issue #135）"""

    def test_the_default_folder_is_a_temporary_one(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """既定の置き場が本物のままだと、描くだけの試験がほかのプラグインまで初期化する"""
        base = tmp_path_factory.getbasetemp().resolve()
        roots = plugin.default_plugin_roots()
        assert roots
        for root in roots:
            assert root.resolve().is_relative_to(base)
            assert not touches_real_aviutl2(root)

    def test_plugins_are_told_a_temporary_folder(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """本人の置き場を渡すと、合成フォントが本人の設定で組み、試験の結果が機械で変わる"""
        path = plugin.app_data_path()
        assert path is not None
        assert path.resolve().is_relative_to(tmp_path_factory.getbasetemp().resolve())

    def test_the_real_plugin_is_skipped_off_windows(self, monkeypatch: Any) -> None:
        """ファイルがあるだけで走らせると、DLL を読めない OS で実物の試験が落ちる"""
        monkeypatch.setattr("sys.platform", "linux")
        assert real_comfont_missing() == "comfont.aux2 は Windows の DLL で、この OS では読めない"

    def test_the_guard_protects_the_real_default_folder(self) -> None:
        """守る置き場がアプリの既定の置き場と食い違うと、守っているつもりで本物を読ませる"""
        program_data = os.environ.get("PROGRAMDATA")
        expected = (Path(program_data) / "aviutl2" / "Plugin",) if program_data else ()
        assert expected == conftest.PROTECTED_PLUGIN_ROOTS

    def test_reading_the_protected_folder_fails_the_test(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """読もうとしても落ちないと、握って先へ進む描画の中で本物を読んでも気付けない

        本物の置き場は CI には無い（無ければ中を読みに行かず、守りが試されない）
        守る置き場を、偽の .aux2 を置いた一時フォルダへ差し替えて確かめる
        守りが無ければ偽の DLL は読めない物として記録され、例外は出ない
        """
        protected = tmp_path / "Plugin"
        protected.mkdir()
        (protected / "偽物.aux2").write_bytes(b"MZ")
        monkeypatch.setattr(conftest, "PROTECTED_PLUGIN_ROOTS", (protected,))
        monkeypatch.setattr("sys.platform", "win32")
        with pytest.raises(pytest.fail.Exception, match="本人の AviUtl2"):
            plugin.script_modules((protected,))


def _library(required: int | None) -> Any:
    """``RequiredVersion`` だけを持つ DLL の代わり"""

    class _Library:
        pass

    library = _Library()
    if required is not None:

        def required_version() -> int:
            return required

        library.RequiredVersion = required_version  # type: ignore[attr-defined]
    return library


class TestHostVersion:
    """``RequiredVersion`` とこちらが名乗る版"""

    def test_a_plugin_asking_for_a_newer_host_is_refused(self) -> None:
        """求められた版をそのまま名乗ると、その版で増えた枠があるものとして
        表の外を呼ばれ、Sashimono ごと落ちる
        """
        with pytest.raises(native.NativeModuleError, match="求めている"):
            native.host_version_for(_library(native.HOST_VERSION + 1), Path("新しい.aux2"))

    def test_what_we_claim_is_what_we_copied(self) -> None:
        """プラグインが求めた版をそのまま名乗ると、写した並び（v2.1.6a）と
        違う版だと伝えることになり、版で振る舞いを変えるプラグインが
        こちらの表と合わない前提で動く
        """
        claimed = native.host_version_for(_library(2010100), Path("comfont.aux2"))
        assert claimed == native.HOST_VERSION == 2010601

    def test_no_required_version_is_accepted(self) -> None:
        """``RequiredVersion`` は任意 無いだけで断ると、WhisperAutoSub や
        テレビ字幕のモジュールが読めなくなる
        """
        assert native.host_version_for(_library(None), Path("無印.aux2")) == native.HOST_VERSION

    def test_the_plugins_seen_in_the_wild_are_accepted(self) -> None:
        """手元の配布物が求める版（comfont 2010100、AIEdit 2010400 ほか）を
        断ると、今まで動いていた合成フォントやテレビ字幕が消える
        """
        for required in (2003300, 2004900, 2010100, 2010400):
            native.host_version_for(_library(required), Path("手元.aux2"))


class TestRealPlugin:
    """実物の ``comfont.aux2`` 形式の推測ではなく、配布されている物で確かめる

    実物だけを一時フォルダへ写して読む（``real_comfont``） 本人の ``Plugin`` フォルダを
    渡すと、ほかの汎用プラグインまで初期化して本人の置き場へ書かせる（Issue #135）
    """

    def test_registers_the_composite_font_module(self, real_comfont: Path) -> None:
        """登録を受け取れないと ``obj.module("compositefont")`` が nil になり、
        配布エイリアスが 1 行目で落ちて真っ黒になる
        """
        modules = plugin.script_modules((real_comfont,))
        assert "compositefont" in modules
        assert "decorate_layout" in modules["compositefont"].names

    def test_api_version_can_be_called(self, real_comfont: Path) -> None:
        """呼べないと、エイリアスが ``api_version()`` の行で落ちる"""
        module = plugin.script_modules((real_comfont,))["compositefont"]
        assert int(module.call("api_version", [])[0]) >= 9

    def test_obj_module_finds_it_by_name(self, real_comfont: Path, monkeypatch: Any) -> None:
        """名前で絞ったせいで実物を読まなくなると、配布エイリアスが真っ黒に戻る"""
        monkeypatch.setattr(plugin, "default_plugin_roots", lambda: (real_comfont,))
        runtime = LuaScriptRuntime()
        text = runtime.expand_text(
            "<?obj.mes(obj.module('compositefont').api_version())?>", _state()
        )
        assert int(text) >= 9

    def test_layout_survives_a_font_name(self, real_comfont: Path) -> None:
        """``edit`` が空のままだと、書体名を渡した時点で中から落ちて
        Sashimono ごと消える（捕まえる手段は無い）
        """
        module = plugin.script_modules((real_comfont,))["compositefont"]
        results = module.call("decorate_layout", ["あいう", "default", 64.0, 0.0, "Yu Gothic UI"])
        assert results and isinstance(results[0], str)
