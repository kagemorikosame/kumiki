"""GPU 合成とフレームレンダラ

色の扱いを厚めに確認する 符号化されたままの値を混ぜると半透明やフェードが
暗く沈むが、絵としては「なんとなく変」に見えるだけで気づきにくい 数値で押さえる
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from fractions import Fraction

import numpy as np
import pytest

from kumiki.core.commands import AddClip, AddMedia, AddTrack
from kumiki.core.model import (
    AnimatedValue,
    Clip,
    Keyframe,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from kumiki.core.timebase import FrameRate
from kumiki.engine.decode import probe_media
from kumiki.engine.gpu import (
    Compositor,
    GLContextError,
    OffscreenGLContext,
    Placement,
    Texture,
    fit_placement,
)
from kumiki.engine.render import FrameRenderer, RenderQuality
from tests.media_fixtures import SampleMedia


@pytest.fixture(scope="session")
def gl_context() -> Iterator[OffscreenGLContext]:
    """オフスクリーンの GL コンテキスト 作れない環境ではテストを飛ばす"""
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


def solid(width: int, height: int, rgb: tuple[int, int, int], alpha: int = 255) -> np.ndarray:
    image = np.zeros((height, width, 4), dtype=np.uint8)
    image[..., 0], image[..., 1], image[..., 2] = rgb
    image[..., 3] = alpha
    return image


def srgb_to_linear(value: float) -> float:
    value /= 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def linear_to_srgb(value: float) -> float:
    encoded = value * 12.92 if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
    return encoded * 255.0


class TestPlacement:
    def test_fits_inside_a_wider_frame(self) -> None:
        # 正方形を横長のフレームへ 左右に余白ができる
        assert fit_placement(100, 100, 200, 100) == Placement(50.0, 0.0, 100.0, 100.0)

    def test_fits_inside_a_taller_frame(self) -> None:
        assert fit_placement(100, 100, 100, 200) == Placement(0.0, 50.0, 100.0, 100.0)

    def test_exact_match_fills_the_frame(self) -> None:
        assert fit_placement(1920, 1080, 1920, 1080) == Placement(0.0, 0.0, 1920.0, 1080.0)

    def test_upscales_small_sources(self) -> None:
        assert fit_placement(320, 240, 640, 480) == Placement(0.0, 0.0, 640.0, 480.0)

    def test_degenerate_source_fills_the_frame(self) -> None:
        assert fit_placement(0, 0, 200, 100) == Placement(0.0, 0.0, 200.0, 100.0)


class TestCompositor:
    def test_opaque_layer_round_trips_exactly(self, gl_context: OffscreenGLContext) -> None:
        # sRGB で入れた値がリニアを経由して同じ値で戻ること ここがずれると
        # 何も加工していない素材の色が変わってしまう
        with gl_context:
            compositor = Compositor(32, 16)
            texture = Texture.from_array(solid(32, 16, (128, 64, 200)))
            compositor.begin()
            compositor.draw(texture)
            result = compositor.read()
            compositor.release()
            texture.release()

        assert result.shape == (16, 32, 4)
        assert result[8, 16, :3].tolist() == [128, 64, 200]

    def test_blending_happens_in_linear_space(self, gl_context: OffscreenGLContext) -> None:
        # sRGB 128 を 50% で黒に重ねる 符号化されたまま混ぜれば 64、
        # リニアで混ぜれば 92 前後 ここが 64 なら色管理が壊れている
        with gl_context:
            compositor = Compositor(16, 16)
            texture = Texture.from_array(solid(16, 16, (128, 128, 128)))
            compositor.begin((0.0, 0.0, 0.0, 1.0))
            compositor.draw(texture, opacity=0.5)
            result = compositor.read()
            compositor.release()
            texture.release()

        expected = linear_to_srgb(srgb_to_linear(128) * 0.5)
        assert result[8, 8, 0] == pytest.approx(expected, abs=2)
        assert result[8, 8, 0] > 80, "符号化されたまま混ざっている"

    def test_layers_stack_in_draw_order(self, gl_context: OffscreenGLContext) -> None:
        with gl_context:
            compositor = Compositor(16, 16)
            back = Texture.from_array(solid(16, 16, (255, 0, 0)))
            front = Texture.from_array(solid(16, 16, (0, 255, 0)))
            compositor.begin()
            compositor.draw(back)
            compositor.draw(front)
            result = compositor.read()
            compositor.release()
            back.release()
            front.release()

        # 後に描いた方が手前
        assert result[8, 8, :3].tolist() == [0, 255, 0]

    def test_transparent_layer_leaves_the_background(self, gl_context: OffscreenGLContext) -> None:
        with gl_context:
            compositor = Compositor(16, 16)
            back = Texture.from_array(solid(16, 16, (255, 0, 0)))
            front = Texture.from_array(solid(16, 16, (0, 255, 0), alpha=0))
            compositor.begin()
            compositor.draw(back)
            compositor.draw(front)
            result = compositor.read()
            compositor.release()
            back.release()
            front.release()

        assert result[8, 8, :3].tolist() == [255, 0, 0]

    def test_letterboxing_leaves_the_background_visible(
        self, gl_context: OffscreenGLContext
    ) -> None:
        # 正方形の素材を横長のフレームへ 左右に背景が残る
        with gl_context:
            compositor = Compositor(64, 32)
            texture = Texture.from_array(solid(32, 32, (255, 255, 255)))
            compositor.begin((0.0, 0.0, 0.0, 1.0))
            compositor.draw(texture)
            result = compositor.read()
            compositor.release()
            texture.release()

        assert result[16, 32, :3].tolist() == [255, 255, 255], "中央に素材が無い"
        assert result[16, 2, :3].tolist() == [0, 0, 0], "左端に余白が無い"

    def test_orientation_is_preserved(self, gl_context: OffscreenGLContext) -> None:
        # GL は左下原点、画像は左上原点 上下が入れ替わっていないか確かめる
        image = np.zeros((16, 16, 4), dtype=np.uint8)
        image[..., 3] = 255
        image[:4, :, 0] = 255  # 上端だけ赤

        with gl_context:
            compositor = Compositor(16, 16)
            texture = Texture.from_array(image)
            compositor.begin()
            compositor.draw(texture)
            result = compositor.read()
            compositor.release()
            texture.release()

        assert result[1, 8, 0] == 255, "上端が赤くない"
        assert result[14, 8, 0] == 0, "下端が赤い"

    def test_resize(self, gl_context: OffscreenGLContext) -> None:
        with gl_context:
            compositor = Compositor(16, 16)
            compositor.resize(32, 8)
            compositor.begin()
            result = compositor.read()
            compositor.release()
        assert result.shape == (8, 32, 4)

    def test_rejects_bad_size(self, gl_context: OffscreenGLContext) -> None:
        with gl_context, pytest.raises(ValueError, match="解像度"):
            Compositor(0, 16)

    def test_texture_rejects_wrong_dtype(self, gl_context: OffscreenGLContext) -> None:
        with gl_context, pytest.raises(ValueError, match="RGBA uint8"):
            Texture.from_array(np.zeros((4, 4, 3), dtype=np.uint8))


@pytest.fixture
def rendered_project(sample_av: SampleMedia) -> Project:
    """320x240 の素材を 1 本置いた 320x240 のプロジェクト"""
    media = probe_media(sample_av.path)
    project = Project.create(ProjectSettings(width=320, height=240, frame_rate=FrameRate(30)))
    project = AddMedia(media).apply(project)
    track = Track(kind=TrackKind.VIDEO, name="V1")
    project = AddTrack(track).apply(project)
    clip = Clip(timeline_start=0, duration=60, media_id=media.id)
    return AddClip(track.id, clip).apply(project)


class TestFrameRenderer:
    def test_renders_at_project_resolution(
        self, rendered_project: Project, gl_context: OffscreenGLContext
    ) -> None:
        renderer = FrameRenderer(rendered_project, context=gl_context)
        try:
            frame = renderer.render(0)
        finally:
            renderer.close()
        assert frame.shape == (240, 320, 4)
        assert frame.dtype == np.uint8

    def test_empty_timeline_renders_black(self, gl_context: OffscreenGLContext) -> None:
        project = Project.create(ProjectSettings(width=32, height=32))
        renderer = FrameRenderer(project, context=gl_context)
        try:
            frame = renderer.render(0)
        finally:
            renderer.close()
        assert frame[..., :3].max() == 0

    def test_gap_between_clips_renders_black(
        self, rendered_project: Project, gl_context: OffscreenGLContext
    ) -> None:
        # クリップの外側は何も無い 前のフレームが残ると「消したのに映る」になる
        renderer = FrameRenderer(rendered_project, context=gl_context)
        try:
            renderer.render(0)
            beyond = renderer.render(120)
        finally:
            renderer.close()
        assert beyond[..., :3].max() == 0

    def test_different_frames_differ(
        self, rendered_project: Project, gl_context: OffscreenGLContext
    ) -> None:
        renderer = FrameRenderer(rendered_project, context=gl_context)
        try:
            first = renderer.render(0)
            later = renderer.render(30)
        finally:
            renderer.close()
        assert not np.array_equal(first, later), "時間が進んでも絵が変わっていない"

    def test_opacity_keyframes_are_applied(
        self, rendered_project: Project, gl_context: OffscreenGLContext
    ) -> None:
        track = rendered_project.timeline.tracks[0]
        faded = replace(
            track.clips[0],
            opacity=AnimatedValue(
                keyframes=(Keyframe(frame=0, value=0.0), Keyframe(frame=30, value=1.0))
            ),
        )
        project = rendered_project.with_timeline(
            rendered_project.timeline.replace_track(track.with_clips((faded,)))
        )

        renderer = FrameRenderer(project, context=gl_context)
        try:
            transparent = renderer.render(0)
            opaque = renderer.render(30)
        finally:
            renderer.close()

        assert transparent[..., :3].max() == 0, "不透明度 0 なのに映っている"
        assert opaque[..., :3].max() > 0

    def test_disabled_clip_is_skipped(
        self, rendered_project: Project, gl_context: OffscreenGLContext
    ) -> None:
        track = rendered_project.timeline.tracks[0]
        project = rendered_project.with_timeline(
            rendered_project.timeline.replace_track(
                track.with_clips((replace(track.clips[0], enabled=False),))
            )
        )
        renderer = FrameRenderer(project, context=gl_context)
        try:
            frame = renderer.render(0)
        finally:
            renderer.close()
        assert frame[..., :3].max() == 0

    def test_muted_track_is_skipped(
        self, rendered_project: Project, gl_context: OffscreenGLContext
    ) -> None:
        track = rendered_project.timeline.tracks[0]
        project = rendered_project.with_timeline(
            rendered_project.timeline.replace_track(replace(track, muted=True))
        )
        renderer = FrameRenderer(project, context=gl_context)
        try:
            frame = renderer.render(0)
        finally:
            renderer.close()
        assert frame[..., :3].max() == 0

    def test_reduced_quality_shrinks_the_output(
        self, rendered_project: Project, gl_context: OffscreenGLContext
    ) -> None:
        renderer = FrameRenderer(rendered_project, context=gl_context, quality=RenderQuality(2))
        try:
            frame = renderer.render(0)
        finally:
            renderer.close()
        assert frame.shape == (120, 160, 4)

    def test_quality_can_change_at_runtime(
        self, rendered_project: Project, gl_context: OffscreenGLContext
    ) -> None:
        renderer = FrameRenderer(rendered_project, context=gl_context)
        try:
            assert renderer.render(0).shape == (240, 320, 4)
            renderer.set_quality(RenderQuality(4))
            assert renderer.render(0).shape == (60, 80, 4)
        finally:
            renderer.close()

    def test_offline_media_renders_black_instead_of_crashing(
        self, rendered_project: Project, gl_context: OffscreenGLContext
    ) -> None:
        # 素材が 1 本行方不明でも、プロジェクト全体が開けなくなってはいけない
        original = rendered_project.media[0]
        broken = replace(original, path=original.path.parent / "行方不明.mp4")
        project = rendered_project.replace_media(broken)
        renderer = FrameRenderer(project, context=gl_context)
        try:
            frame = renderer.render(0)
        finally:
            renderer.close()
        assert frame[..., :3].max() == 0

    def test_set_project_closes_removed_media(
        self, rendered_project: Project, gl_context: OffscreenGLContext
    ) -> None:
        renderer = FrameRenderer(rendered_project, context=gl_context)
        try:
            renderer.render(0)
            emptied = Project.create(rendered_project.settings)
            renderer.set_project(emptied)
            assert renderer.render(0)[..., :3].max() == 0
        finally:
            renderer.close()

    def test_speed_changes_which_source_frame_is_shown(
        self, rendered_project: Project, gl_context: OffscreenGLContext
    ) -> None:
        # 2 倍速のクリップは、15 フレーム目でソースの 30 フレーム目を映す
        track = rendered_project.timeline.tracks[0]
        fast = replace(track.clips[0], speed=Fraction(2), duration=30)
        project = rendered_project.with_timeline(
            rendered_project.timeline.replace_track(track.with_clips((fast,)))
        )

        renderer = FrameRenderer(project, context=gl_context)
        try:
            at_15 = renderer.render(15)
        finally:
            renderer.close()

        normal = FrameRenderer(rendered_project, context=gl_context)
        try:
            source_30 = normal.render(30)
        finally:
            normal.close()

        assert np.array_equal(at_15, source_30)

    def test_closed_renderer_refuses_to_render(
        self, rendered_project: Project, gl_context: OffscreenGLContext
    ) -> None:
        renderer = FrameRenderer(rendered_project, context=gl_context)
        renderer.close()
        with pytest.raises(RuntimeError, match="閉じたレンダラ"):
            renderer.render(0)

    def test_close_is_idempotent(
        self, rendered_project: Project, gl_context: OffscreenGLContext
    ) -> None:
        renderer = FrameRenderer(rendered_project, context=gl_context)
        renderer.close()
        renderer.close()


class TestRenderQuality:
    def test_rejects_zero(self) -> None:
        with pytest.raises(ValueError, match="1 以上"):
            RenderQuality(0)

    def test_never_shrinks_below_one_pixel(self) -> None:
        assert RenderQuality(100).apply(32, 32) == (1, 1)
