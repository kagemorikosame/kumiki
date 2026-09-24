"""仮想バッファ（``obj.setoption("drawtarget", "tempbuffer")``）へ描く

仮想バッファへ描いた物は、スクリプトの中で ``obj.load("tempbuffer")`` として
読み戻される 画面へ出す描画（GPU）を待っていられないので、ここで CPU で描く
仮想バッファはオブジェクト 1 つ分の大きさで、字幕の板のような用途では十分に速い

座標は AviUtl のまま 仮想バッファの真ん中が原点、右と**下**が正
（仕様書 lua.txt「オブジェクトの持っている座標等の設定は反映せず引数の
座標そのままで描画されます」）

色はストレートアルファで持つ（:mod:`sashimono.compat.aviutl.objapi` と同じ）
重ね方は「通常」だけ ほかの合成モードは呼ぶ側が記録に残して素通しにする
"""

from __future__ import annotations

import numpy as np

__all__ = ["draw_image", "draw_triangle", "resize"]


#: 双線形のリサイズで 1 度に補間する行の数 出力全体を一度に float で持つと、
#: 上限の 4096 x 4096 で 1 枚 256 MiB の作業用の配列が 3 枚並び、描画ごと止まりうる
RESIZE_BAND = 256


def resize(
    image: np.ndarray, width: int, height: int, *, smooth: bool = True, band: int = RESIZE_BAND
) -> np.ndarray:
    """絵を ``width`` x ``height`` へ引き伸ばす（``obj.effect("リサイズ")``）

    ``smooth`` が偽なら最も近い画素を取る（リサイズの 補間なし） 真なら双線形で、
    色は不透明度を掛けてから混ぜる ストレートのまま混ぜると、透明な所の色（黒）が
    縁へにじんで暗い輪が出る 双線形は ``band`` 行ずつ補間して作業用の配列を小さく保つ
    """
    h, w = image.shape[:2]
    width, height = max(1, width), max(1, height)
    if w == 0 or h == 0:
        return np.zeros((height, width, 4), np.uint8)
    if (w, h) == (width, height):
        return image.copy()
    if not smooth:
        rows = np.arange(height) * h // height
        cols = np.arange(width) * w // width
        picked: np.ndarray = np.ascontiguousarray(image[rows][:, cols])
        return picked

    ys = np.clip((np.arange(height) + 0.5) * h / height - 0.5, 0, h - 1)
    xs = np.clip((np.arange(width) + 0.5) * w / width - 0.5, 0, w - 1)
    x0 = np.floor(xs).astype(np.intp)
    x1 = np.minimum(x0 + 1, w - 1)
    fx = (xs - x0)[None, :, None]
    resized = np.empty((height, width, 4), np.uint8)
    for start in range(0, height, max(1, band)):
        rows = ys[start : start + max(1, band)]
        y0 = np.floor(rows).astype(np.intp)
        y1 = np.minimum(y0 + 1, h - 1)
        fy = (rows - y0)[:, None, None]
        # 元の絵も要る行だけを float にする 元の絵を丸ごと float にすると、
        # 上限の大きさの絵では 1 枚でまた 256 MiB になる
        upper, lower = _premultiplied(image[y0]), _premultiplied(image[y1])
        top = upper[:, x0] * (1 - fx) + upper[:, x1] * fx
        bottom = lower[:, x0] * (1 - fx) + lower[:, x1] * fx
        mixed = top * (1 - fy) + bottom * fy
        alpha = mixed[..., 3:4]
        with np.errstate(divide="ignore", invalid="ignore"):
            mixed[..., :3] = np.where(alpha > 0, mixed[..., :3] / alpha, 0.0)
        resized[start : start + len(rows)] = np.clip(np.rint(mixed * 255.0), 0, 255)
    return resized


def _premultiplied(pixels: np.ndarray) -> np.ndarray:
    """0〜255 のストレートを 0〜1 の事前乗算へ"""
    values = pixels.astype(np.float32) / 255.0
    values[..., :3] *= values[..., 3:4]
    return values


