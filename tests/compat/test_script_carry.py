"""積んだ効果を先に掛けられないとき、バッファと一緒に持ち運ぶ（Issue #212）

焼き込みの上限や GPU の大きさの上限で積んだ効果を先に掛けられないと、``obj.copybuffer`` や
仮想バッファへの ``obj.draw`` は効果の掛かっていない絵を写す 前はその絵のまま進み、読み戻した
絵には効果が掛からなかった 絵と一緒に効果を持ち運び、読み戻したときに積み直せば、描くときに
AviUtl と同じ順で掛かる

焼き込めない所は、掛ける関数を渡さないランタイムで作る（GPU の無い道具と同じ）
"""

from __future__ import annotations

import numpy as np

from sashimono.compat.aviutl.objapi import EffectRequest, ObjectState
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.aviutl.runtime import LuaScriptRuntime

BLUR = 'obj.effect("ぼかし", "範囲", 4)'


def _run(source: str, *, bake: bool = False) -> tuple[ObjectState, CompatibilityReport]:
    state = ObjectState(image=np.full((4, 4, 4), 255, np.uint8), screen_w=32, screen_h=18)
    report = CompatibilityReport()

    def grow(image: np.ndarray, effects: tuple[object, ...]) -> np.ndarray:
        del effects
        return np.pad(image, ((2, 2), (2, 2), (0, 0)))

    runtime = LuaScriptRuntime(report=report, apply_effects=grow if bake else None)
    result = runtime.run(source, state)
    assert not result.failed, result.message
    return state, report


def _kinds(state: ObjectState) -> list[str]:
    return [effect.kind for effect in state.result()[-1].effects]


class TestCarry:
    def test_a_copied_picture_keeps_its_effects(self) -> None:
        """写して読み戻した絵にも、積んだ効果が掛かる

        前は写した絵が効果の無いまま残り、読み戻すと効果が消えていた（ぼかした板が
        くっきり描かれる） 読み戻す前に積んでいた効果は捨て、写した分だけを積み直す
        """
        state, report = _run(
            f'{BLUR} obj.copybuffer("cache:a", "obj") obj.copybuffer("obj", "cache:a")'
        )
        assert state.image.shape[:2] == (4, 4)
        assert _kinds(state) == ["blur"]
        # 持ち運べたので、順が入れ替わったとは記録しない
        assert not any("copybuffer" in line for line in report.missing)

    def test_loading_the_tempbuffer_brings_the_effects_back(self) -> None:
        # obj.load("tempbuffer") も読み戻す道 写した効果を積み直さないと、読み戻した絵が素通しになる
        state, _ = _run(f'{BLUR} obj.copybuffer("tmp", "obj") obj.load("tempbuffer")')
        assert _kinds(state) == ["blur"]

    def test_a_copy_between_buffers_keeps_the_effects(self) -> None:
        state, _ = _run(
            f'{BLUR} obj.copybuffer("cache:a", "obj") obj.copybuffer("cache:b", "cache:a")'
            ' obj.copybuffer("obj", "cache:b")'
        )
        assert _kinds(state) == ["blur"]

    def test_the_whole_picture_drawn_to_an_empty_tempbuffer_keeps_its_effects(self) -> None:
        """同じ大きさの空の仮想バッファへ真ん中に不透明なまま描いて読み戻す形

        仮想バッファは絵そのものになるので、効果ごと持ち運べる 前は効果の無い絵を貼り、
        読み戻した絵に効果が掛からなかった
        """
        state, report = _run(
            f'{BLUR} obj.setoption("drawtarget", "tempbuffer", 4, 4) obj.draw()'
            ' obj.setoption("drawtarget", "framebuffer") obj.load("tempbuffer")'
        )
        assert _kinds(state) == ["blur"]
        assert not any("obj.draw" in line for line in report.missing)

    def test_a_shifted_draw_is_still_recorded(self) -> None:
        # ずらして重ねた物は、効果を掛けた後の絵でないと重ねられない 黙ると順が違う理由が分からない
        state, report = _run(
            f'{BLUR} obj.setoption("drawtarget", "tempbuffer", 4, 4) obj.draw(1, 0)'
            ' obj.setoption("drawtarget", "framebuffer") obj.load("tempbuffer")'
        )
        assert _kinds(state) == []
        assert any("obj.draw" in line and "掛けずに" in line for line in report.missing)

    def test_drawing_onto_a_carried_buffer_applies_its_effects_first(self) -> None:
        """持ち運んだ効果のある仮想バッファへ重ねる前に、下の絵へ効果を掛ける

        後で掛けると、重ねた物までぼける 掛けられる所では下の絵だけへ掛かる
        """
        state = ObjectState(image=np.full((4, 4, 4), 255, np.uint8), screen_w=32, screen_h=18)
        state.buffers["tmp"] = np.zeros((4, 4, 4), np.uint8)
        state.buffer_effects["tmp"] = (EffectRequest(kind="blur", params={}, original="ぼかし"),)
        seen: list[tuple[int, ...]] = []

        def grow(image: np.ndarray, effects: tuple[object, ...]) -> np.ndarray:
            del effects
            seen.append(image.shape)
            return np.pad(image, ((2, 2), (2, 2), (0, 0)))

        runtime = LuaScriptRuntime(apply_effects=grow)
        result = runtime.run('obj.setoption("drawtarget", "tempbuffer") obj.draw()', state)
        assert not result.failed, result.message
        assert seen == [(4, 4, 4)]
        assert state.buffers["tmp"].shape[:2] == (8, 8)
        assert "tmp" not in state.buffer_effects

    def test_a_new_tempbuffer_forgets_the_carried_effects(self) -> None:
        # 作り直した空の仮想バッファに前の絵の効果が残ると、後から描いた絵がぼける
        state, _ = _run(
            f'{BLUR} obj.copybuffer("tmp", "obj")'
            ' obj.setoption("drawtarget", "tempbuffer", 4, 4)'
            ' obj.setoption("drawtarget", "framebuffer") obj.load("tempbuffer")'
        )
        assert _kinds(state) == []

    def test_a_baked_copy_carries_nothing(self) -> None:
        # 先に掛けられれば今までどおり掛けた絵を写す 効果も持ち運ぶと 2 回掛かる
        state, _ = _run(f'{BLUR} obj.copybuffer("tmp", "obj") obj.load("tempbuffer")', bake=True)
        assert _kinds(state) == []
        assert state.image.shape[:2] == (8, 8)
