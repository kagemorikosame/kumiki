"""範囲を決めて掛けるエフェクト 部分モザイク・ぼかしと部分フィルタ（Issue #27）

顔や画面の一部を隠すためのもの 壊れると、隠したはずの所が見える（範囲がずれる・
上下が逆・回す向きが逆）か、隠さなくてよい所まで崩れる（範囲の外が変わる）

縞の絵を使う 一色の絵ではモザイクもぼかしも何も変えないので、掛かったかどうかが
見分けられない
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace

import numpy as np
import pytest
from OpenGL import GL

from sashimono.core.io import project_from_dict, project_to_dict
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    Effect,
    GeneratedSource,
    Keyframe,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.effects import ParamInput, registry
from sashimono.effects.region import PARTIAL_FILTER, REGION_BLUR
from sashimono.effects.sources import FILTER
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.gpu.effects import EffectProcessor
from sashimono.engine.gpu.glutil import ScreenQuad, Texture
from sashimono.engine.render import FrameRenderer

WIDTH, HEIGHT = 128, 64
#: 縞の幅（画素） 粒やぼかしの幅より細くして、掛かれば必ず色が変わるようにする
STRIPE = 2


@pytest.fixture(scope="module")
def gl() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


def stripes() -> np.ndarray:
    """白と黒の縦縞 不透明"""
    image = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    white = (np.arange(WIDTH) // STRIPE) % 2 == 0
    image[:, white, :3] = 255
    image[..., 3] = 255
    return image


def white() -> np.ndarray:
    return np.full((HEIGHT, WIDTH, 4), 255, dtype=np.uint8)


#: エフェクトを掛けた結果（リニア・下の行から Y は上が正）
type Apply = Callable[..., np.ndarray]


@pytest.fixture
def apply(gl: OffscreenGLContext) -> Iterator[Apply]:
    """絵にエフェクトを掛けて、結果を下の行から並べた配列で返す

    行の番号がそのまま「下からの画素」になるので、Y が上で正の設定と見比べやすい
    """
    with gl:
        quad = ScreenQuad()
        processor = EffectProcessor(WIDTH, HEIGHT, quad)
        textures: list[Texture] = []

        def run(image: np.ndarray, effects: tuple[Effect, ...], *, frame: int = 0) -> np.ndarray:
            texture = Texture.from_array(image)
            textures.append(texture)
            # 素材は上の行から積んで渡す 読み返しは下の行からなので、比べる前にそろえる
            result = processor.apply(texture, effects, frame=frame, fps=30.0)
            result.bind()
            data = GL.glReadPixels(0, 0, WIDTH, HEIGHT, GL.GL_RGBA, GL.GL_FLOAT)
            pixels = np.frombuffer(data, dtype=np.float32).reshape(HEIGHT, WIDTH, 4).copy()
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
            return pixels

        try:
            yield run
        finally:
            for texture in textures:
                texture.release()
            processor.release()
            quad.release()


def region(**params: ParamInput) -> Effect:
    return registry.require(REGION_BLUR).create(**params)


def partial(**params: ParamInput) -> Effect:
    return registry.require(PARTIAL_FILTER).create(**params)


def invert() -> Effect:
    return registry.require("invert").create()


def changed(before: np.ndarray, after: np.ndarray, tolerance: float = 0.02) -> np.ndarray:
    """画素ごとに色が変わったか"""
    difference: np.ndarray = np.abs(after[..., :3] - before[..., :3]).max(axis=2)
    return difference > tolerance


def at(x: float, y: float) -> tuple[int, int]:
    """画面の中央から数えた位置（Y は上が正）を、下の行から数えた配列の番号へ"""
    return int(HEIGHT / 2 + y), int(WIDTH / 2 + x)


#: 範囲の中と外を見る所 中は範囲 40x20 の真ん中、外は範囲から十分離れた所
CENTRE_BOX = {"region_width": 40.0, "region_height": 20.0}


class TestRegionBlur:
    @pytest.mark.parametrize("mode", ["mosaic", "blur"])
    def test_only_the_inside_changes(self, apply: Apply, mode: str) -> None:
        # 範囲の外まで変わると、隠さなくてよい所まで崩れる 中が変わらなければ隠れていない
        plain = apply(stripes(), ())
        done = apply(
            stripes(), (region(mode=mode, mosaic_size=8.0, blur_radius=8.0, **CENTRE_BOX),)
        )
        moved = changed(plain, done)
        inside = moved[HEIGHT // 2 - 8 : HEIGHT // 2 + 8, WIDTH // 2 - 18 : WIDTH // 2 + 18]
        assert inside.mean() > 0.4
        outside = moved.copy()
        outside[HEIGHT // 2 - 11 : HEIGHT // 2 + 11, WIDTH // 2 - 21 : WIDTH // 2 + 21] = False
        assert not outside.any()

    def test_the_mosaic_makes_even_blocks(self, apply: Apply) -> None:
        # 粒の中は 1 色 縞が残っていれば、粒の大きさが効いていない
        done = apply(stripes(), (region(mode="mosaic", mosaic_size=8.0, **CENTRE_BOX),))
        row, column = at(0, 0)
        block = done[row : row + 8, column : column + 8, :3]
        assert np.ptp(block) < 0.02

    def test_the_blur_mixes_the_stripes_into_grey(self, apply: Apply) -> None:
        # 2 画素の縞を 8 画素でぼかせば、白と黒が混ざって中間になる
        # 混ざらないと、ぼかしたつもりの所で縞（顔の輪郭や字）が読めてしまう
        done = apply(stripes(), (region(mode="blur", blur_radius=8.0, **CENTRE_BOX),))
        red = done[at(0, 0)][0]
        assert 0.3 < red < 0.7

    def test_positive_y_moves_the_region_up(self, apply: Apply) -> None:
        # Y は上が正（テキスト・変形・マスクと同じ） 逆だと、上の顔を隠すつもりで下が崩れる
        plain = apply(stripes(), ())
        done = apply(stripes(), (region(center_y=16.0, region_width=40.0, region_height=12.0),))
        moved = changed(plain, done)
        assert moved[at(0, 16)[0], WIDTH // 2 - 16 : WIDTH // 2 + 16].any()
        assert not moved[at(0, -16)[0], :].any()

    def test_positive_x_moves_the_region_right(self, apply: Apply) -> None:
        # X は右が正 逆だと、右の顔を隠すつもりで左が崩れ、右の顔は見えたままになる
        plain = apply(stripes(), ())
        done = apply(stripes(), (region(center_x=40.0, region_width=20.0, region_height=20.0),))
        moved = changed(plain, done)
        row = HEIGHT // 2
        assert moved[row, WIDTH // 2 + 34 : WIDTH // 2 + 46].any()
        assert not moved[row, : WIDTH // 2].any()

    def test_invert_blurs_the_outside_instead(self, apply: Apply) -> None:
        # 反転は「ここだけ残して周りを隠す」ためのもの 中が変わると残したい所が崩れる
        plain = apply(stripes(), ())
        done = apply(stripes(), (region(invert=True, mosaic_size=8.0, **CENTRE_BOX),))
        moved = changed(plain, done)
        assert not moved[HEIGHT // 2 - 8 : HEIGHT // 2 + 8, WIDTH // 2 - 18 : WIDTH // 2 + 18].any()
        assert moved[:, :20].mean() > 0.4

    def test_rotation_turns_the_region_clockwise(self, apply: Apply) -> None:
        # 回転は変形と同じく正で時計回り 横長の範囲を 30 度回すと、右の端が下がる
        # 向きが逆だと、傾いた顔に合わせた範囲が反対へ傾いて顔の端が出る
        plain = apply(white(), ())
        effects = (partial(region_width=90.0, region_height=6.0, rotation=30.0), invert())
        done = apply(white(), effects)
        moved = changed(plain, done)
        # 回す前の右 36 画素の点 時計回りなら (31, -18)、反時計回りなら (31, 18) へ行く
        assert moved[at(31, -18)]
        assert not moved[at(31, 18)]

    def test_the_ellipse_leaves_the_corners(self, apply: Apply) -> None:
        # 楕円なのに四角で掛かると、丸く隠したつもりの角まで崩れる
        plain = apply(white(), ())
        box = {"region_width": 60.0, "region_height": 40.0}
        rect = changed(plain, apply(white(), (partial(shape="rect", **box), invert())))
        oval = changed(plain, apply(white(), (partial(shape="ellipse", **box), invert())))
        corner = at(27, 17)
        assert rect[corner]
        assert not oval[corner]
        assert oval[at(0, 0)]

    def test_the_feather_fades_the_edge(self, apply: Apply) -> None:
        # なじませる幅を付けると、境目の内側が段々に変わる 付けなければ境目の内側は丸ごと変わる
        # 効かないと、隠した所の縁がくっきり出て目立つ
        box = {"region_width": 60.0, "region_height": 40.0}
        hard = apply(white(), (partial(**box), invert()))
        soft = apply(white(), (partial(feather=16.0, **box), invert()))
        just_inside = at(26, 0)
        assert hard[just_inside][0] < 0.02
        assert 0.1 < soft[just_inside][0] < 0.95
        # 中央はどちらも掛かりきっている
        assert soft[at(0, 0)][0] < 0.02
        # 外へは漏れない
        assert soft[at(34, 0)][0] > 0.98

    def test_keyframes_move_the_region(self, apply: Apply) -> None:
        # 範囲を動かして、動く顔を追いかける 動かないと、頭のフレームの所だけが隠れ続ける
        moving = AnimatedValue(
            keyframes=(Keyframe(frame=0, value=-40.0), Keyframe(frame=10, value=40.0))
        )
        effect = region(center_x=moving, region_width=20.0, region_height=20.0)
        plain = apply(stripes(), ())
        start = changed(plain, apply(stripes(), (effect,), frame=0))
        end = changed(plain, apply(stripes(), (effect,), frame=10))
        row = HEIGHT // 2
        assert start[row, WIDTH // 2 - 46 : WIDTH // 2 - 34].any()
        assert not start[row, WIDTH // 2 :].any()
        assert end[row, WIDTH // 2 + 34 : WIDTH // 2 + 46].any()
        assert not end[row, : WIDTH // 2].any()


class TestPartialFilter:
    def test_effects_after_it_stay_inside(self, apply: Apply) -> None:
        # 後ろの反転が範囲の中だけに効く 外まで反転すると、部分フィルタが何もしていない
        done = apply(white(), (partial(**CENTRE_BOX), invert()))
        assert done[at(0, 0)][0] < 0.02
        assert done[at(40, 0)][0] > 0.98

    def test_effects_before_it_work_everywhere(self, apply: Apply) -> None:
        # 部分フィルタより前のエフェクトは絵全体に効く 前まで範囲に絞ると、並べ方の意味が変わる
        done = apply(white(), (invert(), partial(**CENTRE_BOX), invert()))
        assert done[at(0, 0)][0] > 0.98
        assert done[at(40, 0)][0] < 0.02

    def test_a_second_partial_filter_starts_a_new_region(self, apply: Apply) -> None:
        # 次の部分フィルタで前の範囲が閉じる 閉じないと、2 つ目の範囲の効果が 1 つ目にも出る
        left = partial(center_x=-40.0, region_width=20.0, region_height=20.0)
        right = partial(center_x=40.0, region_width=20.0, region_height=20.0)
        done = apply(white(), (left, invert(), right, invert()))
        assert done[at(-40, 0)][0] < 0.02
        assert done[at(40, 0)][0] < 0.02
        assert done[at(0, 0)][0] > 0.98

    def test_the_fixed_items_close_the_range(self, apply: Apply) -> None:
        # 置いたクリップの描画の欄（配置・反転）は足したエフェクトの後ろにある 範囲に含めると、
        # 部分フィルタを足したクリップを X で動かしたとき、範囲の中の絵だけが動く
        fixed = replace(invert(), fixed=True)
        done = apply(white(), (partial(**CENTRE_BOX), invert(), fixed))
        assert done[at(0, 0)][0] > 0.98
        assert done[at(40, 0)][0] < 0.02

    def test_alone_it_changes_nothing(self, apply: Apply) -> None:
        # 積んだばかり（後ろに何も無い）の部分フィルタで絵が消えたり欠けたりしない
        plain = apply(stripes(), ())
        done = apply(stripes(), (partial(**CENTRE_BOX),))
        assert not changed(plain, done).any()

    def test_a_disabled_partial_filter_lets_everything_through(self, apply: Apply) -> None:
        # 切った部分フィルタは無いのと同じ 後ろの反転は絵全体に効く
        off = replace(partial(**CENTRE_BOX), enabled=False)
        done = apply(white(), (off, invert()))
        assert done[at(40, 0)][0] < 0.02


# --- フィルタのクリップに積む（下のトラック全体の範囲だけを隠す） ---

SETTINGS = ProjectSettings(width=160, height=90, frame_rate=FrameRate(30))


def _box(x: float) -> GeneratedSource:
    """白い 30px の四角 横の位置だけ変える"""
    return GeneratedSource(
        kind="shape",
        params={
            "shape": "rect",
            "color": (1.0, 1.0, 1.0, 1.0),
            "width": AnimatedValue(30.0),
            "height": AnimatedValue(30.0),
            "pos_x": AnimatedValue(x),
        },
    )


def _filter_project(*effects: Effect) -> Project:
    """左と右の四角を別のトラックに置き、その上にフィルタ"""
    project = Project.create(SETTINGS)
    tracks = (
        Track(TrackKind.VIDEO, "V1", (Clip(timeline_start=0, duration=30, source=_box(-40)),)),
        Track(TrackKind.VIDEO, "V2", (Clip(timeline_start=0, duration=30, source=_box(40)),)),
        Track(
            TrackKind.VIDEO,
            "V3",
            (Clip(timeline_start=0, duration=30, source=FILTER.create(), effects=effects),),
        ),
    )
    return replace(project, timeline=replace(project.timeline, tracks=tracks))


def _render(project: Project, gl: OffscreenGLContext) -> np.ndarray:
    renderer = FrameRenderer(project, context=gl)
    try:
        return renderer.render(5)
    finally:
        renderer.close()


class TestInAFilterClip:
    def test_positions_count_from_the_centre_of_the_screen(self, gl: OffscreenGLContext) -> None:
        # フィルタに積むと、下のトラック全体の決めた範囲だけが変わる 位置は画面の中央から
        # 数える 左の四角（中央から -40）を範囲に入れれば左だけが反転し、右は元のまま
        effects = (partial(center_x=-40.0, region_width=40.0, region_height=40.0), invert())
        image = _render(_filter_project(*effects), gl)
        # 画像は上の行から 中央の行で左右の四角の真ん中を見る
        row = SETTINGS.height // 2
        assert image[row, 80 - 40, 0] < 10
        assert image[row, 80 + 40, 0] > 245

    def test_region_blur_blurs_only_the_chosen_part(self, gl: OffscreenGLContext) -> None:
        # 左の四角の縁だけがぼける 右の四角の縁までぼければ、範囲が画面全体に掛かっている
        effect = region(
            mode="blur", blur_radius=8.0, center_x=-40.0, region_width=60.0, region_height=60.0
        )
        plain = _render(_filter_project(), gl).astype(int)
        done = _render(_filter_project(effect), gl).astype(int)
        row = SETTINGS.height // 2
        left_edge = 80 - 40 - 15
        right_edge = 80 + 40 - 15
        assert abs(done[row, left_edge, 0] - plain[row, left_edge, 0]) > 30
        assert np.array_equal(done[:, 80:], plain[:, 80:])
        assert done[row, right_edge, 0] == plain[row, right_edge, 0]


class TestSaving:
    def test_the_region_and_its_keyframes_survive_saving(self) -> None:
        # 開き直すと範囲が既定へ戻る・動きが止まると、隠していた顔が見える
        moving = AnimatedValue(
            keyframes=(Keyframe(frame=0, value=-40.0), Keyframe(frame=10, value=40.0))
        )
        effects = (
            region(
                mode="blur",
                shape="ellipse",
                center_x=moving,
                center_y=12.0,
                region_width=80.0,
                region_height=50.0,
                rotation=15.0,
                feather=6.0,
                invert=True,
                blur_radius=20.0,
            ),
            partial(center_x=-40.0),
            invert(),
        )
        project = _filter_project(*effects)
        loaded = project_from_dict(project_to_dict(project))
        clip = loaded.timeline.tracks[2].clips[0]
        assert clip.effects == project.timeline.tracks[2].clips[0].effects
        center_x = clip.effects[0].params["center_x"]
        assert isinstance(center_x, AnimatedValue)
        assert center_x.at(10) == pytest.approx(40.0)
