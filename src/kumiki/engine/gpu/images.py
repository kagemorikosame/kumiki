"""エフェクトが読む画像を GPU の上に置いておく

画像合成の絵や縁取りの模様は、パスで指すだけの外のファイル 描くたびに
読み直すと、1 フレームごとに画像のデコードと転送が走る 1 度読んだら
テクスチャのまま持っておき、**ファイルが書き換わったときだけ**読み直す

書き換わったかは、更新時刻と大きさで見る 中身を毎回読んで比べると、
読み直さないために毎回読むことになる
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from kumiki.engine.decode.image import read_image
from kumiki.engine.gpu.glutil import Texture

__all__ = ["EffectImages", "ImageTexture"]

_log = logging.getLogger(__name__)

#: ファイルの手がかり（更新時刻、大きさ） 無ければ ``None``
type _Signature = tuple[int, int] | None


class ImageTexture(Protocol):
    """GPU に置いた画像 :class:`~kumiki.engine.gpu.Texture` が満たす"""

    width: int
    height: int
    handle: int

    def release(self) -> None: ...


def _upload(image: np.ndarray) -> ImageTexture:
    # 画像は sRGB で符号化されている シェーダでリニアとして読めるよう sRGB で作る
    return Texture.from_array(image, srgb=True)


@dataclass(slots=True)
class _Entry:
    signature: _Signature
    texture: ImageTexture | None


class EffectImages:
    """パスごとに 1 枚、GPU の上に画像を持つ

    GL のコンテキストが current な所で使うこと（読み直しと解放が GL を触る）
    :meth:`stale` だけは GL を触らない 先読みの絵を捨てる判断に使う
    """

    def __init__(
        self,
        *,
        reader: Callable[[Path], np.ndarray | None] = read_image,
        upload: Callable[[np.ndarray], ImageTexture] = _upload,
    ) -> None:
        self._reader = reader
        self._upload = upload
        self._entries: dict[str, _Entry] = {}
        #: :meth:`stale` が最後に見たファイルの手がかり
        #:
        #: 読み込んだときの手がかりと別に持つ 描くときに先に読み直されると、
        #: 読み込んだ側の手がかりは新しくなって変化が見えず、先読みした別の
        #: フレームの古い絵が捨てられずに残る 逆に読み込んだ側と今のファイルを
        #: 比べるだけだと、読み直すまで毎回「変わった」と返し、描き直した絵まで
        #: 捨て続ける
        self._reported: dict[str, _Signature] = {}
        #: 見つからない・読めないと 1 度伝えたパス 毎フレーム書くとログが埋まる
        self._warned: set[str] = set()

    def get(self, path: str) -> ImageTexture | None:
        """``path`` の画像 読めなければ ``None``（空のパスも ``None``）"""
        if not path:
            return None
        signature = _signature(path)
        entry = self._entries.get(path)
        if entry is not None and entry.signature == signature:
            return entry.texture
        if entry is None:
            # 初めて読む画像 これより前にこの画像で描いた絵は無いので、捨てる物も無い
            self._reported[path] = signature
        elif entry.texture is not None:
            entry.texture.release()

        texture: ImageTexture | None = None
        image = self._reader(Path(path)) if signature is not None else None
        if image is not None:
            texture = self._upload(image)
            self._warned.discard(path)
        elif path not in self._warned:
            self._warned.add(path)
            reason = "見つからない" if signature is None else "読めない"
            _log.warning("エフェクトの画像が%s: %s", reason, path)
        self._entries[path] = _Entry(signature, texture)
        return texture

    @property
    def missing(self) -> frozenset[str]:
        """見つからない・読めなかった画像のパス"""
        return frozenset(path for path, entry in self._entries.items() if entry.texture is None)

    def stale(self) -> frozenset[str]:
        """前に読んだときから書き換わった（消えた・現れた）画像のパス

        1 度返した変化は次からは返さない 読み直しは次に描くときに行う
        """
        changed: set[str] = set()
        for path in self._entries:
            current = _signature(path)
            if self._reported.get(path) != current:
                changed.add(path)
                self._reported[path] = current
        return frozenset(changed)

    def release(self) -> None:
        for entry in self._entries.values():
            if entry.texture is not None:
                entry.texture.release()
        self._entries.clear()
        self._reported.clear()


def _signature(path: str) -> _Signature:
    try:
        status = Path(path).stat()
    except (OSError, ValueError):
        # ValueError は NUL 文字の入ったパス ファイルから読んだ値は形も信用できない
        return None
    return status.st_mtime_ns, status.st_size
