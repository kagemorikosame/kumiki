"""字幕起こしの実行環境を、ソフト内から導入する。

音声認識の依存（faster-whisper と CUDA のランタイム）は合計で 2 GB を超える。
これを最初から同梱すると、字幕を使わない人にまで配布サイズを負担させることになる。
そこで**初期状態では未導入**とし、必要になった時点で画面のボタンから入れる。

導入は ``pip`` の子プロセスとして走らせる。出力は 1 行ずつ呼び出し側へ渡すので、
UI はそれをそのままログとして出せばよい。数分かかる処理なので、進んでいることが
見えないと止まったのと区別が付かない。

パッケージ版（PyInstaller）では ``sys.executable`` がアプリ本体になり、そこへは
書き込めない。その場合は ``--target`` で専用フォルダへ入れ、起動時にそのフォルダを
``sys.path`` へ足す。:func:`activate_runtime` がその役目を負う。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

__all__ = [
    "CUDA_PACKAGES",
    "REQUIRED_PACKAGES",
    "PackageStatus",
    "RuntimeStatus",
    "activate_runtime",
    "install_command",
    "install_runtime",
    "model_cache_dir",
    "runtime_status",
    "runtime_target_dir",
]

#: 起こしそのものに要るもの。
REQUIRED_PACKAGES: tuple[str, ...] = ("faster-whisper",)

#: GPU で動かすために要るもの。CTranslate2 は cuDNN と cuBLAS の DLL を実行時に
#: 探す。これが無いと CUDA を指定した瞬間に落ちる。無くても CPU では動く。
CUDA_PACKAGES: tuple[str, ...] = ("nvidia-cublas-cu12", "nvidia-cudnn-cu12")

#: pip に渡す指定。バージョンの下限だけ決め、上は開けておく。
_REQUIREMENTS: dict[str, str] = {
    "faster-whisper": "faster-whisper>=1.1",
    "nvidia-cublas-cu12": "nvidia-cublas-cu12",
    "nvidia-cudnn-cu12": "nvidia-cudnn-cu12>=9.1",
}

#: 導入したものを置くフォルダの名前。
_RUNTIME_DIR = "runtime"


@dataclass(frozen=True, slots=True)
class PackageStatus:
    """1 つのパッケージの導入状況。"""

    name: str
    version: str | None

    @property
    def installed(self) -> bool:
        return self.version is not None


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """起こしを動かせるかどうか。"""

    packages: tuple[PackageStatus, ...]
    cuda: tuple[PackageStatus, ...]

    @property
    def ready(self) -> bool:
        """CPU でよければ動くか。"""
        return all(p.installed for p in self.packages)

    @property
    def cuda_ready(self) -> bool:
        return self.ready and all(p.installed for p in self.cuda)

    def missing(self, *, cuda: bool) -> tuple[str, ...]:
        """まだ入っていないものの名前。"""
        pending = [p.name for p in self.packages if not p.installed]
        if cuda:
            pending.extend(p.name for p in self.cuda if not p.installed)
        return tuple(pending)

    def summary(self) -> str:
        """画面に 1 行で出す説明。"""
        if not self.ready:
            return "未導入。ここから環境を用意できます。"
        if not self.cuda_ready:
            return "導入済み（CPU）。GPU で動かすには CUDA ランタイムが要ります。"
        return "導入済み（GPU）。"


def runtime_status() -> RuntimeStatus:
    """いま何が入っているかを調べる。

    パッケージを import せずに配布メタデータだけを見る。faster-whisper の
    import は数秒かかるうえ、CUDA の DLL 探索まで走るので、状態確認のために
    やってよい重さではない。
    """
    return RuntimeStatus(
        packages=tuple(PackageStatus(name, _version(name)) for name in REQUIRED_PACKAGES),
        cuda=tuple(PackageStatus(name, _version(name)) for name in CUDA_PACKAGES),
    )


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _is_frozen() -> bool:
    """PyInstaller などで固めた実行ファイルとして動いているか。"""
    return bool(getattr(sys, "frozen", False))


def runtime_target_dir() -> Path | None:
    """導入先の専用フォルダ。通常の実行では ``None``（環境へ直接入れる）。"""
    if not _is_frozen():
        return None
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "NovaEdit" / _RUNTIME_DIR


def activate_runtime() -> Path | None:
    """専用フォルダへ入れたものを import できるようにする。

    起動時に 1 度呼ぶ。通常の実行では何もしない。
    """
    target = runtime_target_dir()
    if target is None or not target.exists():
        return None
    path = str(target)
    if path not in sys.path:
        # 先頭へ入れる。同名の古いものが同梱されていた場合に、あとから入れた方を
        # 使わせるため。
        sys.path.insert(0, path)
    return target


def install_command(
    *, cuda: bool = True, upgrade: bool = False, python: str | None = None
) -> list[str]:
    """導入に使う ``pip`` のコマンド列を組み立てる。

    実行せずに文字列として得られるようにしてあるのは、画面に「これを実行します」と
    出すため。何が入るのか分からないまま 2 GB のダウンロードが始まるのは不安が大きい。
    """
    names = list(REQUIRED_PACKAGES) + (list(CUDA_PACKAGES) if cuda else [])
    command = [python or sys.executable, "-m", "pip", "install"]
    if upgrade:
        command.append("--upgrade")
    target = runtime_target_dir()
    if target is not None:
        command.extend(["--target", str(target)])
    command.extend(_REQUIREMENTS.get(name, name) for name in names)
    return command


def install_runtime(
    *,
    cuda: bool = True,
    on_output: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    command: Sequence[str] | None = None,
) -> int:
    """``pip`` を子プロセスで走らせる。戻り値は終了コード（0 が成功）。

    出力は 1 行ずつ ``on_output`` へ渡す。まとめて最後に渡すと、数分間なにも
    起きていないように見える。
    """
    argv = list(command) if command is not None else install_command(cuda=cuda)
    if on_output is not None:
        on_output("> " + " ".join(argv))

    target = runtime_target_dir()
    if target is not None:
        target.mkdir(parents=True, exist_ok=True)

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # 子プロセスの出力を UTF-8 に揃える。Windows の既定は cp932 で、素材やユーザー名に
    # 日本語が入っているとログが文字化けし、失敗の原因が読めなくなる。
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        # 引数はここで組み立てたものだけで、shell も通さない。
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


def model_cache_dir() -> Path:
    """モデルの取得先。

    Hugging Face の既定に合わせる。ここを独自の場所にすると、他のツールで
    落とし済みのモデルを二重に持つことになる。
    """
    override = os.environ.get("HF_HOME")
    if override:
        return Path(override) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"