def _over(target: np.ndarray, color: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """``color``（ストレート）を不透明度 ``alpha`` で ``target`` の上へ重ねた結果

    ``target`` も ``color`` も ``(..., 4)`` の 0〜255 ``alpha`` は 0〜1
    """
    below = target.astype(np.float32) / 255.0
    above = color.astype(np.float32) / 255.0
    a_top = above[..., 3] * alpha
    a_bottom = below[..., 3]
    a_out = a_top + a_bottom * (1.0 - a_top)
    with np.errstate(divide="ignore", invalid="ignore"):
        rgb = (
            above[..., :3] * a_top[..., None]
            + below[..., :3] * (a_bottom * (1.0 - a_top))[..., None]
        ) / a_out[..., None]
    rgb = np.where(a_out[..., None] > 0, rgb, 0.0)
    out = np.concatenate([rgb, a_out[..., None]], axis=-1)
    return np.clip(np.rint(out * 255.0), 0, 255).astype(np.uint8)


def draw_image(
    buffer: np.ndarray, image: np.ndarray, x: float = 0.0, y: float = 0.0, alpha: float = 1.0
) -> None:
    """絵を等倍で、真ん中が ``(x, y)`` に来るように重ねる

    位置は画素の格子へ合わせる（等倍なので引き伸ばさない） 小数の位置で
    補間すると、文字の縁がにじむ
    """
    height, width = buffer.shape[:2]
    h, w = image.shape[:2]
    left = int(np.floor((width - w) / 2.0 + x))
    top = int(np.floor((height - h) / 2.0 + y))
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(width, left + w), min(height, top + h)
    if x0 >= x1 or y0 >= y1:
        return
    part = image[y0 - top : y1 - top, x0 - left : x1 - left]
    region = buffer[y0:y1, x0:x1]
    buffer[y0:y1, x0:x1] = _over(region, part, np.full(part.shape[:2], alpha, np.float32))


def draw_triangle(
    buffer: np.ndarray,
    points: tuple[tuple[float, float], ...],
    *,
    texture: np.ndarray | None = None,
    uvs: tuple[tuple[float, float], ...] | None = None,
    colors: tuple[tuple[float, float, float, float], ...] | None = None,
    alpha: float = 1.0,
) -> None:
    """三角形を 1 つ描く 絵を貼る（``uvs``）か、頂点の色で塗る（``colors``）

    ``points`` は仮想バッファの座標（真ん中が原点、下が正）
    ``uvs`` は絵の中の位置（0〜1 仕様書どおり正規化） 端の外は端の色（clamp
    仕様書の drawpoly の既定）
    ``colors`` は 0〜1 の乗算済みアルファ（仕様書どおり）

    画素の真ん中が三角形の中にある画素を塗る 隣り合う三角形の境目が
    二重に塗られて濃くなったり、隙間が空いたりしないよう、辺の上は片側だけに含める
    """
    height, width = buffer.shape[:2]
    pts = np.array(points, dtype=np.float64) + np.array([width / 2.0, height / 2.0])
    x_min = max(0, int(np.floor(pts[:, 0].min())))
    x_max = min(width, int(np.ceil(pts[:, 0].max())))
    y_min = max(0, int(np.floor(pts[:, 1].min())))
    y_max = min(height, int(np.ceil(pts[:, 1].max())))
    if x_min >= x_max or y_min >= y_max:
        return

    order = [0, 1, 2]
    (ax, ay), (bx, by), (cx, cy) = pts
    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    if abs(area) < 1e-12:
        return
    if area < 0:
        # 向きをそろえる（下が正の座標で時計回り） 上と左の辺を決める決まりは
        # 向きが決まっていないと当てられない
        order = [0, 2, 1]
        pts = pts[order]
        area = -area
    xs, ys = np.meshgrid(
        np.arange(x_min, x_max, dtype=np.float64) + 0.5,
        np.arange(y_min, y_max, dtype=np.float64) + 0.5,
    )
    # 各頂点の向かいの辺で測った重み 3 つとも 0 以上なら三角形の中
    weights = []
    inside = np.ones(xs.shape, dtype=bool)
    for i in range(3):
        start_point, end_point = pts[(i + 1) % 3], pts[(i + 2) % 3]
        edge = _edge(start_point, end_point, xs, ys)
        weights.append(edge / area)
        inside &= (edge > 0) | ((edge == 0) & _top_left(start_point, end_point))
    if not inside.any():
        return
    # 並べ替えた頂点に合わせて、色や位置の並びも入れ替える
    w0, w1, w2 = weights
    uvs = tuple(uvs[i] for i in order) if uvs is not None else None
    colors = tuple(colors[i] for i in order) if colors is not None else None

    region = buffer[y_min:y_max, x_min:x_max]
    if colors is not None:
        rgba = np.array(colors, dtype=np.float64)
        premultiplied = w0[..., None] * rgba[0] + w1[..., None] * rgba[1] + w2[..., None] * rgba[2]
        a = np.clip(premultiplied[..., 3], 0.0, 1.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            straight = np.where(a[..., None] > 0, premultiplied[..., :3] / a[..., None], 0.0)
        color = np.concatenate([straight, np.ones_like(a)[..., None]], axis=-1) * 255.0
        coverage = (a * alpha).astype(np.float32)
    else:
        if texture is None or uvs is None:
            return
        uv = np.array(uvs, dtype=np.float64)
        u = w0 * uv[0, 0] + w1 * uv[1, 0] + w2 * uv[2, 0]
        v = w0 * uv[0, 1] + w1 * uv[1, 1] + w2 * uv[2, 1]
        th, tw = texture.shape[:2]
        column = np.clip(np.floor(u * tw), 0, tw - 1).astype(np.intp)
        row = np.clip(np.floor(v * th), 0, th - 1).astype(np.intp)
        color = texture[row, column].astype(np.float64)
        coverage = np.full(u.shape, alpha, np.float32)
    coverage = np.where(inside, coverage, 0.0).astype(np.float32)
    blended = _over(region, np.clip(color, 0, 255).astype(np.uint8), coverage)
    buffer[y_min:y_max, x_min:x_max] = np.where(inside[..., None], blended, region)


#: 辺の上にちょうど乗っているとみなす幅 画素の真ん中と頂点が整数の座標だと、
#: 計算の誤差で 0 が ±1e-16 になり、どちら側に入るかが揺れる
_ON_EDGE = 1e-9


def _edge(start: np.ndarray, end: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """辺 ``start → end`` の左右どちらにあるか（時計回りの三角形では中が正）"""
    value = (end[0] - start[0]) * (ys - start[1]) - (end[1] - start[1]) * (xs - start[0])
    return np.where(np.abs(value) < _ON_EDGE, 0.0, value)


def _top_left(start: np.ndarray, end: np.ndarray) -> bool:
    """辺の上の画素をこの三角形に入れるか（上と左の辺だけを含める GPU と同じ決まり）

    境目を両方に含めると、2 つの三角形で作った四角形の対角線が二重に塗られる
    半透明の板では、そこだけ濃い線になって見える どちらにも含めなければ隙間になる
    """
    dx, dy = end[0] - start[0], end[1] - start[1]
    top = dy == 0 and dx > 0
    left = dy < 0
    return bool(top or left)
