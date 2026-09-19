"""AviUtl の効果を、こちらのエフェクトへ写す

項目の名前と既定値は AviUtl2（v2.1.6a）に効果を積んだエイリアスを作らせて
読み取った ここに並ぶ名前はその実物から取ったもので、推測していない
"""

from __future__ import annotations

import pytest

from kumiki.compat.aviutl.exo import parse_exo
from kumiki.compat.aviutl.mapping import map_object
from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.core.model import AnimatedValue, Effect
from kumiki.core.timebase import FrameRate

RATE = FrameRate(60)


def _object(*filters: str) -> str:
    """図形 1 つに ``filters`` を積んだ本文 各要素は ``名前\n項目=値`` の形"""
    blocks = [
        "[Object]",
        "frame=0,59",
        "[Object.0]",
        "effect.name=テキスト",
        "テキスト=あ",
        "[Object.1]",
        "effect.name=標準描画",
        "X=0.00",
        "合成モード=通常",
    ]
    for index, block in enumerate(filters, start=2):
        name, _, body = block.partition("\n")
        blocks.append(f"[Object.{index}]")
        blocks.append(f"effect.name={name}")
        blocks.extend(line for line in body.splitlines() if line)
    return "\n".join(blocks) + "\n"


def _effects(*filters: str) -> tuple[list[Effect], CompatibilityReport]:
    report = CompatibilityReport()
    item = map_object(parse_exo(_object(*filters)).objects[0], RATE, report=report)
    assert item is not None
    # 標準描画は既定のままなので変形は作られない 積んだフィルタだけが並ぶ
    return list(item.clip.effects), report


def _one(*filters: str) -> Effect:
    effects, _ = _effects(*filters)
    assert len(effects) == 1, [effect.kind for effect in effects]
    return effects[0]


def _value(effect: Effect, name: str) -> float:
    value = effect.params[name]
    assert isinstance(value, AnimatedValue)
    return value.static


class TestTheTableIsKeyedByTheAviutlName:
    """``座標`` ``拡大率`` ``回転`` はどれも変形へ写すが、``X`` の意味が違う"""

    def test_the_coordinate_filter_moves(self) -> None:
        effect = _one("座標\nX=100\nY=50\nZ=0")
        assert effect.kind == "transform"
        assert _value(effect, "pos_x") == 100.0
        # AviUtl の Y は下が正 こちらは上が正
        assert _value(effect, "pos_y") == -50.0

    def test_the_zoom_filter_scales_both_axes(self) -> None:
        # 種別で引いていたころは、ここで X が位置として読まれていた
        effect = _one("拡大率\n拡大率=200\nX=0\nY=0\nZ=0")
        assert _value(effect, "scale") == 200.0
        assert _value(effect, "scale_y") == 200.0
        assert _value(effect, "pos_x") == 0.0

    def test_the_rotate_filter_turns(self) -> None:
        effect = _one("回転\nX=0\nY=0\nZ=45")
        assert _value(effect, "rotation") == 45.0


class TestValuesThatNeedFixing:
    def test_transparency_becomes_opacity(self) -> None:
        # AviUtl の透明度は 0 で不透明 そのまま入れると真っ透明になる
        effect = _one("透明度\n透明度=30")
        assert effect.kind == "opacity"
        assert _value(effect, "amount") == 70.0

    def test_the_shadow_offset_flips(self) -> None:
        effect = _one("ドロップシャドウ\nX=10\nY=20\n濃さ=80\n拡散=5\n影色=000000")
        assert effect.kind == "shadow"
        assert _value(effect, "offset_y") == -20.0
        assert _value(effect, "blur") == 5.0

    def test_a_shake_fills_both_axes(self) -> None:
        # 震えるは振幅を 1 つしか持たない 片方だけ入れると横にしか揺れない
        effect = _one("震える\n振幅=8\n角度=0\n間隔=2")
        assert effect.kind == "random_move"
        assert _value(effect, "range_x") == 8.0
        assert _value(effect, "range_y") == 8.0

    def test_a_quarter_turn_becomes_degrees(self) -> None:
        effect = _one("ローテーション\n90度回転=2")
        assert _value(effect, "rotation") == 180.0


