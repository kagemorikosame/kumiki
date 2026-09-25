"""積んだ ``ぼかし`` の「サイズ固定」を先に掛ける段で扱い、続くクリッピングをその場で切る（#190）

sigma の アクリル矩形・磨りガラス矩形 は、画面を写した絵を ぼかし の広がりの分だけ大きく
切り出し、``obj.effect("ぼかし", …, "サイズ固定", 1)`` を積んでから クリッピング で余分を
落とす 先に掛ける段がサイズ固定を知らないと、ぼかした絵が四方へ広がったまま切られて、
板が四方に 16px 大きく残る そのためクリッピングは描くときまで待たせていた

GPU は使わない 掛ける関数は、何を渡されたかを覚えて周りを 2 画素の透明で広げる偽物
（本物のぼかしも絵を広げる）
"""

from __future__ import annotations

import numpy as np

from sashimono.compat.aviutl.objapi import EffectRequest, ObjectState
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.aviutl.runtime import LuaScriptRuntime

FIXED_BLUR = 'obj.effect("ぼかし", "範囲", 4, "サイズ固定", 1)'


class _Baker:
    def __init__(self) -> None:
        self.calls: list[tuple[EffectRequest, ...]] = []

    def __call__(self, image: np.ndarray, effects: tuple[EffectRequest, ...]) -> np.ndarray:
        self.calls.append(effects)
        return np.pad(image, ((2, 2), (2, 2), (0, 0)), constant_values=255)


def _picture() -> np.ndarray:
    image = np.full((10, 12, 4), 255, np.uint8)
    image[0, 0] = (255, 0, 0, 255)
    return image


def _run(source: str, baker: _Baker | None = None) -> tuple[ObjectState, CompatibilityReport]:
    report = CompatibilityReport()
    state = ObjectState(image=_picture(), screen_w=320, screen_h=180)
    result = LuaScriptRuntime(report=report, apply_effects=baker).run(source, state)
    assert not result.failed, result.message
    return state, report


class TestFixedSizeBlur:
    def test_a_fixed_size_blur_keeps_the_size(self) -> None:
        # 広がった絵のまま大きさを読むと、sigma が切り出しの量を決め違える
        baker = _Baker()
        state, _ = _run(f"{FIXED_BLUR} obj.ox = obj.w obj.oy = obj.h", baker)
        assert (state.ox, state.oy) == (12.0, 10.0)
        # 真ん中から同じ幅ずつ落とす 片側だけ落とすと絵が横へずれる
        assert tuple(state.image[0, 0]) == (255, 0, 0, 255)

    def test_the_flag_is_not_passed_on_as_unknown(self) -> None:
        # サイズ固定 は先に掛ける段で扱ったので、写す先の ぼかし へは渡さない 渡すと
        # 扱えた物まで互換性レポートに「項目: サイズ固定」と出る
        baker = _Baker()
        _run(f"{FIXED_BLUR} obj.ox = obj.w", baker)
        ((blur,),) = baker.calls
        assert "サイズ固定" not in blur.params

    def test_effects_after_it_grow_on_their_own(self) -> None:
        # サイズ固定 はそのぼかしだけの物 後に積んだ効果が広げた分は残す
        baker = _Baker()
        state, _ = _run(f'{FIXED_BLUR} obj.effect("縁取り", "サイズ", 2) obj.ox = obj.w', baker)
        assert [tuple(e.kind for e in call) for call in baker.calls] == [("blur",), ("border",)]
        assert state.ox == 16.0

    def test_effects_before_it_are_applied_before_the_size_is_taken(self) -> None:
        # 前に積んだ効果が広げた後の大きさを保つ 元の絵の大きさへ戻すと、縁取りが切れる
        baker = _Baker()
        state, _ = _run(f'obj.effect("縁取り", "サイズ", 2) {FIXED_BLUR} obj.ox = obj.w', baker)
        assert [tuple(e.kind for e in call) for call in baker.calls] == [("border",), ("blur",)]
        assert state.ox == 16.0


class TestClippingAfterStackedEffects:
    def test_clipping_cuts_at_once_after_a_fixed_blur(self) -> None:
        # アクリル矩形の形 ぼかし の分だけ大きい絵を切り戻す 描くときまで待つと、
        # その間に読む大きさが切る前のまま
        baker = _Baker()
        state, _ = _run(
            f'{FIXED_BLUR} obj.effect("クリッピング", "上", 2, "下", 2, "左", 3, "右", 3)'
            " obj.ox = obj.w obj.oy = obj.h",
            baker,
        )
        assert state.effects == []
        assert (state.ox, state.oy) == (6.0, 6.0)

    def test_a_growing_effect_is_applied_before_the_cut(self) -> None:
        # AviUtl の obj.effect はその場で掛かる 広げた絵から切る
        baker = _Baker()
        state, _ = _run(
            'obj.effect("ぼかし", "範囲", 4) obj.effect("クリッピング", "上", 2, "左", 2)'
            " obj.ox = obj.w obj.oy = obj.h",
            baker,
        )
        assert state.effects == []
        assert (state.ox, state.oy) == (14.0, 12.0)

    def test_a_lens_blur_keeps_the_size_before_the_cut(self) -> None:
        # 磨りガラス矩形 は レンズブラー に サイズ固定 を付けずに同じ切り出し方をする
        # 広げてから切ると、板が四方にぼかしの範囲だけ大きく残る（2 画素広げる偽物で 16x14）
        baker = _Baker()
        state, _ = _run(
            'obj.effect("レンズブラー", "範囲", 4) obj.effect("クリッピング", "上", 2, "下", 2)'
            " obj.ox = obj.w obj.oy = obj.h",
            baker,
        )
        assert (state.ox, state.oy) == (12.0, 6.0)

    def test_without_a_baker_the_cut_still_waits(self) -> None:
        # 先に掛けられなければ、順を守るため切るのも描くときへ回す
        state, report = _run(f'{FIXED_BLUR} obj.effect("クリッピング", "上", 2)')
        assert [effect.kind for effect in state.effects] == ["blur", "crop"]
        assert state.image.shape[:2] == (10, 12)
