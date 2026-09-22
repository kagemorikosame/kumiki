"""音声波形（AviUtl2 の ``音声波形表示``）の形の計算

描くのは :mod:`sashimono.engine.sources`（Qt） ここは数だけを扱い、Qt を持ち込まない
決まりは AviUtl2 の書き出し（p6 の見本）を素材のサンプルと突き合わせて読んだ

* 線は 1 画素（升目にするなら 1 升）に 1 サンプル 窓の頭はそのフレームの時刻
* 振幅 1 で高さの半分 **正の値が下へ出る** 線の太さは 2（升目なら 2 升）
* スペクトラムは周波数を対数で横に並べ、下から振幅の大きさだけ塗る
* 横解像度・縦解像度を決めると、その升目の数の小さな絵に描いてから引き伸ばす
  スペースは升目どうしのすき間
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "SPECTRUM_SIZE",
    "WAVEFORM_LEAD",
    "WAVEFORM_LINE",
    "cell_mask",
    "spectrum_levels",
    "waveform_cells",
    "waveform_points",
]

#: 音声波形の線の太さ（画素または升） AviUtl2 の無音の所は中心の下 2 行が塗られ、
#: 縦解像度 16 では 2 升（50 画素）が塗られていた
WAVEFORM_LINE = 2.0

#: スペクトラムを取る窓の大きさ（サンプル） 1024〜8192 を当てはめて一番合った
SPECTRUM_SIZE = 2048

#: スペクトラムの窓を、フレームの時刻よりどれだけ前から取るか（サンプル）
#: 時刻から後ろだけの窓より、この位置の方が実物と合った
#: レンダラはこのぶん前から音を渡す（線は ``WAVEFORM_LEAD`` 番目のサンプルから描く）
WAVEFORM_LEAD = 512

#: スペクトラムの横軸の周波数（Hz） 左端から右端まで対数で並ぶ
#: 範囲を 10〜100Hz と 5k〜22kHz で振って一番合った組
SPECTRUM_LOW, SPECTRUM_HIGH = 40.0, 20000.0

#: スペクトラムの高さ 帯の振幅（ハン窓 窓の長さで割った値）にこれと高さを掛ける
#: 高さ 400 の見本で当てはめると、800 列では 1735、16 列では 1918 だった その間を取る
SPECTRUM_GAIN = 1800.0 / 400.0


def waveform_points(
    samples: np.ndarray,
    width: float,
    height: float,
    volume: float,
    *,
    drop: float = WAVEFORM_LINE / 2.0,
) -> np.ndarray:
    """音声波形の線の点 画面の座標（中心から、Y は下が正）で ``(点の数, 2)``

    1 画素に 1 サンプル 振幅 1 で高さの半分まで振れ、**正の値が下へ出る**
    （AviUtl2 の絵を素材のサンプルと突き合わせると、下向きを正として相関 0.99 だった
    上向きに描くと相関が -0.99 になり、波形が上下逆さまに出る）
    線は太さ 2 で中心の下 ``drop`` だけずらして引く にじませて引く線は 1 画素
    （無音の線が中心の下 2 行に乗る実物に合わせる） 升目の絵は 0.5 升
    （縦 16 升の無音が中心をまたぐ 2 升、振れ 0.4 升で中心の下 2 升になる実物に合わせる）
    幅より多いサンプルは捨てる 描く範囲の外へ線がはみ出さないように
    """
    count = min(len(samples), max(0, round(width)))
    x = -width / 2.0 + np.arange(count) + 0.5
    y = drop + samples[:count] * (volume / 100.0) * height / 2.0
    return np.stack([x, y], axis=1)


def spectrum_levels(
    window: np.ndarray, columns: int, sample_rate: int, volume: float
) -> np.ndarray:
    """列ごとのスペクトラムの高さ（描く高さに対する割合 1 を超えることもある）

    ``window`` は ``SPECTRUM_SIZE`` 個のサンプル 列は周波数を対数で等分した帯で、
    帯に入る周波数の振幅を 2 乗して足した平方根を読む 帯に 1 本も入らない狭い帯
    （低い音の細かい列）は、真ん中の周波数の振幅を隣どうしから直線で補う
    真ん中だけを読むと、16 列のような粗い帯で山を取りこぼし、棒が低く出た（相関 0.66 → 0.84）
    """
    count = max(1, columns)
    size = len(window)
    if size < 2:
        return np.zeros(count)
    taper = np.hanning(size)
    amplitude = np.abs(np.fft.rfft(window * taper)) / size * (volume / 100.0)
    frequencies = np.fft.rfftfreq(size, 1.0 / max(sample_rate, 1))
    ratio = SPECTRUM_HIGH / SPECTRUM_LOW
    edges = SPECTRUM_LOW * ratio ** (np.arange(count + 1) / count)
    centres = SPECTRUM_LOW * ratio ** ((np.arange(count) + 0.5) / count)
    levels: np.ndarray = np.interp(centres, frequencies, amplitude)
    low = np.searchsorted(frequencies, edges[:-1])
    high = np.searchsorted(frequencies, edges[1:])
    power = np.concatenate([[0.0], np.cumsum(amplitude**2)])
    inside = high > low
    levels[inside] = np.sqrt(power[high[inside]] - power[low[inside]])
    scaled: np.ndarray = levels * SPECTRUM_GAIN
    return scaled


#: 升目の絵で、線を中心からずらす量（升） 縦 16 升の実物で、無音の線が中心をまたぐ
#: 2 升（上と下に 1 升ずつ）、振れ 0.4 升で中心の下 2 升に乗った 0.5 だと無音の線が
#: ちょうど升の境目に来て、どちらへ寄るかが丸めしだいになる
GRID_DROP = 0.45


def waveform_cells(samples: np.ndarray, columns: int, rows: int, volume: float) -> np.ndarray:
    """升目の数の小さな絵に線を引いたときに塗られる升 ``(縦の升, 横の升)``

    1 升に 1 サンプル（横解像度 16 なら頭の 16 サンプル）、線の太さは 2 升
    隣の升との間は真ん中までつなぐ（縦に大きく振れた所で線が途切れないように）
    実物の升目は塗るか塗らないかの 2 つで、半端な明るさの升が無いので、にじませない
    """
    count = min(len(samples), max(0, columns))
    lit = np.zeros((max(rows, 0), max(columns, 0)), dtype=bool)
    if count == 0 or rows <= 0:
        return lit
    y = rows / 2.0 + GRID_DROP + samples[:count] * (volume / 100.0) * rows / 2.0
    left = np.concatenate([[y[0]], (y[:-1] + y[1:]) / 2.0])
    right = np.concatenate([(y[:-1] + y[1:]) / 2.0, [y[-1]]])
    low = np.minimum(np.minimum(left, right), y) - WAVEFORM_LINE / 2.0
    high = np.maximum(np.maximum(left, right), y) + WAVEFORM_LINE / 2.0
    centres = np.arange(rows)[:, None] + 0.5
    lit[:, :count] = (centres >= low[None, :]) & (centres < high[None, :])
    return lit


def cell_mask(lit: np.ndarray, width: int, height: int, gap_x: float, gap_y: float) -> np.ndarray:
    """升目の小さな絵（``lit`` は升ごとの塗り）を ``width`` × ``height`` の画素へ広げる

    すき間は升の幅（高さ）に対する % 升の境目を中心に空ける 横 16 升・スペース 4 では
    幅 50 の升の境目に 2 画素、縦 16 升では高さ 25 の升の境目に 1 画素のすき間だった
    """
    rows, columns = lit.shape
    if rows == 0 or columns == 0 or width <= 0 or height <= 0:
        return np.zeros((max(height, 0), max(width, 0)), dtype=bool)
    across = (np.arange(width) + 0.5) * columns / width
    down = (np.arange(height) + 0.5) * rows / height
    column_of = np.minimum(across.astype(int), columns - 1)
    row_of = np.minimum(down.astype(int), rows - 1)
    mask: np.ndarray = lit[row_of][:, column_of]
    cell_w, cell_h = width / columns, height / rows
    within_x = (across - np.floor(across)) * cell_w
    within_y = (down - np.floor(down)) * cell_h
    half_x, half_y = gap_x * cell_w / 200.0, gap_y * cell_h / 200.0
    # 画素の真ん中がちょうどすき間の縁に来たときに、浮動小数の揺れで升ごとに
    # すき間の幅が変わらないよう、少しだけ升の側へ寄せて比べる
    open_x = (within_x >= half_x - 1e-9) & (within_x < cell_w - half_x - 1e-9)
    open_y = (within_y >= half_y - 1e-9) & (within_y < cell_h - half_y - 1e-9)
    result: np.ndarray = mask & open_y[:, None] & open_x[None, :]
    return result
