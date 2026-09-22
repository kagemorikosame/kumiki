"""色の行列を確かめるための帯の絵と、それを書いた素材

原色と灰色を縦の帯にして並べる 原色は行列の違い（BT.709 と BT.601）がいちばん大きく出る
素材は PyAV で作る 行列とタグを 1 つずつ指定でき、ffmpeg のコマンドの既定
（勝手にタグを付けることがある）に左右されない
"""

from __future__ import annotations

from pathlib import Path

import av
import av.codec
import numpy as np
import pytest
from av.video.reformatter import ColorRange, Colorspace

__all__ = [
    "AVCOL_SPC_BT470BG",
    "AVCOL_SPC_BT709",
    "AVCOL_SPC_UNSPECIFIED",
    "BT709_YUV",
    "COLORS",
    "assert_close",
    "bars",
    "rgb_at_bars",
    "skip_without_libx264",
    "write_bars",
    "yuv_at_bars",
]

#: 縦の帯にして並べる 4 色
COLORS = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (128, 128, 128))
#: 上の 4 色を BT.709 / limited の (Y, Cb, Cr) にした値 規格の式から計算したもの
#: BT.601 なら赤は (81, 90, 240)、緑は (145, 54, 34) で、10 以上離れる
BT709_YUV = ((63, 102, 240), (173, 42, 26), (32, 240, 118), (126, 128, 128))

#: AVColorSpace の値 PyAV は読むときに素の整数で返す
AVCOL_SPC_BT709 = 1
AVCOL_SPC_UNSPECIFIED = 2
AVCOL_SPC_BT470BG = 5

Triple = tuple[int, int, int]


def bars(width: int, height: int) -> np.ndarray:
    """:data:`COLORS` を左から等幅の帯にした RGB の絵"""
    image = np.zeros((height, width, 3), np.uint8)
    step = width // len(COLORS)
    for index, color in enumerate(COLORS):
        image[:, index * step : (index + 1) * step] = color
    return image


def _bar_centers(width: int) -> list[int]:
    step = width // len(COLORS)
    return [index * step + step // 2 for index in range(len(COLORS))]


def yuv_at_bars(frame: av.VideoFrame) -> list[Triple]:
    """yuv420p のフレームから、各帯の真ん中の (Y, Cb, Cr) を拾う"""
    width, height = frame.width, frame.height
    planes = frame.to_ndarray(format="yuv420p")
    luma = planes[:height]
    quarter = height // 4
    cb = planes[height : height + quarter].reshape(height // 2, width // 2)
    cr = planes[height + quarter :].reshape(height // 2, width // 2)
    row = height // 2
    return [
        (int(luma[row, x]), int(cb[row // 2, x // 2]), int(cr[row // 2, x // 2]))
        for x in _bar_centers(width)
    ]


def rgb_at_bars(image: np.ndarray) -> list[Triple]:
    """RGB（RGBA でもよい）の配列から、各帯の真ん中の色を拾う"""
    row = image.shape[0] // 2
    return [
        (int(image[row, x, 0]), int(image[row, x, 1]), int(image[row, x, 2]))
        for x in _bar_centers(image.shape[1])
    ]


def assert_close(
    actual: list[Triple], expected: tuple[Triple, ...] | list[Triple], tolerance: int
) -> None:
    for got, want in zip(actual, expected, strict=True):
        assert all(abs(a - b) <= tolerance for a, b in zip(got, want, strict=True)), (
            f"{actual} が {list(expected)} から {tolerance} より離れている"
        )


def skip_without_libx264() -> None:
    """libx264 が入っていなければ、素材を作る前に試験を飛ばす

    ここで作るのは試験の入力で、確かめたいのは読み方の方 libx264 の無い PyAV
    （自前で組んだ FFmpeg など）で「作れない」を失敗にすると、読み方の回帰と見分けが付かない
    確かめるのは作る前だけで、エンコードが始まってからの失敗はそのまま落とす
    """
    try:
        av.codec.Codec("libx264", "w")
    except Exception:
        # 無いときに投げる例外の種類は PyAV の版で違うので、種類では絞らない
        pytest.skip("libx264 が無いので、色の試験の素材を作れない")


def write_bars(
    path: Path,
    width: int,
    height: int,
    *,
    matrix: Colorspace,
    tag: int | None,
) -> Path:
    """帯の絵を ``matrix`` で YUV にして可逆で書く ``tag`` が ``None`` ならタグを付けない

    可逆（qp 0）にするのは、ずれが行列の違いだけで出るようにするため
    """
    skip_without_libx264()
    rgb = av.VideoFrame.from_ndarray(bars(width, height), format="rgb24")
    yuv = rgb.reformat(format="yuv420p", dst_colorspace=matrix, dst_color_range=ColorRange.MPEG)
    # reformat はフレームに行列のタグを付ける タグの無い素材を作るときに残すと、
    # エンコーダによってはそれをファイルへ写してしまう
    yuv.colorspace = AVCOL_SPC_UNSPECIFIED if tag is None else tag
    yuv.color_range = ColorRange.UNSPECIFIED if tag is None else ColorRange.MPEG
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("libx264", rate=30)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        stream.options = {"qp": "0"}
        if tag is not None:
            stream.codec_context.colorspace = tag
            stream.codec_context.color_range = ColorRange.MPEG
        for index in range(3):
            yuv.pts = index
            container.mux(stream.encode(yuv))
        container.mux(stream.encode(None))
    return path
