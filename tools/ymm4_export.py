r"""YMM4 に .ymmp を書き出させる（UI Automation で本体の画面を操作する）

    .venv\Scripts\python.exe tools\ymm4_export.py --project X.ymmp --output X.mp4
    .venv\Scripts\python.exe tools\ymm4_export.py --project X.ymmp --output X.mp4 --no-compressor

YMM4 との突き合わせ（``tools/ymm4_compare.py`` の各探り）は、YMM4 で書き出す所だけが
手作業だった これを本人の手を借りずに回すための道具（Issue #116）

**動かしている間は YMM4 の窓が前に出て、マウスとキーボードを取り合う** 本人に断ってから
走らせる YMM4 が既に開いていれば、作業中の物を壊さないように何もせず止まる

``--no-compressor`` は、書き出しの窓の〔音量調整 / 音割れ対策（コンプレッサー）〕を
「音量調整しない」「圧縮しない」にしてから書き出す 既定の「自動」のままだと音量の比が
潰れて、音の探りが測れない 終わったら成功でも失敗でも元の値へ戻し、開き直して確かめる
戻せなかったときは大きく知らせ、終了コード 2 で終わる

画面の操作は同梱の ``ymm4_export.ps1`` が受け持つ ここは引数を読み、走らせてよいかを
確かめ、PowerShell を呼んで出力を中継する

形を分けた理由 UI Automation は .NET の ``UIAutomationClient`` を PowerShell から
そのまま呼べ、2026-09-23 に実際に 3 回書き出せたのもこの形だった Python だけで書くには
``comtypes`` などを ``.venv`` へ足すことになり、この道具 1 つのために依存が増える
"""

from __future__ import annotations

import argparse
import io
import locale
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT = Path(__file__).with_name("ymm4_export.ps1")
EXE_NAME = "YukkuriMovieMaker.exe"
#: YMM4 の場所を教える環境変数 引数より弱く、関連付けや置き場より強い
ENVIRONMENT_KEY = "YMM4_EXE"
#: 書き出しを待つ長さの既定（秒） 探りはどれも数分で終わる 長いプロジェクトは引数で延ばす
DEFAULT_TIMEOUT = 1800
#: PowerShell が固まったときに見切るまでの上乗せ（秒） 起動と閉じる待ちの分
WATCHDOG_MARGIN = 600
#: コンプレッサーを戻せなかったときの終了コード（ymm4_export.ps1 と合わせる）
RESTORE_FAILED = 2
#: YMM4 は zip を好きな所へ展開して使うので、決まった置き場は無い よく見る所だけ当たる
FOLDER_NAMES = ("YukkuriMovieMaker_v4", "YukkuriMovieMaker4")
DRIVES = ("C:\\", "D:\\")
PARENT_FOLDERS = ("", "Program", "Programs", "Tools", "Soft")


def positive_seconds(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("1 秒以上を指定してください")
    return value


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", type=Path, required=True, help="書き出す .ymmp")
    parser.add_argument("--output", type=Path, required=True, help="書き出す先の .mp4")
    parser.add_argument(
        "--no-compressor",
        action="store_true",
        help="コンプレッサーと音量調整を切って書き出し、終わったら元へ戻す（音の探り用）",
    )
    parser.add_argument(
        "--ymm4",
        type=Path,
        default=None,
        help=(
            f"YukkuriMovieMaker.exe の場所"
            f"（既定は {ENVIRONMENT_KEY}・.ymmp の関連付け・よく見る置き場の順に探す）"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=positive_seconds,
        default=DEFAULT_TIMEOUT,
        help=f"書き出しを待つ秒数（既定 {DEFAULT_TIMEOUT}）",
    )
    return parser.parse_args(argv)


def exe_from_command(command: str) -> Path | None:
    """関連付けの開く命令（``"D:\\…\\YukkuriMovieMaker.exe" "%1"``）から exe を取り出す"""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        head = command[1:end] if end > 0 else ""
    else:
        head = command.split(" ", 1)[0]
    return Path(head) if head.lower().endswith(".exe") else None


def association_command() -> str | None:
    """.ymmp を開く命令 YMM4 が関連付けを登録していれば、置き場所をここから辿れる"""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, ".ymmp") as key:
            kind = str(winreg.QueryValue(key, None))
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{kind}\shell\open\command") as key:
            return str(winreg.QueryValue(key, None))
    except OSError:
        return None


def common_places(environ: Mapping[str, str]) -> list[Path]:
    """よく見る置き場 展開した場所は人それぞれなので、関連付けが無いときの控え"""
    bases = [
        Path(environ[name])
        for name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)")
        if environ.get(name)
    ]
    if environ.get("USERPROFILE"):
        profile = Path(environ["USERPROFILE"])
        bases += [profile / "Desktop", profile / "Downloads", profile / "Documents"]
    bases += [Path(drive) / parent for drive in DRIVES for parent in PARENT_FOLDERS]
    return [base / folder / EXE_NAME for base in bases for folder in FOLDER_NAMES]


