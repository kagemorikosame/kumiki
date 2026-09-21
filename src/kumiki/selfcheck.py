r"""同梱した部品が、この機械で本当に動くかを確かめる

    Kumiki.exe --self-check
    .venv\Scripts\python.exe -m kumiki --self-check

画面は出さない 1 項目 1 行で結果を出し、どれか 1 つでも動かなければ終了コード 1

配った zip で一番起きやすい失敗は「組み立てたときに部品を積み忘れた」
（動的に読み込む Lua の実体、FFmpeg の DLL、Qt の GL プラグイン）
起動しただけでは分からない 描いて・書き出して・走らせて、初めて分かる
使う人の機械で確かめてもらうときも、この 1 行を打ってもらえば足りる

どの項目も**実際の道を通す** 読み込めるかだけを見ると、DLL が足りないのに
「読み込めた」と通ってしまう（読み込みは遅延で、呼んだ時点で初めて探しに行く）
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

__all__ = ["CheckResult", "format_results", "run_self_check"]

#: 見本を描く大きさ 小さくてよい 動くかどうかだけを見る
#: 偶数にする h.264 は幅も高さも偶数でないと符号化できない
SAMPLE_SIZE = 64


@dataclass(frozen=True, slots=True)
class CheckResult:
    """1 項目の結果"""

    name: str
    #: 動いたか
    ok: bool
    detail: str = ""
    #: 動かなくても困らない項目（音の出口が無い機械など） 合否に混ぜない
    optional: bool = False


def run_self_check() -> list[CheckResult]:
    """全項目を順に確かめる 途中で落ちても、残りは続ける

    1 つの失敗で止めると、ほかに何が足りないのかが分からないまま
    組み立て直しを繰り返すことになる
    """
    checks: list[tuple[str, Callable[[], str], bool]] = [
        ("版", _version, False),
        ("Qt", _qt, False),
        ("編集画面を組み立てる", _editor, False),
        ("GL で描く", _render, False),
        ("書き出す（FFmpeg）", _export, False),
        ("AviUtl スクリプト（Lua）", _lua, False),
        ("音の出口", _sound, True),
        ("スクリプト置き場", _script_roots, False),
        ("追加機能の導入（pip）", _pip, False),
    ]
    results = []
    for name, check, optional in checks:
        try:
            detail = check()
        except Exception as exc:
            last = traceback.extract_tb(exc.__traceback__)[-1:]
            where = f"（{Path(last[0].filename).name}:{last[0].lineno}）" if last else ""
            results.append(
                CheckResult(name, False, f"{type(exc).__name__}: {exc}{where}", optional)
            )
            continue
        results.append(CheckResult(name, True, detail, optional))
    return results


def format_results(results: list[CheckResult]) -> str:
    """結果を人が読める形に"""
    lines = []
    for result in results:
        mark = "ok" if result.ok else ("--" if result.optional else "NG")
        lines.append(f"[{mark}] {result.name}: {result.detail}")
    failed = [result for result in results if not result.ok and not result.optional]
    lines.append("すべて動いた" if not failed else f"動かない項目が {len(failed)} 個")
    return "\n".join(lines)


def main() -> int:
    results = run_self_check()
    print(format_results(results), flush=True)
    return 0 if all(result.ok or result.optional for result in results) else 1


def _version() -> str:
    from kumiki import __version__

    frozen = "配布版" if getattr(sys, "frozen", False) else "開発環境"
    return f"{__version__}（{frozen} Python {sys.version.split()[0]}）"


def _qt() -> str:
    from PySide6.QtCore import qVersion
    from PySide6.QtWidgets import QApplication

    # 画面は出さないが、GL のコンテキストは QGuiApplication が無いと作れない
    if QApplication.instance() is None:
        QApplication([sys.argv[0] if sys.argv else "kumiki"])
    return f"Qt {qVersion()}"


#: 編集画面が読み書きする、本人の置き場 確かめる間だけ一時フォルダへ向ける
USER_FOLDER_VARIABLES = ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_STATE_HOME")


@contextmanager
def _isolated_user_folders() -> Iterator[Path]:
    """本人の設定・退避・キャッシュの置き場を、確かめる間だけ一時フォルダへ向ける

    編集画面は閉じるときに**画面の並びを保存する** 見せていない窓を閉じると、
    本人が整えた並びを既定の並びで上書きしてしまう 退避と二重起動の印も同じ
    """
    saved = {name: os.environ.get(name) for name in USER_FOLDER_VARIABLES}
    with tempfile.TemporaryDirectory(prefix="kumiki-check-home-") as folder:
        for name in USER_FOLDER_VARIABLES:
            os.environ[name] = str(Path(folder) / name.lower())
        try:
            yield Path(folder)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _editor() -> str:
    """編集画面を、見せずに組み立てて閉じる

    部品を 1 つずつ動かす項目だけでは、**画面の側だけが読み込む部品**の
    積み忘れを見逃す（自己診断は通るのに、起動すると落ちる）
    """
    from PySide6.QtWidgets import QWidget

    from kumiki.core.model import Project
    from kumiki.ui.main_window import MainWindow

    with _isolated_user_folders():
        window = MainWindow(Project.create(), confirm_unsaved=False)
        try:
            parts = len(window.findChildren(QWidget))
        finally:
            window.close()
    return f"部品 {parts} 個"


def _sample_project() -> object:
    """文字と図形を 1 つずつ置いた見本 2 コマ

    文字を入れるのは、書体の読み込み（Qt のフォントの仕組み）まで通すため
    図形だけだとシェーダしか通らない
    """
    from kumiki.core.commands import AddClip, AddTrack
    from kumiki.core.model import (
        Clip,
        GeneratedSource,
        Project,
        ProjectSettings,
        Track,
        TrackKind,
    )
    from kumiki.core.timebase import FrameRate
    from kumiki.effects.sources import TEXT

    settings = ProjectSettings(width=SAMPLE_SIZE, height=SAMPLE_SIZE, frame_rate=FrameRate(30))
    project = Project.create(settings)
    below, above = Track(TrackKind.VIDEO, "V1"), Track(TrackKind.VIDEO, "V2")
    project = AddTrack(below).apply(project)
    project = AddTrack(above).apply(project)
    shape = GeneratedSource(
        kind="shape", params={"shape": "background", "color": (0.2, 0.4, 0.8, 1.0)}
    )
    project = AddClip(below.id, Clip(timeline_start=0, duration=2, source=shape)).apply(project)
    text = TEXT.create(text="木", size=32)
    return AddClip(above.id, Clip(timeline_start=0, duration=2, source=text)).apply(project)


def _render() -> str:
    import numpy as np

    from kumiki.core.model import Project
    from kumiki.engine.render import FrameRenderer

    project = _sample_project()
    assert isinstance(project, Project)
    renderer = FrameRenderer(project)
    try:
        image = renderer.render(0)
    finally:
        renderer.close()
    # 背景の青が出ていること 真っ黒なら、GL は動いたふりをして何も描いていない
    blue = int(np.median(image[..., 2]))
    if blue < 100:
        raise RuntimeError(f"描いた絵が暗すぎる（青 {blue}） シェーダが動いていない")
    return f"{image.shape[1]}x{image.shape[0]} を描けた"


def _export() -> str:
    import av

    from kumiki.core.model import Project
    from kumiki.engine.encode import ExportSettings, available_video_codecs, export_project

    codecs = available_video_codecs()
    if not codecs:
        raise RuntimeError("使える映像コーデックが 1 つも無い（FFmpeg を積み忘れている）")
    project = _sample_project()
    assert isinstance(project, Project)
    with tempfile.TemporaryDirectory(prefix="kumiki-check-") as folder:
        target = Path(folder) / "check.mp4"
        # 実際の書き出しと同じ道を通す 符号化だけを試すと、書き出しが使う
        # 合成・音の混ぜ・mux のどこかで足りないものを見逃す
        # 符号化器は CPU のものを使う 機械ごとに有無が違う GPU の符号化器で
        # 確かめると、同じ zip が機械によって通ったり落ちたりする
        cpu = "libx264" if "libx264" in codecs else codecs[-1]
        export_project(project, ExportSettings(path=target, video_codec=cpu))
        with av.open(str(target)) as container:
            frames = sum(1 for _ in container.decode(video=0))
    if frames < 1:
        raise RuntimeError("書き出した動画から 1 コマも読めない")
    return f"{cpu} で書き出して {frames} コマ読み戻せた（使える符号化器: {', '.join(codecs)}）"


def _lua() -> str:
    from kumiki.compat.aviutl.objapi import ObjectState
    from kumiki.compat.aviutl.runtime import LuaScriptRuntime, blank_image

    runtime = LuaScriptRuntime()
    state = ObjectState(image=blank_image(8, 8))
    # 値を書き換えるだけの 1 行 Lua の実体（lupa の中の DLL）が無いと、
    # ランタイムを作る所か、ここで落ちる
    result = runtime.run("obj.ox = 1 + 2", state, script="動作確認")
    if result.failed:
        raise RuntimeError(result.message)
    if state.ox != 3:
        raise RuntimeError(f"計算が合わない（{state.ox}）")
    return "スクリプトを走らせられた"


def _sound() -> str:
    import sounddevice

    # 出口が無い機械（音の装置を外した PC、リモート接続）でも編集はできる
    # 合否には混ぜないが、PortAudio の DLL が無いのは import の時点で分かる
    device = sounddevice.query_devices(kind="output")
    return f"{device['name']}"


def _script_roots() -> str:
    """見に行く場所と、**実際に読めた本数**

    場所を並べるだけでは、置いたのに読まれない（拡張子の見落とし・文字コード）を
    見逃す 置き場ごとに走査して数える
    """
    from kumiki.compat.aviutl.catalog import ScriptCatalog, default_script_roots
    from kumiki.compat.aviutl.report import CompatibilityReport

    roots = default_script_roots()
    if not roots:
        return "（見に行く場所が無い）"
    parts = []
    for root in roots:
        if not root.exists():
            parts.append(f"{root}（無い）")
            continue
        # 読めなかった記録を本体の記録へ混ぜない 自己診断で動いた跡が残ると、
        # 互換の穴の数え方が狂う
        found = ScriptCatalog((root,), report=CompatibilityReport()).scan()
        parts.append(f"{root}（{len(found)} 本）")
    return " / ".join(parts)


def _pip() -> str:
    """導入ボタンが使う pip が、この実行環境の中にあるか

    配布版には Python の本体が無いので、pip を積み忘れると導入ボタンが黙って動かない
    """
    from pip import __version__ as pip_version

    return f"pip {pip_version}"


if __name__ == "__main__":  # pragma: no cover - 入口は kumiki.app
    raise SystemExit(main())
