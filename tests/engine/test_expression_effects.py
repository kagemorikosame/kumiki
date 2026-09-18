"""動きと加工のエフェクト（YMM4 の配布物に合わせて足したもの）

シェーダは書き間違えても例外にならず、コンパイルに失敗したエフェクトは黙って
素通しになる（再生を止めないため） だから「全部コンパイルできる」ことを
まず押さえ、効き方の要所は画素で見る
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pytest

from kumiki.core.model import Effect
from kumiki.effects.definition import registry
from kumiki.effects.motion import register_motion_effects
from kumiki.effects.stylize import register_stylize_effects
from kumiki.engine.gpu import (
    Compositor,
    EffectProcessor,
    GLContextError,
    OffscreenGLContext,
    Texture,
)
from kumiki.engine.gpu.glutil import Framebuffer

SIZE = 64

#: このフェーズで足した種類 1 つでも登録が漏れると、YMM4 のテンプレートを
#: 読んでも写す先が無く、記録に残るだけになる
NEW_KINDS = (
    "random_move",
    "random_zoom",
    "random_rotate",
    "repeat_move",
    "repeat_rotate",
    "repeat_opacity",
    "inout_move",
    "inout_zoom",
    "inout_jump",
    "inout_getup",
    "skew",
    "spiral",
    "wave",
    "mesh_deform",
    "circular_duplicate",
    "crash",
    "noise_displacement",
    "morphology",
    "crop_angle",
    "round_corner",
    "edge_trim",
    "exposure",
    "halftone_border",
    "stripe_glitch",
    "long_shadow",
    "highlights_shadows",
    "color_shift",
    "radial_blur",
    "circular_blur",
    "invert",
    "tint",
    "edge_detect",
)


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


def _square(value: int = 200, inner: int = 32) -> np.ndarray:
    """透明な地に、真ん中 ``inner`` 四方の灰色の四角"""
    image = np.zeros((SIZE, SIZE, 4), dtype=np.uint8)
    start = (SIZE - inner) // 2
    image[start : start + inner, start : start + inner] = (value, value, value, 255)
    return image


def _run(
    gl_context: OffscreenGLContext,
    processor: EffectProcessor,
    effect: Effect,
    *,
    image: np.ndarray | None = None,
    frame: int = 0,
    duration: int = 60,
) -> np.ndarray:
    """エフェクトを掛けた結果を ``(高さ, 幅, 4)`` の float（リニア、GL の向き）で返す"""
    picture = image if image is not None else _square()
    columns = np.flatnonzero(picture[..., 3].any(axis=0))
    rows = np.flatnonzero(picture[..., 3].any(axis=1))
    # レンダラと同じく、色の付いた範囲を絵の大きさとして渡す GL の向きへ直す分は
    # エフェクトの側でやるので、ここは画像の向き（上が 0）のまま
    bounds = (
        float(columns[0]),
        float(SIZE - rows[-1] - 1),
        float(columns[-1] + 1),
        float(SIZE - rows[0]),
    )
    with gl_context:
        texture = Texture.from_array(picture)
        result: Framebuffer = processor.apply(
            texture,
            (effect,),
            frame=frame,
            fps=30.0,
            duration=duration,
            flip_source=False,
            bounds=bounds,
        )
        from OpenGL import GL

        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, result.handle)
        raw = GL.glReadPixels(0, 0, SIZE, SIZE, GL.GL_RGBA, GL.GL_FLOAT)
        texture.release()
    return np.frombuffer(raw, dtype=np.float32).reshape(SIZE, SIZE, 4).copy()


def _make(kind: str, **params: object) -> Effect:
    definition = registry.require(kind)
    return definition.create(**params)  # type: ignore[arg-type]


class TestRegistration:
    def test_every_new_kind_is_registered(self) -> None:
        missing = [kind for kind in NEW_KINDS if kind not in registry]
        assert missing == []

    def test_registering_twice_is_harmless(self) -> None:
        # 読み込みのたびに呼ばれても「すでに登録されている」で落ちないこと
        register_motion_effects()
        register_stylize_effects()

    @pytest.mark.parametrize("kind", NEW_KINDS)
    def test_every_shader_compiles(self, processor: EffectProcessor, kind: str) -> None:
        # コンパイルに失敗すると黙って素通しになり、効かない理由が見えない
        assert processor.has_work((_make(kind),)), f"{kind} のシェーダがコンパイルできない"

    def test_the_transform_shader_still_compiles(self, processor: EffectProcessor) -> None:
        # 立体の回転と中心の選び方を足した変形が壊れると、既存の位置指定が全部効かなくなる
        assert processor.has_work((_make("transform", rotation_y=30, pivot_h="left"),))


def _alpha_sum(pixels: np.ndarray) -> float:
    return float(pixels[..., 3].sum())


class TestPixels:
    def test_exposure_scales_the_light(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 100% が元のまま 200% で倍 取り違えると配布物の「光る登場」が暗転になる
        base = _run(gl_context, processor, _make("exposure", amount=100))
        doubled = _run(gl_context, processor, _make("exposure", amount=200))
        assert doubled[32, 32, 0] == pytest.approx(base[32, 32, 0] * 2, rel=0.02)

    def test_invert_turns_black_to_white(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 反転しないと、ネガのような演出の配布テンプレートが元の色のまま出る
        dark = _run(gl_context, processor, _make("invert"), image=_square(value=0))
        assert dark[32, 32, 0] == pytest.approx(1.0, abs=0.01)

    def test_round_corner_cuts_the_corners(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 角が残ると、角丸の字幕帯が四角いまま出る
        result = _run(gl_context, processor, _make("round_corner", radius=12, blur=0))
        assert result[16, 16, 3] < 0.1
        assert result[32, 32, 3] > 0.9

    def test_inout_move_starts_off_screen(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 始まりで画面の中に残っていると、「外から入ってくる」ように見えない
        effect = _make("inout_move", direction="top", effect_time=1.0)
        assert _alpha_sum(_run(gl_context, processor, effect, frame=0)) == 0.0
        settled = _run(gl_context, processor, effect, frame=45)
        assert _alpha_sum(settled) == pytest.approx(32 * 32, rel=0.05)

    def test_inout_zoom_can_squash_one_axis(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 軸の割合は隠れたときのその向きの大きさ X=0 Y=100 は横だけ潰れた所から広がる
        # （YMM4 に描かせて確かめた 「広がって登場」の配布物がこの値）
        effect = _make("inout_zoom", zoom=100, zoom_x=0, zoom_y=100, effect_time=1.0)
        halfway = _run(gl_context, processor, effect, frame=15)
        rows = np.nonzero(halfway[..., 3].max(axis=1) > 0.5)[0]
        columns = np.nonzero(halfway[..., 3].max(axis=0) > 0.5)[0]
        assert len(rows) == pytest.approx(32, abs=2)
        assert 0 < len(columns) < 32

    def test_the_exit_counts_back_from_the_end(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 長さを知らないと、退場が始まらずに最後まで表示されたままになる
        effect = _make("inout_zoom", effect_in=False, effect_out=True, effect_time=0.5)
        assert _alpha_sum(_run(gl_context, processor, effect, frame=10, duration=60)) > 0
        assert _alpha_sum(_run(gl_context, processor, effect, frame=59, duration=60)) < 50

    def test_circular_duplicate_makes_copies(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 複製が 1 つに重なると、円形に並べる演出が 1 つの絵にしか見えない
        small = _square(inner=6)
        effect = _make("circular_duplicate", count=4, radius=20, synced=False)
        result = _run(gl_context, processor, effect, image=small)
        assert _alpha_sum(result) == pytest.approx(4 * 36, rel=0.2)

    def test_mesh_without_offsets_changes_nothing(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 動かしていない四隅で絵がずれると、配布物の「変形なし」の設定で絵が崩れる
        result = _run(gl_context, processor, _make("mesh_deform"))
        assert _alpha_sum(result) == pytest.approx(32 * 32, rel=0.05)
        assert result[32, 32, 3] > 0.9

    def test_skewing_both_ways_keeps_the_picture(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 行列式の符号を取り違えると、縦横とも 45 度で割る数が 0 になり絵が消える
        effect = _make("skew", angle_x=45, angle_y=45)
        assert _alpha_sum(_run(gl_context, processor, effect)) > 100

    def test_a_collapsed_mesh_draws_nothing_broken(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 四隅を 1 本の線へ潰すと割る数が 0 になる NaN を描くと、重ねた先の色まで壊れる
        effect = _make("mesh_deform", point1_x=-32, point2_x=-32)
        result = _run(gl_context, processor, effect)
        assert np.all(np.isfinite(result))

    def test_long_shadow_extends_behind(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 影が後ろへ伸びないか本体を塗り潰すと、長い影の字幕が読めなくなる
        effect = _make("long_shadow", angle=0, length_=12, color1=(0.0, 0.0, 0.0, 1.0))
        result = _run(gl_context, processor, effect)
        assert result[32, 52, 3] > 0.9
        assert result[32, 32, 0] > 0.5

    def test_repeat_opacity_swings(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 揺れないと、点滅させる配布テンプレートが表示されたまま止まる
        effect = _make("repeat_opacity", opacity=0, interval=1.0)
        start = _run(gl_context, processor, effect, frame=0)
        middle = _run(gl_context, processor, effect, frame=15)
        assert start[32, 32, 3] > 0.9
        assert middle[32, 32, 3] < 0.1

    def test_a_tilted_transform_narrows(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # YMM4 の RotateEffect の X / Y は板を傾ける 平らなままだと立体の動きが消える
        result = _run(gl_context, processor, _make("transform", rotation_y=60))
        columns = np.nonzero(result[..., 3].max(axis=0) > 0.5)[0]
        assert len(columns) == pytest.approx(16, abs=3)

    def test_the_pivot_can_sit_on_the_object_edge(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 左端を中心に半分へ縮めると、左端は動かない 画面の中央を中心にすると左端が動く
        result = _run(gl_context, processor, _make("transform", scale=50, pivot_h="left"))
        columns = np.nonzero(result[..., 3].max(axis=0) > 0.5)[0]
        assert int(columns.min()) == 16


class TestAxes:
    """Y は上が正 例外は表示名に「下が正」と書いてあるものだけ"""

    def test_the_mask_centre_moves_up_with_a_positive_y(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 逆向きだと、YMM4 から写した切り抜きが上下反対の場所に出る
        picture = np.full((SIZE, SIZE, 4), 200, dtype=np.uint8)
        effect = _make("shape_mask", shape="ellipse", width=16.0, height=16.0, center_y=24.0)
        result = _run(gl_context, processor, effect, image=picture)
        # GL の向きなので、行番号が大きいほど画面の上
        rows = np.flatnonzero(result[..., 3].max(axis=1) > 0.5)
        assert rows.mean() > SIZE / 2

    def test_a_negative_emitter_y_puts_particles_above_the_centre(
        self, gl_context: OffscreenGLContext, processor: EffectProcessor
    ) -> None:
        # 放つ位置だけは YMM4 と同じ下向き正（表示名にもそう書いてある）
        effect = _make(
            "particles",
            rate=60.0,
            lifetime=1.0,
            size=100.0,
            emitter_y=-20.0,
            speed=0.0,
            gravity=0.0,
            randomness=0.0,
        )
        result = _run(gl_context, processor, effect)
        rows = np.flatnonzero(result[..., 3].max(axis=1) > 0.5)
        assert len(rows), "粒が 1 つも出ていない"
        # 下向き正なので -20 は画面の上（GL の向きでは行番号が大きい側）
        assert rows.mean() > SIZE / 2


class TestFrameBuffer:
    def test_it_reads_what_was_drawn_below(self, gl_context: OffscreenGLContext) -> None:
        # 下の絵を読めないと、YMM4 の「背景だけぼかす帯」が黒い板になる
        from kumiki.core.commands import AddClip, AddEffect, AddTrack
        from kumiki.core.model import (
            Clip,
            GeneratedSource,
            Project,
            ProjectSettings,
            Track,
            TrackKind,
        )
        from kumiki.core.timebase import FrameRate
        from kumiki.engine.render import FrameRenderer

        project = Project.create(ProjectSettings(width=64, height=64, frame_rate=FrameRate(30)))
        below, above = Track(TrackKind.VIDEO, "V1"), Track(TrackKind.VIDEO, "V2")
        project = AddTrack(below).apply(project)
        project = AddTrack(above).apply(project)
        white = GeneratedSource(
            kind="shape",
            params={"shape": "background", "color": (1.0, 1.0, 1.0, 1.0)},
        )
        project = AddClip(below.id, Clip(timeline_start=0, duration=10, source=white)).apply(
            project
        )
        grab = Clip(timeline_start=0, duration=10, source=GeneratedSource(kind="framebuffer"))
        project = AddClip(above.id, grab).apply(project)
        project = AddEffect(grab.id, _make("invert")).apply(project)

        renderer = FrameRenderer(project, context=gl_context)
        try:
            image = renderer.render(0)
        finally:
            renderer.close()
        assert int(image[32, 32, 0]) < 8