def find_ymm4(
    explicit: Path | None,
    environ: Mapping[str, str],
    association: Callable[[], str | None] | None = None,
) -> tuple[Path | None, list[str]]:
    """YMM4 の exe と、見つからなかったときに案内する探した先

    引数や環境変数で名指しされた場所に無ければ、ほかを探さずに断る 黙って別の YMM4 を
    使うと、名指しした版と違う版で書き出した結果を突き合わせてしまう
    """
    if explicit is not None:
        return (explicit, []) if explicit.is_file() else (None, [f"--ymm4 {explicit}"])
    named = environ.get(ENVIRONMENT_KEY)
    if named:
        path = Path(named)
        return (path, []) if path.is_file() else (None, [f"{ENVIRONMENT_KEY}={named}"])
    looked: list[str] = []
    command = (association or association_command)()
    if command:
        path = exe_from_command(command)
        if path is not None and path.is_file():
            return path, []
        looked.append(f".ymmp の関連付け {command}")
    else:
        looked.append(".ymmp の関連付け（登録なし）")
    places = common_places(environ)
    for place in places:
        if place.is_file():
            return place, []
    looked += [str(place) for place in places]
    return None, looked


def ymm4_running(
    image: str, run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run
) -> bool:
    """同じ名前の YMM4 が動いているか

    ``tasklist`` の出力は言語で文言が変わる（見つからないときの案内は日本語の環境だと
    日本語） CSV の像名の欄だけを見る
    """
    completed = run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
        capture_output=True,
        check=False,
    )
    text = completed.stdout.decode(locale.getencoding(), errors="replace").lower()
    return f'"{image.lower()}"' in text


def powershell_command(
    ymm4: Path, project: Path, output: Path, *, no_compressor: bool, timeout: int
) -> list[str]:
    """ymm4_export.ps1 を呼ぶ命令

    ``-File`` で渡すと引数が 1 つずつ届く ``-Command`` に文字列で渡すと、パスの空白や
    ``$`` を PowerShell が読み替える ``-ExecutionPolicy Bypass`` はこの 1 回だけに効き、
    本人の実行ポリシーの設定は変えない
    """
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-Ymm4",
        str(ymm4),
        "-Project",
        str(project),
        "-Output",
        str(output),
        "-TimeoutSeconds",
        str(timeout),
    ]
    if no_compressor:
        command.append("-NoCompressor")
    return command


