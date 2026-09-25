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

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "ymm4_export.ps1"
#: 見本のプロジェクトの長さ 最後のアイテムの終わり
PROJECT_FRAMES = 6


def _project(path: Path, spans: list[tuple[int, int]], *, length: int | None = None) -> None:
    """アイテムの（始まり, 長さ）を並べた .ymmp を書く

    タイムラインの ``Length`` は YMM4 の実物と同じく、アイテムの終わりより余分に取る
    """
    items = [{"Frame": frame, "Length": span, "Layer": 0} for frame, span in spans]
    end = max((frame + span for frame, span in spans), default=0)
    timeline = {
        "VideoInfo": {"FPS": 30, "Hz": 48000, "Width": 32, "Height": 32},
        "Items": items,
        "Length": end + 6 if length is None else length,
    }
    document = {"SelectedTimelineIndex": 0, "Timelines": [timeline]}
    path.write_text(json.dumps(document), encoding="utf-8-sig")


def _video(path: Path, frames: int, fps: int = 30, *, faststart: bool = False) -> None:
    """YMM4 の書き出しの代わりの短い mp4 数えるのはコマ数なので、絵は小さい黒で足りる

    ``faststart`` は目次（moov）を頭へ置く 後ろを切っても開けて、目次のコマ数だけが残る
    """
    import av

    path.parent.mkdir(parents=True, exist_ok=True)
    options = {"movflags": "faststart"} if faststart else {}
    with av.open(str(path), "w", format="mp4", options=options) as container:
        stream = container.add_stream("mpeg4", rate=fps)
        stream.width = 32
        stream.height = 32
        stream.pix_fmt = "yuv420p"
        # 目次を頭に置くときは、後ろを切るので 1 コマずつの大きさが要る 黒だと数十バイトで、
        # どこで切ってもほぼ全部のコマが残るか 1 つも残らない
        rng = np.random.default_rng(0)
        for _ in range(frames):
            if faststart:
                pixels = rng.integers(0, 256, (32, 32, 3), dtype=np.uint8)
            else:
                pixels = np.zeros((32, 32, 3), dtype=np.uint8)
            picture = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            for packet in stream.encode(picture):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


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
    _project(project, [(0, 4), (2, PROJECT_FRAMES - 2)])
    state: dict[str, Any] = {
        "exe": exe,
        "project": project,
        "output": tmp_path / "out" / "probe.mp4",
        "commands": [],
        "result": (0, False),
        "writes": True,
        "frames": PROJECT_FRAMES,
        "fps": 30,
    }

    def run_script(command: list[str], limit: int) -> tuple[int, bool]:
        state["commands"].append(command)
        state["limit"] = limit
        state["existed"] = state["output"].exists()
        if state["writes"]:
            _video(state["output"], state["frames"], state["fps"])
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
    """探した所を出さないと、本人は ``--ymm4`` に何を渡せばよいか分からない"""
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


