"""解析結果を置くディスクキャッシュ。

波形もサムネイルも、作るのに時間がかかる割に素材が変わらなければ同じ結果になる。
プロジェクトを開くたびに数十秒待たされるのは論外なので、素材ごとに永続化する。

鍵は「パス + サイズ + 更新時刻」から作る。中身のハッシュを取るのが確実だが、
4K の素材を毎回全部読むことになり、キャッシュの意味が無くなる。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import numpy as np

__all__ = ["CacheStore", "default_cache_root", "load_arrays", "media_key", "save_arrays"]


def default_cache_root() -> Path:
    """キャッシュを置く既定の場所。

    Windows では ``%LOCALAPPDATA%``。ユーザーのプロジェクトフォルダに置くと、
    素材だけ移動したときに取り残される。
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "Kumiki" / "cache"
    return Path.home() / ".cache" / "kumiki"


def media_key(path: Path, *, extra: str = "") -> str:
    """素材を一意に指す鍵。

    ``extra`` には解析条件（サンプリングレートなど）を入れる。条件が違えば
    結果も違うので、同じ鍵にすると古い設定の結果を掴む。
    """
    path = Path(path)
    try:
        stat = path.stat()
        signature = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{extra}"
    except OSError:
        signature = f"{path}|missing|{extra}"
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:32]


class CacheStore:
    """名前空間ごとに分かれたファイル置き場。"""

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else default_cache_root()

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, namespace: str, key: str, suffix: str) -> Path:
        """``namespace`` の中で ``key`` に対応するファイルのパス。

        鍵の先頭 2 文字でサブフォルダを切る。1 つのフォルダにファイルが数万個
        並ぶと、エクスプローラも走査も目に見えて遅くなる。
        """
        return self._root / namespace / key[:2] / f"{key}{suffix}"

    def prepare(self, namespace: str, key: str, suffix: str) -> Path:
        """書き込み先を用意して返す。親フォルダも作る。"""
        path = self.path_for(namespace, key, suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def exists(self, namespace: str, key: str, suffix: str) -> bool:
        return self.path_for(namespace, key, suffix).exists()

    def clear(self, namespace: str | None = None) -> None:
        """キャッシュを捨てる。壊れたときの逃げ道として要る。"""
        target = self._root if namespace is None else self._root / namespace
        shutil.rmtree(target, ignore_errors=True)

    def size_bytes(self) -> int:
        """使用量。設定画面で見せるため。"""
        total = 0
        for path in self._root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total


def save_arrays(path: Path, arrays: Mapping[str, np.ndarray], *, compressed: bool = False) -> Path:
    """numpy の配列群を ``.npz`` として書き出す。

    一時ファイルへ書いてから差し替える。書き込み中に落ちても壊れたキャッシュが
    残らない。壊れたキャッシュは、あとから原因の分かりにくい不具合になる。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")

    # numpy の型スタブは savez の可変キーワードを allow_pickle と同じ bool として
    # 扱うため、名前付きの配列を渡すと型が合わない。ここで 1 度だけ吸収する。
    writer = cast("Callable[..., None]", np.savez_compressed if compressed else np.savez)
    writer(temporary, **arrays)

    # savez は拡張子 .npz を勝手に足す。
    written = temporary if temporary.exists() else temporary.with_name(temporary.name + ".npz")
    written.replace(path)
    return path


def load_arrays(path: Path) -> dict[str, np.ndarray] | None:
    """:func:`save_arrays` で書いたファイルを読む。

    壊れていれば消して ``None`` を返す。作り直せるものなので、ここで
    ユーザーに何かを伝える意味は無い。
    """
    if not path.exists():
        return None
    try:
        with np.load(path) as data:
            return {name: np.asarray(data[name]) for name in data.files}
    except (OSError, ValueError, KeyError):
        path.unlink(missing_ok=True)
        return None
