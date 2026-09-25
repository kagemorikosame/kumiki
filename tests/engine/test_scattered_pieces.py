"""粒や欠片を画面へ撒くエフェクト（パーティクル・破片）が、撒いた分を描き切るか（#199）

粒は出力の画素ごとに「ここへ来る粒」を探して描く 探す数や範囲に上限があると、
上限の外の粒は画面の中に居ても黙って消える 欠片も前は同じ作りで、上限の外の欠片が
消えていた 今は欠片を 1 つずつ四角として描く（#207） ここではそれを実際に
描いて確かめる 乱数の並びは見ず、描かれた量と広がりだけを見る
"""

from __future__ import annotations

import math
import re
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
from sashimono.effects import EffectDefinition, Pieces, TrackSpec, registry
from sashimono.effects.builtin import PIECE_PRELUDE, PRELUDE
from sashimono.effects.sources import SHAPE
from sashimono.engine.gpu import GLContextError, OffscreenGLContext, ScreenQuad
from sashimono.engine.gpu.effects import piece_grid
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
    @pytest.mark.parametrize("extreme", ["default", "minimum", "maximum"])
    def test_nothing_appears_outside_the_picture(
        self, draw: Callable[..., np.ndarray], kind: str, extreme: str
    ) -> None:
        """中身を動かさない印の付いたエフェクトは、どの値でも絵の外（透明な所）に何も置かない

        印を付けた物の後では、粒や欠片を探す範囲を元の絵の範囲に狭める 外に色を置く物に
        印があると、その色が粒や欠片から切れる 既定の値だけで見ると、線形変換の
        不透明度の切片のように、値によって透明な所へ色を置く物を見落とす
        """
        definition = registry.require(kind)
        values = {
            spec.name: AnimatedValue(getattr(spec, extreme))
            for spec in definition.parameters
            if isinstance(spec, TrackSpec)
        }
        # 後ろで白く塗り、外へ置かれた α を黒い背景の上でも見えるようにする
        # 透明な所の黒に α だけを付ける物は、塗らないと背景と見分けられない
        white = registry.require("fill").create(color=(1.0, 1.0, 1.0, 1.0))
        size = 40
        image = draw(definition.create(**values), white, size=size, frame=0)
        half = size // 2 + 2
        outside = image.copy()
        outside[HEIGHT // 2 - half : HEIGHT // 2 + half, WIDTH // 2 - half : WIDTH // 2 + half] = 0
        assert outside[..., :3].max() < 10, "絵の外に色が出た"

    def test_opacity_keeps_the_content(self) -> None:
        """不透明度は α を掛けるだけで外に色を置かない 印が無いと、粒を探す範囲が
        バッファ全体へ広がり、小さな絵の粒でも遠くの画素まで回す"""
        assert registry.require("opacity").keeps_content


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

    def test_late_pieces_are_drawn_after_the_early_ones_fell(
        self, draw: Callable[..., np.ndarray]
    ) -> None:
        """遅れて崩れ始めた欠片は、先に落ちた欠片から遠く離れても描かれる（#207）

        既定の設定でも、欠片の出る時刻は最大 0.5 秒ずれる 0.5 秒の時点で、最初に出た欠片と
        出たばかりの欠片の落ちた量の差は 190 画素ほどで、4 画素の欠片の 31 個分を超える
        見込み位置の周り 63x63 だけを探す作りでは、まだ画面の真ん中にいる遅い欠片が消え、
        描かれたのは 3 割ほどだった
        """
        effect = registry.require("crash").create(**_still(size=4.0))
        size = 100
        image = draw(effect, size=size, frame=15)
        covered = image[..., :3].max(axis=2).sum() / 255.0
        assert covered > size * size * 0.7, f"遅れた欠片が消えた: {covered:.0f} 画素分"

    @pytest.mark.parametrize(("size", "pieces"), [(4.0, 100), (50.0, 4)])
    @pytest.mark.parametrize("frame", [0, 50])
    def test_each_piece_is_drawn_once_however_far_it_flies(
        self,
        draw: Callable[..., np.ndarray],
        monkeypatch: pytest.MonkeyPatch,
        size: float,
        pieces: int,
        frame: int,
    ) -> None:
        """欠片は升目の数だけの四角として 1 回で描く 散った後でも数は増えない（#207）

        出力の画素ごとに周りの欠片を探す作りに戻ると、遠くまで散らすほど 1 画素で調べる
        欠片が増え、1080p を 4 画素の欠片に割った書き出しで 1 コマ 90ms を超える
        時間ではなく描く四角の数で押さえる 40 四方の絵は 480 四方の真ん中（220〜260）に
        あるので、4 画素なら 10x10、50 画素なら 200〜300 の升目 2x2 に掛かる
        """
        drawn: list[int] = []
        original = ScreenQuad.draw_instanced

        def spy(quad: ScreenQuad, count: int) -> None:
            drawn.append(count)
            original(quad, count)

        monkeypatch.setattr(ScreenQuad, "draw_instanced", spy)
        effect = registry.require("crash").create(**_still(size=size, spread=1000.0, impact=1000.0))
        draw(effect, size=40, frame=frame)
        assert drawn == [pieces]

    def test_a_pixel_does_not_search_for_its_piece(self) -> None:
        """破片のシェーダは、画素ごとにも欠片ごとにも繰り返しを回さない（#207）

        欠片の数や散らばりで回る数が変わる繰り返しがあると、細かく割るほど・遠くへ
        散らすほど 1 コマが重くなる 欠片 1 つの手間は、自分の面積の画素を読むだけにする
        """
        definition = registry.require("crash")
        assert definition.pieces is not None
        assert definition.fragment_shader is not None
        fragment = definition.fragment_shader.removeprefix(PRELUDE)
        vertex = definition.pieces.vertex_shader.removeprefix(PIECE_PRELUDE)
        for body in (fragment, vertex):
            assert not re.search(r"\b(for|while)\b", body), body

    def test_pieces_need_a_number_for_their_size(self) -> None:
        """升目の大きさの項目が数の項目でなければ、定義の時点で断る

        読めない大きさはエンジンが 0 と読み、下限の細かさで割って四角の数が膨らむ
        """
        with pytest.raises(ValueError, match="升目の大きさ"):
            EffectDefinition(
                kind="broken_pieces",
                label="壊れた破片",
                category="テスト",
                fragment_shader="",
                pieces=Pieces(size="size", minimum=4.0, vertex_shader=""),
            )

    @pytest.mark.parametrize("minimum", [0.0, -4.0, math.nan, math.inf])
    def test_the_smallest_piece_must_be_a_positive_number(self, minimum: float) -> None:
        """升目の一辺の下限が正の数でなければ、定義の時点で断る

        下限は升目の数を求めるときに割る数になる 0 や NaN を通すと、大きさの項目を 0 に
        した所でプレビューも書き出しも例外で止まる
        """
        with pytest.raises(ValueError, match="下限"):
            EffectDefinition(
                kind="broken_pieces",
                label="壊れた破片",
                category="テスト",
                parameters=(TrackSpec("size", "大きさ", 0, 400, 50, unit="px"),),
                fragment_shader="",
                pieces=Pieces(size="size", minimum=minimum, vertex_shader=""),
            )

    def test_a_piece_barely_touched_by_the_content_is_drawn(self) -> None:
        """中身の端が升目にわずかに掛かるだけでも、その升目は描く

        画質を落とすと升目の一辺も中身の端も半端な数になる 一辺 2.5 で中身が 2.6 まで
        あるとき、端の画素の中心（2.1）で数えると [2.5, 5.0) の升目が抜け、そこに残る
        中身の縁（2.5〜2.6）が消える
        """
        first, columns, rows = piece_grid((0.0, 0.0, 2.6, 2.6), 2.5, 480, 480)
        assert first == (0, 0)
        assert (columns, rows) == (2, 2)

    def test_the_grid_stops_at_the_content_edge(self) -> None:
        """升目の境目で終わる中身は、その先の升目まで数えない 数えると空の四角を描く"""
        assert piece_grid((220.0, 220.0, 260.0, 260.0), 4.0, 480, 480) == ((55, 55), 10, 10)
        assert piece_grid((0.0, 0.0, 250.0, 100.0), 50.0, 480, 480) == ((0, 0), 5, 2)

    def test_nothing_outside_the_buffer_is_counted(self) -> None:
        """バッファの外の中身は読めば透明なので、升目を数えない 画面より大きく置いた絵で
        四角の数だけが膨らむ"""
        assert piece_grid((-1000.0, -1000.0, 5000.0, 5000.0), 10.0, 100, 50) == ((0, 0), 10, 5)
        assert piece_grid((600.0, 0.0, 700.0, 50.0), 10.0, 100, 50)[1] == 0

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
