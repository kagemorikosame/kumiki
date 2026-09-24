"""YMM4 の「図形で切り抜く」（マスク）の位置を、配布物で確かめる（#173）

あおもや式テンプレート集（``tests/fixtures/ymm4/aomoya``）を置くと走る 配布物なので
リポジトリには入れていない（無ければ飛ばす）

手元の配布物（AviUtl 270 本・YMM4 230 本）でマスクの位置を数えた結果

* YMM4 の MaskEffect は 18 個 位置 Y が 0 でないのは 5 個で、木製看板テロップの釘穴 4 つ
  （Y = ±132）と、周辺カラークリスタルノイズの四角（Y = -14）
* AviUtl の 扇クリッピング は 2 個（どちらも手元で作った確かめ用の物） 中心Y はどちらも 0

YMM4 の Y は下が正、こちらの ``center_y`` は上が正で、読み込みが符号を入れ替える
シェーダも上を正として読むので、YMM4 で上にある釘穴は画面でも上に出るはず
表示名だけが「下が正」と書いていた 表示を信じて値を入れると上下が逆になる
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.mapped import MappedObject
from sashimono.compat.ymm4.template import load_template, map_template
from sashimono.core.model import AnimatedValue, Effect
from sashimono.engine.gpu import (
    Compositor,
    EffectProcessor,
    GLContextError,
    OffscreenGLContext,
    Texture,
)

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "ymm4" / "aomoya"
SIGN = ROOT / "テロップ_木製看板テロップ.ymmt"

#: 釘穴（±269, ±132）が収まる大きさ
WIDTH, HEIGHT = 640, 320


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


def _masks(items: list[MappedObject]) -> Iterator[Effect]:
    for item in items:
        yield from (effect for effect in item.clip.effects if effect.kind == "shape_mask")
        yield from _masks(list(item.children))


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _still(node: dict[str, Any], key: str) -> float:
    """YMM4 の動かせる値の最初の値"""
    return float(node[key]["Values"][0]["Value"])


def _hole(gl_context: OffscreenGLContext, effect: Effect) -> tuple[float, float]:
    """一面に塗った絵へ切り抜きを掛け、抜けた所の真ん中（画像の向き 左上が原点）"""
    picture = np.full((HEIGHT, WIDTH, 4), 255, dtype=np.uint8)
    with gl_context:
        compositor = Compositor(WIDTH, HEIGHT)
        processor = EffectProcessor(WIDTH, HEIGHT, compositor.quad)
        texture = Texture.from_array(picture)
        try:
            result = processor.apply(texture, (effect,), frame=0, fps=30.0, duration=60)
            from OpenGL import GL

            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, result.handle)
            raw = GL.glReadPixels(0, 0, WIDTH, HEIGHT, GL.GL_RGBA, GL.GL_FLOAT)
        finally:
            texture.release()
            processor.release()
            compositor.release()
    alpha = np.frombuffer(raw, dtype=np.float32).reshape(HEIGHT, WIDTH, 4)[..., 3]
    # 読み出しは GL の向き（下の行から） 画像の向きへ裏返す
    rows, columns = np.nonzero(alpha[::-1] < 0.5)
    assert rows.size, "切り抜きの穴が見つからない"
    return float(columns.mean()), float(rows.mean())


def test_the_nail_holes_of_the_wooden_sign_stay_where_ymm4_puts_them(
    gl_context: OffscreenGLContext,
) -> None:
    """木製看板テロップの釘穴 4 つは、YMM4 と同じ上下の位置に抜ける

    YMM4 で Y = -132（上）の穴が下に抜けると、看板の上の釘が下の縁へ移り、
    上下で 264 画素ずれる
    """
    if not SIGN.is_file():
        pytest.skip(f"{SIGN.name} が置かれていない（配布物）")
    templates = load_template(SIGN)
    raw = [
        node
        for node in _walk(list(templates[0].items))
        if str(node.get("$type", "")).split(",")[0].endswith("Effects.MaskEffect")
    ]
    masks = list(_masks(map_template(list(templates[0].items), report=CompatibilityReport())))
    assert len(masks) == len(raw) == 4

    for source, effect in zip(raw, masks, strict=True):
        centre_y = effect.params["center_y"]
        assert isinstance(centre_y, AnimatedValue)
        assert centre_y.static != 0.0
        x, y = _hole(gl_context, effect)
        # YMM4 の位置は絵の中心から、Y は下が正 画像の向きと同じなので、そのまま足す
        assert x == pytest.approx(WIDTH / 2 + _still(source, "X"), abs=1.5)
        assert y == pytest.approx(HEIGHT / 2 + _still(source, "Y"), abs=1.5)
