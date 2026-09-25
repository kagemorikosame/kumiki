"""描いた絵を縮めて置くときの読み方（#179）

エンジンは絵をストレートのアルファで持つ 透明な画素にも RGB が残っていて、縮めるときに
隣の画素と RGB のまま混ぜると、その色が縁へにじむ（透明な白なら縁が白っぽく浮き、
透明な黒なら黒ずむ） ここでは、透明な所の色が何であっても縮めた結果に出ないことと、
細かい模様を縮めたときに縞（モアレ）が出ないことを、描いた結果の画素で見る
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pytest

from sashimono.core.model import Effect
from sashimono.effects.definition import registry
from sashimono.engine.gpu import (
    Compositor,
    EffectProcessor,
    GLContextError,
    OffscreenGLContext,
    Texture,
)
from sashimono.engine.gpu.glutil import FULL_RECT, Framebuffer

SIZE = 128


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


@pytest.fixture(scope="module")
def processor(gl_context: OffscreenGLContext) -> Iterator[EffectProcessor]:
    with gl_context:
        compositor = Compositor(SIZE, SIZE)
        created = EffectProcessor(SIZE, SIZE, compositor.quad)
        yield created
        created.release()
        compositor.release()


def _run(
    gl_context: OffscreenGLContext,
    processor: EffectProcessor,
    picture: np.ndarray,
    effects: tuple[Effect, ...] = (),
    *,
    rect: tuple[float, ...] = FULL_RECT,
) -> np.ndarray:
    """``picture`` を置いてエフェクトを掛けた結果（リニア・ストレート、GL の向き）"""
    with gl_context:
        texture = Texture.from_array(picture)
        result: Framebuffer = processor.apply(
            texture, effects, frame=0, fps=30.0, source_rect=rect, flip_source=False
        )
        from OpenGL import GL

        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, result.handle)
        raw = GL.glReadPixels(0, 0, SIZE, SIZE, GL.GL_RGBA, GL.GL_FLOAT)
        texture.release()
    return np.frombuffer(raw, dtype=np.float32).reshape(SIZE, SIZE, 4).copy()


def _red_on(hidden: tuple[int, int, int]) -> np.ndarray:
    """透明な地（RGB は ``hidden``）の真ん中に、赤い四角"""
    image = np.zeros((SIZE, SIZE, 4), dtype=np.uint8)
    image[..., :3] = hidden
    image[40:88, 40:88] = (255, 0, 0, 255)
    return image


def _shrink(scale: float, rotation: float = 0.0) -> Effect:
    return registry.require("transform").create(scale=scale, rotation=rotation)


@pytest.mark.parametrize("hidden", [(255, 255, 255), (0, 0, 0)], ids=["white", "black"])
@pytest.mark.parametrize(
    ("scale", "rotation"), [(50.0, 0.0), (80.4, 0.0), (70.0, 30.0)], ids=["half", "80", "turned"]
)
def test_a_shrunk_edge_keeps_its_colour(
    gl_context: OffscreenGLContext,
    processor: EffectProcessor,
    hidden: tuple[int, int, int],
    scale: float,
    rotation: float,
) -> None:
    """縮めた四角の縁は、透明な所の色が白でも黒でも同じ赤のまま

    RGB のまま混ぜると、透明な白の上では縁が桃色に、透明な黒の上では暗い赤になる
    YMM4 の格子の背景（``#00FFFFFF``）を縮めた SFっぽい吹き出し(右) が白っぽい板になった
    """
    image = _run(gl_context, processor, _red_on(hidden), (_shrink(scale, rotation),))
    seen = image[..., 3] > 0.02
    assert seen.any(), "何も描かれていない"
    assert float(image[seen][:, 1].max()) < 0.02, "透明な白が縁ににじんだ"
    assert float(image[seen][:, 0].min()) > 0.98, "透明な黒が縁ににじんだ"


@pytest.mark.parametrize("hidden", [(255, 255, 255), (0, 0, 0)], ids=["white", "black"])
def test_a_moving_effect_keeps_the_edge_colour(
    gl_context: OffscreenGLContext, processor: EffectProcessor, hidden: tuple[int, int, int]
) -> None:
    """画素の位置で読み直す動きのエフェクト（ランダム拡大など）も、縁に透明な所の色を出さない

    YMM4 の登場・退場の拡大や揺れはこの読み方（``sample_pixel``）で絵を縮める 変形だけを
    直すと、同じ図形が動きのエフェクトで縮めたときだけ縁が白っぽくなる
    """
    zoom = registry.require("random_zoom").create(zoom=100, zoom_x=70, zoom_y=70)
    image = _run(gl_context, processor, _red_on(hidden), (zoom,))
    seen = image[..., 3] > 0.02
    assert seen.any(), "何も描かれていない"
    assert float(image[seen][:, 1].max()) < 0.02, "透明な白が縁ににじんだ"
    assert float(image[seen][:, 0].min()) > 0.98, "透明な黒が縁ににじんだ"


def test_a_placed_picture_keeps_its_edge_colour(
    gl_context: OffscreenGLContext, processor: EffectProcessor
) -> None:
    """素材を小さな枠へ収めて置くときも、透明な所の白が縁へにじまない"""
    small = (-0.4, -0.37, 0.4, 0.43)
    image = _run(gl_context, processor, _red_on((255, 255, 255)), rect=small)
    seen = image[..., 3] > 0.02
    assert seen.any(), "何も描かれていない"
    assert float(image[seen][:, 1].max()) < 0.02, "透明な白が縁ににじんだ"


def test_fine_lines_shrink_without_moire(
    gl_context: OffscreenGLContext, processor: EffectProcessor
) -> None:
    """1 画素おきの線を 60% に縮めると、どこも同じくらいの濃さの灰色になる

    近い 1 点だけを読むと、縮めた画素が線に乗るか隙間に乗るかで濃さが周期的に揺れ、
    大きな縞が浮く（1 点だけを読むと、線の上の行と隙間の行が 0 と 1 まで開く）
    """
    lines = np.zeros((SIZE, SIZE, 4), dtype=np.uint8)
    lines[..., 3] = 255
    lines[::2, :, :3] = 255
    image = _run(gl_context, processor, lines, (_shrink(60.0),))
    middle = image[40:88, 40:88, 1]
    # 白と黒が半分ずつ リニアのまま平均するので 0.5 の前後
    rows = middle.mean(axis=1)
    assert float(rows.max() - rows.min()) < 0.3, f"行ごとの濃さが揺れる: {rows.round(2)}"
