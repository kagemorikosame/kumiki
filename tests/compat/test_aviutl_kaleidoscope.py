"""AviUtl の 万華鏡 と、オブジェクト分割 + 個別オブジェクト の拡大・回転を写す

項目の名前は AviUtl2（v2.1.6a）に効果を積んだエイリアスを作らせて読み取った
（``tests/fixtures/aviutl/probes/kumiki_p5_*`` と ``kumiki_p6_*`` の ``k_`` と ``s_``）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kumiki.compat.aviutl.exo import load_exo, parse_exo
from kumiki.compat.aviutl.mapping import map_object
from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.core.model import AnimatedValue, Effect
from kumiki.core.timebase import FrameRate

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

    def test_different_centres_stay_apart(self) -> None:
        # 軸が違うと入れ替えられないので、まとめずに順に掛ける
        effects, _ = _effects(
            "オブジェクト分割\n横分割数=3\n縦分割数=3",
            "座標の拡大縮小(個別オブジェクト)\n拡大率=50.0\n中心X=100.0\n中心Y=0.0",
            "座標の回転(個別オブジェクト)\n角度=30.0\n中心X=0.0\n中心Y=0.0",
        )
        assert len(effects) == 2


def _real_probes() -> list[Path]:
    names = ("kumiki_p[56]_k_*.object", "kumiki_p[56]_s_*.object")
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
