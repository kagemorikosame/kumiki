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
    Keyframe,
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

    def run(*effects: Effect, size: int, frame: int) -> np.ndarray:
        source = SHAPE.create(shape="rect", width=size, height=size, color=(1.0, 1.0, 1.0, 1.0))
        project = Project.create(
            ProjectSettings(width=WIDTH, height=HEIGHT, frame_rate=FrameRate(FPS))
        )
        clip = Clip(timeline_start=0, duration=60, source=source, effects=effects)
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


def _held(value: float) -> AnimatedValue:
    """範囲で切られない値 キーフレームの付いた値は読み込んだまま届く（前の版の保存など）"""
    return AnimatedValue(
        static=value, keyframes=(Keyframe(frame=0, value=value), Keyframe(frame=60, value=value))
    )


def _enlarged() -> Effect:
    """絵を 4 倍に広げる変形 絵の置かれた範囲（u_object）は広がらないまま後ろへ届く"""
    return registry.require("transform").create(scale=AnimatedValue(400.0))


def _lit_width(image: np.ndarray) -> int:
    columns = np.where(image[..., :3].max(axis=2).max(axis=0) > 100)[0]
    return 0 if not len(columns) else int(columns.max() - columns.min() + 1)


class TestKeepsContent:
    @pytest.mark.parametrize(
        "kind",
        sorted(d.kind for d in registry.all() if d.keeps_content),
    )
    def test_nothing_appears_outside_the_picture(
        self, draw: Callable[..., np.ndarray], kind: str
    ) -> None:
        """中身を動かさない印の付いたエフェクトは、絵の外（透明な所）に何も置かない

        印を付けた物の後では、粒や欠片を探す範囲を元の絵の範囲に狭める 外に色を置く物に
        印があると、その色が粒や欠片から切れる
        """
        size = 40
        image = draw(registry.require(kind).create(), size=size, frame=0)
        half = size // 2 + 2
        outside = image.copy()
        outside[HEIGHT // 2 - half : HEIGHT // 2 + half, WIDTH // 2 - half : WIDTH // 2 + half] = 0
        assert outside[..., :3].max() < 10, "絵の外に色が出た"


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

    def test_a_particle_of_an_enlarged_picture_is_not_cut(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        """前の変形で広げた絵の粒は、広げた大きさのまま描かれる

        粒の絵が届く範囲を絵の置かれた範囲（u_object）から決めると、変形では広がらない
        ので、広げた粒の絵が元の大きさの円で切れる
        """
        particles = registry.require("particles").create(
            **_still(rate=1.0, lifetime=10.0, preroll=0.5, speed=0.0, randomness=0.0)
        )
        image = draw(_enlarged(), particles, size=20, frame=0)
        assert _lit_width(image) > 70, f"粒の絵が切れた: 幅 {_lit_width(image)}"


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

    def test_pieces_of_an_enlarged_picture_are_not_dropped(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        """前の変形で広げた絵は、崩れ始める前なら広げた大きさのまま全部見える

        欠片を探す範囲を絵の置かれた範囲（u_object）で切ると、変形では広がらないので、
        元の大きさの外に出た欠片が消える
        """
        effect = registry.require("crash").create(**_still(start=1.0))
        image = draw(_enlarged(), effect, size=20, frame=0)
        assert _lit_width(image) > 70, f"欠片が消えた: 幅 {_lit_width(image)}"

    def test_out_of_range_values_do_not_empty_the_search(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        """範囲の外の値（負の再生速度）が届いても、欠片を探す範囲は空にならない

        散る量から探す範囲を決めるとき、負の量をそのまま使うと範囲が負になり、
        どの画素も 1 つも欠片を調べずに絵が全部消える
        """
        effect = registry.require("crash").create(
            speed=_held(-100.0), fall=_held(0.0), fly=_held(100.0)
        )
        size = 40
        image = draw(effect, size=size, frame=30)
        covered = image[..., :3].max(axis=2).sum() / 255.0
        assert covered > size * size * 0.7, f"欠片が消えた: {covered:.0f} 画素分"
