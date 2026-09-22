"""追加機能の実行環境を、ソフト内から導入する

字幕起こしも AI 連携も、依存が重い（前者は 2 GB 超、後者は Claude Code 本体を
要求する） これを最初から同梱すると、その機能を使わない人にまで負担させることに
なるので、**初期状態では未導入**とし、必要になった時点で画面のボタンから入れる

機能ごとに :class:`FeaturePack` を 1 つ定義する 導入の手順・状態の見せ方・ログの
流し方は全部の機能で同じなので、ここに 1 つだけ置く

パッケージ版（PyInstaller）では ``sys.executable`` がアプリ本体になり、そこへは
書き込めない その場合は ``--target`` で専用フォルダへ入れ、起動時にそのフォルダを
``sys.path`` へ足す :func:`activate_runtime` がその役目を負う
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

__all__ = [
    "FeaturePack",
    "PackStatus",
    "PackageStatus",
    "activate_runtime",
    "app_dir",
    "install_command",
    "install_runtime",
    "is_frozen",
    "pip_arguments",
    "run_pip",
    "runtime_target_dir",
]

#: 導入したものを置くフォルダの名前（パッケージ版のみ）
_RUNTIME_DIR = "runtime"


@dataclass(frozen=True, slots=True)
class PackageStatus:
    """1 つのパッケージの導入状況"""

    name: str
    version: str | None

    @property
    def installed(self) -> bool:
        return self.version is not None


@dataclass(frozen=True, slots=True)
class FeaturePack:
    """1 つの機能を動かすのに要るもの

    ``extra`` は「あると良いが無くても動く」もの 字幕起こしの CUDA ランタイムが
    これにあたり、外せば導入量を大きく減らせる
    """

    key: str
    label: str
    #: pip の指定 名前だけでも、バージョン条件付きでもよい
    required: tuple[str, ...]
    extra: tuple[str, ...] = ()
    #: ``extra`` を入れると何ができるようになるか
    extra_label: str = ""
    #: おおよその導入量（MB） 何が起きるかを先に見せるために使う
    size_mb: int = 0
    extra_size_mb: int = 0
    #: PATH 上に必要な外部コマンド pip では入らないものを表す
    commands: tuple[str, ...] = ()
    #: 外部コマンドが無いときの案内
    command_hint: str = ""
    #: コマンドの探し方 既定は PATH だけ PATH に載らない場所へ入る
    #: ものがあるので、機能ごとに差し替えられるようにしてある
    locate: Callable[[str], object | None] = shutil.which

    def status(self) -> PackStatus:
        return PackStatus(
            pack=self,
            packages=tuple(PackageStatus(n, _version(_name_of(n))) for n in self.required),
            extras=tuple(PackageStatus(n, _version(_name_of(n))) for n in self.extra),
            missing_commands=tuple(c for c in self.commands if self.locate(c) is None),
        )

    def requirements(self, *, extra: bool) -> tuple[str, ...]:
        return self.required + (self.extra if extra else ())


@dataclass(frozen=True, slots=True)
class PackStatus:
    """機能が動く状態にあるか"""

    pack: FeaturePack
    packages: tuple[PackageStatus, ...]
    extras: tuple[PackageStatus, ...] = ()
    #: PATH に見つからなかった外部コマンド
    missing_commands: tuple[str, ...] = field(default_factory=tuple)

    @property
    def installed(self) -> bool:
        """pip で入るものが揃っているか"""
        return all(p.installed for p in self.packages)

    @property
    def extra_installed(self) -> bool:
        return self.installed and all(p.installed for p in self.extras)

    @property
    def ready(self) -> bool:
        """実際に動かせるか 外部コマンドも含めて見る"""
        return self.installed and not self.missing_commands

    def missing(self, *, extra: bool) -> tuple[str, ...]:
        """まだ入っていないものの pip 指定"""
        pending = [p.name for p in self.packages if not p.installed]
        if extra:
            pending.extend(p.name for p in self.extras if not p.installed)
        return tuple(pending)

    def download_mb(self, *, extra: bool) -> int:
        total = self.pack.size_mb if not self.installed else 0
        if extra and not self.extra_installed:
            total += self.pack.extra_size_mb
        return total

    def summary(self) -> str:
        """画面に 1 行で出す説明"""
        if not self.installed:
            return "未導入 ここから環境を用意できます"
        if self.missing_commands:
            missing = "、".join(self.missing_commands)
            hint = f" {self.pack.command_hint}" if self.pack.command_hint else ""
            return f"導入済み ただし {missing} が見つかりません {hint}"
        if self.extras and not self.extra_installed:
            return f"導入済み {self.pack.extra_label}は入っていません"
        return "導入済み"


def _name_of(requirement: str) -> str:
    """``faster-whisper>=1.1`` のような指定から配布名だけを取り出す"""
    for separator in (">=", "<=", "==", "~=", ">", "<", "[", "!"):
        index = requirement.find(separator)
        if index > 0:
            return requirement[:index].strip()
    return requirement.strip()


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def is_frozen() -> bool:
    """PyInstaller などで固めた実行ファイルとして動いているか"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path | None:
    """配った zip を展開したフォルダ（``Kumiki.exe`` の置き場） 通常の実行では ``None``

    利用者が手で触る物（スクリプト置き場）はここに置く ``_internal`` の中は
    PyInstaller の持ち物で、更新のたびに丸ごと置き換わる
    """
    if not is_frozen():
        return None
    return Path(sys.executable).resolve().parent


