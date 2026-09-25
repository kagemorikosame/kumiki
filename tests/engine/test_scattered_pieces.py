"""粒や欠片を画面へ撒くエフェクト（パーティクル・破片）が、撒いた分を描き切るか（#199）

どちらも出力の画素ごとに「ここへ来る粒・欠片」を探して描く 探す数や範囲に上限が
あると、上限の外の粒や欠片は画面の中に居ても黙って消える ここではそれを実際に
描いて確かめる 乱数の並びは見ず、描かれた量と広がりだけを見る
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import numpy as np
import pytest

from sashimono.core.model import (
    AnimatedValue,
    Clip,
    Effect,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.effects import registry
from sashimono.effects.sources import SHAPE
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer

WIDTH, HEIGHT = 480, 480
FPS = 30


@pytest.fixture(scope="module")
def gl() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


@pytest.fixture
def draw(gl: OffscreenGLContext) -> Callable[..., np.ndarray]:
    """真ん中に白い四角を置いてエフェクトを掛け、``frame`` の絵を返す"""

    def run(effect: Effect, *, size: int, frame: int) -> np.ndarray:
        source = SHAPE.create(shape="rect", width=size, height=size, color=(1.0, 1.0, 1.0, 1.0))
        project = Project.create(
            ProjectSettings(width=WIDTH, height=HEIGHT, frame_rate=FrameRate(FPS))
        )
        clip = Clip(timeline_start=0, duration=60, source=source, effects=(effect,))
        track = Track(kind=TrackKind.VIDEO, clips=(clip,))
        project = project.with_timeline(
            project.timeline.__class__(rate=project.rate, tracks=(track,))
        )
        renderer = FrameRenderer(project, context=gl)
        try:
            return renderer.render(frame).astype(float)
        finally:
            renderer.close()

    return run


def _still(**values: float) -> dict[str, AnimatedValue]:
    return {key: AnimatedValue(value) for key, value in values.items()}


class TestParticles:
    def test_old_particles_past_512_are_still_drawn(self, draw: Callable[..., np.ndarray]) -> None:
        """1 秒に 1000 粒・寿命 1 秒なら、生まれて 0.9 秒の粒も描かれる

        粒は右へ一定の速さで流れるので、左端から右端へ若い順に並ぶ 調べる粒の数が
        512 で切れていると、生まれて 0.512 秒より古い右の半分が消える
        """
        effect = registry.require("particles").create(
            **_still(
                rate=1000.0,
                lifetime=1.0,
                preroll=2.0,
                emitter_x=-100.0,
                emit_angle=0.0,
                speed=200.0,
                randomness=0.0,
            )
        )
        image = draw(effect, size=4, frame=0)
        row = image[HEIGHT // 2 - 4 : HEIGHT // 2 + 4, :, :3].max(axis=(0, 2))
        young = row[WIDTH // 2 - 80 : WIDTH // 2 - 60]
        old = row[WIDTH // 2 + 60 : WIDTH // 2 + 80]
        assert young.min() > 100, "若い粒が描かれていない"
        assert old.min() > 100, "生まれて 0.8 秒より古い粒が消えた"


class TestCrash:
    def test_far_flung_pieces_are_still_drawn(self, draw: Callable[..., np.ndarray]) -> None:
        """欠片が大きく散っても、画面の中にある欠片は数が減らない

        欠片を探す範囲を見込み位置の周り 7x7 に決め打ちすると、欠片の大きさの 3 つ分より
        遠くへ飛んだ欠片は、画面の真ん中にあっても描かれない 重さも回転も切って、
        散った欠片の面積が元の絵の面積とおよそ同じかを見る
        """
        size = 40
        effect = registry.require("crash").create(
            **_still(size=4.0, fall=0.0, delay=0.0, spin=0.0, fly=100.0, impact=100.0)
        )
        # 0.3 秒で真ん中から 90 画素ほど外へ飛ぶ 画面（480 四方）の中に収まる
        image = draw(effect, size=size, frame=9)
        covered = image[..., :3].max(axis=2).sum() / 255.0
        assert covered > size * size * 0.7, f"散った欠片が消えた: {covered:.0f} 画素分"
        # 真ん中に固まったままではなく、ちゃんと散っている
        _, lit_columns = np.where(image[..., :3].max(axis=2) > 100)
        assert lit_columns.max() - lit_columns.min() > size * 3, "欠片が散っていない"
