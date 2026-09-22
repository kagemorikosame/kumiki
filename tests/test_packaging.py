"""配る zip（パッケージ版）でだけ通る道

開発環境では ``sys.frozen`` が無いので、ここにある道は普段まったく通らない
壊れても誰も気づかないまま配ることになるので、固めた状態を真似て確かめる
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from kumiki.app import SELF_CHECK_FLAG, main
from kumiki.compat.aviutl.catalog import PORTABLE_SCRIPTS_DIR, default_script_roots
from kumiki.runtime import app_dir, install_command, pip_arguments, run_pip
from kumiki.selfcheck import CheckResult, format_results, run_self_check

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """固めた exe として動いているふりをする 返すのは exe の場所"""
    executable = tmp_path / "Kumiki" / "Kumiki.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    return executable


@pytest.fixture(scope="module")
def builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_package", ROOT / "tools" / "build_package.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestTheScriptFolderBesideTheExe:
    def test_it_comes_first_in_the_package(self, frozen: Path) -> None:
        """zip を展開した人が最初に目にする所へ置けば読まれる

        ``%APPDATA%`` は隠しフォルダなので、そこだけだと見つけられない
        """
        assert default_script_roots()[0] == frozen.parent / PORTABLE_SCRIPTS_DIR

    def test_it_is_not_looked_at_in_development(self) -> None:
        # 開発環境で .venv の隣を探しに行くと、関係の無いフォルダを読む
        assert app_dir() is None
        assert all(
            root.name != PORTABLE_SCRIPTS_DIR or "Kumiki" in root.parts
            for root in default_script_roots()
        )


class TestPipInsideThePackage:
    """配布版には Python の本体が無い 導入ボタンは exe 自身に pip を走らせる"""

    def test_the_install_button_calls_the_exe_itself(self, frozen: Path) -> None:
        # 導入ボタンが組み立てるコマンド 先頭は exe（ここで受けないと Kumiki が 2 つ立つ）
        from kumiki.runtime import FeaturePack

        command = install_command(FeaturePack(key="x", label="x", required=("pkg",)))
        assert command[:4] == [str(frozen), "-m", "pip", "install"]

    def test_the_exe_hands_it_to_pip(self, frozen: Path) -> None:
        assert pip_arguments([str(frozen), "-m", "pip", "install", "pkg"]) == ["install", "pkg"]

    def test_an_ordinary_start_is_not_taken_for_pip(self, frozen: Path) -> None:
        """プロジェクトを開く起動（``Kumiki.exe 作品.kmk``）を pip と取り違えない"""
        assert pip_arguments([str(frozen), "作品.kmk"]) is None

    def test_development_leaves_it_to_python(self) -> None:
        """開発環境の ``sys.executable`` は本物の Python なので、こちらは受けない

        受けると、``python -m kumiki -m pip`` のような書き方まで pip に回る
        """
        assert pip_arguments(["python", "-m", "pip", "list"]) is None

    def test_main_runs_pip_instead_of_the_window(
        self, frozen: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """窓を作る前に pip へ渡す 窓を作ってからだと、導入のたびに窓が開く"""
        called: list[list[str]] = []

        def fake_pip(arguments: list[str]) -> int:
            called.append(arguments)
            return 0

        monkeypatch.setattr("pip._internal.cli.main.main", fake_pip)
        assert main([str(frozen), "-m", "pip", "--version"]) == 0
        assert called == [["--version"]]

    def test_distlib_learns_how_to_find_its_parts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """pip の中の distlib に、配布版の読み込み方式でも部品を探せるよう教える

        教えないと ``pip install`` が「Unable to locate finder」で落ちる
        ``pip --version`` は部品を探さないので通ってしまい、ここまで気づけなかった
        （配布版で実際に入れてみて見つかった）
        """
        from pip._vendor import distlib
        from pip._vendor.distlib import resources

        class FrozenLoader:
            """PyInstaller の読み込み方式の代わり distlib の一覧に無い型"""

        monkeypatch.setattr(distlib, "__loader__", FrozenLoader(), raising=False)
        monkeypatch.setattr("pip._internal.cli.main.main", lambda arguments: 0)
        # 試験のあとに登録を残さない 残すと、ほかの試験が別の探し方で動く
        registry = resources._finder_registry
        monkeypatch.setattr(resources, "_finder_registry", dict(registry))

        assert run_pip(["--version"]) == 0
        assert resources._finder_registry.get(FrozenLoader) is resources.ResourceFinder


class TestTheSelfCheck:
    def test_everything_works_here(self) -> None:
        """開発環境では全部動く ここで落ちるなら、配る前から壊れている"""
        results = run_self_check()
        failed = [r for r in results if not r.ok and not r.optional]
        assert failed == [], format_results(results)

    def test_the_flag_reaches_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--self-check`` は窓を作らずに終わる

        窓を作ると、使う人の機械で確かめてもらうときに、閉じるまで結果が出ない
        """
        monkeypatch.setattr("kumiki.selfcheck.run_self_check", lambda: [CheckResult("x", True)])
        assert main(["kumiki", SELF_CHECK_FLAG]) == 0

    def test_a_project_with_the_flag_is_opened_not_checked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """プロジェクトと一緒に渡されたら、自己診断ではなく開く起動として扱う

        自己診断を優先すると、頼んだプロジェクトが開かずに黙って終わる
        """
        checked: list[bool] = []

        def fake_check() -> int:
            checked.append(True)
            return 0

        monkeypatch.setattr("kumiki.selfcheck.main", fake_check)

        class ReachedTheWindowError(Exception):
            """いつもの起動の最初の段まで来たら止める ここまで来れば開く起動"""

        def stop() -> None:
            raise ReachedTheWindowError

        # main 自身が見ている名前を差し替える ほかの試験がモジュールを読み直すと、
        # 「kumiki.app」という名前の先と、ここで握っている main の先が別物になる
        monkeypatch.setitem(main.__globals__, "activate_runtime", stop)
        with pytest.raises(ReachedTheWindowError):
            main(["kumiki", "作品.kmk", SELF_CHECK_FLAG])
        assert checked == []

    def test_a_missing_part_fails_the_whole(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """1 つでも動かなければ終了コード 1 組み立ての道具はこれを見て止まる"""
        monkeypatch.setattr(
            "kumiki.selfcheck.run_self_check",
            lambda: [CheckResult("GL で描く", False, "積み忘れ")],
        )
        assert main(["kumiki", SELF_CHECK_FLAG]) == 1

    def test_an_optional_part_does_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """音の出口が無い機械（リモート接続など）でも編集はできる 落とさない"""
        monkeypatch.setattr(
            "kumiki.selfcheck.run_self_check",
            lambda: [CheckResult("音の出口", False, "無い", optional=True)],
        )
        assert main(["kumiki", SELF_CHECK_FLAG]) == 0

    def test_a_failure_says_which_part(self) -> None:
        # どの項目が動かないかが 1 行で分かる 分からないと組み立て直しを繰り返す
        text = format_results([CheckResult("書き出す（FFmpeg）", False, "DLL が無い")])
        assert "[NG] 書き出す（FFmpeg）: DLL が無い" in text


class TestTheZip:
    def test_it_unpacks_into_one_folder(self, builder: ModuleType, tmp_path: Path) -> None:
        """展開すると ``Kumiki\\`` が 1 つできる

        ばらで入れると、展開した場所（デスクトップなど）に部品が散らばる
        """
        bundle = tmp_path / "bundle"
        (bundle / "_internal").mkdir(parents=True)
        (bundle / "Kumiki.exe").write_bytes(b"MZ")
        (bundle / "_internal" / "part.dll").write_bytes(b"x")
        builder.assemble(bundle)
        archive = builder.make_zip(bundle, tmp_path / "out.zip")
        with zipfile.ZipFile(archive) as opened:
            names = opened.namelist()
        assert all(name.startswith("Kumiki/") for name in names)
        assert "Kumiki/Kumiki.exe" in names

    def test_the_script_folder_is_already_there(self, builder: ModuleType, tmp_path: Path) -> None:
        """空でも置き場を作っておく 無いと、どこへ置けばいいのかが分からない

        zip は空のフォルダを持てないので、説明書きを 1 つ入れて残す
        """
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        builder.assemble(bundle)
        archive = builder.make_zip(bundle, tmp_path / "out.zip")
        with zipfile.ZipFile(archive) as opened:
            assert f"Kumiki/{PORTABLE_SCRIPTS_DIR}/README.txt" in opened.namelist()

    def test_an_unfinished_zip_is_not_left_as_the_real_one(
        self, builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """途中で止まった zip を完成品と取り違えない"""
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "Kumiki.exe").write_bytes(b"MZ")

        def broken(self: zipfile.ZipFile, *args: object, **kwargs: object) -> None:
            raise OSError("書けない")

        monkeypatch.setattr(zipfile.ZipFile, "write", broken)
        with pytest.raises(OSError):
            builder.make_zip(bundle, tmp_path / "out.zip")
        assert not (tmp_path / "out.zip").exists()
        # 書きかけも残さない 100 MB ずつ溜まるうえ、手で配るときに紛れる
        assert list(tmp_path.glob("*.writing")) == []

    def test_a_failed_rename_leaves_nothing_behind(
        self, builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """書き終えても、名前を付けられなければ書いた物を残さない

        そこで残すと、書き終えた 100 MB がそのまま溜まる
        """
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "Kumiki.exe").write_bytes(b"MZ")
        target = tmp_path / "out.zip"

        def locked(self: Path, other: Path) -> Path:
            raise PermissionError("使用中")

        monkeypatch.setattr(Path, "replace", locked)
        with pytest.raises(PermissionError):
            builder.make_zip(bundle, target)
        assert list(tmp_path.glob("*.writing")) == []

    def test_the_previous_zip_does_not_survive_a_failure(
        self, builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """前の組み立ての zip を、今回の版の名前で残さない

        今回が途中で落ちたときに前の物が残ると、新しい物と取り違えて配る
        """
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "Kumiki.exe").write_bytes(b"MZ")
        target = tmp_path / "out.zip"
        target.write_bytes(b"previous build")

        def broken(self: zipfile.ZipFile, *args: object, **kwargs: object) -> None:
            raise OSError("書けない")

        monkeypatch.setattr(zipfile.ZipFile, "write", broken)
        with pytest.raises(OSError):
            builder.make_zip(bundle, target)
        assert not target.exists(), "前の組み立ての zip が今回の名前で残っている"

    def test_the_check_does_not_borrow_the_developers_path(self, builder: ModuleType) -> None:
        """確かめるときは開発機の PATH を使わない

        開発機の PATH には Python や FFmpeg が載っている 残すと、zip に
        積み忘れた DLL をそちらから拾って通ってしまう
        """
        environment = builder.minimal_environment(
            {"PATH": r"C:\Python314;C:\ffmpeg\bin", "SystemRoot": r"C:\Windows", "TEMP": "t"}
        )
        assert "Python" not in environment["PATH"]
        assert "ffmpeg" not in environment["PATH"]
        assert environment["SYSTEMROOT"] == r"C:\Windows"

    def test_the_optional_features_are_left_out(self, builder: ModuleType) -> None:
        """字幕起こしと AI 連携は積まない 開発機に入っていると拾われて 2 GB を超える"""
        arguments = builder.pyinstaller_arguments(Path("w"), Path("d"))
        excluded = {
            arguments[i + 1] for i, value in enumerate(arguments) if value == "--exclude-module"
        }
        assert {"faster_whisper", "claude_agent_sdk"} <= excluded

    def test_the_dynamically_loaded_parts_are_collected(self, builder: ModuleType) -> None:
        """名前で読む部品はまとめて積む

        lupa は Lua の実体を名前で選び（``lupa.lua51``）、pip は導入ボタンが使う
        PyInstaller は名前で読む import を辿れないので、積み忘れても組み立ては通る
        """
        arguments = builder.pyinstaller_arguments(Path("w"), Path("d"))
        collected = {
            arguments[i + 1] for i, value in enumerate(arguments) if value == "--collect-all"
        }
        assert {"lupa", "pip"} <= collected


class TestTheEditorCheckLeavesNoTrace:
    """編集画面の組み立ては、本人の設定に触れない

    編集画面は閉じるときに画面の並びを保存する 見せていない窓を閉じて保存すると、
    本人が整えた並びが既定の並びで上書きされる
    """

    def test_the_layout_is_not_written(self) -> None:
        from kumiki.selfcheck import _editor
        from kumiki.ui.workspace import config_root

        layout = config_root() / "workspace.ini"
        before = layout.read_bytes() if layout.exists() else None
        _editor()
        after = layout.read_bytes() if layout.exists() else None
        assert after == before, "確かめただけで画面の並びを書き換えている"

    def test_the_folders_come_back(self) -> None:
        # 向け先を戻し忘れると、そのあとの項目（スクリプト置き場）が一時フォルダを見る
        import os

        from kumiki.selfcheck import USER_FOLDER_VARIABLES, _isolated_user_folders

        before = {name: os.environ.get(name) for name in USER_FOLDER_VARIABLES}
        with _isolated_user_folders():
            assert os.environ["APPDATA"] != before["APPDATA"]
        assert {name: os.environ.get(name) for name in USER_FOLDER_VARIABLES} == before


class TestTheExportCheckUsesTheCpu:
    def test_without_libx264_it_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CPU の符号化器が無ければ落とす GPU の符号化器へ逃げない

        逃げると、GPU の符号化器がある開発機では通り、無い機械では書き出せない
        zip を「動いた」として配ることになる
        """
        from kumiki import selfcheck

        monkeypatch.setattr(
            "kumiki.engine.encode.available_video_codecs", lambda: ["h264_nvenc", "h264_qsv"]
        )
        with pytest.raises(RuntimeError, match="libx264"):
            selfcheck._export()


class TestWithoutAConsole:
    """窓だけの exe をパイプ無しで起動すると、標準出力が無い（``sys.stdout`` が None）

    ダブルクリックやコマンドでそのまま打つとこうなる 書いても誰にも届かず、
    自己診断を頼んだ人には何も起きないように見える
    """

    def test_the_result_is_shown_in_a_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from PySide6.QtWidgets import QMessageBox

        shown: list[str] = []
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(
            "kumiki.selfcheck.run_self_check", lambda: [CheckResult("GL で描く", True, "描けた")]
        )
        monkeypatch.setattr(
            QMessageBox, "information", lambda parent, title, text: shown.append(text)
        )
        assert main(["kumiki", SELF_CHECK_FLAG]) == 0
        assert shown and "GL で描く" in shown[0]

    def test_a_failure_is_shown_as_a_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 落ちた項目があるときは目立つ形で出す 情報の窓だと読み流される
        from PySide6.QtWidgets import QMessageBox

        warned: list[str] = []
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(
            "kumiki.selfcheck.run_self_check", lambda: [CheckResult("GL で描く", False, "x")]
        )
        monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warned.append(text))
        assert main(["kumiki", SELF_CHECK_FLAG]) == 1
        assert warned


class TestOnlyCheckedZipsRemain:
    """dist に zip がある＝確かめ済み

    確かめて落ちた zip を残すと、動かない物を完成品と取り違えて配る
    """

    def _bundle(self, tmp_path: Path) -> Path:
        bundle = tmp_path / "Kumiki"
        bundle.mkdir()
        (bundle / "Kumiki.exe").write_bytes(b"MZ")
        return bundle

    def test_a_failed_check_takes_the_zip_away(
        self, builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(builder, "smoke_test", lambda archive: 1)
        target = tmp_path / "out.zip"
        assert builder.package(self._bundle(tmp_path), target) == 1
        assert not target.exists(), "確かめて落ちた zip が残っている"

    def test_the_folder_stays_for_a_look(
        self, builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 何が足りないかを調べるには、組み立てたフォルダの方が要る
        monkeypatch.setattr(builder, "smoke_test", lambda archive: 1)
        bundle = self._bundle(tmp_path)
        builder.package(bundle, tmp_path / "out.zip")
        assert (bundle / "Kumiki.exe").exists()

    def test_a_passed_check_keeps_it(
        self, builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(builder, "smoke_test", lambda archive: 0)
        target = tmp_path / "out.zip"
        assert builder.package(self._bundle(tmp_path), target) == 0
        assert target.exists()
