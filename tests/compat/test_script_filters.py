"""スクリプトの ``obj`` から、組み込みのフィルタと画面を使う（#170）

sigma の 単純図形σ の 菱形・アクリル矩形・磨りガラス矩形 は、図形を読んだあと
``obj.effect`` で 斜めクリッピング・単色化・色調補正・レンズブラー を掛け、
アクリル矩形は ``obj.copybuffer("obj", "frm")`` で下に重ねた画面を写して板にする
どれか 1 つでも欠けると、置いても四角のままか何も映らない
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np

from sashimono.compat.aviutl.catalog import ScriptCatalog
from sashimono.compat.aviutl.mapping import script_filter_effects
from sashimono.compat.aviutl.objapi import DrawCall, ObjectState
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.aviutl.runtime import LuaScriptRuntime, blank_image
from sashimono.core.commands.fixed import with_fixed_items
from sashimono.core.model import AnimatedValue, Clip, Effect
from sashimono.engine.render.scripts import ScriptStage, requested_effects


def _state(
    width: int = 40,
    height: int = 20,
    *,
    framebuffer: Callable[[], np.ndarray | None] | None = None,
) -> ObjectState:
    image = np.zeros((height, width, 4), np.uint8)
    image[..., 3] = 255
    return ObjectState(image=image, framebuffer=framebuffer)


def _run(code: str, state: ObjectState, report: CompatibilityReport | None = None) -> None:
    runtime = LuaScriptRuntime(report=report or CompatibilityReport(), instruction_limit=200_000)
    result = runtime.run(code, state)
    assert not result.failed, result.message


def _requested(code: str, report: CompatibilityReport | None = None) -> tuple[Effect, ...]:
    """スクリプトが頼んだ効果を、描くときに掛けるエフェクトの形で"""
    state = _state()
    _run(code, state, report)
    (call,) = state.result()
    return requested_effects(call)


def _value(effect: Effect, name: str) -> float:
    value = effect.params[name]
    assert isinstance(value, AnimatedValue)
    return value.static


class TestSize:
    def test_getpixel_without_arguments_gives_the_size(self) -> None:
        """``obj.getpixel()`` は絵の幅と高さを返す（lua.txt）

        1 画素目の色を返すと、菱形は幅 0xffffff の絵として角度を求め、四角のまま残る
        """
        state = _state(40, 20)
        _run("local w, h = obj.getpixel() obj.ox = w obj.oy = h", state)
        assert (state.ox, state.oy) == (40.0, 20.0)

    def test_getpixel_with_a_position_still_reads_the_colour(self) -> None:
        # 引数なしの分岐が位置つきの呼び出しまで幅と高さを返すと、画素を読んで色を決める
        # スクリプトが全部、幅と高さを色として読んで崩れる
        state = _state(4, 4)
        state.image[1, 2] = (255, 0, 0, 255)
        _run("local c, a = obj.getpixel(2, 1) obj.ox = c obj.oy = a", state)
        assert (state.ox, state.oy) == (0xFF0000, 1.0)


class TestFramebuffer:
    def test_copybuffer_reads_the_screen_below(self) -> None:
        """``obj.copybuffer("obj", "frm")`` で、それまでに下へ重ねた画面が絵になる

        写せないと、アクリル矩形は 1 画素の透明な絵のまま何も映らない
        """
        screen = np.zeros((36, 64, 4), np.uint8)
        screen[..., 0] = 200
        screen[..., 3] = 255
        state = _state(framebuffer=lambda: screen)
        _run('obj.copybuffer("obj", "frm")', state)
        assert state.image.shape == (36, 64, 4)
        assert int(state.image[0, 0, 0]) == 200
        # 写した物を書き換えても、画面の方は変わらない
        state.writable_image()[0, 0] = 0
        assert int(screen[0, 0, 0]) == 200

    def test_the_screen_is_read_only_when_asked(self) -> None:
        # 画面を読み戻すのは重い 使わないスクリプトで毎フレーム読むと再生の余裕を削る
        asked: list[int] = []

        def screen() -> np.ndarray:
            asked.append(1)
            return blank_image(4, 4)

        _run("obj.ox = 1", _state(framebuffer=screen))
        assert asked == []

    def test_without_a_screen_it_is_recorded(self) -> None:
        # 描く側が画面を渡さない所（テキストの埋め込み）では写せない 黙ると理由が分からない
        report = CompatibilityReport()
        _run('obj.copybuffer("obj", "frm")', _state(), report)
        assert any("frm" in line for line in report.lines())


class TestClip:
    def test_a_clip_cuts_the_picture_at_once(self) -> None:
        """先に積んだ効果が無ければ、クリッピングはその場で絵を切る

        アクリル矩形は画面を写した絵を板の大きさへ切り、切った後の大きさを読んで
        次を決める 描くときまで待つと、画面の大きさのままぼかしと切り落としが進む
        """
        state = _state(40, 20)
        _run(
            'obj.effect("クリッピング", "上", 2, "下", 4, "左", 6, "右", 10,'
            ' "中心の位置を変更", 1)',
            state,
        )
        assert state.image.shape[:2] == (14, 24)
        assert state.effects == []
        # 中心の位置を変更 は切った後の絵の真ん中を、オブジェクトの位置へ置く
        assert (state.ox, state.oy) == (0.0, 0.0)

    def test_a_clip_keeps_the_rest_in_place_by_default(self) -> None:
        # 中心の位置を変更 が無ければ、残った所は元の場所に留まる 左を多く切ると右へ寄る
        state = _state(40, 20)
        _run('obj.effect("クリッピング", "上", 2, "下", 4, "左", 6, "右", 10)', state)
        assert (state.ox, state.oy) == (-2.0, -1.0)
        assert (state.cx, state.cy) == (2.0, 1.0)

    def test_a_clip_after_other_effects_waits_for_them(self) -> None:
        # 先に積んだぼかしより前に切ると、ぼかしが切り口の外の絵を混ぜなくなる
        effects = _requested(
            'obj.effect("ぼかし", "範囲", 4) obj.effect("クリッピング", "上", 3, "下", 3)'
        )
        assert [effect.kind for effect in effects] == ["blur", "crop"]
        assert _value(effects[1], "top") == 3.0

    def test_a_waiting_clip_still_recentres(self) -> None:
        """後に回したクリッピングでも、中心の位置を変更 は切った後の平行移動として続く

        落とすと、先に効果を積んでから片側を切るスクリプトで、残りが真ん中へ戻らない
        動かす量はエイリアスの読み込み（AviUtl2 で測った）と同じ 横が (右 − 左) / 2
        """
        effects = _requested(
            'obj.effect("ぼかし", "範囲", 4)'
            ' obj.effect("クリッピング", "上", 10, "左", 20, "右", 80, "中心の位置を変更", 1)'
        )
        assert [effect.kind for effect in effects] == ["blur", "crop", "transform"]
        assert _value(effects[2], "pos_x") == 30.0
        assert _value(effects[2], "pos_y") == 5.0


class TestFilters:
    def test_the_slant_clip_is_called(self) -> None:
        """斜めクリッピング を ``obj.effect`` から呼べる（菱形の 4 回の切り落とし）

        中心Y は AviUtl の下が正からこちらの上が正へ直す 呼べないと菱形が四角のまま残る
        """
        report = CompatibilityReport()
        (effect,) = _requested(
            'obj.effect("斜めクリッピング", "ぼかし", 0, "中心Y", 50, "角度", 45)', report
        )
        assert effect.kind == "crop_slant"
        assert _value(effect, "center_y") == -50.0
        assert _value(effect, "angle") == 45.0
        assert not report.lines()

    def test_the_monochrome_takes_the_colour_by_number(self) -> None:
        """単色化 の色は ``color`` という名前の数（0xRRGGBB）で来る

        ダイアログの名前（色）だけで引くと、既定の白で塗られる
        """
        (effect,) = _requested(
            'obj.effect("単色化", "color", 0x808080, "強さ", 20, "輝度を保持する", 1)'
        )
        assert effect.kind == "fill"
        colour = effect.params["color"]
        assert isinstance(colour, tuple)
        assert tuple(round(part * 255) for part in colour[:3]) == (128, 128, 128)
        assert _value(effect, "amount") == 20.0
        assert effect.params["keep_luma"] is True

    def test_a_colour_that_is_not_a_number_does_not_stop_the_frame(self) -> None:
        # Lua の 0/0 は NaN のまま来る int() が例外を出すと、そのフレームの描画ごと止まる
        # 色の欄は既定のままにして、読めなかったことを記録に残す
        report = CompatibilityReport()
        for broken in (float("nan"), float("inf")):
            (effect,) = script_filter_effects("単色化", {"color": broken}, report=report)
            assert effect.kind == "fill"
        assert any("単色化の color" in line for line in report.lines())

    def test_the_colour_correction_counts_from_a_hundred(self) -> None:
        """スクリプトの 色調補正 は 100 が元のまま 輝度は倍率、明るさは足す量

        アクリル矩形は 輝度 30 と 明るさ 135 で、明るさの幅を 30% に縮めて真ん中へ寄せる
        そのまま入れると 輝度 と 明るさ が同じ項目へ入り、明るさが倍になって白く飛んでいた
        """
        (effect,) = _requested(
            'obj.effect("色調補正", "明るさ", 135, "輝度", 30, "彩度", 100, "色相", 0)'
        )
        assert effect.kind == "color"
        assert _value(effect, "gain") == 30.0
        assert _value(effect, "offset") == 35.0
        assert _value(effect, "saturation") == 0.0
        assert _value(effect, "brightness") == 0.0

    def test_the_lens_blur_keeps_its_brightness(self) -> None:
        # 磨りガラス矩形は 光の強さ 32 を渡す 明るさの倍率へ入れると板が暗く沈む
        (effect,) = _requested('obj.effect("レンズブラー", "範囲", 16, "光の強さ", 32)')
        assert effect.kind == "lens_blur"
        assert _value(effect, "radius") == 16.0
        assert _value(effect, "brightness") == 100.0
        # 写せない値は、描くときに記録へ残る
        report = CompatibilityReport()
        script_filter_effects("レンズブラー", {"範囲": 16.0, "光の強さ": 32.0}, report=report)
        assert any("レンズブラーの項目: 光の強さ" in line for line in report.lines())

    def test_a_filter_the_import_knows_is_called(self) -> None:
        # 読み込みで写せる効果は、スクリプトからも同じ名前で呼べる（表を 1 つにした）
        # 表が分かれると、読み込みでは写せる効果が obj.effect では未対応として記録されて
        # 掛からないか、別の効果に化ける（以前の表は 領域拡張 を切り抜きの crop へ写していた）
        (effect,) = _requested('obj.effect("領域拡張", "上", 10)')
        assert effect.kind == "expand_area"
        assert _value(effect, "top") == 10.0


class TestPlacement:
    def test_obj_x_is_where_the_clip_is_placed(self, tmp_path: Path) -> None:
        """``obj.x`` ``obj.y`` はクリップの配置の位置（Y は下が正）

        0 のままだと、画面を写して自分の位置の所を切り出すアクリル矩形が、
        どこへ動かしても画面の真ん中の絵を映す
        """
        catalog = ScriptCatalog(roots=(tmp_path,))
        entry = catalog.add_text("aviutl:試験/位置.anm:0", "obj.ox = obj.x obj.oy = obj.y")
        clip = with_fixed_items(Clip(timeline_start=0, duration=30), picture=True)
        placed = tuple(
            effect.with_param("pos_x", AnimatedValue(100.0)).with_param(
                "pos_y", AnimatedValue(40.0)
            )
            if effect.kind == "transform"
            else effect
            for effect in clip.effects
        )
        stage = ScriptStage(catalog, screen=(640, 360))
        calls = stage.run(
            clip,
            (Effect(kind=entry.identifier, params={}),),
            blank_image(4, 4),
            frame=0,
            fps=30.0,
        )
        assert calls[0].x == 0.0
        clip = Clip(timeline_start=0, duration=30, effects=placed)
        (call,) = stage.run(
            clip,
            (Effect(kind=entry.identifier, params={}),),
            blank_image(4, 4),
            frame=0,
            fps=30.0,
        )
        assert isinstance(call, DrawCall)
        assert (call.x, call.y) == (100.0, -40.0)
