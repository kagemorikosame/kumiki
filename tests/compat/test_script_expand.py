"""スクリプトの ``obj.effect("領域拡張")`` がその場で絵を広げること（#186）

大きさを読む所は広げた後の大きさを見る

sigma と PSDToolKit は 領域拡張 の直後に ``obj.getpixel()`` や ``obj.w`` で大きさを読み、
``obj.copybuffer`` で広げた絵を取っておく 描くときまで待つと、その間に読む大きさが
広げる前のままで、はみ出させる余白を足した座標がずれ、取っておいた絵も小さいまま

あわせて ``obj.w`` ``obj.h`` と ``obj.getpixel()`` は、先に積んだ効果を掛けた後の大きさを返す
AviUtl の ``obj.effect`` はその場で絵を変える（ぼかしは絵を広げる）

GPU は使わない 掛ける関数は、何を渡されたかを覚えて絵の周りを 2 画素広げる偽物
"""

from __future__ import annotations

import numpy as np

from sashimono.compat.aviutl.objapi import EffectRequest, ObjectState
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.aviutl.runtime import LuaScriptRuntime

BLUR = 'obj.effect("ぼかし", "範囲", 4)'


class _Baker:
    """渡された効果を覚え、周りを 2 画素の透明で広げて返す"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, image: np.ndarray, effects: tuple[EffectRequest, ...]) -> np.ndarray:
        self.calls.append(tuple(effect.kind for effect in effects))
        return np.pad(image, ((2, 2), (2, 2), (0, 0)))


def _picture(width: int = 6, height: int = 4) -> np.ndarray:
    """左上だけ赤く、残りは白い不透明な絵 どちらへ広げたかが画素で分かる"""
    image = np.full((height, width, 4), 255, np.uint8)
    image[0, 0] = (255, 0, 0, 255)
    return image


def _run(
    source: str, *, baker: _Baker | None = None, report: CompatibilityReport | None = None
) -> ObjectState:
    state = ObjectState(image=_picture(), screen_w=320, screen_h=180)
    runtime = LuaScriptRuntime(report=report or CompatibilityReport(), apply_effects=baker)
    result = runtime.run(source, state)
    assert not result.failed, result.message
    return state


class TestExpandAtOnce:
    def test_expanding_grows_the_picture_before_the_size_is_read(self) -> None:
        # sigma の canvas_resize は広げた直後に obj.getpixel() で大きさを読む 広げる前の
        # 大きさが返ると、続く 塗りつぶし や切り落としの量が広げた分だけずれる
        state = _run(
            'obj.effect("領域拡張", "上", 3, "下", 1, "左", 5, "右", 2)'
            " local w, h = obj.getpixel() obj.ox = w obj.oy = h"
        )
        assert (state.ox, state.oy) == (13.0, 8.0)
        assert state.image.shape[:2] == (8, 13)
        # 描くときに GPU で広げ直すと 2 回広がる
        assert state.effects == []

    def test_the_picture_stays_where_the_amounts_put_it(self) -> None:
        # 元の絵は 上 と 左 の量だけ内側に入る 広げた所は透明 逆に置くと、PSDToolKit の
        # 吹き出しの尻尾が反対側に付く
        state = _run('obj.effect("領域拡張", "上", 3, "左", 5)')
        image = state.image
        assert tuple(image[3, 5]) == (255, 0, 0, 255)
        assert image[:3, :, 3].max() == 0
        assert image[:, :5, 3].max() == 0
        # 絵の真ん中がオブジェクトの位置へ来る（.exa の 領域拡張 と同じく、元の絵は
        # 広げた量の半分だけずれて見える） 位置を足して戻すと .exa の読み込みと食い違う
        assert (state.ox, state.oy, state.cx, state.cy) == (0.0, 0.0, 0.0, 0.0)

    def test_obj_w_sees_the_expanded_picture(self) -> None:
        # PSDToolKit は吹き出しの尻尾の分を広げた後に obj.w と obj.h で描く所を決める
        state = _run(
            'obj.effect("領域拡張", "下", 10) obj.ox = obj.w obj.oy = obj.h'
            ' obj.copybuffer("tmp", "obj")'
        )
        assert (state.ox, state.oy) == (6.0, 14.0)
        assert state.buffers["tmp"].shape[:2] == (14, 6)

    def test_fill_repeats_the_edge(self) -> None:
        # 塗りつぶし は広げた所を縁の色で埋める sigma の canvas_resize と ディザ が使う
        # 透明のままだと、縁の色を伸ばして敷く背景が欠ける
        state = _run('obj.effect("領域拡張", "上", 2, "左", 2, "塗りつぶし", 1)')
        image = state.image
        assert image.shape[:2] == (6, 8)
        assert tuple(image[0, 0]) == (255, 0, 0, 255)
        assert tuple(image[0, 7]) == (255, 255, 255, 255)
        assert image[..., 3].min() == 255

    def test_effects_stacked_before_are_applied_first(self) -> None:
        # ぼかしてから広げる 広げた後の絵へぼかしを掛けると、ぼけの広がる所が変わる
        baker = _Baker()
        state = _run(f'{BLUR} obj.effect("領域拡張", "右", 4)', baker=baker)
        assert baker.calls == [("blur",)]
        assert state.image.shape[:2] == (8, 14)
        assert state.effects == []

    def test_without_a_baker_it_waits_for_the_draw(self) -> None:
        # 先に積んだ効果を掛けられない所では、広げるのも描くときへ回して順を守る
        # その場で広げると、描くときのぼかしが広げた後の絵へ掛かる
        report = CompatibilityReport()
        state = _run(f'{BLUR} obj.effect("領域拡張", "右", 4)', report=report)
        assert [effect.kind for effect in state.effects] == ["blur", "expand_area"]
        assert state.image.shape[:2] == (4, 6)

    def test_broken_amounts_are_recorded(self) -> None:
        # 数でない量で広げると大きさが決まらない 黙って捨てると、広がらなかった理由が出ない
        report = CompatibilityReport()
        state = _run('obj.effect("領域拡張", "上", 0/0)', report=report)
        assert state.image.shape[:2] == (4, 6)
        assert any("領域拡張" in line for line in report.missing)


class TestSizeAfterStackedEffects:
    def test_obj_w_applies_the_stacked_effects(self) -> None:
        # AviUtl のぼかしは絵を広げる 積んだまま大きさを読むと、広がる前の大きさになる
        baker = _Baker()
        state = _run(f"{BLUR} obj.ox = obj.w obj.oy = obj.h", baker=baker)
        assert baker.calls == [("blur",)]
        assert (state.ox, state.oy) == (10.0, 8.0)

    def test_getpixel_without_arguments_applies_the_stacked_effects(self) -> None:
        # sigma は大きさを obj.getpixel() で読む obj.w と同じく掛けた後の大きさを返す
        baker = _Baker()
        state = _run(f"{BLUR} local w, h = obj.getpixel() obj.ox = w obj.oy = h", baker=baker)
        assert baker.calls == [("blur",)]
        assert (state.ox, state.oy) == (10.0, 8.0)

    def test_reading_the_size_without_effects_does_not_bake(self) -> None:
        # 積んだ効果が無ければ GPU へ渡さない 大きさを読むたびに読み戻すと重い
        baker = _Baker()
        _run("obj.ox = obj.w local w, h = obj.getpixel()", baker=baker)
        assert baker.calls == []


class TestBakerRefuses:
    def test_effects_stay_stacked_when_the_baker_cannot_apply_them(self) -> None:
        # 焼き込めない大きさ（GPU の上限を超える作業場）では、掛ける関数が None を返す
        # そのときは効果を積んだまま描くときへ回す 例外で止めると、フレームごと描けなくなる
        def refuse(image: np.ndarray, effects: tuple[EffectRequest, ...]) -> None:
            del image, effects

        state = ObjectState(image=_picture(), screen_w=320, screen_h=180)
        runtime = LuaScriptRuntime(report=CompatibilityReport(), apply_effects=refuse)
        result = runtime.run(f"{BLUR} obj.ox = obj.w", state)
        assert not result.failed, result.message
        assert state.ox == 6.0
        assert [effect.kind for effect in state.effects] == ["blur"]