def decode_line(raw: bytes) -> str:
    """PowerShell の出力 1 行を文字へ直す

    ymm4_export.ps1 は自分の文言を UTF-8 で書くが、PowerShell 自身が出すエラーは
    Shift_JIS で来る 行ごとに UTF-8 で読み、読めなければ手元の文字コードで読む
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getencoding(), errors="replace")


def run_script(command: list[str], limit: int) -> tuple[int, bool]:
    """PowerShell を走らせて出力を中継する 終了コードと、見切って止めたかを返す

    保存の窓へ送った ``BM_CLICK`` が返らないなど、PowerShell ごと固まることがあり得る
    見切らないと、この道具を待つ側（探りを回す手順）まで止まったままになる
    """
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    expired = threading.Event()

    def expire() -> None:
        expired.set()
        process.kill()

    watchdog = threading.Timer(limit, expire)
    watchdog.start()
    try:
        assert process.stdout is not None
        for raw in process.stdout:
            print(decode_line(raw).rstrip("\r\n"), flush=True)
        code = process.wait()
    finally:
        watchdog.cancel()
    return code, expired.is_set()


def _alarm(lines: list[str]) -> None:
    bar = "!" * 60
    print(bar)
    for line in lines:
        print(f"!!! {line}")
    print(bar)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    if sys.platform != "win32":
        print("YMM4 は Windows でしか動かないので、この道具も Windows 専用です")
        return 1
    project: Path = arguments.project.resolve()
    if not project.is_file():
        print(f"{project} がありません")
        return 1
    if project.suffix.lower() != ".ymmp":
        print(f"{project} は .ymmp ではありません")
        return 1
    output: Path = arguments.output.resolve()
    if output.suffix.lower() != ".mp4":
        # 書き出しの窓は mp4 の窓（Mp4ConfigViewModel）を前提に手順を組んである
        print(f"{output} は .mp4 ではありません 書き出し先は .mp4 にしてください")
        return 1
    if output.is_dir():
        # 名前が .mp4 で終わるフォルダ 置き換えの Move-Item が書き出しをフォルダの中へ入れ、
        # 指定の場所には何もできないまま失敗する YMM4 を起こして書き出す前に断る
        print(f"{output} はフォルダです 書き出し先はファイルの名前にしてください")
        return 1
    ymm4, looked = find_ymm4(arguments.ymm4, os.environ)
    if ymm4 is None:
        print("YMM4（YukkuriMovieMaker.exe）が見つかりません 探した所")
        for place in looked:
            print(f"  {place}")
        print(f"--ymm4 か環境変数 {ENVIRONMENT_KEY} で場所を教えてください")
        return 1
    if ymm4_running(ymm4.name):
        # 開いているプロジェクトの上から別のプロジェクトを開くと、作業中の物を壊しかねない
        print("YMM4 が既に開いています 作業中の物を壊さないように止めました")
        print("YMM4 を閉じてから走らせてください")
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    # 前の書き出しは消さない ymm4_export.ps1 は一時の名前（<出力の名前>.sashimono-<印>.part.mp4）へ
    # 書き出し、書き終えたと確かめてから置き換える 途中で失敗すれば前の物が残る
    print("YMM4 を動かします 終わるまでマウスとキーボードに触らないでください")
    command = powershell_command(
        ymm4,
        project,
        output,
        no_compressor=arguments.no_compressor,
        timeout=arguments.timeout,
    )
    limit = arguments.timeout + WATCHDOG_MARGIN
    code, expired = run_script(command, limit)
    if expired:
        # 途中で止めたので、ymm4_export.ps1 の後始末（設定を戻す・閉じる）は走っていない
        lines = [
            f"PowerShell が {limit} 秒で終わらないので止めました",
            "YMM4 が開いたままかもしれません 画面を確かめてください",
        ]
        if arguments.no_compressor:
            lines.append(
                "コンプレッサーの設定が戻っていないかもしれません 書き出しの窓で確かめてください"
            )
        _alarm(lines)
        return RESTORE_FAILED if arguments.no_compressor else 1
    if code == RESTORE_FAILED:
        _alarm(
            [
                "コンプレッサーの設定を元へ戻せませんでした",
                "YMM4 を開き、〔ファイル〕→〔動画出力〕の",
                "〔音量調整 / 音割れ対策（コンプレッサー）〕を、",
                "上に出ている元の値へ手で戻してください",
            ]
        )
        return code
    if code != 0:
        return code
    if not output.is_file() or output.stat().st_size == 0:
        print(f"YMM4 は終わりましたが {output} ができていません")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
