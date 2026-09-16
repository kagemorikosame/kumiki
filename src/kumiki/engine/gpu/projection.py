"""奥行きのある配置の計算 GL を使わない数式だけの部分

板を X 軸・Y 軸で傾ける変形も、``obj.drawpoly`` で任意の四角形へ貼る描画も、
最後は「元の矩形の四隅を、画面上のどの 4 点へ写すか」に帰着する 4 点が決まれば、
その間は射影変換（ホモグラフィ）1 つで埋まる

座標は画面の画素で、原点は左上、Y は下が正（AviUtl の描画パラメータと同じ）
奥行き Z は画面の奥が正
"""

from __future__ import annotations

import math

import numpy as np

__all__ = [
    "CAMERA_DISTANCE",
    "Corners",
    "homography",
    "project",
    "rotate",
    "to_clip",
]

#: 画面からカメラまでの距離（画素） AviUtl の既定のカメラと同じ値にしてある
#: 奥行きのあるスクリプトは、この距離を前提に Z の量を決めて書かれている
#: 値を変えると、同じ ``oz`` でも大きさの変わり方が配布物と食い違う
CAMERA_DISTANCE = 1024.0

#: 四隅 左上・右上・右下・左下の順
Corners = tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]

#: カメラの目の前すれすれまで来た点は、これより手前へは出さない
#: 距離が 0 に近づくと拡大率が発散し、四隅の 1 つだけが画面の外の遠くへ飛ぶ
_NEAREST = 1.0


def rotate(
    point: tuple[float, float, float], rx: float, ry: float, rz: float
) -> tuple[float, float, float]:
    """点を X → Y → Z の順に回す 角度は度

    向きは画面の見え方で決めてある Z は時計回りが正（平面の回転と同じ）、
    X は正で上端が奥へ倒れ、Y は正で右端が奥へ倒れる
    """
    x, y, z = point
    if rx:
        c, s = math.cos(math.radians(rx)), math.sin(math.radians(rx))
        y, z = y * c + z * s, -y * s + z * c
    if ry:
        c, s = math.cos(math.radians(ry)), math.sin(math.radians(ry))
        x, z = x * c - z * s, x * s + z * c
    if rz:
        c, s = math.cos(math.radians(rz)), math.sin(math.radians(rz))
        x, y = x * c - y * s, x * s + y * c
    return x, y, z


def project(point: tuple[float, float, float], width: float, height: float) -> tuple[float, float]:
    """画面中央を原点とした 3 次元の点を、画面の画素へ写す"""
    x, y, z = point
    depth = max(CAMERA_DISTANCE + z, _NEAREST)
    scale = CAMERA_DISTANCE / depth
    return width / 2.0 + x * scale, height / 2.0 + y * scale


def homography(source: Corners, target: Corners) -> np.ndarray | None:
    """``source`` の四隅を ``target`` の四隅へ写す 3x3 の射影変換

    四隅が一直線に潰れているなど、解けないときは ``None`` 板を真横から見た
    瞬間（Y 軸 90 度）がこれにあたり、そのときは描かないのが正しい
    """
    rows: list[list[float]] = []
    values: list[float] = []
    for (sx, sy), (tx, ty) in zip(source, target, strict=True):
        rows.append([sx, sy, 1.0, 0.0, 0.0, 0.0, -tx * sx, -tx * sy])
        rows.append([0.0, 0.0, 0.0, sx, sy, 1.0, -ty * sx, -ty * sy])
        values.extend((tx, ty))
    matrix = np.array(rows, dtype=np.float64)
    if abs(np.linalg.det(matrix)) < 1e-9:
        return None
    solved = np.linalg.solve(matrix, np.array(values, dtype=np.float64))
    return np.append(solved, 1.0).reshape(3, 3)


def to_clip(width: float, height: float) -> np.ndarray:
    """画面の画素（左上原点、Y 下向き）から GL のクリップ空間へ"""
    return np.array(
        [[2.0 / width, 0.0, -1.0], [0.0, -2.0 / height, 1.0], [0.0, 0.0, 1.0]], dtype=np.float64
    )