def test_an_export_that_stopped_partway_is_a_failure(
    tool: ModuleType, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """YMM4 が途中で書くのを止めても、PowerShell は書き終わりを待てたと言う（Issue #210）

    成功と言うと、探りの measure が途中で切れた動画を測り、後ろの枠を「YMM4 が何も描かない」
    と読む 何コマ目で止まったかが出ないと、止めたアイテムを探せない
    """
    ready["frames"] = PROJECT_FRAMES - 2
    assert tool.main(_arguments(ready)) == 1
    out = capsys.readouterr().out
    assert f"{PROJECT_FRAMES - 2} / {PROJECT_FRAMES} コマ" in out
    assert "途中で止まりました" in out
    # 出力の名前に残すと、次の measure が途中で切れた物を測る 名前を変えて見られるようにする
    assert not ready["output"].exists()
    [short] = ready["output"].parent.glob("probe.sashimono-*.short.mp4")
    assert str(short) in out


def test_an_export_as_long_as_the_project_is_a_success(
    tool: ModuleType, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """揃っているのに失敗と言うと、書き出せた物まで退けてしまう"""
    assert tool.main(_arguments(ready)) == 0
    assert ready["output"].is_file()
    assert f"{PROJECT_FRAMES} / {PROJECT_FRAMES} コマ" in capsys.readouterr().out


def test_an_export_at_another_frame_rate_is_counted_by_time(
    tool: ModuleType, ready: dict[str, Any]
) -> None:
    """書き出しの窓の fps がプロジェクトと違っても、長さが揃っていれば止まってはいない

    コマ数をそのまま比べると、15fps の書き出しは揃っていても足りないと出る
    """
    ready["fps"] = 15
    ready["frames"] = PROJECT_FRAMES // 2
    assert tool.main(_arguments(ready)) == 0
    ready["frames"] = PROJECT_FRAMES // 2 - 1
    assert tool.main(_arguments(ready)) == 1


def test_an_export_that_cannot_be_read_is_a_failure(
    tool: ModuleType,
    ready: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """書き終えずに止まった mp4 は目次（moov）が無く開けない 長さを測れないのに成功と言わない"""

    def run_script(command: list[str], limit: int) -> tuple[int, bool]:
        ready["output"].parent.mkdir(parents=True, exist_ok=True)
        ready["output"].write_bytes(b"not an mp4")
        return 0, False

    monkeypatch.setattr(tool, "run_script", run_script)
    assert tool.main(_arguments(ready)) == 1
    out = capsys.readouterr().out
    assert "読めません" in out
    # 出力の名前に残すと、次の書き出しが置き換えて、壊れ方を確かめる物が無くなる
    assert not ready["output"].exists()
    [aside] = ready["output"].parent.glob("probe.sashimono-*.short.mp4")
    assert aside.read_bytes() == b"not an mp4"
    assert str(aside) in out


def test_an_export_whose_index_outruns_its_pictures_is_a_failure(
    tool: ModuleType,
    ready: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目次（moov）のコマ数を信じると、後ろの絵が欠けた動画を揃っていると読む

    目次を頭に置いた mp4 の後ろを切り落とすと、目次は 6 コマと言うが、復号できるのは
    切った所まで 数えるのは復号できたコマ
    """

    def run_script(command: list[str], limit: int) -> tuple[int, bool]:
        _video(ready["output"], PROJECT_FRAMES, faststart=True)
        data = ready["output"].read_bytes()
        # 絵の塊（mdat）の半ばで切る 目次より前で切ると、開けない方の失敗になる
        start = data.index(b"mdat")
        ready["output"].write_bytes(data[: start + (len(data) - start) // 2])
        return 0, False

    monkeypatch.setattr(tool, "run_script", run_script)
    assert tool.main(_arguments(ready)) == 1
    assert "途中で止まりました" in capsys.readouterr().out


def test_a_last_frame_left_short_at_another_rate_is_caught(
    tool: ModuleType, ready: dict[str, Any]
) -> None:
    """換算したコマ数を切り捨てると、終わりの 1 コマ足りない書き出しを揃っていると読む

    30fps で 5 コマ（1/6 秒）のプロジェクトを 15fps で書くと 2.5 コマ 2 コマでは 1/30 秒足りない
    """
    _project(ready["project"], [(0, 5)])
    ready["fps"] = 15
    ready["frames"] = 2
    assert tool.main(_arguments(ready)) == 1
    ready["frames"] = 3
    assert tool.main(_arguments(ready)) == 0


@pytest.mark.parametrize("value", [None, "abc", [1]])
def test_a_project_with_a_broken_item_is_refused_without_a_traceback(
    tool: ModuleType, ready: dict[str, Any], capsys: pytest.CaptureFixture[str], value: Any
) -> None:
    """JSON として読めても値が数でなければ、案内の無い traceback で終わっていた"""
    document = json.loads(ready["project"].read_text(encoding="utf-8-sig"))
    document["Timelines"][0]["Items"][0]["Frame"] = value
    ready["project"].write_text(json.dumps(document), encoding="utf-8-sig")
    assert tool.main(_arguments(ready)) == 1
    assert "長さ" in capsys.readouterr().out
    assert ready["commands"] == []


@pytest.mark.parametrize("span", [None, 0])
def test_an_item_without_a_length_still_takes_one_frame(
    tool: ModuleType, tmp_path: Path, span: int | None
) -> None:
    """``Length`` が無いか 0 のアイテムを 0 コマと数えると、終わりの 1 コマ欠けた書き出しを通す

    読み込み（``compat/ymm4/template.py``）も比べる道具も、欠けた ``Length`` を 1 コマとして読む
    """
    project = tmp_path / "a.ymmp"
    _project(project, [(0, 4)])
    document = json.loads(project.read_text(encoding="utf-8-sig"))
    last: dict[str, Any] = {"Frame": 5, "Layer": 1}
    if span is not None:
        last["Length"] = span
    document["Timelines"][0]["Items"].append(last)
    project.write_text(json.dumps(document), encoding="utf-8-sig")
    assert tool.project_length(project) == (6, 30)


def test_the_project_length_is_the_end_of_the_last_item(tool: ModuleType, tmp_path: Path) -> None:
    """タイムラインの ``Length`` で数えると、揃った書き出しまで足りないと言う

    YMM4 が書き出すのは最後のアイテムの終わりまで 手元に残る実物の書き出し 20 本では、
    コマ数がアイテムの終わりと同じで、タイムラインの ``Length`` より 6〜30 少なかった
    """
    project = tmp_path / "a.ymmp"
    _project(project, [(0, 300), (250, 404)], length=660)
    assert tool.project_length(project) == (654, 30)


def test_a_project_without_items_is_refused_before_ymm4_starts(
    tool: ModuleType, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """長さが分からないまま書き出すと、止まったかどうかを確かめられない"""
    _project(ready["project"], [])
    assert tool.main(_arguments(ready)) == 1
    assert "長さ" in capsys.readouterr().out
    assert ready["commands"] == []


def test_a_folder_named_like_the_output_is_refused_before_ymm4_starts(
    tool: ModuleType, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """通すと、書き出しがフォルダの中へ入り、指定の場所には何もできないまま失敗する"""
    ready["output"].mkdir(parents=True)
    assert tool.main(_arguments(ready)) == 1
    assert "フォルダです" in capsys.readouterr().out
    assert ready["commands"] == []


def test_a_project_that_is_not_ymmp_is_refused(
    tool: ModuleType, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """テンプレート（.ymmt）などを渡すと YMM4 が別の窓を出し、帯の名前を待ったまま止まる"""
    other = ready["project"].with_suffix(".ymmt")
    other.write_bytes(b"")
    assert tool.main(["--project", str(other), "--output", str(ready["output"])]) == 1
    assert ".ymmp ではありません" in capsys.readouterr().out
    assert ready["commands"] == []


@pytest.mark.parametrize("seconds", ["0", "-5"])
def test_a_timeout_that_cannot_wait_is_refused(tool: ModuleType, seconds: str) -> None:
    """0 秒以下を通すと、書き出しが始まる前に「終わらない」で止まり、YMM4 だけが残る"""
    with pytest.raises(SystemExit):
        tool.parse_arguments(["--project", "a.ymmp", "--output", "a.mp4", "--timeout", seconds])


def test_the_association_command_gives_the_exe(tool: ModuleType) -> None:
    """読み違えると、関連付けのある機械でも「見つかりません」になり、毎回 ``--ymm4`` が要る"""
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
    """YMM4 は zip を好きな所へ展開する 関連付けを見ないと、決まった置き場以外で見つからない"""
    _no_common_places(tool, monkeypatch, tmp_path)
    exe = tmp_path / "anywhere" / "YukkuriMovieMaker.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    found, _ = tool.find_ymm4(None, {}, association=lambda: f'"{exe}" "%1"')
    assert found == exe


def test_a_common_place_is_used_when_nothing_is_registered(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """関連付けを登録していない機械では、よく見る置き場に置いてあっても見つからなくなる"""
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
    """探した先を返さないと、見つからないときの案内が空になり、どこを直せばよいか分からない"""
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
    """中継しないと、本人は YMM4 がどこで止まったのか、何を戻せばよいのかを読めない"""
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
    """構文が壊れると、YMM4 を開く前に PowerShell が落ち、書き出しは 1 本もできない"""
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
    # 本人がたまたま持っている、一時の名前に似た普通の動画
    own = tmp_path / "a.part.mp4"
    own.write_bytes(b"own video")
    command = tool.powershell_command(
        missing, tmp_path / "a.ymmp", earlier, no_compressor=True, timeout=5
    )
    names = []
    for _ in range(2):
        completed = subprocess.run(command, capture_output=True, check=False, timeout=120)
        lines = [tool.decode_line(raw) for raw in completed.stdout.splitlines()]
        assert completed.returncode == 1, lines
        assert any("書き出せませんでした" in line for line in lines), lines
        # 変える前に落ちたので、戻す所は通らない（通れば YMM4 の無い所で戻そうとして 2 になる）
        assert not any("戻せませんでした" in line for line in lines), lines
        names += [line.split(" ", 1)[1] for line in lines if line.startswith("一時の名前 ")]
    # 書き出せなかったので、前の書き出しはそのまま残る 先に消すと失敗で前の物まで失う
    assert earlier.read_bytes() == b"earlier"
    # 道具が作った物か分からないファイルは触らない
    assert own.read_bytes() == b"own video"
    # 印は毎回変わる 同じ名前を使い回すと、前の回の残りと今回の物を取り違える
    assert len(names) == 2 and names[0] != names[1], names
    for name in names:
        assert re.fullmatch(r"a\.sashimono-[0-9a-f]{8}\.part\.mp4", Path(name).name), name
        assert Path(name).parent == tmp_path
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a.mp4", "a.part.mp4"]


@_WINDOWS_ONLY
def test_the_script_refuses_a_folder_named_like_the_output(
    tool: ModuleType, tmp_path: Path
) -> None:
    """入口を通らずに呼ばれても、YMM4 を起こす前に断る 通すと書き出しがフォルダの中へ入る

    名前の違う exe を渡すので、ここで止まらなければ起動の失敗で終わり、案内が出ない
    """
    folder = tmp_path / "a.mp4"
    folder.mkdir()
    command = tool.powershell_command(
        tmp_path / "NotYmm4AtAll.exe", tmp_path / "a.ymmp", folder, no_compressor=False, timeout=5
    )
    completed = subprocess.run(command, capture_output=True, check=False, timeout=120)
    lines = [tool.decode_line(raw) for raw in completed.stdout.splitlines()]
    assert completed.returncode == 1, lines
    assert any("フォルダです" in line for line in lines), lines
    assert not any("起動します" in line for line in lines), lines
    assert list(folder.iterdir()) == []


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
    assert "Publish-Export $Partial $Output" in text
    assert "Move-Item -LiteralPath $From -Destination $To" in bodies["Publish-Export"]


#: 置き換えと片付けを本物の関数で走らせる 出力の名前のファイルを共有なしで掴んでおき、
#: 動画プレーヤーが開いているときと同じく置き換えを失敗させる
_PUBLISH = """
$script:StandardOutput = [Console]::OpenStandardOutput()
function Say([string]$Text) {SAY}
function Publish-Export {PUBLISH}
function Clear-Unfinished {CLEAR}
$written = [bool]::Parse($env:SASHIMONO_WRITTEN)
$code = 0
$lock = [System.IO.File]::Open($env:SASHIMONO_OUTPUT, 'Open', 'Read', 'None')
try {
    if ($written) { Publish-Export $env:SASHIMONO_PARTIAL $env:SASHIMONO_OUTPUT }
} catch {
    Say "書き出せませんでした $($_.Exception.Message)"
    $code = 1
} finally {
    $lock.Dispose()
}
if (-not $written) { $code = 1 }
Clear-Unfinished $env:SASHIMONO_PARTIAL $written $code
exit $code
"""


def _publish(tool: ModuleType, tmp_path: Path, *, written: bool) -> tuple[Path, Path, list[str]]:
    bodies = _function_bodies(SCRIPT)
    run = (
        _PUBLISH.replace("{SAY}", bodies["Say"])
        .replace("{PUBLISH}", bodies["Publish-Export"])
        .replace("{CLEAR}", bodies["Clear-Unfinished"])
    )
    output = tmp_path / "a.mp4"
    output.write_bytes(b"earlier")
    partial = tmp_path / "a.sashimono-0123abcd.part.mp4"
    partial.write_bytes(b"finished export")
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", run],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "SASHIMONO_OUTPUT": str(output),
            "SASHIMONO_PARTIAL": str(partial),
            "SASHIMONO_WRITTEN": str(written),
        },
        timeout=120,
    )
    lines = [tool.decode_line(raw) for raw in completed.stdout.splitlines()]
    assert completed.returncode == 1, lines
    return output, partial, lines


@_WINDOWS_ONLY
def test_a_failed_replace_keeps_the_finished_export_and_says_where(
    tool: ModuleType, tmp_path: Path
) -> None:
    """置き換えに失敗しただけで書き終えた物を消すと、本人は書き出しからやり直すことになる"""
    output, partial, lines = _publish(tool, tmp_path, written=True)
    # 途中で落ちた残り（.part.mp4）と同じ名前の形で残すと、あとで見つけた人が消してしまう
    done = tmp_path / "a.sashimono-0123abcd.done.mp4"
    assert not partial.exists(), lines
    assert done.read_bytes() == b"finished export", lines
    assert output.read_bytes() == b"earlier", lines
    assert any("置き換えられませんでした" in line for line in lines), lines
    assert any(str(done) in line and "残してあります" in line for line in lines), lines


@_WINDOWS_ONLY
def test_an_unfinished_export_is_still_thrown_away(tool: ModuleType, tmp_path: Path) -> None:
    """書き終えなかった一時の書き出しを残すと、途中で切れた動画が置き場に溜まる"""
    output, partial, lines = _publish(tool, tmp_path, written=False)
    assert not partial.exists(), lines
    assert output.read_bytes() == b"earlier", lines


@_WINDOWS_ONLY
def test_the_temporary_name_steps_around_a_file_that_already_exists(tmp_path: Path) -> None:
    """重なる名前を選ぶと、元からあるファイルを上書きするか、消してから書くことになる

    印を決め打ちで返し、1 つ目の名前に本人のファイルを置く 2 つ目の印の名前が選ばれ、
    置いたファイルはそのまま残る
    """
    body = _function_bodies(SCRIPT)["New-PartialName"]
    taken = tmp_path / "foo.sashimono-aaaaaaaa.part.mp4"
    taken.write_bytes(b"own video")
    run = "\n".join(
        [
            f"function New-PartialName {body}",
            "$marks = [System.Collections.Queue]::new(@('aaaaaaaa', 'bbbbbbbb'))",
            "$chosen = New-PartialName $env:SASHIMONO_OUTPUT { $marks.Dequeue() }",
            "$bytes = [System.Text.Encoding]::UTF8.GetBytes($chosen)",
            "[Console]::OpenStandardOutput().Write($bytes, 0, $bytes.Length)",
        ]
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", run],
        capture_output=True,
        check=False,
        env={**os.environ, "SASHIMONO_OUTPUT": str(tmp_path / "foo.mp4")},
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr.decode("cp932", errors="replace")
    chosen = Path(completed.stdout.decode("utf-8"))
    assert chosen == tmp_path / "foo.sashimono-bbbbbbbb.part.mp4"
    assert taken.read_bytes() == b"own video"


def test_the_same_rate_written_two_ways_is_not_a_frame_short(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """プロジェクトの 29.97 と書き出しの 30000/1001 は同じ fps 比で掛けると端数が切り上がる

    20 分（35964 コマ）の揃った書き出しを 1 コマ足りないと読み、出力を退けていた（PR #221）
    """
    from fractions import Fraction

    output = tmp_path / "a.mp4"
    output.write_bytes(b"")
    monkeypatch.setattr(tool, "written_length", lambda _: (35964, Fraction(30000, 1001)))
    assert tool.check_length(output, 35964, Fraction(2997, 100))
    monkeypatch.setattr(tool, "written_length", lambda _: (35963, Fraction(30000, 1001)))
    monkeypatch.setattr(tool, "short_name", lambda path: tmp_path / "short.mp4")
    assert not tool.check_length(output, 35964, Fraction(2997, 100))


def test_a_slightly_different_rate_is_still_converted_by_time(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """30 と 30.001 は本当に違う fps 同じと見ると、終わりの 2 コマに届かない書き出しを通す

    NTSC の書き方の違いを割合の近さで揃えていたころは、1 万分の 1 より近い fps を
    時間に直さずに数えていた（PR #221）
    """
    from fractions import Fraction

    output = tmp_path / "a.mp4"
    output.write_bytes(b"")
    rate = Fraction(30001, 1000)
    monkeypatch.setattr(tool, "short_name", lambda path: tmp_path / "short.mp4")
    monkeypatch.setattr(tool, "written_length", lambda _: (36000, rate))
    assert not tool.check_length(output, 36000, Fraction(30))
    output.write_bytes(b"")
    monkeypatch.setattr(tool, "written_length", lambda _: (36002, rate))
    assert tool.check_length(output, 36000, Fraction(30))


@pytest.mark.parametrize(("project", "written"), [("23.976", 24000), ("59.94", 60000)])
def test_other_ntsc_rates_written_two_ways_are_the_same(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project: str, written: int
) -> None:
    """29.97 の他の NTSC の組も揃える 揃えないと端数の切り上げで 1 コマ足りないと読む"""
    from fractions import Fraction

    output = tmp_path / "a.mp4"
    output.write_bytes(b"")
    rate = Fraction(written, 1001)
    monkeypatch.setattr(tool, "written_length", lambda _: (35964, rate))
    assert tool.check_length(output, 35964, Fraction(project))


@pytest.mark.parametrize("fps", ["NaN", "Infinity", "true"])
def test_a_project_with_a_broken_rate_is_refused_without_a_traceback(
    tool: ModuleType, tmp_path: Path, fps: str
) -> None:
    """JSON の読み込みは NaN と Infinity を通し、true は int として通る

    そのまま分数へ直すと ValueError で止まっていた
    """
    project = tmp_path / "a.ymmp"
    _project(project, [(0, 4)])
    text = project.read_text(encoding="utf-8-sig").replace('"FPS": 30', f'"FPS": {fps}')
    project.write_text(text, encoding="utf-8-sig")
    assert tool.project_length(project) is None
