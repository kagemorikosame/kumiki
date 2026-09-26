"""プレビューで部分モザイク・ぼかしと部分フィルタの範囲をつまんで動かす計算

画面（ウィジェット）から切り離してある 窓を作らずに試験で押さえるため 座標は
:mod:`sashimono.ui.preview_handles` と同じく合成の画素（Y は下が正）で受け取り、
範囲の値の座標（絵の原点から 画面の画素 Y は上が正）へ戻してから値を決める
戻すのは :class:`~sashimono.engine.render.region_outline.RegionFrame` の写しの逆
後ろに積んだ配置で拡大や回転をしていても、掴んだ所と動く所が合う

- 枠の中 範囲を動かす（Shift で縦か横の一方だけ）
- 角 幅と高さを変える 向かいの角は動かさない（Alt で中心を動かさずに広げる）
- 辺の真ん中 その向きの大きさだけを変える 向かいの辺は動かさない（Alt で中心を保つ）
- 上の辺から出た丸 回す（Shift で 15 度刻み）
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from sashimono.engine.render.outline import Point
from sashimono.engine.render.region_outline import (
    RegionFrame,
    apply,
    from_local,
    invert,
    to_local,
)
from sashimono.ui.preview_handles import ROTATION_STEP, Grip, Hit, inside, rotation_knob

__all__ = [
    "edge_points",
    "region_changes",
    "region_hit_test",
    "region_turn",
    "to_region",
]

#: 角と辺の真ん中の、範囲の中での向き（横, 縦 上が正） 角は 左上・右上・右下・左下、
#: 辺は 上・右・下・左
_CORNER_SIGNS = ((-1, 1), (1, 1), (1, -1), (-1, -1))
_EDGE_SIGNS = ((0, 1), (1, 0), (0, -1), (-1, 0))


def edge_points(corners: Sequence[Point]) -> list[Point]:
    """辺の真ん中 上・右・下・左"""
    return [
        (
            (corners[index][0] + corners[(index + 1) % 4][0]) / 2.0,
            (corners[index][1] + corners[(index + 1) % 4][1]) / 2.0,
        )
        for index in range(4)
    ]


def region_hit_test(
    corners: Sequence[Point], point: Point, *, grip: float, knob: float
) -> Hit | None:
    """``point`` で掴むのは範囲の何か 何も無ければ ``None``

    座標は画面の上の同じ物差し（ウィジェットの座標）で渡す 角 → 辺 → 回転の丸 → 中 の順
    クリップの枠と違い、角の外で回す所は持たない 範囲はクリップの枠の中にあることが多く、
    外まで広げると、クリップを掴みたい所まで範囲が取ってしまう
    """
    for index, (x, y) in enumerate(corners):
        if math.hypot(point[0] - x, point[1] - y) <= grip:
            return Hit(Grip.SCALE, index)
    for index, (x, y) in enumerate(edge_points(corners)):
        if math.hypot(point[0] - x, point[1] - y) <= grip:
            return Hit(Grip.EDGE, index)
    found = rotation_knob(corners, knob)
    if found is not None:
        tip = found[1]
        if math.hypot(point[0] - tip[0], point[1] - tip[1]) <= grip:
            return Hit(Grip.ROTATE)
    if inside(corners, point):
        return Hit(Grip.MOVE)
    return None


def to_region(frame: RegionFrame, point: Point) -> Point | None:
    """合成の画素 → 範囲の値の座標 写しが潰れていれば ``None``"""
    back = invert(frame.to_canvas)
    return None if back is None else apply(back, point)


def region_turn(frame: RegionFrame, before: Point, after: Point) -> float:
    """範囲の中心の周りで ``before`` から ``after`` へ回った角度（度 時計回りが正）

    範囲の値の座標（Y は上が正）で数える 画面で数えると、後ろの配置で裏返した絵では
    逆に回る 1 回の動きの差だけを返し、呼ぶ側で足していく
    """
    first = to_region(frame, before)
    second = to_region(frame, after)
    if first is None or second is None:
        return 0.0
    center = (frame.values.get("center_x", 0.0), frame.values.get("center_y", 0.0))
    start = math.atan2(first[1] - center[1], first[0] - center[0])
    end = math.atan2(second[1] - center[1], second[0] - center[0])
    # Y が上の座標で反時計回りに増えるので、時計回りを正にするため逆にする
    step = -math.degrees(end - start)
    return (step + 180.0) % 360.0 - 180.0


def region_changes(
    frame: RegionFrame,
    hit: Hit,
    press: Point,
    current: Point,
    *,
    turned: float = 0.0,
    one_axis: bool = False,
    symmetric: bool = False,
    snap: bool = False,
) -> dict[str, float]:
    """掴んで ``press`` から ``current`` へ動かしたときの範囲の値 ``frame`` は掴んだ時点の枠

    ``turned`` は回した角度の合計（:func:`region_turn` を足した物）
    """
    values = frame.values
    center = (values.get("center_x", 0.0), values.get("center_y", 0.0))
    rotation = values.get("rotation", 0.0)
    if hit.grip is Grip.ROTATE:
        angle = rotation + turned
        if snap:
            angle = round(angle / ROTATION_STEP) * ROTATION_STEP
        return {"rotation": angle}
    start = to_region(frame, press)
    end = to_region(frame, current)
    if start is None or end is None:
        return {}
    if hit.grip is Grip.MOVE:
        dx, dy = end[0] - start[0], end[1] - start[1]
        if one_axis:
            if abs(dx) >= abs(dy):
                dy = 0.0
            else:
                dx = 0.0
        return {"center_x": center[0] + dx, "center_y": center[1] + dy}

    signs = _CORNER_SIGNS[hit.corner] if hit.grip is Grip.SCALE else _EDGE_SIGNS[hit.corner]
    width = values.get("region_width", 0.0)
    height = values.get("region_height", 0.0)
    moved = to_local(center, rotation, end)
    changes: dict[str, float] = {}
    local_center = [0.0, 0.0]
    for axis, (sign, size, name) in enumerate(
        ((signs[0], width, "region_width"), (signs[1], height, "region_height"))
    ):
        if sign == 0:
            continue
        if symmetric:
            changes[name] = 2.0 * abs(moved[axis])
            continue
        # 向かいの辺（角）は動かさない そこから掴んだ所までが新しい大きさ
        opposite = -sign * size / 2.0
        grown = max(0.0, sign * (moved[axis] - opposite))
        changes[name] = grown
        local_center[axis] = opposite + sign * grown / 2.0
    if not symmetric:
        new_center = from_local(center, rotation, (local_center[0], local_center[1]))
        changes["center_x"] = new_center[0]
        changes["center_y"] = new_center[1]
    return changes
