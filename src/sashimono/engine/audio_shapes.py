"""音声波形（AviUtl2 の ``音声波形表示``）の形の計算

描くのは :mod:`sashimono.engine.sources`（Qt） ここは数だけを扱い、Qt を持ち込まない
決まりは AviUtl2 の書き出し（p6 の見本）を素材のサンプルと突き合わせて読んだ

* 線は 1 画素（升目にするなら 1 升）に 1 サンプル 窓の頭はそのフレームの時刻
* 振幅 1 で高さの半分 **正の値が下へ出る** 線の太さは 2（升目なら 2 升）
* スペクトラムは周波数を対数で横に並べ、下から振幅の大きさだけ塗る
  ミラー表示では、それに真ん中の 1 升を足した棒を上下の真ん中に置く
* 横解像度・縦解像度を決めると、その升目の数の小さな絵に描いてから引き伸ばす
  スペースは升目どうしのすき間
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "SPECTRUM_SIZE",
    "SPECTRUM_WINDOW",
    "WAVEFORM_LINE",
    "bar_mask",
    "cell_mask",
    "spectrum_cells",
    "spectrum_levels",
    "spectrum_window",
    "waveform_cells",
    "waveform_points",
]

#: 音声波形の線の太さ（画素または升） AviUtl2 の無音の所は中心の下 2 行が塗られ、
#: 縦解像度 16 では 2 升（50 画素）が塗られていた
WAVEFORM_LINE = 2.0

#: スペクトラムが読む音（サンプル） **フレームの時刻から 735 サンプル**（44.1kHz・60fps の
#: 1 フレームぶん） 窓関数は掛けない
#: 音の入りで決めた 見本の曲は 18700 サンプル目から大きく鳴り、AviUtl2 は 24 フレーム目
#: （17640）に何も出さず 25 フレーム目（18375）から棒を出した 窓の終わりは時刻から
#: 325〜1060 サンプルの間にある 前の作り（時刻の 512 前から 2048 のハン窓）は 24 フレーム目に
#: 棒を出していた 窓を振った当てはめでは、この窓が 4 つの見本（800 列・16 列・64x32・
#: 32x40）のどれでも一番よく合った（相関 0.77〜0.84 → 0.85〜0.88）
#: 30fps のプロジェクトで窓が 1 フレームぶん（1470）に伸びるかは測っていないので、数で持つ
#: 数は 44.1kHz でのもの 他のレートでは :func:`spectrum_window` で同じ時間（16.7 ミリ秒）に直す
#: サンプルの数のまま使うと、48kHz では 15.3 ミリ秒で窓が切れ、同じ音でも棒が変わる
SPECTRUM_WINDOW = 735

#: スペクトラムの FFT の大きさ（44.1kHz でのもの） 窓の後ろを 0 で埋めて伸ばす
#: 1024 では低い音の帯が粗くなり、16 列の見本で相関が 0.88 から 0.84 へ落ちた
#: 他のレートでも周波数の刻み（21.5Hz）が変わらないよう、窓と同じ割合で伸ばす
#: 刻みが変わると、帯に入る周波数の数が変わり、同じ音の棒の高さが変わる
SPECTRUM_SIZE = 2048

#: ``SPECTRUM_WINDOW`` と ``SPECTRUM_SIZE`` を測ったレート
_MEASURED_RATE = 44100


def spectrum_window(sample_rate: int) -> int:
    """このレートでスペクトラムが読むサンプルの数（44.1kHz の 735 と同じ時間）"""
    return max(2, round(SPECTRUM_WINDOW * max(sample_rate, 1) / _MEASURED_RATE))


def _spectrum_size(sample_rate: int) -> int:
    return max(2, round(SPECTRUM_SIZE * max(sample_rate, 1) / _MEASURED_RATE))


#: スペクトラムの横軸の周波数（Hz） 左端から右端まで対数で並ぶ
#: 範囲を 20〜120Hz と 4k〜22kHz で振ると 45〜14000 が少しだけ良かった（相関で 0.01 未満）
#: その差は曲 1 本の当てはめの揺れと見分けられないので、切りの良い組のままにする
SPECTRUM_LOW, SPECTRUM_HIGH = 40.0, 20000.0

#: スペクトラムの高さ 帯の振幅（窓の長さで割った値）にこれを掛けると、描く高さに対する割合
#: 高さ 400 の見本で当てはめると、800 列・16 列・64x32・32x40 で 1.7〜1.9 だった
SPECTRUM_GAIN = 1.85


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
    samples: np.ndarray, columns: int, sample_rate: int, volume: float
) -> np.ndarray:
    """列ごとのスペクトラムの高さ（描く高さに対する割合 1 を超えることもある）

    ``samples`` はフレームの時刻からの音 頭の :func:`spectrum_window` 個を読み、足りなければ
    無音として扱う 列は周波数を対数で等分した帯で、帯に入る周波数の振幅を 2 乗して
    足した平方根を読む 帯に 1 本も入らない狭い帯（低い音の細かい列）は、真ん中の周波数の
    振幅を隣どうしから直線で補う 真ん中だけを読むと、16 列のような粗い帯で山を取りこぼし、
    棒が低く出た（相関 0.66 → 0.84）
    """
    count = max(1, columns)
    length = spectrum_window(sample_rate)
    size = _spectrum_size(sample_rate)
    window = np.asarray(samples[:length], dtype=np.float64)
    if len(window) < 2:
        return np.zeros(count)
    padded = np.zeros(size)
    padded[: len(window)] = window
    # 割るのは読んだ数ではなく窓の長さ 終わり際で音が短く切れても、同じ音量の音が
    # 同じ高さに出るように（短い方で割ると、切れた所だけ棒が伸びる）
    amplitude = np.abs(np.fft.rfft(padded)) / length * (volume / 100.0)
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


def spectrum_cells(levels: np.ndarray, rows: int, *, mirror: bool = False) -> np.ndarray:
    """列ごとに塗る升の数 ``levels`` は :func:`spectrum_levels` の割合

    **升いっぱいまで届いた升だけを塗る**（切り捨て） 64x32 と 32x40 の見本で、四捨五入より
    升の数が合った（一致 0.67 → 0.73、0.51 → 0.65）
    ミラー表示は、それに**真ん中の 1 升を足す** 40x40 の見本は、無音の所でも真ん中に
    1 升の棒を出し（上下 5 画素ずつ）、鳴っている所では棒の升の数がミラーなしの升の数に
    1 足した数と一番よく合った（一致 0.67 上下に同じ高さを伸ばして 2 倍にすると 0.49）
    """
    count = np.floor(np.nan_to_num(levels, nan=0.0, posinf=float(rows)) * rows)
    if mirror:
        count = count + 1
    cells: np.ndarray = np.clip(count, 0, max(rows, 0)).astype(int)
    return cells


def _axis(count: int, size: int, gap: float) -> tuple[np.ndarray, np.ndarray]:
    """1 つの向きについて、画素ごとの升の番号と、すき間でないか

    すき間は升の幅に対する % 升の境目を中心に空ける
    """
    along = (np.arange(size) + 0.5) * count / size
    index = np.minimum(along.astype(int), count - 1)
    cell = size / count
    half = gap * cell / 200.0
    within = (along - np.floor(along)) * cell
    # 画素の真ん中がちょうどすき間の縁に来たときに、浮動小数の揺れで升ごとに
    # すき間の幅が変わらないよう、少しだけ升の側へ寄せて比べる
    open_ = (within >= half - 1e-9) & (within < cell - half - 1e-9)
    return index, open_


def cell_mask(lit: np.ndarray, width: int, height: int, gap_x: float, gap_y: float) -> np.ndarray:
    """升目の小さな絵（``lit`` は升ごとの塗り）を ``width`` × ``height`` の画素へ広げる

    すき間は升の幅（高さ）に対する % 升の境目を中心に空ける 横 16 升・スペース 4 では
    幅 50 の升の境目に 2 画素、縦 16 升では高さ 25 の升の境目に 1 画素のすき間だった
    """
    rows, columns = lit.shape
    if rows == 0 or columns == 0 or width <= 0 or height <= 0:
        return np.zeros((max(height, 0), max(width, 0)), dtype=bool)
    column_of, open_x = _axis(columns, width, gap_x)
    row_of, open_y = _axis(rows, height, gap_y)
    mask: np.ndarray = lit[row_of][:, column_of]
    result: np.ndarray = mask & open_y[:, None] & open_x[None, :]
    return result


def bar_mask(
    cells: np.ndarray,
    rows: int,
    width: int,
    height: int,
    gap_x: float,
    gap_y: float,
    *,
    mirror: bool = False,
) -> np.ndarray:
    """スペクトラムの棒（列ごとに ``cells`` 升）を ``width`` × ``height`` の画素へ置く

    ミラーなしは下の辺から積む ミラーは棒の真ん中を上下の真ん中に合わせる
    **ミラーの升は描く枠の升目に乗らない** 40x40 の見本で 1 升の棒は上下 5 画素ずつ、
    2 升の棒は 10 画素ずつに出た（枠の升目に乗せると、奇数の升の棒が半升ずれる）
    縦のすき間は棒の升ごとに空ける（ミラーで縦スペースを付けた見本はまだ無い）
    """
    columns = len(cells)
    if rows <= 0 or columns == 0 or width <= 0 or height <= 0:
        return np.zeros((max(height, 0), max(width, 0)), dtype=bool)
    column_of, open_x = _axis(columns, width, gap_x)
    counts = np.asarray(cells, dtype=np.float64)[column_of]
    # 棒の上の端（升単位 枠の上から） ミラーは半升の位置にもなる
    top = (rows - counts) / 2.0 if mirror else rows - counts
    down = (np.arange(height) + 0.5) * rows / height
    inside = down[:, None] - top[None, :]
    cell_h = height / rows
    half = gap_y * cell_h / 200.0
    within = (inside - np.floor(inside)) * cell_h
    open_y = (within >= half - 1e-9) & (within < cell_h - half - 1e-9)
    lit = (inside >= 0.0) & (inside < counts[None, :])
    result: np.ndarray = lit & open_y & open_x[None, :]
    return result