class TestAppearance:
    """登場と退場 AviUtl はフェードとワイプだけ「イン」「アウト」を秒で持つ"""

    def test_fade_reads_both_ends(self) -> None:
        effect = _one("フェード\nイン=0.50\nアウト=0.50")
        assert effect.kind == "inout_fade"
        assert effect.params["effect_in"] is True
        assert effect.params["effect_out"] is True
        assert _value(effect, "effect_time") == 0.5

    def test_only_an_out_time_means_no_entrance(self) -> None:
        effect = _one("フェード\nイン=0\nアウト=1.0")
        assert effect.params["effect_in"] is False
        assert effect.params["effect_out"] is True
        assert _value(effect, "effect_time") == 1.0

    def test_two_different_times_are_recorded(self) -> None:
        # こちらは時間を 1 つしか持てない 黙って片方を捨てると気付けない
        effects, report = _effects("フェード\nイン=0.20\nアウト=1.00")
        assert _value(effects[0], "effect_time") == 1.0
        assert any("違う時間" in line for line in report.lines())

    def test_a_wipe_keeps_its_shape(self) -> None:
        effect = _one(
            "ワイプ\nイン=0.5\nアウト=0\nぼかし=2\nワイプの種類=ワイプ(円)\n反転(イン)=1\n反転(アウト)=0"
        )
        assert effect.kind == "inout_wipe"
        assert effect.params["pattern"] == "circle"
        assert effect.params["reverse_in"] is True
        assert _value(effect, "tolerance") == 2.0

    def test_an_unknown_wipe_shape_falls_back_to_fade(self) -> None:
        # 付属の 5 枚以外の絵は式で作れない 黙って別の形にすると見た目が変わる
        effects, report = _effects("ワイプ\nイン=0.5\nアウト=0\nワイプの種類=ワイプ(渦巻)")
        assert effects[0].params["pattern"] == "fade"
        assert any("ワイプの種類" in line for line in report.lines())

    @pytest.mark.parametrize(
        ("angle", "expected"),
        [(0.0, "right"), (90.0, "bottom"), (180.0, "left"), (270.0, "top")],
    )
    def test_the_angle_chooses_the_direction(self, angle: float, expected: str) -> None:
        effect = _one(f"画面外から登場\n時間=0.5\n角度={angle}\n数=1\nランダム方向=0")
        assert effect.kind == "inout_move"
        assert effect.params["direction"] == expected

    def test_an_angle_between_directions_is_recorded(self) -> None:
        effects, report = _effects("画面外から登場\n時間=0.5\n角度=45\n数=1\nランダム方向=0")
        assert effects[0].params["direction"] in ("right", "bottom")
        assert any("角度" in line for line in report.lines())

    def test_spreading_flattens_one_axis(self) -> None:
        effect = _one("広がって登場\n時間=0.3\n縦方向=0")
        assert effect.kind == "inout_zoom"
        # 隠れたときは横が 0（縦方向の旗が立つと軸が入れ替わる）
        assert _value(effect, "zoom_x") == 0.0
        assert _value(effect, "zoom_y") == 100.0

    def test_spreading_vertically_swaps_the_axes(self) -> None:
        effect = _one("広がって登場\n時間=0.3\n縦方向=1")
        assert _value(effect, "zoom_x") == 100.0
        assert _value(effect, "zoom_y") == 0.0

    def test_the_bounce_count_becomes_one_period(self) -> None:
        effect = _one("弾んで登場\n時間=2.0\n高さ=200\n回数=4")
        assert effect.kind == "inout_jump"
        assert _value(effect, "height") == 200.0
        assert _value(effect, "period") == 0.5

    def test_the_easing_flag_smooths_both_ends(self) -> None:
        effect = _one("拡大縮小して登場\n時間=0.3\n拡大率=300\n加減速=1")
        assert effect.kind == "inout_zoom"
        assert _value(effect, "zoom") == 300.0
        assert effect.params["easing"] == "sine"
        assert effect.params["easing_mode"] == "inout"

    def test_settings_that_cannot_be_copied_are_recorded(self) -> None:
        _, report = _effects("起き上がって登場\n時間=0.6\n勢い=2.0")
        assert any("勢い" in line for line in report.lines())


