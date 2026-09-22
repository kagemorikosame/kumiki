"""YMM4 の「合成する」グループを、配布物で確かめる

あおもや式テンプレート集（``tests/fixtures/ymm4/aomoya``）を置くと走る 配布物なので
リポジトリには入れていない（無ければ飛ばす）

YMM4 に描かせた絵と並べて読んだこと

* 合成するグループは、範囲の中身を 1 枚に重ねてから反転・拡大・縁取り・登場の動きを掛ける
  中身 1 つずつに配ると、リボンのテロップの縁取りが中身ごとに付き、吹き出しの差が倍になる
* グループの範囲の外の中身には何も掛からない 掛けると、リボンのテロップの文字に吹き出しの
  登場の動きが重なり、倍の距離を飛んでくる（差 25.3 → 2.6）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.compat.mapped import MappedObject
from kumiki.compat.ymm4.template import load_template, map_template

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "ymm4" / "aomoya"
RIBBON = ROOT / "フキダシ・テロップ_ピンクと水色リボンのテロップ.ymmt"
BUBBLE = ROOT / "SFっぽい吹き出し(右).ymmt"
PAINT = ROOT / "場面切り替え_ペイントトランジション.ymmt"


def _mapped(path: Path) -> list[MappedObject]:
    if not path.is_file():
        pytest.skip(f"{path.name} が置かれていない（配布物）")
    templates = load_template(path)
    return map_template(list(templates[0].items), report=CompatibilityReport())


def test_the_ribbon_text_is_not_moved_twice() -> None:
    """リボンのテロップの文字は、自分の登場の動きだけを持つ

    文字はグループの範囲（9 段）の外にある 範囲を見ずにグループのエフェクトを
    配ると、吹き出しの登場の動きと縁取りがもう 1 組ずつ文字に付く
    """
    mapped = _mapped(RIBBON)
    text = next(item for item in mapped if item.kind == "text")
    # 比べる相手は、同じ文字をグループ抜きで写した結果 同じ種類のエフェクトを
    # テンプレートが自分で 2 つ持っていても、それは文字自身のものなので数に入れない
    items = load_template(RIBBON)[0].items
    alone = next(item for item in items if item.get("$type", "").split(",")[0].endswith("TextItem"))
    own = map_template([alone], report=CompatibilityReport())[0]
    assert [e.kind for e in text.clip.effects] == [e.kind for e in own.clip.effects]


def test_the_ribbon_balloon_is_one_picture() -> None:
    # 吹き出しとリボンの図形 7 つは合成するグループの中 1 枚の絵に縁取りと登場の動きが掛かる
    mapped = _mapped(RIBBON)
    scenes = [item for item in mapped if item.kind == "scene"]
    assert len(scenes) == 1
    assert len(scenes[0].children) == 7
    assert "border" in [effect.kind for effect in scenes[0].clip.effects]


def test_the_sf_balloon_is_flipped_as_a_whole() -> None:
    # グループの反転は、まとめた絵ごと画面の中心で掛かる 中身の文字も絵の中にある
    mapped = _mapped(BUBBLE)
    assert [item.kind for item in mapped] == ["scene"]
    scene = mapped[0]
    assert "flip" in [effect.kind for effect in scene.clip.effects]
    assert "text" in [child.kind for child in scene.children]


def test_the_empty_slot_of_the_paint_transition_adds_no_scene() -> None:
    """ペイントトランジションの合成するグループは、次の場面を置くための空の枠

    中身が無いので、シーンを作らない 作ると空のシーンとトラックが増えるだけになる
    差 249 はこのグループではなく、前の場面の決め方から来ている（ここでは扱わない）
    """
    mapped = _mapped(PAINT)
    assert all(not item.children for item in mapped)
