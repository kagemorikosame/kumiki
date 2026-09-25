"""スクリプトの ``obj.effect`` を呼んだ順に掛けること（Issue #176）

AviUtl の ``obj.effect("ぼかし")`` はその場で絵を変える こちらはエフェクトを積んでおき、
描くときに GPU でまとめて掛ける そのままだと、後から来た ``obj.effect("リサイズ")`` のように
その場で絵を変える呼び出しより**後に**掛かり、ぼかしてから広げた絵が、広げてからぼかした
絵と同じになる 壊れると、sigma の 単純図形σ のように 1 画素の絵を効果ごと広げる配布物の
ぼけ幅や縁の太さが、AviUtl の何倍も細く出る
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pytest

from sashimono.compat.aviutl import catalog as catalog_module
from sashimono.compat.aviutl.catalog import ScriptCatalog, set_script_catalog
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.core.commands import AddClip, AddTrack
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    Effect,
    GeneratedSource,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.effects import registry
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer
from sashimono.engine.render.script_bake import (
    BAKE_CANVAS_LIMIT,
    BAKE_MARGIN,
    ScriptEffectBaker,
    bake_margin,
    fitted_margin,
)

SETTINGS = ProjectSettings(width=96, height=96, frame_rate=FrameRate(30))

#: 8 画素の白い四角を作る AviUtl の図形は 1 回の読み込みで絵の大きさちょうどになる
LOAD = 'obj.load("figure", "四角形", 0xffffff, 8)'
BLUR = 'obj.effect("ぼかし", "範囲", 4)'
RESIZE = 'obj.effect("リサイズ", "拡大率", 400)'


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


def _render(source: str, gl_context: OffscreenGLContext) -> np.ndarray:
    """``source`` を積んだクリップを 1 枚描く 画面は黒 絵は真ん中"""
    saved = catalog_module._catalog
    catalog = ScriptCatalog(roots=())
    catalog.add_text("aviutl:試験.anm:順", source)
    set_script_catalog(catalog)
    try:
        return _render_with(gl_context)
    finally:
        # 戻さないと、後に走る試験の script_catalog() が中身の無い試験用の一覧を返し、
        # 配布物のスクリプトを探す試験が走らせる順によって落ちる
        catalog_module._catalog = saved
        if saved is not None:
            saved.register_all()


def _render_with(gl_context: OffscreenGLContext) -> np.ndarray:
    definition = registry.get("aviutl:試験.anm:順")
    assert definition is not None
    clip = Clip(
        timeline_start=0,
        duration=30,
        source=GeneratedSource(
            kind="shape",
            params={"shape": "rect", "width": AnimatedValue(8.0), "height": AnimatedValue(8.0)},
        ),
        effects=(definition.create(),),
    )
    # 編集と同じ Command で組む モデルを直接書き換えると、編集の経路が守る決まりを通らない
    track = Track(TrackKind.VIDEO, "V1")
    project = AddTrack(track).apply(Project.create(SETTINGS))
    project = AddClip(track.id, clip).apply(project)
    renderer = FrameRenderer(project, context=gl_context)
    try:
        return renderer.render(5)
    finally:
        renderer.close()


def _row(image: np.ndarray) -> np.ndarray:
    """真ん中の行の明るさ（赤） 四角は白なので 1 つの色で足りる"""
    return image[48, :, 0].astype(int)


class TestOrder:
    def test_blur_then_resize_widens_the_blur(self, gl_context: OffscreenGLContext) -> None:
        # ぼかしてから 4 倍にすると、ぼけの幅も 4 倍になる 広げた後にぼかすと元のぼけ幅のまま
        # 同じ絵になるなら、先に積んだぼかしがリサイズの後へ回っている
        blurred_first = _row(_render(f"{LOAD} {BLUR} {RESIZE}", gl_context))
        resized_first = _row(_render(f"{LOAD} {RESIZE} {BLUR}", gl_context))
        assert not np.array_equal(blurred_first, resized_first)
        # 四角は 32..64 縁から 6 画素内側は、広げてからぼかせばほぼ白、
        # ぼかしてから広げれば 4 倍に広がったぼけの中で暗い
        assert resized_first[38] > 240
        assert blurred_first[38] < resized_first[38] - 40
        # 縁から 6 画素外側も、ぼかしてから広げた方だけ明るさが届く
        assert resized_first[26] < 16
        assert blurred_first[26] > resized_first[26] + 16

    def test_resize_then_blur_is_unchanged(self, gl_context: OffscreenGLContext) -> None:
        # リサイズの後に積んだぼかしは、これまでどおり描くときに掛かる 広げた四角の真ん中は白、
        # 四角から遠い所は黒 焼き込みの仕組みを入れても、こちらの順は変わらない
        row = _row(_render(f"{LOAD} {RESIZE} {BLUR}", gl_context))
        assert row[48] > 250
        assert row[4] < 4


def _shadow(offset_x: float) -> Effect:
    """真横へ ``offset_x`` ずらした濃い影 ぼかさない"""
    definition = registry.get("shadow")
    assert definition is not None
    return definition.create(offset_x=offset_x, offset_y=0.0, blur=0.0, opacity=100.0)


class TestMargin:
    """効果を掛ける所の余白は、積んだ効果が絵を外へ動かす量から決める（#186）

    余白を決め打ちにすると、それより遠くへ動かす影や広げる物が掛けた所で切れ、
    ``obj.w`` や写し取った絵から消える
    """

    def test_a_far_shadow_survives_the_bake(self, gl_context: OffscreenGLContext) -> None:
        # 影を 200 画素ずらす 128 画素の余白では影が作業場の外へ出て消える
        image = np.full((8, 8, 4), 255, np.uint8)
        baker = ScriptEffectBaker()
        with gl_context:
            try:
                baked = baker.apply(image, (_shadow(200.0),), 0, 30.0, 30)
            finally:
                baker.release()
        assert baked is not None
        # 真ん中は動かさないので、影の分だけ左右へ同じ幅で広がる
        assert baked.shape[1] >= 8 + 2 * 200
        middle = baked[baked.shape[0] // 2]
        assert middle[-4:, 3].max() > 0

    def test_the_margin_covers_what_the_effects_move(self) -> None:
        # 画素で決める項目の分だけ外へ出うる 足りない余白は掛けた所で切れる
        assert bake_margin((_shadow(200.0),), 0) >= 200
        definition = registry.get("expand_area")
        assert definition is not None
        grown = definition.create(top=300.0, left=40.0)
        assert bake_margin((grown, _shadow(-150.0)), 0) >= 300 + 150

    def test_the_margin_never_drops_below_the_old_floor(self) -> None:
        # 画素の項目を持たない効果（色だけ変える物）でも、これまでの余白は残す
        # 縮めると、範囲の決まらない広がり（グローの光など）が切れる
        assert bake_margin((), 0) == BAKE_MARGIN

    def test_the_reach_is_not_cut_silently(self) -> None:
        # 効果を重ねた到達距離は上限で丸めない 丸めると、足りない余白で掛けて外側が
        # 黙って欠ける 足りないかどうかは作業場の大きさを決める所（fitted_margin）が見る
        definition = registry.get("displacement_map")
        assert definition is not None
        far = definition.create(move_x=4000.0)
        assert bake_margin((far, _shadow(200.0)), 0) >= 4200

    def test_the_canvas_stays_within_the_limit(self) -> None:
        # 絵と余白を合わせた作業場の一辺は上限までに抑える 4096 画素の絵に 4096 画素の
        # 余白を足すと 12288 画素四方のバッファを何枚も作り、GPU のメモリが尽きる
        report = CompatibilityReport()
        margin = fitted_margin(4096, 100, 4096, report)
        assert 4096 + 2 * margin <= BAKE_CANVAS_LIMIT
        # 足りない余白で掛けたことは記録に残す 黙ると絵の外側が欠けた理由が分からない
        assert any("余白" in line for line in report.missing)

    def test_a_margin_that_fits_is_kept_and_not_recorded(self) -> None:
        # 収まる余白まで縮めると、遠くへ動かす影の外側が欠ける 収まるのに記録すると、
        # 本当に足りなかったときの記録が埋もれる
        report = CompatibilityReport()
        assert fitted_margin(100, 60, 300, report) == 300
        assert not report.missing

    def test_a_canvas_over_the_gpu_limit_is_not_baked(self, gl_context: OffscreenGLContext) -> None:
        # GPU が作れる大きさを超える作業場は作らない 作ろうとするとフレームバッファの例外が
        # 描画まで伝わり、フレームごと描けなくなる 焼き込まずに None を返し、効果は描くときへ回る
        image = np.full((8, 8, 4), 255, np.uint8)
        baker = ScriptEffectBaker()
        baker.gpu_limit = 64
        with gl_context:
            try:
                assert baker.apply(image, (_shadow(200.0),), 0, 30.0, 30) is None
            finally:
                baker.release()