class TestShapesAndSwings:
    def test_the_fan_clip_uses_the_fan_shape(self) -> None:
        effect = _one("扇クリッピング\n中心X=0\n中心Y=0\n基準角=0\n範囲角=90\nぼかし=0")
        assert effect.kind == "shape_mask"
        assert effect.params["shape"] == "fan"
        assert _value(effect, "span") == 90.0

    def test_a_pendulum_swings_around_its_start(self) -> None:
        # 片側にだけ振れると、振り子ではなく回転になる
        effect = _one("振り子\n速さ=2.0\n角度=30\nずらし=0")
        assert effect.kind == "repeat_rotate"
        assert effect.params["centering"] is True
        assert _value(effect, "angle_z") == 30.0


class TestTheThingsReviewFound:
    def test_an_angle_near_zero_goes_right(self) -> None:
        # 角度は 1 周でつながっている まっすぐ引き算すると 359 度が上になる
        effect = _one("画面外から登場\n時間=0.5\n角度=359\n数=1\nランダム方向=0")
        assert effect.params["direction"] == "right"

    def test_a_negative_angle_also_goes_right(self) -> None:
        # 折り返しを見落とすと、-2 度が上（270 度に近い）として扱われる
        effect = _one("画面外から登場\n時間=0.5\n角度=-2\n数=1\nランダム方向=0")
        assert effect.params["direction"] == "right"

    def test_a_plain_number_reaches_a_value_spec(self) -> None:
        # スライダーを持たない数値を文字列のまま渡すと、既定値（8 個）へ落ちる
        effect = _one("円形配置\n円周=100\n半径=200\n数=3")
        assert effect.kind == "circular_duplicate"
        assert effect.params["count"] == 3

    def test_an_unreadable_value_keeps_the_target_default(self) -> None:
        # 写し先の既定値に変換を掛けると、不透明度 100 が 0 になって全透明になる
        effects, report = _effects("透明度\n透明度=おかしな値")
        assert _value(effects[0], "amount") == 100.0
        assert any("数として読めない値" in line for line in report.lines())

    def test_an_appearance_value_can_move(self) -> None:
        # 登場の値に動きが付いていたら、キーフレームとして残す
        effect = _one("拡大縮小して登場\n時間=0.3\n拡大率=100,300,直線移動,0\n加減速=0")
        value = effect.params["zoom"]
        assert isinstance(value, AnimatedValue)
        assert value.is_animated

    def test_a_dropped_setting_is_recorded(self) -> None:
        # 効果を写せても、項目を落とせば見た目は変わる（震えるの角度）
        _, report = _effects("震える\n振幅=8\n角度=45\n間隔=2")
        assert any("震えるの項目: 角度" in line for line in report.lines())

    def test_structural_keys_are_not_recorded(self) -> None:
        # Group は並びの区切りで、値ではない 記録に出すと本当の穴が埋もれる
        _, report = _effects("座標\nX=10\nY=0\nZ=0\nGroup=1")
        assert not any("Group" in line for line in report.lines())

    def test_an_empty_value_is_recorded(self) -> None:
        # 記録しないと、指定したつもりの値が既定値へ置き換わったことに気付けない
        _, report = _effects("透明度\n透明度=")
        assert any("数として読めない値" in line for line in report.lines())

    def test_an_unknown_choice_is_recorded(self) -> None:
        # 表に無い選択肢は既定値のままになる 写せたことにすると形が変わったまま残る
        _, report = _effects("マスク\n種類=三角\n中心X=0\n中心Y=0")
        assert any("マスクの項目: 種類" in line for line in report.lines())

    def test_a_still_expression_on_a_plain_number_is_recorded(self) -> None:
        # 値が同じでも、式なら時間で変わりうる
        _, report = _effects("円形配置\n円周=100\n半径=200\n数=3,3,瞬間移動,8|3+time")
        assert any("動く値を写せない項目" in line for line in report.lines())


