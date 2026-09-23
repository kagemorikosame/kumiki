"""AviUtl2 と比べる道具（tools/aviutl_compare.py）の、合成フォントの下ごしらえ

合成フォント（``comfont.aux2``）は設定（``compositefont\\profiles.json``）が無いと
本文をそのまま返し、書体の組み替えが起きない 道具は見本の設定を作業フォルダへ書き、
AviUtl2 の置き場へ写す手順を出す **利用者の設定へは書きに行かない**

実物のプラグインを使う試験は、入っている機械でだけ走る（CI では飛ぶ）
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from sashimono.compat.aviutl import plugin

ROOT = Path(__file__).resolve().parent.parent

#: 実物の置き場 利用者の AviUtl2 にしか無い
_REAL_PLUGIN = Path(os.environ.get("PROGRAMDATA", "")) / "aviutl2" / "Plugin" / "comfont.aux2"

#: 実物（comfont.aux2 v0.2.0）が受け付けた 1 文字種ぶんの項目
#: 臨時の置き場に置いたとき ``resolve("あ", "default")`` が書いた書体を返した
_ACCEPTED_ADJUSTMENT = {
    "font_family",
    "fallback_font_families",
    "size_ratio",
    "baseline_shift_em",
    "tracking_adjust_em",
    "metric_unit",
    "size_px",
    "baseline_shift_px",
    "tracking_adjust_px",
    "vertical_scale_ratio",
    "horizontal_scale_ratio",
}
_ACCEPTED_KINDS = ("western", "hiragana", "katakana", "kanji", "digit", "symbol", "other")


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "aviutl_compare", ROOT / "tools" / "aviutl_compare.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _profile(document: dict[str, Any]) -> dict[str, Any]:
    profiles = document["profiles"]
    assert isinstance(profiles, list) and len(profiles) == 1
    profile: dict[str, Any] = profiles[0]
    return profile


class TestSampleProfile:
    def test_has_the_fields_the_real_plugin_accepted(self, tool: ModuleType) -> None:
        """項目が欠けたり名前が違ったりすると、実物は設定を読まずに本文をそのまま返し、
        組み替えの起きない絵を AviUtl2 の絵と比べることになる
        """
        document = tool.composite_profiles()
        assert document["schema_version"] == 1
        profile = _profile(document)
        expected = {"name", *_ACCEPTED_KINDS, *(f"{kind}_fallbacks" for kind in _ACCEPTED_KINDS)}
        assert set(profile) == expected
        for kind in _ACCEPTED_KINDS:
            assert set(profile[kind]) == _ACCEPTED_ADJUSTMENT

    def test_uses_the_profile_the_aliases_ask_for(self, tool: ModuleType) -> None:
        """配布エイリアスは ``profile="default"`` を引く 名前が違うと組み替えが起きない"""
        assert _profile(tool.composite_profiles())["name"] == "default"

    def test_neighbouring_kinds_get_different_fonts(self, tool: ModuleType) -> None:
        """同じ書体が並ぶと、組み替えが起きたかどうかを絵で見分けられない"""
        profile = _profile(tool.composite_profiles())
        japanese = {profile[kind]["font_family"] for kind in ("hiragana", "katakana", "kanji")}
        assert len(japanese) == 3
        assert profile["western"]["font_family"] not in japanese

    def test_avoids_the_default_font(self, tool: ModuleType) -> None:
        """Sashimono の既定（Yu Gothic UI）を混ぜると、組み替えずに既定で描いた絵と
        区別が付かない
        """
        profile = _profile(tool.composite_profiles())
        assert all(profile[kind]["font_family"] != "Yu Gothic UI" for kind in _ACCEPTED_KINDS)

    def test_only_the_font_changes(self, tool: ModuleType) -> None:
        """大きさや位置まで動かすと、絵の違いが書体から来たのか見分けられない"""
        profile = _profile(tool.composite_profiles())
        for kind in _ACCEPTED_KINDS:
            adjustment = profile[kind]
            assert adjustment["size_ratio"] == 1.0
            assert adjustment["baseline_shift_em"] == 0.0
            assert adjustment["tracking_adjust_em"] == 0.0

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows の書体で確かめる")
    def test_fonts_are_installed(self, tool: ModuleType) -> None:
        """無い書体を指すと、AviUtl2 も Sashimono も既定の書体で描き、組み替えが見えない"""
        assert tool.missing_fonts() == []


class TestProfileCommand:
    def _run(self, tool: ModuleType, work: Path) -> None:
        assert tool.command_profile(SimpleNamespace(work=work)) == 0

    def test_writes_only_into_the_work_folder(
        self, tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """利用者の置き場へ書くと、合成フォントのエディターで作った設定を黙って潰す"""
        program_data = tmp_path / "ProgramData"
        (program_data / "aviutl2").mkdir(parents=True)
        monkeypatch.setenv("PROGRAMDATA", str(program_data))
        work = tmp_path / "work"
        self._run(tool, work)
        sample = work / "appdata" / "compositefont" / "profiles.json"
        assert json.loads(sample.read_text(encoding="utf-8")) == tool.composite_profiles()
        assert not (program_data / "aviutl2" / "compositefont").exists()

    def test_an_existing_user_setting_is_left_alone(
        self, tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """控えも取らずに上書きすると、本人の設定が戻らなくなる"""
        program_data = tmp_path / "ProgramData"
        target = program_data / "aviutl2" / "compositefont" / "profiles.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"本人": 1}', encoding="utf-8")
        monkeypatch.setenv("PROGRAMDATA", str(program_data))
        self._run(tool, tmp_path / "work")
        assert target.read_text(encoding="utf-8") == '{"本人": 1}'
        assert not target.with_name("profiles.json.bak").exists()

    def test_tells_where_to_put_it(
        self,
        tool: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """置き場を示さないと、本人は AviUtl2 のどこへ置けば効くのか分からない"""
        program_data = tmp_path / "ProgramData"
        monkeypatch.setenv("PROGRAMDATA", str(program_data))
        self._run(tool, tmp_path / "work")
        out = capsys.readouterr().out
        assert str(program_data / "aviutl2" / "compositefont" / "profiles.json") in out
        assert "控え" not in out

    def test_asks_for_a_backup_when_one_exists(self, tool: ModuleType, tmp_path: Path) -> None:
        """既にある設定へそのまま写させると、本人の組み合わせが消える"""
        target = tmp_path / "compositefont" / "profiles.json"
        target.parent.mkdir()
        target.write_text("{}", encoding="utf-8")
        sample = tmp_path / "見本.json"
        lines = tool.profile_guide(sample, target)
        assert any("控え" in line for line in lines)
        backup = next(i for i, line in enumerate(lines) if f"{target}.bak" in line)
        copy = next(i for i, line in enumerate(lines) if f'Copy-Item "{sample}"' in line)
        # 控えを取る行が、写す行より先に来ること 後だと上書きした物の控えになる
        assert backup < copy


class TestAppDataPath:
    @pytest.fixture(autouse=True)
    def reset(self) -> Any:
        yield
        plugin.set_app_data_path(None)

    def test_override_reaches_the_plugin(self, tmp_path: Path) -> None:
        """差し替えが届かないと、見本を試すには利用者の設定を書き換えるしかなくなる"""
        plugin.set_app_data_path(tmp_path)
        assert plugin._config().app_data_path == str(tmp_path)

    def test_without_override_the_users_folder_is_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """既定が変わると、利用者が AviUtl2 で作った書体の組み合わせが見えなくなる"""
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        plugin.set_app_data_path(tmp_path / "一時")
        plugin.set_app_data_path(None)
        assert plugin._config().app_data_path == str(tmp_path / "aviutl2")


@pytest.mark.skipif(not _REAL_PLUGIN.is_file(), reason="comfont.aux2 が入っていない")
def test_the_real_plugin_composes_with_the_sample(tool: ModuleType, tmp_path: Path) -> None:
    """見本の設定を実物が読まないと、組み替えの無い絵を AviUtl2 と比べることになる

    プラグインは読んだときの置き場を持ち続けるので、別のプロセスで読ませる
    同じプロセスで先に読まれていると、見本の置き場が届かない
    """
    tool.command_profile(SimpleNamespace(work=tmp_path))
    script = (
        "import sys, json\n"
        "from pathlib import Path\n"
        "from sashimono.compat.aviutl import plugin\n"
        "plugin.set_app_data_path(Path(sys.argv[1]))\n"
        "module = plugin.script_modules(roots=(Path(sys.argv[2]),))['compositefont']\n"
        "text = module.call('decorate_layout', ['あア亜A1', 'default', 64.0, 0.0, ''])\n"
        "print(json.dumps(text, ensure_ascii=False))\n"
    )
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "appdata"), str(_REAL_PLUGIN.parent)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=60,
        check=True,
    )
    decorated = json.loads(result.stdout.strip().splitlines()[-1])[0]
    for _kind, family, _file in tool.COMPOSITE_FONTS[:5]:
        assert f"<@{family}>" in decorated
