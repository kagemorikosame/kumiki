"""部分モザイク・ぼかしと部分フィルタの範囲が、画面のどこに来るか（プレビューで掴む枠）

範囲の値（中心 X・Y・幅・高さ・回転）は、エフェクトを掛けるときの入れ物の中で、絵の原点
（素材や図形は絵の中央、フィルタのクリップは画面の中央）から数える 画面の画素で Y は上が正
（:mod:`sashimono.effects.region`） そのエフェクトより後ろに積んだ配置や反転は、範囲ごと
絵を動かすので、枠もそれに合わせて動かす

式は :mod:`sashimono.engine.render.outline` と同じ物を使う 配置の当て方を別に書くと、
クリップの枠と範囲の枠が食い違う GL は使わない
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sashimono.core.commands.fixed import FLIP_EFFECT_KIND, TRANSFORM_EFFECT_KIND
from sashimono.core.model import Clip, Effect, EffectId, Project
from sashimono.effects import registry
from sashimono.effects.definition import turned_object
from sashimono.effects.region import PARTIAL_FILTER, REGION_BLUR
from sashimono.engine.render.outline import (
    _SHAPING_CATEGORIES,
    Extent,
    Point,
    _flip,
    _grow,
    _transform,
    _turn_pivot,
    canvas_scale,
    placed_rect,
    transform_values,
)
from sashimono.engine.render.scripts import split_effects

__all__ = [
    "REGION_KINDS",
    "Affine",
    "RegionFrame",
    "region_effects",
    "region_frame",
    "region_values",
]

#: 範囲を持つエフェクトの種類
REGION_KINDS = frozenset({REGION_BLUR, PARTIAL_FILTER})

#: 1 次の写し ``(a, b, c, d, e, f)`` で ``x' = a x + b y + c``、``y' = d x + e y + f``
Affine = tuple[float, float, float, float, float, float]

#: 範囲の数の項目
_NUMBERS = ("center_x", "center_y", "region_width", "region_height", "rotation")


@dataclass(frozen=True, slots=True)
class RegionFrame:
    """範囲の枠（合成の画素 Y は下が正）と、範囲の値の座標から合成の画素への写し"""

    #: 四隅 範囲を回す前の 左上・右上・右下・左下 が行き着いた所
    corners: tuple[Point, Point, Point, Point]
    #: 範囲の値の座標（絵の原点から 画面の画素 Y は上が正）→ 合成の画素（Y は下が正）
    to_canvas: Affine
    #: 近い値でしかない（後ろに形を変えるエフェクト・奥行きの回転・スクリプトがある）
    approximate: bool
    #: その時刻の範囲の値（中心 X・Y・幅・高さ・回転） 掴んだときの始めの値
    values: dict[str, float]


def region_effects(clip: Clip) -> tuple[Effect, ...]:
    """クリップに積んだ、範囲を持つエフェクト（積んだ順）"""
    return tuple(effect for effect in clip.effects if effect.kind in REGION_KINDS)


def region_values(effect: Effect, local_frame: int) -> dict[str, float]:
    """範囲の数の値（その時刻 設定の単位のまま）"""
    values = transform_values(effect, local_frame)
    return {
        name: float(value)
        for name, value in values.items()
        if name in _NUMBERS and isinstance(value, int | float) and not isinstance(value, bool)
    }


def region_frame(
    project: Project,
    clip: Clip,
    effect_id: EffectId,
    frame: int,
    *,
    canvas: tuple[int, int] | None = None,
    extent: Extent | None = None,
) -> RegionFrame | None:
    """``frame``（タイムラインのフレーム）での範囲の枠 出せなければ ``None``

    ``canvas`` と ``extent`` は :func:`~sashimono.engine.render.outline.clip_outline` と同じ
    """
    effect = next((e for e in clip.effects if e.id == effect_id), None)
    if effect is None or effect.kind not in REGION_KINDS:
        return None
    width, height = canvas if canvas is not None else project.settings.resolution
    placed = placed_rect(project, clip, (width, height), extent)
    if placed is None:
        return None
    rect, space, offset = placed
    scale = canvas_scale(project.settings.resolution, (width, height))[0]
    local = frame - clip.timeline_start
    left, top, right, bottom = rect
    space_height = space[1]
    # シェーダと同じ向き（Y は上が正）の入れ物 原点は読み込んだときの入れ物の中央
    # （EffectProcessor の u_origin） 後ろで入れ物を広げても原点は動かない
    obj = (left, space_height - bottom, right, space_height - top)
    origin = ((obj[0] + obj[2]) / 2.0, (obj[1] + obj[3]) / 2.0)
    # 値の座標の 3 点（原点・右へ 1・上へ 1）を、描く側と同じ道で送る 写しは 1 次なので、
    # 3 点から全体が決まる 四隅を送るより、幅や高さが 0 の範囲でも写しが潰れない
    points: list[Point] = [
        origin,
        (origin[0] + scale, origin[1]),
        (origin[0], origin[1] + scale),
    ]
    approximate = any(e.enabled for e in split_effects(clip.effects)[1])
    reached = False
    # 部分フィルタは、後ろのエフェクトを掛けた絵を、ここへ来た時点の座標の範囲で混ぜる
    # 後ろのエフェクトが絵を動かしても範囲は動かない 範囲が閉じるのは、次の部分フィルタか
    # 固定の欄（配置・反転）の手前（EffectProcessor の決まり）
    scoped = effect.kind == PARTIAL_FILTER
    gpu_effects, _ = split_effects(clip.effects)
    for current in gpu_effects:
        if current.id == effect.id:
            reached = True
            continue
        if not current.enabled:
            continue
        definition = registry.get(current.kind)
        if definition is None or definition.audio_process is not None:
            continue
        if reached and scoped:
            if current.fixed or definition.scopes_following:
                scoped = False
            else:
                continue
        if reached and current.kind == TRANSFORM_EFFECT_KIND:
            values = transform_values(current, local, scale)
            points, _, tilted = _transform(points, values, space, obj)
            approximate = approximate or tilted
            continue
        if reached and current.kind == FLIP_EFFECT_KIND:
            points = _flip(points, current, obj)
            continue
        if definition.is_idle(current):
            continue
        if reached and (
            definition.category in _SHAPING_CATEGORIES
            or definition.expands_object is not None
            or definition.turns_object
        ):
            approximate = True
        if definition.turns_object:
            obj = turned_object(obj, _turn_pivot(current, obj, space, local, scale))
        if definition.expands_object is not None:
            obj = _grow(obj, definition.expands_object, current, local, scale)

    def screen(point: Point) -> Point:
        return (point[0] + offset[0], space_height - point[1] + offset[1])

    base, right_step, up_step = (screen(point) for point in points)
    to_canvas: Affine = (
        right_step[0] - base[0],
        up_step[0] - base[0],
        base[0],
        right_step[1] - base[1],
        up_step[1] - base[1],
        base[1],
    )
    numbers = region_values(effect, local)
    corners = tuple(apply(to_canvas, point) for point in region_corners(numbers))
    return RegionFrame(
        corners=(corners[0], corners[1], corners[2], corners[3]),
        to_canvas=to_canvas,
        approximate=approximate,
        values=numbers,
    )


def region_corners(values: dict[str, float]) -> list[Point]:
    """範囲の四隅（値の座標 Y は上が正） 左上・右上・右下・左下

    回転は正で時計回り（シェーダの ``region_local`` の逆向き）
    """
    center = (values.get("center_x", 0.0), values.get("center_y", 0.0))
    half_width = values.get("region_width", 0.0) / 2.0
    half_height = values.get("region_height", 0.0) / 2.0
    return [
        from_local(center, values.get("rotation", 0.0), (x, y))
        for x, y in (
            (-half_width, half_height),
            (half_width, half_height),
            (half_width, -half_height),
            (-half_width, -half_height),
        )
    ]


def from_local(center: Point, rotation: float, local: Point) -> Point:
    """範囲の中の座標（回す前 中心から）→ 値の座標 時計回りに ``rotation`` 度回す"""
    angle = math.radians(rotation)
    cosine, sine = math.cos(angle), math.sin(angle)
    x, y = local
    return (center[0] + cosine * x + sine * y, center[1] - sine * x + cosine * y)


def to_local(center: Point, rotation: float, point: Point) -> Point:
    """:func:`from_local` の逆（シェーダの ``region_local`` と同じ式）"""
    angle = math.radians(rotation)
    cosine, sine = math.cos(angle), math.sin(angle)
    dx, dy = point[0] - center[0], point[1] - center[1]
    return (cosine * dx - sine * dy, sine * dx + cosine * dy)


def apply(affine: Affine, point: Point) -> Point:
    a, b, c, d, e, f = affine
    return (a * point[0] + b * point[1] + c, d * point[0] + e * point[1] + f)


def invert(affine: Affine) -> Affine | None:
    """逆の写し 潰れていて戻せなければ ``None``（拡大率 0 の配置の後ろなど）"""
    a, b, c, d, e, f = affine
    determinant = a * e - b * d
    if abs(determinant) < 1e-12:
        return None
    ia, ib = e / determinant, -b / determinant
    id_, ie = -d / determinant, a / determinant
    return (ia, ib, -(ia * c + ib * f), id_, ie, -(id_ * c + ie * f))
