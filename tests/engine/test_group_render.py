"""グループ制御を描くと、受け持つクリップ 1 本ずつが動くこと（GPU で描いて画素で比べる）

グループ制御の配置は、クリップ自身の配置の後ろへ掛かる（画面の中央を支点に拡大・回転して、
グループの位置だけずらす） AviUtl のグループ制御の座標・拡大率・回転と同じ 不透明度は掛け合わせ、
エフェクトはクリップ 1 本ずつに掛かる 比べる相手は、同じ動きをクリップ自身に書いた絵
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sashimono.core.commands import AddClip, AddMedia, AddTrack
from sashimono.core.commands.fixed import TRANSFORM_EFFECT_KIND, with_fixed_items
from sashimono.core.model import (
    GROUP_KIND,
    GROUP_LAYERS,
    AnimatedValue,
    Clip,
    Effect,
    GeneratedSource,
    MediaItem,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.effects import registry
from sashimono.engine.decode import probe_media
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer

SETTINGS = ProjectSettings(width=320, height=180, frame_rate=FrameRate(30))


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


@pytest.fixture
def picture(tmp_path: Path) -> MediaItem:
    """64 × 32 の画像 左半分が赤、右半分が青（回したときの向きが分かるように）"""
    from PySide6.QtGui import QImage

    pixels = np.zeros((32, 64, 4), dtype=np.uint8)
    pixels[:, :32] = (255, 40, 40, 255)
    pixels[:, 32:] = (40, 80, 255, 255)
    data = np.ascontiguousarray(pixels)
    image = QImage(data.tobytes(), 64, 32, 64 * 4, QImage.Format.Format_RGBA8888)
    path = tmp_path / "赤と青.png"
    assert image.save(str(path))
    return probe_media(path)


def _placed(clip: Clip, **values: float) -> Clip:
    clip = with_fixed_items(clip, picture=True)
    effects = []
    for effect in clip.effects:
        if effect.fixed and effect.kind == TRANSFORM_EFFECT_KIND:
            for name, value in values.items():
                effect = effect.with_param(name, AnimatedValue(value))
        effects.append(effect)
    return replace(clip, effects=tuple(effects))


def _object(media: MediaItem, **values: float) -> Clip:
    return _placed(
        Clip(timeline_start=0, duration=10, media_id=media.id, native_size=True), **values
    )


def _group(layers: int = 1, effects: tuple[Effect, ...] = (), **values: float) -> Clip:
    clip = Clip(
        timeline_start=0,
        duration=10,
        source=GeneratedSource(kind=GROUP_KIND, params={GROUP_LAYERS: layers}),
        effects=effects,
    )
    return _placed(clip, **values)


def _render(context: OffscreenGLContext, media: MediaItem, *clips: Clip) -> np.ndarray:
    project = AddMedia(media).apply(Project.create(SETTINGS))
    for index, clip in enumerate(clips):
        track = Track(kind=TrackKind.VIDEO, name=f"V{index + 1}")
        project = AddClip(track.id, clip).apply(AddTrack(track).apply(project))
    renderer = FrameRenderer(project, context=context)
    try:
        return renderer.render(0).astype(np.int16)
    finally:
        renderer.close()


def _same(first: np.ndarray, second: np.ndarray) -> None:
    difference = np.abs(first - second)
    assert (difference > 8).mean() < 0.002, "グループで動かした絵と、同じ動きを書いた絵が違う"
    assert first[:, :, :3].max() > 100, "何も描かれていない"


def test_the_group_moves_and_scales_around_its_position(
    gl_context: OffscreenGLContext, picture: MediaItem
) -> None:
    # クリップの X 10 を 2 倍してグループの X 40 を足す 右へ 60 ずれて 2 倍
    grouped = _render(
        gl_context, picture, _group(pos_x=40.0, scale=200.0), _object(picture, pos_x=10.0)
    )
    direct = _render(gl_context, picture, _object(picture, pos_x=60.0, scale=200.0))
    _same(grouped, direct)


def test_the_group_turns_each_object_around_its_position(
    gl_context: OffscreenGLContext, picture: MediaItem
) -> None:
    # 画面の中央から右へ 50 の物を時計回りに 90 度 中央から下へ 50 の所で縦向きになる
    grouped = _render(gl_context, picture, _group(rotation=90.0), _object(picture, pos_x=50.0))
    direct = _render(gl_context, picture, _object(picture, pos_y=-50.0, rotation=90.0))
    _same(grouped, direct)


def test_the_opacity_multiplies(gl_context: OffscreenGLContext, picture: MediaItem) -> None:
    group = replace(_group(), opacity=AnimatedValue(0.5))
    grouped = _render(gl_context, picture, group, _object(picture))
    direct = _render(gl_context, picture, replace(_object(picture), opacity=AnimatedValue(0.5)))
    _same(grouped, direct)


def test_the_group_effects_go_on_each_object(
    gl_context: OffscreenGLContext, picture: MediaItem
) -> None:
    # グループのエフェクトはクリップ 1 本ずつに掛かる（AviUtl のグループ制御のフィルタ効果）
    invert = registry.require("invert").create()
    grouped = _render(gl_context, picture, _group(effects=(invert,)), _object(picture))
    own = _object(picture)
    direct = _render(gl_context, picture, replace(own, effects=(invert, *own.effects)))
    _same(grouped, direct)


def test_layers_past_the_reach_stay_put(gl_context: OffscreenGLContext, picture: MediaItem) -> None:
    # 対象レイヤー数 1 で 2 本手前の物まで動くと、グループに入れていない物が一緒に動く
    grouped = _render(
        gl_context,
        picture,
        _group(pos_x=80.0),
        _object(picture, pos_y=40.0),
        _object(picture, pos_y=-40.0),
    )
    direct = _render(
        gl_context,
        picture,
        _object(picture, pos_x=80.0, pos_y=40.0),
        _object(picture, pos_y=-40.0),
    )
    _same(grouped, direct)


def test_the_group_itself_draws_nothing(gl_context: OffscreenGLContext, picture: MediaItem) -> None:
    image = _render(gl_context, picture, _group(pos_x=40.0))
    assert image[:, :, :3].max() == 0
