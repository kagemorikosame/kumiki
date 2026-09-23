"""YMM4 に書き出させる道具（tools/ymm4_export.py と ymm4_export.ps1）

YMM4 そのものは起動しない（本人の画面を取り合うので、実機で確かめるのは人がやる）
ここで見るのは、走らせてよいかの判断・YMM4 の探し方・PowerShell へ渡す命令・
出力の中継・PowerShell のスクリプトが読めること
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "ymm4_export.ps1"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    return _load("ymm4_export")


@pytest.fixture
def ready(tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """YMM4 が見つかり、動いていない状態 走らせた命令を記録する"""
    exe = tmp_path / "YMM4 v4" / "YukkuriMovieMaker.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    project = tmp_path / "probe.ymmp"
    project.write_bytes(b"")
    state: dict[str, Any] = {
        "exe": exe,
        "project": project,
        "output": tmp_path / "out" / "probe.mp4",
        "commands": [],
        "result": (0, False),
        "writes": True,
    }

    def run_script(command: list[str], limit: int) -> tuple[int, bool]:
        state["commands"].append(command)
        state["limit"] = limit
        state["existed"] = state["output"].exists()
        if state["writes"]:
            state["output"].write_bytes(b"mp4")
        result: tuple[int, bool] = state["result"]
        return result

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(tool, "find_ymm4", lambda explicit, environ: (exe, []))
    monkeypatch.setattr(tool, "ymm4_running", lambda image: False)
    monkeypatch.setattr(tool, "run_script", run_script)
    return state


def _arguments(state: dict[str, Any], *extra: str) -> list[str]:
    return ["--project", str(state["project"]), "--output", str(state["output"]), *extra]


def test_an_open_ymm4_stops_the_tool_before_it_touches_anything(
    tool: ModuleType,
    ready: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """開いている YMM4 の上から別のプロジェクトを開くと、作業中の物を壊しかねない

    前の書き出しも消さない 止まったのに消えていたら、本人の物を失う
    """
    monkeypatch.setattr(tool, "ymm4_running", lambda image: True)
    ready["output"].parent.mkdir()
    ready["output"].write_bytes(b"earlier")
    assert tool.main(_arguments(ready)) == 1
    assert "既に開いています" in capsys.readouterr().out
    assert ready["commands"] == []
    assert ready["output"].read_bytes() == b"earlier"


def test_a_missing_ymm4_says_where_it_looked_and_how_to_tell(
    tool: ModuleType,
    ready: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        tool, "find_ymm4", lambda explicit, environ: (None, [r"Z:\nowhere\YukkuriMovieMaker.exe"])
    )
    assert tool.main(_arguments(ready)) == 1
    out = capsys.readouterr().out
    assert "見つかりません" in out
    assert r"Z:\nowhere\YukkuriMovieMaker.exe" in out
    assert "--ymm4" in out and tool.ENVIRONMENT_KEY in out
    assert ready["commands"] == []


def test_the_compressor_flag_reaches_powershell_with_each_path_as_one_argument(
    tool: ModuleType, ready: dict[str, Any]
) -> None:
    """パスに空白があっても 1 つの引数のまま届く 割れると YMM4 が別のファイルを探す"""
    assert tool.main(_arguments(ready, "--no-compressor", "--timeout", "90")) == 0
    [command] = ready["commands"]
    assert command[:7] == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(tool.SCRIPT),
    ]
    assert command[command.index("-Ymm4") + 1] == str(ready["exe"])
    assert command[command.index("-Project") + 1] == str(ready["project"])
    assert command[command.index("-Output") + 1] == str(ready["output"])
    assert command[command.index("-TimeoutSeconds") + 1] == "90"
    assert "-NoCompressor" in command
    # PowerShell が固まったときに見切る長さは、書き出しを待つ長さより長い
    assert ready["limit"] > 90


def test_without_the_flag_the_compressor_is_left_alone(
    tool: ModuleType, ready: dict[str, Any]
) -> None:
    """映像の探りで本人の書き出しの設定を触る理由は無い"""
    assert tool.main(_arguments(ready)) == 0
    [command] = ready["commands"]
    assert "-NoCompressor" not in command


def test_a_relative_project_is_passed_as_an_absolute_path(
    tool: ModuleType, ready: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """YMM4 はこの道具の作業フォルダを知らない 相対のままだと開けない"""
    monkeypatch.chdir(ready["project"].parent)
    assert tool.main(["--project", "probe.ymmp", "--output", "out/probe.mp4"]) == 0
    [command] = ready["commands"]
    assert Path(command[command.index("-Project") + 1]).is_absolute()
    assert Path(command[command.index("-Output") + 1]).is_absolute()


def test_the_entry_leaves_the_previous_export_for_the_script_to_remove(
    tool: ModuleType, ready: dict[str, Any]
) -> None:
    """入口で消すと、確かめてから PowerShell が確かめ直すまでの間に本人が YMM4 を開いたとき、
    止まったのに前の書き出しだけ消えている 消すのは PowerShell が確かめ直した直後
    """
    ready["output"].parent.mkdir()
    ready["output"].write_bytes(b"earlier")
    ready["writes"] = False
    ready["result"] = (1, False)
    assert tool.main(_arguments(ready)) == 1
    assert ready["existed"]


def test_a_failed_restore_is_shouted_and_kept_as_exit_code_two(
    tool: ModuleType, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """戻せなかったのに静かに終わると、本人の書き出しが以後ずっと圧縮なしになる"""
    ready["result"] = (tool.RESTORE_FAILED, False)
    assert tool.main(_arguments(ready, "--no-compressor")) == 2
    out = capsys.readouterr().out
    assert "!!!" in out and "戻せませんでした" in out


def test_a_hung_powershell_warns_that_cleanup_did_not_run(
    tool: ModuleType, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """見切って止めた PowerShell は、設定を戻す所も YMM4 を閉じる所も通っていない"""
    ready["result"] = (1, True)
    assert tool.main(_arguments(ready, "--no-compressor")) == tool.RESTORE_FAILED
    out = capsys.readouterr().out
    assert "開いたまま" in out and "コンプレッサー" in out


def test_success_without_a_file_is_not_reported_as_success(
    tool: ModuleType, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """保存の窓が別の所へ書いたなら、探りの measure は「まだ書き出されていない」で止まる"""
    ready["writes"] = False
    assert tool.main(_arguments(ready)) == 1
    assert "できていません" in capsys.readouterr().out


def test_a_project_that_is_not_ymmp_is_refused(
    tool: ModuleType, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    other = ready["project"].with_suffix(".ymmt")
    other.write_bytes(b"")
    assert tool.main(["--project", str(other), "--output", str(ready["output"])]) == 1
    assert ".ymmp ではありません" in capsys.readouterr().out
    assert ready["commands"] == []


@pytest.mark.parametrize("seconds", ["0", "-5"])
def test_a_timeout_that_cannot_wait_is_refused(tool: ModuleType, seconds: str) -> None:
    with pytest.raises(SystemExit):
        tool.parse_arguments(["--project", "a.ymmp", "--output", "a.mp4", "--timeout", seconds])


def test_the_association_command_gives_the_exe(tool: ModuleType) -> None:
    command = r'"D:\Program\YukkuriMovieMaker_v4\YukkuriMovieMaker.exe" "%1"'
    assert tool.exe_from_command(command) == Path(
        r"D:\Program\YukkuriMovieMaker_v4\YukkuriMovieMaker.exe"
    )
    assert tool.exe_from_command(r"C:\YMM4\YukkuriMovieMaker.exe %1") == Path(
        r"C:\YMM4\YukkuriMovieMaker.exe"
    )
    # exe でない物（別のソフトへ関連付けを奪われた形）は使わない
    assert tool.exe_from_command('"C:\\x\\open.bat" "%1"') is None


def _no_common_places(tool: ModuleType, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    # 実機の C:\ D:\ に YMM4 があると、探し方の試験がそれを拾ってしまう
    monkeypatch.setattr(tool, "DRIVES", (str(root),))


def test_the_association_finds_ymm4_wherever_it_was_unpacked(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_common_places(tool, monkeypatch, tmp_path)
    exe = tmp_path / "anywhere" / "YukkuriMovieMaker.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    found, _ = tool.find_ymm4(None, {}, association=lambda: f'"{exe}" "%1"')
    assert found == exe


def test_a_common_place_is_used_when_nothing_is_registered(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_common_places(tool, monkeypatch, tmp_path)
    exe = tmp_path / "Program" / "YukkuriMovieMaker_v4" / "YukkuriMovieMaker.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    found, _ = tool.find_ymm4(None, {}, association=lambda: None)
    assert found == exe


def test_a_named_ymm4_that_is_missing_is_not_swapped_for_another(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """名指しした版と違う版で書き出すと、突き合わせの結果の出どころが食い違う"""
    _no_common_places(tool, monkeypatch, tmp_path)
    other = tmp_path / "other" / "YukkuriMovieMaker.exe"
    other.parent.mkdir()
    other.write_bytes(b"")
    missing = tmp_path / "gone" / "YukkuriMovieMaker.exe"

    def registered() -> str:
        return f'"{other}" "%1"'

    found, looked = tool.find_ymm4(missing, {}, association=registered)
    assert found is None and str(missing) in looked[0]
    environ = {tool.ENVIRONMENT_KEY: str(missing)}
    found, looked = tool.find_ymm4(None, environ, association=registered)
    assert found is None and tool.ENVIRONMENT_KEY in looked[0]
    # 名指しが無ければ関連付けの方を使う
    assert tool.find_ymm4(None, {}, association=registered)[0] == other


def test_the_places_looked_at_are_listed_when_nothing_is_found(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_common_places(tool, monkeypatch, tmp_path)
    found, looked = tool.find_ymm4(None, {}, association=lambda: None)
    assert found is None
    assert any("関連付け" in line for line in looked)
    assert any(str(tmp_path) in line for line in looked)


def test_the_running_check_reads_the_image_column_not_the_message(tool: ModuleType) -> None:
    """見つからないときの案内は言語で文言が変わる 像名の欄だけで判断する"""

    def answer(text: bytes) -> Any:
        return lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout=text)

    running = b'"YukkuriMovieMaker.exe","1234","Console","1","512,000 K"\r\n'
    assert tool.ymm4_running("YukkuriMovieMaker.exe", run=answer(running))
    none = "情報: 指定された条件に一致するタスクは実行されていません".encode("cp932")
    assert not tool.ymm4_running("YukkuriMovieMaker.exe", run=answer(none))
    assert not tool.ymm4_running("YukkuriMovieMaker.exe", run=answer(b"INFO: No tasks"))


def test_powershell_lines_are_read_as_utf8_first(tool: ModuleType) -> None:
    """スクリプトの文言は UTF-8 手元の文字コードで先に読むと、全部化ける"""
    assert tool.decode_line("書き出しました".encode()) == "書き出しました"
    # PowerShell 自身のエラーは手元の文字コードで来る 読めなくても落ちない
    assert isinstance(tool.decode_line(b"\x83G\x83\x89\x81[ \xff"), str)


def test_the_output_is_relayed_line_by_line(
    tool: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code = "import sys; sys.stdout.buffer.write('書き出しています\\n'.encode()); sys.exit(3)"
    assert tool.run_script([sys.executable, "-c", code], 60) == (3, False)
    assert "書き出しています" in capsys.readouterr().out


def test_a_hung_script_is_cut_off(tool: ModuleType) -> None:
    """固まった PowerShell を待ち続けると、探りを回す側まで止まる"""
    code, expired = tool.run_script([sys.executable, "-c", "import time; time.sleep(60)"], 1)
    assert expired and code != 0


def test_the_script_is_utf8_with_a_bom() -> None:
    """BOM が無いと Windows PowerShell 5.1 は Shift_JIS として読み、日本語の名前が全部化ける

    化けた名前では〔ファイル(F)〕も「名前を付けて保存」も見つからない
    """
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    raw[3:].decode("utf-8")


def test_every_flag_the_entry_passes_is_a_script_parameter(tool: ModuleType) -> None:
    """名前が食い違うと PowerShell は「そんな引数は無い」で落ち、YMM4 は開きもしない"""
    text = SCRIPT.read_text(encoding="utf-8-sig")
    block = text[text.index("param(") : text.index(")\n\n")]
    parameters = {name.lower() for name in re.findall(r"\$(\w+)", block)}
    command = tool.powershell_command(
        Path("a.exe"), Path("a.ymmp"), Path("a.mp4"), no_compressor=True, timeout=5
    )
    flags = command[command.index("-File") + 2 :: 1]
    passed = {flag[1:].lower() for flag in flags if flag.startswith("-")}
    assert passed <= parameters
    assert passed == {"ymm4", "project", "output", "timeoutseconds", "nocompressor"}


_POWERSHELL = shutil.which("powershell.exe")
_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32" or _POWERSHELL is None, reason="Windows PowerShell が要る"
)


@_WINDOWS_ONLY
def test_the_script_parses_in_windows_powershell() -> None:
    command = (
        "$errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{SCRIPT}', [ref]$null, [ref]$errors); "
        "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout.decode("cp932", errors="replace")


@_WINDOWS_ONLY
def test_the_script_loads_its_types_and_fails_cleanly_without_ymm4(
    tool: ModuleType, tmp_path: Path
) -> None:
    """読めるだけでなく、型を引く所まで走る 名前の違う exe を渡すので YMM4 は起動しない

    Add-Type より前に UI Automation の型を書くと、ここで「型が見つからない」で落ちる
    """
    missing = tmp_path / "NotYmm4AtAll.exe"
    earlier = tmp_path / "a.mp4"
    earlier.write_bytes(b"earlier")
    leftover = tmp_path / "a.part.mp4"
    leftover.write_bytes(b"leftover")
    command = tool.powershell_command(
        missing, tmp_path / "a.ymmp", earlier, no_compressor=True, timeout=5
    )
    completed = subprocess.run(command, capture_output=True, check=False, timeout=120)
    lines = [tool.decode_line(raw) for raw in completed.stdout.splitlines()]
    assert completed.returncode == 1, lines
    assert any("書き出せませんでした" in line for line in lines), lines
    # 書き出せなかったので、前の書き出しはそのまま残る 先に消すと失敗で前の物まで失う
    assert earlier.read_bytes() == b"earlier", lines
    # 前に失敗した一時の書き出しは消える 残ると保存の窓が上書きを尋ねて止まる
    assert not leftover.exists(), lines
    # 変える前に落ちたので、戻す所は通らない（通れば YMM4 の無い所で戻そうとして 2 になる）
    assert not any("戻せませんでした" in line for line in lines), lines


@_WINDOWS_ONLY
def test_an_open_program_found_by_the_script_keeps_the_previous_export(
    tool: ModuleType, tmp_path: Path
) -> None:
    """PowerShell 側でも開いていると分かったら、何も消さずに止まる

    YMM4 は起動しない 動いているのが確かな powershell.exe（この試験が走らせている物）を
    YMM4 の代わりに名指しする
    """
    earlier = tmp_path / "a.mp4"
    earlier.write_bytes(b"earlier")
    leftover = tmp_path / "a.part.mp4"
    leftover.write_bytes(b"leftover")
    command = tool.powershell_command(
        Path("powershell.exe"), tmp_path / "a.ymmp", earlier, no_compressor=False, timeout=5
    )
    completed = subprocess.run(command, capture_output=True, check=False, timeout=120)
    lines = [tool.decode_line(raw) for raw in completed.stdout.splitlines()]
    assert completed.returncode == 1, lines
    assert any("既に開いています" in line for line in lines), lines
    assert earlier.read_bytes() == b"earlier"
    assert leftover.read_bytes() == b"leftover"


#: PowerShell 自身に構文木を作らせ、関数ごとの本体を JSON で返させる 括弧を自前で数えると、
#: 文字列やコメントの中の括弧・行の頭に置いた入れ子の閉じ括弧で本体の終わりを取り違える
_BODIES = r"""
$errors = $null
$language = 'System.Management.Automation.Language'
$tree = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:SASHIMONO_PS1, [ref]$null, [ref]$errors)
if ($errors.Count) { exit 1 }
$bodies = @{}
$isFunction = { param($node) $node -is ($language + '.FunctionDefinitionAst' -as [type]) }
foreach ($found in $tree.FindAll($isFunction, $true)) {
    $bodies[$found.Name] = $found.Body.Extent.Text
}
$bytes = [System.Text.Encoding]::UTF8.GetBytes(($bodies | ConvertTo-Json -Compress))
$out = [Console]::OpenStandardOutput()
$out.Write($bytes, 0, $bytes.Length)
"""


def _function_bodies(script: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _BODIES],
        capture_output=True,
        check=False,
        env={**os.environ, "SASHIMONO_PS1": str(script)},
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr.decode("cp932", errors="replace")
    bodies: dict[str, str] = json.loads(completed.stdout.decode("utf-8"))
    return bodies


#: 起動した process の窓だけを相手にする関数
_WINDOW_FUNCTIONS = ("Get-TopWindows", "Wait-MainWindow", "Close-Ymm4")


def _counting_by_name(script: Path) -> list[str]:
    bodies = _function_bodies(script)
    return [name for name in _WINDOW_FUNCTIONS if "Get-Process" in bodies[name]]


@_WINDOWS_ONLY
def test_only_the_windows_of_the_started_ymm4_are_touched() -> None:
    """名前で process を数えると、道具が動いている間に本人が開いた YMM4 まで操作して閉じる

    起動した process の Id の窓だけを探し、閉じたかもその process で見る
    """
    assert _counting_by_name(SCRIPT) == []
    assert "$script:Ymm4Process.Id" in _function_bodies(SCRIPT)["Get-TopWindows"]
    assert "$script:Ymm4Process = Start-Process" in SCRIPT.read_text(encoding="utf-8-sig")


@_WINDOWS_ONLY
def test_counting_by_name_late_in_a_function_is_still_caught(tmp_path: Path) -> None:
    """関数の後半に戻ってきた名前での数え方も見落とさない

    行の頭の閉じ括弧で本体を切ると、その後ろに足した ``Get-Process -Name`` が検査から外れた
    """
    text = SCRIPT.read_text(encoding="utf-8-sig")
    late = "\nif ($false) {\n}\n    [void](Get-Process -Name $ProcessName)\n"
    for name, anchor in (
        ("Wait-MainWindow", '    throw "YMM4 の下の帯の'),
        ("Close-Ymm4", '    throw "YMM4 が $DialogSeconds 秒で閉じない'),
    ):
        start = text.index(f"function {name} ")
        at = text.index(anchor, start)
        text = text[:at] + late + text[at:]
    tampered = tmp_path / "tampered.ps1"
    tampered.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    assert _counting_by_name(tampered) == ["Wait-MainWindow", "Close-Ymm4"]


@_WINDOWS_ONLY
def test_the_export_is_written_under_the_temporary_name() -> None:
    """出力の名前へ直に書くと、前の書き出しを先に消すことになり、失敗で前の物まで失う

    保存の名前・書き手が手放したかの確かめ・大きさの見張りは一時の名前で行う
    """
    bodies = _function_bodies(SCRIPT)
    for name in ("Save-As", "Test-Released", "Wait-Written"):
        assert "$Partial" in bodies[name], name
        assert "$Output" not in bodies[name], name
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert "'.part.mp4'" in text
    assert "Move-Item -LiteralPath $Partial -Destination $Output" in text