def pip_arguments(argv: Sequence[str]) -> list[str] | None:
    """``Kumiki.exe -m pip ...`` と呼ばれたときの pip への引数 それ以外は ``None``

    パッケージ版には Python の本体が無い 導入ボタンは ``sys.executable -m pip`` を
    呼ぶが、パッケージ版の ``sys.executable`` は ``Kumiki.exe`` 自身なので、
    ここで受けて pip を動かさないと、**導入するつもりで Kumiki がもう 1 つ起動する**

    通常の実行では受けない ``sys.executable`` が本物の Python なので、そちらが
    pip を動かす
    """
    if not is_frozen():
        return None
    if list(argv[1:3]) != ["-m", "pip"]:
        return None
    return list(argv[3:])


def runtime_target_dir() -> Path | None:
    """導入先の専用フォルダ 通常の実行では ``None``（動いている環境へ直接入れる）"""
    if not is_frozen():
        return None
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "Kumiki" / _RUNTIME_DIR


def activate_runtime() -> Path | None:
    """専用フォルダへ入れたものを import できるようにする

    起動時に 1 度呼ぶ 通常の実行では何もしない
    """
    target = runtime_target_dir()
    if target is None or not target.exists():
        return None
    path = str(target)
    if path not in sys.path:
        # 先頭へ入れる 同名の古いものが同梱されていた場合に、あとから入れた方を
        # 使わせるため
        sys.path.insert(0, path)
    return target


def run_pip(arguments: Sequence[str]) -> int:
    """配布版の中で pip を走らせる :func:`pip_arguments` が受けたときに使う

    pip が中で使う distlib は、同梱の部品（``t64.exe`` など）を
    **読み込み方式ごとの探し方**で見つける PyInstaller の読み込み方式は
    distlib の一覧に無いので、``pip install`` は部品を探す所で落ちる
    （``pip --version`` は部品を探さないので通ってしまう 実物で確かめた）

    配布版では部品が ``_internal`` の下にファイルとして置いてあるので、
    ファイルとして探す方式を割り当てれば見つかる
    """
    from pip._vendor import distlib
    from pip._vendor.distlib import resources

    loader = getattr(distlib, "__loader__", None)
    if loader is not None:
        # distlib は型を配っていない 呼び方は distlib 0.3 系の resources.py で確かめた
        resources.register_finder(loader, resources.ResourceFinder)  # type: ignore[no-untyped-call]

    from pip._internal.cli.main import main as pip_main

    return int(pip_main(list(arguments)))


def install_command(
    pack: FeaturePack, *, extra: bool = True, upgrade: bool = False, python: str | None = None
) -> list[str]:
    """導入に使う ``pip`` のコマンド列を組み立てる

    実行せずに文字列として得られるようにしてあるのは、画面に「これを実行します」と
    出すため 何が入るのか分からないままダウンロードが始まるのは不安が大きい
    """
    command = [python or sys.executable, "-m", "pip", "install"]
    if upgrade:
        command.append("--upgrade")
    target = runtime_target_dir()
    if target is not None:
        command.extend(["--target", str(target)])
    command.extend(pack.requirements(extra=extra))
    return command


def install_runtime(
    pack: FeaturePack | None = None,
    *,
    extra: bool = True,
    on_output: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    command: Sequence[str] | None = None,
) -> int:
    """``pip`` を子プロセスで走らせる 戻り値は終了コード（0 が成功）

    出力は 1 行ずつ ``on_output`` へ渡す まとめて最後に渡すと、数分間なにも
    起きていないように見える
    """
    if command is None:
        if pack is None:
            raise ValueError("pack か command のどちらかが要る")
        command = install_command(pack, extra=extra)
    argv = list(command)
    if on_output is not None:
        on_output("> " + " ".join(argv))

    target = runtime_target_dir()
    if target is not None:
        target.mkdir(parents=True, exist_ok=True)

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # 子プロセスの出力を UTF-8 に揃える Windows の既定は cp932 で、素材やユーザー名に
    # 日本語が入っているとログが文字化けし、失敗の原因が読めなくなる
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        # 引数はここで組み立てたものだけで、shell も通さない
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flags,
            env=child_env,
        )
    except OSError as exc:
        if on_output is not None:
            on_output(f"pip を起動できない: {exc}")
        return 1

    assert process.stdout is not None
    with process:
        for line in process.stdout:
            if on_output is not None:
                on_output(line.rstrip())
            if should_cancel is not None and should_cancel():
                process.terminate()
                if on_output is not None:
                    on_output("中断した")
                break
    return process.returncode if process.returncode is not None else 1