class TestTheLeftoverSettings:
    """取りこぼしていた項目 記録の数え方を直したら、本当の穴だけが残った"""

    def test_the_gradient_keeps_its_blend(self) -> None:
        # 配布物 36 本で 8 回使われていた 通常のままだと見た目が別物になる
        effect = _one("グラデーション\n強さ=100\n合成モード=加算\n形状=線形\n開始色=ffffff")
        assert effect.kind == "gradient"
        assert effect.params["blend"] == "add"

    def test_a_single_colour_fill_keeps_its_strength(self) -> None:
        # 単色化は色と強さを持つ 強さを落とすと必ず真っ白（指定色）になる
        effect = _one("単色化\n強さ=40\n色=ff0000\n輝度を保持する=0")
        assert effect.kind == "fill"
        assert _value(effect, "amount") == 40.0
        assert effect.params["color"] == (1.0, 0.0, 0.0, 1.0)

    def test_the_flip_filter_reads_both_axes(self) -> None:
        effect = _one("反転\n上下反転=1\n左右反転=0\n輝度反転=0\n色相反転=0\n透明度反転=0")
        assert effect.kind == "flip"
        assert effect.params["vertical"] is True
        assert effect.params["horizontal"] is False

    def test_a_colour_inversion_is_recorded(self) -> None:
        # 上下左右の反転しか写せない 輝度や色相の反転は別の効果
        _, report = _effects("反転\n上下反転=0\n左右反転=0\n輝度反転=1\n色相反転=0\n透明度反転=0")
        assert any("反転の項目: 輝度反転" in line for line in report.lines())

    def test_the_mirror_filter_is_not_a_flip(self) -> None:
        # AviUtl の ミラー は鏡像を映す効果（透明度・減衰・境目調整・向きを持つ）
        # 反転として写すと、上下がひっくり返った別の絵になる
        effects, report = _effects("ミラー\n透明度=0\n減衰=50\nミラーの方向=下側")
        assert not effects
        assert any("フィルタ: ミラー" in line for line in report.lines())

    def test_the_luminance_key_mode(self) -> None:
        effect = _one("ルミナンスキー\n基準輝度=2048\n輝度範囲=512\nモード=明るい部分を透過")
        assert effect.kind == "luminance_key"
        assert effect.params["invert"] is True

    def test_settings_left_at_zero_are_not_recorded(self) -> None:
        # 0 や空は「使っていない」 記録に出すと、本当に埋めるべき穴が埋もれる
        # （縁取りのぼかしが 26 本とも 0 なのに、一番多い穴として並んでいた）
        _, report = _effects("縁取り\nサイズ=6\nぼかし=0\n縁色=ffffff\nパターン画像=")
        assert not any("縁取りの項目" in line for line in report.lines())

    def test_a_setting_in_use_is_still_recorded(self) -> None:
        _, report = _effects("縁取り\nサイズ=6\nぼかし=3\n縁色=ffffff")
        assert any("縁取りの項目: ぼかし" in line for line in report.lines())

    def test_a_zero_that_moves_is_still_in_use(self) -> None:
        # 0 から動く値を「使っていない」と数えると、動きを落としたことが記録から消える
        _, report = _effects("縁取り\nサイズ=6\nぼかし=0,10,直線移動,0\n縁色=ffffff")
        assert any("縁取りの項目: ぼかし" in line for line in report.lines())

    def test_a_zero_with_an_expression_is_still_in_use(self) -> None:
        # 参照式は 0 から始まっても時間で変わる
        _, report = _effects("縁取り\nサイズ=6\nぼかし=0,0,瞬間移動,8|time\n縁色=ffffff")
        assert any("縁取りの項目: ぼかし" in line for line in report.lines())
