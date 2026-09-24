"""``obj.effect`` で積んだ効果を、絵を読む・変える呼び出しの前に掛けること（Issue #176）

AviUtl の ``obj.effect`` はその場で絵を変える こちらは積んでおき、描くときに GPU で
まとめて掛ける 間にリサイズや ``obj.copybuffer`` が挟まったときだけ、描画側から
渡された関数で先に掛ける 壊れると、ぼかしてから広げた絵のぼけ幅が元のまま・
写し取った絵に効果が無い・読み込み直した図形が前の絵のための効果でぼける

GPU は使わない 掛ける関数は、何を渡されたかを覚えて絵の周りを 2 画素広げる偽物
（AviUtl のぼかしも絵を広げる）
"""

from __future__ import annotations

import numpy as np

from sashimono.compat.aviutl.objapi import EffectRequest, ObjectState
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.aviutl.runtime import LuaScriptRuntime

BLUR = 'obj.effect("ぼかし", "範囲", 4)'


class _Baker:
    """渡された絵と効果を覚え、周りを 2 画素の透明で広げて返す"""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[int, ...], tuple[str, ...]]] = []

    def __call__(self, image: np.ndarray, effects: tuple[EffectRequest, ...]) -> np.ndarray:
        self.calls.append((image.shape, tuple(effect.kind for effect in effects)))
        return np.pad(image, ((2, 2), (2, 2), (0, 0)))


def _run(source: str) -> tuple[ObjectState, _Baker, CompatibilityReport]:
    state = ObjectState(image=np.full((4, 4, 4), 255, np.uint8), screen_w=320, screen_h=180)
    baker = _Baker()
    report = CompatibilityReport()
    runtime = LuaScriptRuntime(report=report, apply_effects=baker)
    result = runtime.run(source, state)
    assert not result.failed, result.message
    return state, baker, report


class TestSettle:
    def test_effects_before_a_resize_are_applied_to_the_small_picture(self) -> None:
        # ぼかしは 4 画素の絵へ掛かり、広がった 8 画素の絵を 2 倍にする 描くときまで待つと
        # 広げた後の絵へ掛かり、ぼけ幅が 2 倍にならない
        state, baker, report = _run(f'{BLUR} obj.effect("リサイズ", "拡大率", 200)')
        assert baker.calls == [((4, 4, 4), ("blur",))]
        assert state.image.shape[:2] == (16, 16)
        assert state.effects == []
        assert not any("先に積んだ" in line for line in report.missing)

    def test_effects_after_a_resize_wait_for_the_draw(self) -> None:
        # 後に積んだ効果はこれまでどおり描くときに GPU で掛ける ここで読み戻すと、
        # 効果を積んで描くだけのスクリプトまで 1 回ごとに GPU から読み戻す
        state, baker, _ = _run(f'obj.effect("リサイズ", "拡大率", 200) {BLUR}')
        assert baker.calls == []
        assert [effect.kind for effect in state.effects] == ["blur"]

    def test_copybuffer_keeps_the_applied_picture(self) -> None:
        # sigma は 縁取り や 領域拡張 の直後に絵を写して取っておく 写す絵に効果が無いと、
        # 取っておいた絵を後で描いたときに縁取りが消える
        state, baker, _ = _run(f'{BLUR} obj.copybuffer("tmp", "obj")')
        assert baker.calls == [((4, 4, 4), ("blur",))]
        assert state.buffers["tmp"].shape[:2] == (8, 8)
        assert state.effects == []

    def test_offscreen_applies_the_stacked_effects(self) -> None:
        # オフスクリーン描画 は積んだ効果を焼き込む物 焼き込まずに素通しにすると、
        # 後に積んだ効果と一緒に描くときに掛かり、順が入れ替わる
        state, baker, report = _run(f'{BLUR} obj.effect("オフスクリーン描画")')
        assert baker.calls == [((4, 4, 4), ("blur",))]
        assert state.effects == []
        assert not any("先に積んだ" in line for line in report.missing)

    def test_reading_a_pixel_sees_the_applied_picture(self) -> None:
        # 効果を掛けた後の絵の画素を読む 掛ける前の絵を読むと、広がった所が見えない
        state, baker, _ = _run(f"{BLUR} _, a = obj.getpixel(0, 0) obj.ox = a")
        assert len(baker.calls) == 1
        # 広げた 2 画素は透明
        assert state.ox == 0.0

    def test_replacing_the_picture_drops_the_stacked_effects(self) -> None:
        # AviUtl では効果は差し替える前の絵に掛かって、絵ごと消える 残すと、読み込み直した
        # 絵へ描くときに掛かる
        state, baker, _ = _run(f'obj.copybuffer("tmp", "obj") {BLUR} obj.load("tempbuffer")')
        assert baker.calls == []
        assert state.effects == []
        assert state.image.shape[:2] == (4, 4)

    def test_without_a_baker_the_order_is_recorded(self) -> None:
        # 掛ける関数を持たない所（GPU の無い道具）では焼き込めない 黙ると順が入れ替わった
        # 理由が分からないので、互換性レポートに残す
        state = ObjectState(image=np.full((4, 4, 4), 255, np.uint8), screen_w=320, screen_h=180)
        report = CompatibilityReport()
        LuaScriptRuntime(report=report).run(f'{BLUR} obj.effect("リサイズ", "拡大率", 200)', state)
        assert any("リサイズ" in line and "先に積んだ" in line for line in report.missing)
        assert [effect.kind for effect in state.effects] == ["blur"]
