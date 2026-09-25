"""選んだクリップが画面のどこに描かれるか（プレビューで掴む外枠）

プレビューで絵を直接動かすために、クリップの四隅が合成の画素のどこへ来るかを求める
GL を使わない純粋な計算にしてある 別のスレッドの先読みが出した絵にも、描かずに
枠を重ねられるように

式は描く側と同じにする 置き方はレンダラの ``_media_placement`` と ``_draw_oversized``、
配置はシェーダの ``_TRANSFORM``、反転は ``_FLIP`` ずれると、枠と絵が食い違って
掴んだ所と動く物が合わなくなる 一致は tests/engine/test_outline.py が画素で見る
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from sashimono.core.commands.fixed import FLIP_EFFECT_KIND, TRANSFORM_EFFECT_KIND
from sashimono.core.model import Clip, Effect, Project
from sashimono.effects import registry
from sashimono.effects.definition import turned_object
from sashimono.effects.spec import CheckSpec, SelectSpec, TrackSpec
from sashimono.engine.gpu import fit_placement
from sashimono.engine.render.scripts import split_effects

__all__ = [
    "Outline",
    "Point",
    "canvas_scale",
    "clip_outline",
    "has_outline",
    "is_generated",
    "media_pixel_size",
    "transform_values",
]

#: 画素の座標（左上が原点、Y は下が正 画面へ描くときの向き）
Point = tuple[float, float]

#: 範囲（左・上・右・下 画素、Y は下が正）
Box = tuple[float, float, float, float]

#: 絵の入れ物と絵の大きさ 生成オブジェクトだけがレンダラから受け取る
Extent = tuple[Box, tuple[int, int]]


#: 絵の見える範囲や位置を変えうる分類（切り抜き・形・動き・登場と退場） 色やぼかしは変えない
_SHAPING_CATEGORIES = frozenset({"変形", "形", "動き", "登場・退場"})


@dataclass(frozen=True, slots=True)
class Outline:
    """クリップの外枠（合成の画素 Y は下が正）"""

    #: 四隅 置く前の絵の 左上・右上・右下・左下 が行き着いた所 回せば並びごと回る
    corners: tuple[Point, Point, Point, Point]
    #: 固定の配置の拡大と回転の中心が、画面のどこに来ているか
    pivot: Point
    #: 近い値でしかない 固定の配置より後ろに絵を変えるエフェクトがある、奥行きの回転がある、
    #: スクリプトを積んでいる、のどれか 枠を点線にして、ぴったりではないと分かるようにする
    approximate: bool


def canvas_scale(resolution: tuple[int, int], canvas: tuple[int, int]) -> tuple[float, float]:
    """合成の画素 1 つが、画面（プロジェクトの解像度）の画素いくつ分かの逆数（横, 縦）

    画質を落としたプレビューは合成が小さい 画素で決める値（位置・大きさ・ぼかしの強さ）は
    これを掛けて合成の画素へ直す 割り切れない解像度（1366 の 1/4 は 341）もあるので、
    分母ではなく実際の大きさの比で数える 描く側（レンダラ）・外枠・ドラッグの換算が同じ物を
    使う 別々に数えると、枠と絵が端数の分だけずれる
    """
    return canvas[0] / max(resolution[0], 1), canvas[1] / max(resolution[1], 1)


def is_generated(clip: Clip) -> bool:
    """絵をこちらで作るクリップか

    音声波形は素材（音声ファイル）を持つが、絵は素材から取り出すのではなく描く
    素材を持つからと映像を取り出しに行くと、音声だけの素材なので何も出ない
    """
    if clip.media_id is None:
        return True
    source = clip.source
    return (
        source is not None and source.kind == "shape" and source.params.get("shape") == "waveform"
    )


def has_outline(clip: Clip) -> bool:
    """外枠を出せるクリップか

    シーン・画面の写し取り・フィルタ・場面切り替えは、自分の絵を 1 枚の矩形として置かない
    （下の絵や入れ子の合成をそのまま使う） 枠を出すと、掴んでも枠のとおりには動かない
    """
    if clip.scene_id is not None or clip.is_filter:
        return False
    source = clip.source
    return source is None or source.kind not in ("framebuffer", "transition")


def media_pixel_size(project: Project, clip: Clip) -> tuple[int, int] | None:
    """素材の映像の、回転を当てた後の画素の大きさ 分からなければ ``None``"""
    if clip.media_id is None or is_generated(clip):
        return None
    media = project.find_media(clip.media_id)
    if media is None or not media.video_streams:
        return None
    # デコーダと同じ選び方（番号が合う物、無ければ最初の映像）
    stream = next(
        (s for s in media.video_streams if s.index == clip.stream_index),
        media.video_streams[0],
    )
    # 縦に撮った素材はデコーダが回して渡す 回す前の幅と高さで置くと縦横が入れ替わる
    if stream.rotation in (90, 270):
        return stream.height, stream.width
    return stream.width, stream.height


def transform_values(
    effect: Effect, local_frame: int, scale: float = 1.0
) -> dict[str, float | str | bool]:
    """配置のエフェクトの、その時刻の値 欠けた項目は定義の既定で埋める

    ``scale`` は画素の値（X・Y・中心）を合成の画素へ縮める割合（:func:`canvas_scale`）
    描く側（EffectProcessor）と同じく縮める 省くと設定の値そのもの（ドラッグの始めの値）
    """
    definition = registry.get(effect.kind)
    values: dict[str, float | str | bool] = {}
    if definition is None:
        return values
    for spec in definition.parameters:
        raw = effect.params.get(spec.name)
        if isinstance(spec, TrackSpec):
            # 壊れた数は描く側と同じく既定へ戻す（scaled_at）
            values[spec.name] = spec.scaled_at(
                spec.coerce(spec.default_value() if raw is None else raw), local_frame, scale
            )
        elif isinstance(spec, SelectSpec | CheckSpec):
            values[spec.name] = spec.coerce(spec.default_value() if raw is None else raw)
    return values


def clip_outline(
    project: Project,
    clip: Clip,
    frame: int,
    *,
    canvas: tuple[int, int] | None = None,
    extent: Extent | None = None,
) -> Outline | None:
    """``frame``（タイムラインのフレーム）に描いた ``clip`` の外枠 出せなければ ``None``

    ``canvas`` は合成の大きさ（画質を落としたプレビューなら小さい） 省くとプロジェクトの
    解像度 ``extent`` は生成オブジェクトの入れ物と絵の大きさで、レンダラの
    :meth:`FrameRenderer.object_extent` が返す物を渡す 素材のクリップは要らない
    """
    if not has_outline(clip):
        return None
    width, height = canvas if canvas is not None else project.settings.resolution
    local = frame - clip.timeline_start

    if is_generated(clip):
        if extent is None:
            return None
        box, (image_width, image_height) = extent
        if image_width > width or image_height > height:
            # 画面より大きく作った絵は、絵の大きさのバッファで配置を掛けてから中心に置く
            # （レンダラの _draw_oversized） 支点の「画面の中央」は絵の中央と重なる
            space = (float(image_width), float(image_height))
            offset = ((width - image_width) / 2.0, (height - image_height) / 2.0)
            rect = box
        else:
            placed = fit_placement(image_width, image_height, width, height)
            space = (float(width), float(height))
            offset = (0.0, 0.0)
            scale_x = placed.width / max(image_width, 1)
            scale_y = placed.height / max(image_height, 1)
            rect = (
                placed.left + box[0] * scale_x,
                placed.top + box[1] * scale_y,
                placed.left + box[2] * scale_x,
                placed.top + box[3] * scale_y,
            )
    else:
        size = media_pixel_size(project, clip)
        if size is None:
            return None
        if clip.native_size:
            project_width, project_height = project.settings.resolution
            placed_width = size[0] * width / max(project_width, 1)
            placed_height = size[1] * height / max(project_height, 1)
            left, top = (width - placed_width) / 2.0, (height - placed_height) / 2.0
        else:
            placed = fit_placement(size[0], size[1], width, height)
            left, top, placed_width, placed_height = (
                placed.left,
                placed.top,
                placed.width,
                placed.height,
            )
        space = (float(width), float(height))
        offset = (0.0, 0.0)
        rect = (left, top, left + placed_width, top + placed_height)

    # 描く側（EffectProcessor.pixel_scale）と同じく横の比 1 つで縮める
    scale = canvas_scale(project.settings.resolution, (width, height))[0]
    return _follow_effects(clip, local, rect, space, offset, scale)


def _follow_effects(
    clip: Clip, local: int, rect: Box, space: tuple[float, float], offset: Point, scale: float
) -> Outline:
    """置いた矩形に、クリップのエフェクトの並びのうち形を動かす物を順に当てる

    ``scale`` は画素の値を合成の画素へ縮める割合 画質を落としたプレビューでは、描く側が
    X・Y や広げる量を縮めて当てる 枠だけ縮めずに当てると、掴む枠が絵から 2 倍・4 倍離れる
    """
    space_width, space_height = space
    # ここから先はシェーダと同じ向き（Y は上が正）で数える
    left, top, right, bottom = rect
    points: list[Point] = [
        (left, space_height - top),
        (right, space_height - top),
        (right, space_height - bottom),
        (left, space_height - bottom),
    ]
    # 絵が置かれた範囲（u_object 左・下・右・上） 配置では変わらず、広げるエフェクトだけが広げる
    obj = (left, space_height - bottom, right, space_height - top)
    # 固定の配置がまだ無ければ、足したときの既定（画面の中央）が中心になる
    pivot: Point = (space_width / 2.0, space_height / 2.0)
    approximate = False
    after_fixed = False

    gpu_effects, scripts = split_effects(clip.effects)
    if any(effect.enabled for effect in scripts):
        # スクリプトは別の道で描き、絵をどう動かすかは中身次第
        approximate = True
    for effect in gpu_effects:
        if not effect.enabled:
            continue
        definition = registry.get(effect.kind)
        if definition is None or definition.audio_process is not None:
            continue
        if effect.kind == TRANSFORM_EFFECT_KIND:
            # 既定のままの配置も通す 描く側は飛ばすが、絵は動かないので点も動かない
            # 飛ばすと、中心だけをずらした配置で拡大の中心を見失う
            values = transform_values(effect, local, scale)
            points, center, tilted = _transform(points, values, space, obj)
            approximate = approximate or tilted or after_fixed
            if effect.fixed:
                pivot = center
                after_fixed = True
            continue
        if definition.is_idle(effect):
            continue
        if effect.kind == FLIP_EFFECT_KIND:
            points = _flip(points, effect, obj)
            continue
        if after_fixed:
            # 置いた後の絵に掛かる物は、形を変えるかどうかを中身から決められない
            approximate = True
        elif (
            definition.category in _SHAPING_CATEGORIES
            or definition.expands_object is not None
            or definition.turns_object
        ):
            # 置く前でも、切り抜き・領域拡張・動きの効果は絵の見える範囲や位置を変える
            # 枠は置いた矩形のままなので、ぴったりだと言わない
            approximate = True
        if definition.turns_object:
            obj = turned_object(obj)
        if definition.expands_object is not None:
            obj = _grow(obj, definition.expands_object, effect, local, scale)

    def screen(point: Point) -> Point:
        return (point[0] + offset[0], space_height - point[1] + offset[1])

    return Outline(
        corners=(screen(points[0]), screen(points[1]), screen(points[2]), screen(points[3])),
        pivot=screen(pivot),
        approximate=approximate,
    )


def _transform(
    points: Sequence[Point],
    values: dict[str, float | str | bool],
    space: tuple[float, float],
    obj: Box,
) -> tuple[list[Point], Point, bool]:
    """シェーダの _TRANSFORM を前向きに当てる 当てた点・拡大と回転の中心・奥行きの回転の有無

    シェーダは出力の画素から入力を逆算する（支点を引く → 回す → 縮める → 支点を足す）
    ここではその逆を順に辿る 回転は正で時計回り（画面の向き）
    """

    def number(name: str, default: float) -> float:
        value = values.get(name, default)
        return float(value) if isinstance(value, int | float) else default

    left, bottom, right, top = obj
    center = ((left + right) / 2.0, (bottom + top) / 2.0)
    # 原点は範囲の中央（EffectProcessor が origin を渡さないとき）
    origin = center
    base_x, base_y = space[0] / 2.0, space[1] / 2.0
    pivot_h = str(values.get("pivot_h", "screen"))
    pivot_v = str(values.get("pivot_v", "screen"))
    base_x = {
        "left": left,
        "right": right,
        "center": center[0],
        "origin": origin[0],
    }.get(pivot_h, base_x)
    base_y = {
        "top": top,
        "bottom": bottom,
        "middle": center[1],
        "origin": origin[1],
    }.get(pivot_v, base_y)
    anchor = (base_x + number("anchor_x", 0.0), base_y + number("anchor_y", 0.0))
    shift = (number("pos_x", 0.0), number("pos_y", 0.0))
    if values.get("move_to_pivot") is True:
        # シェーダは出力に (anchor - center) を足してから逆算する 前向きでは引く
        shift = (shift[0] - (anchor[0] - center[0]), shift[1] - (anchor[1] - center[1]))
    whole = max(number("scale", 100.0) / 100.0, 0.0001)
    scale_x = max(number("scale_x", 100.0) / 100.0, 0.0001) * whole
    scale_y = max(number("scale_y", 100.0) / 100.0, 0.0001) * whole
    angle = math.radians(number("rotation", 0.0))
    cosine, sine = math.cos(angle), math.sin(angle)

    moved: list[Point] = []
    for x, y in points:
        dx = (x - anchor[0]) * scale_x
        dy = (y - anchor[1]) * scale_y
        # 上が正の座標で時計回り
        rx = cosine * dx + sine * dy
        ry = -sine * dx + cosine * dy
        moved.append((rx + anchor[0] + shift[0], ry + anchor[1] + shift[1]))
    pivot = (anchor[0] + shift[0], anchor[1] + shift[1])
    tilted = number("rotation_x", 0.0) != 0.0 or number("rotation_y", 0.0) != 0.0
    return moved, pivot, tilted


def _flip(points: Sequence[Point], effect: Effect, obj: Box) -> list[Point]:
    """シェーダの _FLIP と同じく、絵の置かれた範囲の中心を軸に裏返す"""
    definition = registry.get(effect.kind)
    flags = {"horizontal": False, "vertical": False}
    if definition is not None:
        for spec in definition.parameters:
            if isinstance(spec, CheckSpec) and spec.name in flags:
                raw = effect.params.get(spec.name)
                flags[spec.name] = spec.coerce(spec.default_value() if raw is None else raw)
    center_x, center_y = (obj[0] + obj[2]) / 2.0, (obj[1] + obj[3]) / 2.0
    return [
        (
            2.0 * center_x - x if flags["horizontal"] else x,
            2.0 * center_y - y if flags["vertical"] else y,
        )
        for x, y in points
    ]


def _grow(
    obj: Box, names: tuple[str, str, str, str], effect: Effect, local: int, scale: float
) -> Box:
    """入れ物を広げるエフェクトの後の範囲（EffectProcessor._grow_object と同じ）"""
    definition = registry.get(effect.kind)
    amounts: list[float] = []
    for name in names:
        spec = definition.spec(name) if definition is not None else None
        raw = effect.params.get(name)
        if isinstance(spec, TrackSpec):
            # 壊れた数は描く側（EffectProcessor）と同じく既定へ戻す（scaled_at） そのまま
            # 足すと枠が描けない座標になる
            raw_value = spec.default_value() if raw is None else raw
            amounts.append(spec.scaled_at(spec.coerce(raw_value), local, scale))
        else:
            amounts.append(0.0)
    grow_top, grow_bottom, grow_left, grow_right = amounts
    return (obj[0] - grow_left, obj[1] - grow_bottom, obj[2] + grow_right, obj[3] + grow_top)
