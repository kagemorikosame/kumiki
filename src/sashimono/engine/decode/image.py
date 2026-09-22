"""エフェクトが読む画像ファイル（画像合成の絵、縁取りの模様）を開く

素材の静止画と同じく PyAV（FFmpeg）で読む 画像のためだけに別の読み手を
足すと、素材としては読めるのにエフェクトでは読めない形式が出る
"""

from __future__ import annotations

from pathlib import Path

import av
import av.error
import numpy as np

from sashimono.engine.colorspace import to_rgb_array

__all__ = ["read_image"]


def read_image(path: Path) -> np.ndarray | None:
    """先頭の 1 枚を ``(高さ, 幅, 4)`` の RGBA uint8（ストレートアルファ）で返す

    読めなければ ``None`` 例外にしないのは、描くたびに呼ばれる所だから
    画像 1 枚が壊れているだけで、プレビューも書き出しも止まってしまう

    動画を渡されたら先頭のコマを返す AviUtl2 の画像合成は動画も受けるが、
    再生までは写していない（読み込みの記録に残る）
    """
    try:
        with av.open(str(path)) as container:
            if not container.streams.video:
                return None
            for frame in container.decode(video=0):
                # 行列は素材の読み込みと同じ決め方にする 素の to_ndarray だと、
                # 動画を渡されたときにタグの無い HD が BT.601 で読まれ、素材として
                # 置いたときと画像合成で使ったときとで色が変わる
                return np.ascontiguousarray(to_rgb_array(frame, "rgba"))
    except (av.error.FFmpegError, OSError, ValueError):
        return None
    return None
