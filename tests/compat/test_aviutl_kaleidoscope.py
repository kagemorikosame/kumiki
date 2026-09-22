"""AviUtl の 万華鏡 と、オブジェクト分割 + 個別オブジェクト の拡大・回転を写す

項目の名前は AviUtl2（v2.1.6a）に効果を積んだエイリアスを作らせて読み取った
（``tests/fixtures/aviutl/probes/kumiki_p5_*`` と ``kumiki_p6_*`` の ``k_`` と ``s_``）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sashimono.compat.aviutl.exo import load_exo, parse_exo
from sashimono.compat.aviutl.mapping import map_object
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.core.model import AnimatedValue, Effect
from sashimono.core.timebase import FrameRate

RATE = FrameRate(60)
PROBES = Path(__file__).resolve().parents[1] / "fixtures" / "aviutl" / "probes"


def _object(*filters: str) -> str:
    """テキスト 1 つに ``filters`` を積んだ本文 各要素は ``名前\n項目=値`` の形"""
    blocks = ["[Object]", "frame=0,59", "[Object.0]", "effect.name=テキスト", "テキスト=田"]
    for index, block in enumerate(filters, start=1):
        name, _, body = block.partition("\n")
        blocks.append(f"[Object.{index}]")
        blocks.append(f"effect.name={name}")
        blocks.extend(line for line in body.splitlines() if line)
    blocks += [f"[Object.{len(filters) + 1}]", "effect.name=標準描画", "X=0.00"]
    return "\n".join(blocks) + "\n"


def _effects(*filters: str) -> tuple[list[Effect], CompatibilityReport]:
    report = CompatibilityReport()
    item = map_object(parse_exo(_object(*filters)).objects[0], RATE, report=report)
    assert item is not None
    return list(item.clip.effects), report


def _value(effect: Effect, name: str) -> float:
    value = effect.params[name]
    assert isinstance(value, AnimatedValue)
    return value.static


KALEIDOSCOPE = (
    "万華鏡\n中心X=10.0\n中心Y=20.0\n長さ=150.0\n回転=0.0\n角数(偶数)=4\n繰り返し回数=3\n"
    "固定サイズ=200.0\n円形マスク=1\n回転同期=1\n領域外を透過=1\n表示位置確認=0"
)


class TestKaleidoscope:
    def test_every_item_is_carried(self) -> None:
        # 写し先が無いと 万華鏡 ごと落ちて、元の文字がそのまま出る（差 12〜20）
        (effect,), report = _effects(KALEIDOSCOPE)
        assert effect.kind == "kaleidoscope"
        assert _value(effect, "span") == 150.0
        assert _value(effect, "corners") == 4.0
        assert _value(effect, "repeats") == 3.0
        assert _value(effect, "fixed_size") == 200.0
        assert effect.params["circle_mask"] is True
        assert effect.params["spin_pattern"] is True
        assert effect.params["clip_outside"] is True
        assert not report.lines()

    def test_the_centre_y_is_turned_up(self) -> None:
        # AviUtl の Y は下が正 こちらは上が正なので、そのままだと上下にずれた所を読む
        (effect,), _ = _effects(KALEIDOSCOPE)
        assert _value(effect, "center_x") == 10.0
        assert _value(effect, "center_y") == -20.0


class TestSplitPieces:
    def test_the_split_alone_draws_nothing_new(self) -> None:
        # 分けるだけでは絵は変わらない（実測で元と同じ広がり） 記録にも出さない
        effects, report = _effects("オブジェクト分割\n横分割数=3\n縦分割数=3")
        assert effects == []
        assert not report.lines()

    def test_other_split_items_are_recorded(self) -> None:
        # 横と縦の数のほかに使われている項目は記録へ出す 黙って捨てると、
        # 写せたつもりのまま違う絵が出たことに気付けない 0 の項目は使っていないので出さない
        _, report = _effects("オブジェクト分割\n横分割数=3\n縦分割数=3\n間隔=12\n遅延=0")
        lines = report.lines()
        assert any("オブジェクト分割の項目: 間隔" in line for line in lines)
        assert not any("遅延" in line or "分割数" in line for line in lines)

    def test_the_grid_goes_to_the_piece_effect(self) -> None:
        # 分け方を渡し損ねると 1 マスのままになり、拡大しても何も動かない
        (effect,), report = _effects(
            "オブジェクト分割\n横分割数=3\n縦分割数=2",
            "座標の拡大縮小(個別オブジェクト)\n拡大率=50.0\n中心X=100.0\n中心Y=40.0",
        )
        assert effect.kind == "split_pieces"
        assert _value(effect, "columns") == 3.0
        assert _value(effect, "rows") == 2.0
        assert _value(effect, "scale") == 50.0
        assert _value(effect, "center_x") == 100.0
        assert _value(effect, "center_y") == -40.0
        assert not report.lines()

    def test_the_angle_is_carried(self) -> None:
        # 角度を写し損ねると、分割片の位置が回らず元の並びのまま出る
        (effect,), _ = _effects(
            "オブジェクト分割\n横分割数=2\n縦分割数=1",
            "座標の回転(個別オブジェクト)\n角度=90.0\n中心X=0.0\n中心Y=0.0",
        )
        assert _value(effect, "angle") == 90.0
        assert _value(effect, "scale") == 100.0

    def test_without_a_split_there_is_one_piece(self) -> None:
        # AviUtl2 でも分割なしの 個別の拡大 50 は元の絵のままだった
        (effect,), _ = _effects("座標の拡大縮小(個別オブジェクト)\n拡大率=50.0")
        assert _value(effect, "columns") == 1.0
        assert _value(effect, "rows") == 1.0

    def test_a_zoom_then_a_turn_become_one(self) -> None:
        """続けて積んだ拡大と回転は 1 つにまとまる

        別々に並べると、2 つ目が拡大の後の位置ではなく元の升目を基準に動かす
        """
        effects, _ = _effects(
            "オブジェクト分割\n横分割数=3\n縦分割数=3",
            "座標の拡大縮小(個別オブジェクト)\n拡大率=50.0\n中心X=0.0\n中心Y=0.0",
            "座標の回転(個別オブジェクト)\n角度=30.0\n中心X=0.0\n中心Y=0.0",
        )
        assert [effect.kind for effect in effects] == ["split_pieces"]
        assert _value(effects[0], "scale") == 50.0
        assert _value(effects[0], "angle") == 30.0

    def test_different_centres_become_one_with_a_shift(self) -> None:
        """軸の違う拡大と回転も 1 つにまとまり、残りの平行移動は ずらし へ入る

        別々に並べると、2 つ目が拡大の後ではなく元の升目を読み直し、断片が欠ける
        中心X 100 で 50% にすると c → 0.5c + (50, 0)、続けて時計回りに 90 度回すと
        c → 0.5·R(90)c + (0, -50)（Y は上が正）
        """
        effects, report = _effects(
            "オブジェクト分割\n横分割数=3\n縦分割数=3",
            "座標の拡大縮小(個別オブジェクト)\n拡大率=50.0\n中心X=100.0\n中心Y=0.0",
            "座標の回転(個別オブジェクト)\n角度=90.0\n中心X=0.0\n中心Y=0.0",
        )
        assert [effect.kind for effect in effects] == ["split_pieces"]
        assert _value(effects[0], "scale") == pytest.approx(50.0)
        assert _value(effects[0], "angle") == pytest.approx(90.0)
        assert _value(effects[0], "offset_x") == pytest.approx(0.0, abs=1e-9)
        assert _value(effects[0], "offset_y") == pytest.approx(-50.0)
        assert not report.lines()

    def test_two_zooms_about_different_centres(self) -> None:
        # 200%（中心X 100）の後に 50%（中心 0）なら大きさは元に戻り、左へ 50 ずれるだけ
        # 別々に並べると 2 つ目が元の升目を縮めてしまい、断片が中心へ寄る
        (effect,), _ = _effects(
            "オブジェクト分割\n横分割数=3\n縦分割数=1",
            "座標の拡大縮小(個別オブジェクト)\n拡大率=200.0\n中心X=100.0\n中心Y=0.0",
            "座標の拡大縮小(個別オブジェクト)\n拡大率=50.0\n中心X=0.0\n中心Y=0.0",
        )
        assert _value(effect, "scale") == pytest.approx(100.0)
        assert _value(effect, "offset_x") == pytest.approx(-50.0)

    def test_moving_values_that_cannot_merge_are_recorded(self) -> None:
        # 動く値で軸が違うと 1 つの式に畳めない 並べて描くしかないので、記録に残す
        effects, report = _effects(
            "オブジェクト分割\n横分割数=3\n縦分割数=3",
            "座標の拡大縮小(個別オブジェクト)\n拡大率=50,100,直線移動,0\n中心X=100.0\n中心Y=0.0",
            "座標の回転(個別オブジェクト)\n角度=30.0\n中心X=0.0\n中心Y=0.0",
        )
        assert len(effects) == 2
        assert any("動く値で続けて積んだ" in line for line in report.lines())


def _real_probes() -> list[Path]:
    # 見本は旧名の頭で保存してある 番号を 1 つずつ書くのは、改名の道具が
    # ``kumiki_p5_`` の形だけを旧名のまま残すため（``p[56]`` と書くと書き換えられた）
    names = (
        "kumiki_p5_k_*.object",
        "kumiki_p6_k_*.object",
        "kumiki_p5_s_*.object",
        "kumiki_p6_s_*.object",
    )
    return sorted(path for name in names for path in PROBES.glob(name))


@pytest.mark.skipif(not _real_probes(), reason="AviUtl2 に作らせた見本が手元に無い")
@pytest.mark.parametrize("path", _real_probes(), ids=lambda path: path.stem)
def test_the_real_probes_map_without_gaps(path: Path) -> None:
    """AviUtl2 に作らせた見本が、落とす項目なしで写る

    推測で書いた項目名は実物と合わない 実物のエイリアスで読んで、
    記録に 万華鏡 や 個別オブジェクト が出ないことを確かめる
    """
    report = CompatibilityReport()
    for obj in load_exo(path).objects:
        map_object(obj, RATE, report=report)
    lines = [
        line
        for line in report.lines()
        if any(word in line for word in ("万華鏡", "個別オブジェクト", "オブジェクト分割"))
    ]
    assert lines == []
