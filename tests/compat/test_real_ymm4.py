"""実際に配布されている ``.ymmt`` を、全部読ませてみる

``tests/fixtures/ymm4`` に ``.ymmt`` を置くと、ここが拾って通す 配布物なので
リポジトリには入れていない（無ければ黙って飛ばす）

開発中は、ネットで配布されている 2 本を置いて確かめた

* ``AviUtlアニメーション効果.ymmt`` — 17 本（うづき）
* ``YMM4Teテンプレート一式.ymmt`` — 106 本（てとら）

ここが落ちたときに疑うのは対応表であって、テンプレートではない
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.compat.catalog import TemplateCatalog, TemplateEntry, place, restyle
from kumiki.compat.mapped import MappedObject
from kumiki.core.model import AnimatedValue, Clip, GeneratedSource, Project

#: 棚に並んだテンプレートと、それを読んだ結果
type Loaded = list[tuple[TemplateEntry, list[MappedObject]]]

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "ymm4"
FILES = sorted(ROOT.glob("*.ymmt")) if ROOT.is_dir() else []

pytestmark = pytest.mark.skipif(not FILES, reason="tests/fixtures/ymm4 に .ymmt が置かれていない")


@pytest.fixture(scope="module")
def catalog() -> TemplateCatalog:
    shelf = TemplateCatalog()
    shelf.scan((ROOT,))
    return shelf


@pytest.fixture(scope="module")
def loaded(catalog: TemplateCatalog) -> Loaded:
    report = CompatibilityReport()
    return [(entry, entry.load(report=report)) for entry in catalog.all()]


def test_every_file_yields_templates(catalog: TemplateCatalog) -> None:
    # 1 ファイルに何本も入っている 0 本なら包み方を読み違えている
    assert len(catalog.all()) >= len(FILES)


def test_most_templates_produce_something(loaded: Loaded) -> None:
    # 中身もエフェクトも出ないものは、要求しているエフェクトがこちらに
    # 無いものだけのはず 全体の 2 割を超えたら読み落としを疑う
    empty = [entry.name for entry, objects in loaded if not objects]
    assert len(empty) < len(loaded) * 0.2, f"空になった: {empty[:10]}"


def test_the_text_survives(loaded: Loaded) -> None:
    # タイマーの図形も文字として描くが、文字は時刻から作るので ``text`` を持たない
    texts = [
        item.clip.source.params.get("text")
        for _, objects in loaded
        for item in objects
        if item.clip.source is not None
        and item.clip.source.kind == "text"
        and not item.clip.source.params.get("timer_format")
    ]
    assert texts
    assert all(isinstance(text, str) and text for text in texts)


def test_the_fonts_survive(loaded: Loaded) -> None:
    # 配布物はフォント指定が肝 既定フォントに落ちていたら見た目が別物になる
    named = [
        item
        for _, objects in loaded
        for item in objects
        if item.clip.source is not None and "font" in item.clip.source.params
    ]
    assert named


def test_the_outlines_survive(loaded: Loaded) -> None:
    # 縁取りは ``OutlineEffect`` に入っている ``Decorations`` だけを見ていると
    # 1 つも出ない
    bordered = [
        item
        for _, objects in loaded
        for item in objects
        if item.clip.source is not None and "border_width" in item.clip.source.params
    ]
    assert bordered


def test_the_animations_span_the_whole_item(loaded: Loaded) -> None:
    """動くパラメータが、2 フレームで終わっていないこと

    ``Values`` の並び順をフレーム番号だと思って読むと、300 フレームかけて動く
    はずのものが 2 フレームで終わる 見た目には「動いていない」になる
    """
    spans: list[int] = []
    for _, objects in loaded:
        for item in objects:
            for effect in item.clip.effects:
                for value in effect.params.values():
                    if isinstance(value, AnimatedValue) and value.is_animated:
                        spans.append(value.keyframes[-1].frame - value.keyframes[0].frame)
    assert spans, "動くパラメータが 1 つも無い"
    assert max(spans) > 10, f"いちばん長い動きでも {max(spans)} フレームしかない"


def test_effect_only_templates_can_be_worn(loaded: Loaded) -> None:
    """中身のないテンプレート（アニメーション効果）は、今のクリップに着せられる"""
    effects_only = [
        objects
        for _, objects in loaded
        if objects and not any(item.has_picture for item in objects)
    ]
    assert effects_only, "エフェクトだけのテンプレートが 1 つも無い"

    clip = Clip(
        timeline_start=0,
        duration=60,
        source=GeneratedSource(kind="text", params={"text": "字幕"}),
    )
    commands = restyle(effects_only[0], clip)
    assert commands
    # 中身には触らない エフェクトを足すだけ
    assert all(type(command).__name__ == "AddEffect" for command in commands)


def test_effect_only_templates_are_not_placed(loaded: Loaded) -> None:
    # 絵を持たないので、置いても何も映らない
    effects_only = [
        objects
        for _, objects in loaded
        if objects and not any(item.has_picture for item in objects)
    ]
    assert place(effects_only[0], Project.create()) == []


def test_no_video_effect_or_item_is_left_unmapped(catalog: TemplateCatalog) -> None:
    """実物に出てくる映像エフェクトとアイテムを、1 つも取りこぼさないこと

    フェーズ 4 で 33 種の映像エフェクトと ``FrameBufferItem`` を埋めた ここで
    記録が出たら、配布物に新しい種類が増えたか、写し方を壊した
    """
    report = CompatibilityReport()
    for entry in catalog.all():
        entry.load(report=report)
    leftovers = [
        line
        for line in report.lines()
        if "YMM4 の映像エフェクト" in line or "YMM4 のアイテム" in line
    ]
    assert leftovers == []


def test_every_template_renders(loaded: Loaded) -> None:
    """全テンプレートを置いて、途中のフレームを描いてみる

    写し方が合っていても、値の組み合わせでシェーダが落ちたり、例外で描画が
    止まったりすれば配布物は開けない 小さな画面で 1 枚ずつ描く
    """
    from kumiki.core.model import ProjectSettings
    from kumiki.core.timebase import FrameRate
    from kumiki.engine.gpu import GLContextError, OffscreenGLContext
    from kumiki.engine.render import FrameRenderer

    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    settings = ProjectSettings(width=320, height=180, frame_rate=FrameRate(30))
    drawn = 0
    try:
        for _, objects in loaded:
            project = Project.create(settings)
            for command in place(objects, project):
                project = command.apply(project)
            if project.duration == 0:
                continue
            renderer = FrameRenderer(project, context=context)
            try:
                image = renderer.render(project.duration // 2)
            finally:
                renderer.close()
            assert image.shape == (180, 320, 4)
            drawn += 1
    finally:
        context.release()
    assert drawn > 0
