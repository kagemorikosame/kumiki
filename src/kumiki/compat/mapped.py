"""外部形式を写した結果。AviUtl 側と YMM4 側で共通に使う。

どちらのソフトから来ても「1 オブジェクト = 1 クリップ + 置くレイヤー + 参照して
いる素材」に落ちる。同じ形にしておくと、タイムラインへ置く処理
（:mod:`kumiki.compat.catalog`）を 1 つで済ませられる。
"""

from __future__ import annotations

from dataclasses import dataclass

from kumiki.core.model import Clip

__all__ = ["MappedObject"]


@dataclass(frozen=True, slots=True)
class MappedObject:
    """1 オブジェクトを写した結果。"""

    clip: Clip
    layer: int
    #: 素材ファイルを参照している場合のパス。読み込みは呼び出し側が行う。
    media_path: str = ""
    kind: str = ""
    #: 元のファイルが長さを指定していたか。
    #:
    #: エイリアスは長さを持たないことがある。その場合、1 フレームのクリップを
    #: 置くのではなく、置く側が既定の長さを決める。
    has_span: bool = True
