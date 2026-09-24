"""配布されている AviUtl1 世代のエイリアス（``.exa``）を全部通す

置き場は ``tests/fixtures/aviutl/aviutl1`` 中身は次の 4 つの配布物をそのまま置いたもので、
リポジトリには入れていない（作り方は ``tests/fixtures/aviutl/README.md``）

- sigma-axis/sigma_aviutl_scripts（MIT） ``.exa`` 38 本と、それが呼ぶスクリプト
- sigma-axis/AviUtl-Alias-FPS-Counter（Unlicense） 2 本
- sigma-axis/aviutl_localfont2（MIT） 2 本
- oov/aviutl_psdtoolkit（MIT） 25 本 日本語版と英語版（``*_en.exa``）の組

無ければ飛ぶ ここが落ちたときに疑うのは読み手であって、エイリアスではない
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from sashimono.compat.aviutl import catalog as catalog_module
from sashimono.compat.aviutl.catalog import ScriptCatalog, set_script_catalog
from sashimono.compat.aviutl.custom_object import (
    custom_object_clip,
    custom_object_script,
    is_custom_object_kind,
    script_label,
)
from sashimono.compat.aviutl.exo import load_exo
from sashimono.compat.aviutl.mapping import map_object
from sashimono.compat.aviutl.objapi import ObjectState
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.aviutl.runtime import LuaScriptRuntime
from sashimono.compat.mapped import MappedObject
from sashimono.core.timebase import FrameRate
from sashimono.engine.render.scripts import _apply_params

ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "aviutl" / "aviutl1"
FILES = sorted(ROOT.rglob("*.exa")) if ROOT.is_dir() else []
SIGMA = ROOT / "sigma_aviutl_scripts"
PSDTOOLKIT = ROOT / "aviutl_psdtoolkit"
LOCALFONT = ROOT / "aviutl_localfont2"

#: 配布物ごとのアニメーション効果の数（手元の 67 本を数えた）
#: 4 つのうち一部だけを置いた機械でも、置いた分だけで数を合わせる
EFFECTS_IN = {SIGMA: 30, PSDTOOLKIT: 6}


def _require(folder: Path) -> Path:
    """その配布物が置かれていなければ飛ばす

    一部だけを置いた機械で、置いていない配布物の数を当てにして落ちると、
    読み手の不具合と置き場の不足の見分けが付かない
    """
    if not folder.is_dir():
        pytest.skip(f"{folder.name} が手元に無い")
    return folder


pytestmark = pytest.mark.skipif(not FILES, reason="AviUtl1 の配布エイリアスが手元に無い")

#: ffi（LuaJIT の C 呼び出し）が無いと動かないと、スクリプト自身が書いているもの
#: ``sigma_dither.lua`` は ffi が無いと読み込みで止まる こちらは C を呼ばせないので動かない
NEEDS_FFI = frozenset({"ディザα階調", "ディザフェード", "ディザマスク", "ディザ減色"})


@pytest.fixture(scope="module")
def scripts() -> Iterator[ScriptCatalog]:
    """配布物に入っていたスクリプトだけの一覧 終わったら元の一覧へ戻す"""
    saved = catalog_module._catalog
    created = ScriptCatalog(roots=(ROOT,))
    created.scan()
    set_script_catalog(created)
    yield created
    catalog_module._catalog = saved
    if saved is not None:
        saved.register_all()


@pytest.fixture(scope="module")
def mapped(scripts: ScriptCatalog) -> tuple[list[tuple[Path, MappedObject]], CompatibilityReport]:
    del scripts
    report = CompatibilityReport()
    results: list[tuple[Path, MappedObject]] = []
    for path in FILES:
        for obj in load_exo(path).objects:
            item = map_object(obj, FrameRate(30), report=report)
            if item is not None:
                results.append((path, item))
    return results, report


def test_every_file_gives_one_object() -> None:
    # 以前は開けても 0 個だった ``[vo.0]`` を全体設定として読んでいたため
    empty = [path.name for path in FILES if len(load_exo(path).objects) != 1]
    assert not empty, f"オブジェクトが 1 つにならない: {empty}"
    assert all(load_exo(path).generation == 1 for path in FILES)


def test_every_object_maps(
    mapped: tuple[list[tuple[Path, MappedObject]], CompatibilityReport],
) -> None:
    written = {path for path, _ in mapped[0]}
    assert [path.name for path in FILES if path not in written] == []


def test_every_animation_effect_finds_its_script(
    mapped: tuple[list[tuple[Path, MappedObject]], CompatibilityReport],
) -> None:
    """アニメーション効果は 1 本残らずスクリプトに繋がる

    ``name=内側シャドー@効果集σ`` を表示名とそのまま比べていた頃は 0 本だった
    """
    items, report = mapped
    lost = [line for line in report.missing if line.startswith("アニメーション効果")]
    assert lost == []
    # 4 つそろえば 36 個（日本語版 34・英語版 2） 何も読めずに記録も空、という形で
    # 通らないよう、繋がった数そのものを見る
    # カスタムオブジェクト（中身を作るスクリプト）は数えない 数は下の試験で見る
    connected = [
        e
        for _, item in items
        for e in item.clip.effects
        if e.kind.startswith("aviutl:") and not is_custom_object_kind(e.kind)
    ]
    assert len(connected) == sum(n for folder, n in EFFECTS_IN.items() if folder.is_dir())


def test_every_custom_object_is_placed_like_the_add_menu(
    mapped: tuple[list[tuple[Path, MappedObject]], CompatibilityReport],
) -> None:
    """カスタムオブジェクトは右クリックの〔追加〕と同じ形（空のテキストとスクリプト）になる（#147）

    以前は「カスタムオブジェクト: 矩形@単純図形σ」と数えるだけで、中身の無いクリップを置いていた
    置いても何も映らず、右クリックから置いた物とも見分けが付かなかった
    """
    items, report = mapped
    assert not [line for line in report.missing if line.startswith("カスタムオブジェクト")]
    placed = sorted(
        script_label(script.kind)
        for _, item in items
        if (script := custom_object_script(item.clip)) is not None
    )
    expected: list[str] = []
    if SIGMA.is_dir():
        expected += ["アクリル矩形", "楕円", "矩形", "磨りガラス矩形", "菱形"]
    if PSDTOOLKIT.is_dir():
        # 日本語版と英語版の組 口パク準備 は 2 組
        expected += ["口パク準備"] * 4 + ["多目的スライダー"] * 2
    assert placed == sorted(expected)


def test_the_file_heads_are_not_listed(scripts: ScriptCatalog) -> None:
    """``@`` で束ねたファイルの頭（使用許諾の注釈）が一覧に項目として出ない（#170）

    AviUtl は ``@名前`` の行から次の ``@名前`` までを 1 本とし、最初の ``@`` より前は
    どのスクリプトにも属さない 配布物 8 本のうち 6 本は頭に注釈を置いていて、
    どれも注釈だけ（コードは無い） 以前はこれがファイル名（``@単純図形σ``）の
    項目として並び、置いても何も描かなかった
    """
    _require(SIGMA)
    listed = [entry.label for entry in scripts.all()]
    assert not [label for label in listed if label.startswith("@")], listed
    objects = sorted(entry.label for entry in scripts.of_kind("obj") if "sigma" in str(entry.path))
    assert objects == sorted(["矩形", "楕円", "菱形", "アクリル矩形", "磨りガラス矩形"])


def test_the_simple_shapes_draw_their_size(scripts: ScriptCatalog, qt_application: object) -> None:
    """単純図形σ の 矩形 と 楕円 が 100 x 100 に描かれる（#147）

    どちらも 1 画素か 400 画素の図形を読み、リサイズで 100 x 100 にする リサイズを位置の
    ずれとして読んでいた頃は 矩形 が 1 画素、楕円 は太さ 8000 の輪郭で透明になり、
    どちらも何も見えなかった
    """
    del qt_application
    from sashimono.core.commands import AddClip, AddTrack
    from sashimono.core.commands.fixed import with_fixed_items
    from sashimono.core.model import Project, ProjectSettings, Track, TrackKind
    from sashimono.engine.gpu import GLContextError, OffscreenGLContext
    from sashimono.engine.render import FrameRenderer

    _require(SIGMA)
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    settings = ProjectSettings(width=640, height=360, frame_rate=FrameRate(30), sample_rate=48000)
    try:
        for label, least in (("矩形", 100 * 100), ("楕円", int(0.75 * 100 * 100))):
            (entry,) = [e for e in scripts.of_kind("obj") if e.label == label]
            clip = custom_object_clip(entry.definition().create(), duration=30)
            clip = with_fixed_items(clip, picture=True, sound=False)
            track = Track(kind=TrackKind.VIDEO, name="V1")
            project = AddTrack(track).apply(Project.create(settings))
            project = AddClip(track.id, clip).apply(project)
            renderer = FrameRenderer(project, context=context)
            try:
                lit = renderer.render(0)[:, :, :3].max(axis=2) > 128
            finally:
                renderer.close()
            rows, columns = np.nonzero(lit)
            assert int(lit.sum()) >= least, label
            # 真ん中に 100 x 100 の中に収まる
            assert rows.max() - rows.min() + 1 <= 100, label
            assert columns.max() - columns.min() + 1 <= 100, label
    finally:
        context.release()


def test_what_is_left_is_only_the_scripted_contents(
    mapped: tuple[list[tuple[Path, MappedObject]], CompatibilityReport],
) -> None:
    """残る穴はスクリプトが中身を作る物と スクリプト制御 だけ

    新しい穴がここへ出たら、回数を数えて埋める順を決め直す
    """
    _, report = mapped
    kinds = {line.split(":", 1)[0] for line in report.missing}
    # どの穴が出るかは置いた配布物で決まる シーンチェンジは sigma、スクリプト制御は
    # localfont2 にある カスタムオブジェクトはスクリプトが揃っていれば穴にならない（#147）
    expected = set()
    if SIGMA.is_dir():
        expected.add("シーンチェンジ")
    if LOCALFONT.is_dir():
        expected.add("フィルタ")
    assert kinds == expected, report.missing
    assert {line for line in report.missing if line.startswith("フィルタ")} <= {
        "フィルタ: スクリプト制御"
    }


def test_no_alias_has_a_middle_point() -> None:
    """この 67 本に中間点を持つものは無い

    AviUtl1 は中間点で区切った区間を別のオブジェクトとして書くので、1 つだけを
    持つエイリアスには出てこない 中間点を持つ実物が手に入ったら、ここが落ちて
    動きの試験を足す番だと分かる
    """
    assert all(len(load_exo(path).objects[0].points) == 2 for path in FILES)


def test_the_english_twins_read_the_same(
    mapped: tuple[list[tuple[Path, MappedObject]], CompatibilityReport],
) -> None:
    """PSDToolKit の日本語版と英語版は、名前を寄せれば同じ中身になる

    違ってよいのはフォントと、作者が変えた 透明度 だけ（行ごとに突き合わせて確かめた）
    """
    twins = sorted(_require(PSDTOOLKIT).rglob("*_en.exa"))
    assert twins
    for english in twins:
        japanese = english.with_name(english.name.replace("_en.exa", ".exa"))
        left = load_exo(japanese).objects[0].entries
        right = load_exo(english).objects[0].entries
        assert [entry.name for entry in left] == [entry.name for entry in right], english.name
        for one, other in zip(left, right, strict=True):
            assert set(one.params) == set(other.params), english.name
            differing = {key for key in one.params if one.params[key] != other.params[key]}
            assert differing <= {"font", "透明度"}, (english.name, differing)


def test_the_slider_stays_still(
    mapped: tuple[list[tuple[Path, MappedObject]], CompatibilityReport],
) -> None:
    """多目的スライダーの ``0.00,0.00,3`` は動かない値として読む

    番号を値として読むと、0 から 3 へ動くうえ中間点と数が合わないと記録されていた
    """
    items, report = mapped
    assert not [line for line in report.missing if "移動方法" in line or "中間点" in line]
    _require(PSDTOOLKIT)
    sliders = [item for path, item in items if path.stem.startswith("MultiPurposeSlider")]
    assert len(sliders) == 2


def test_the_sigma_effects_run(scripts: ScriptCatalog) -> None:
    """sigma の効果が Lua で最後まで走る（ffi を要るディザだけは除く）

    ``package`` が無い・``TRACK,_0=nil`` の欄に 0 が入る、のどちらか 1 つでも
    残っていると、26 本がどれも頭の数行で落ちる
    """
    report = CompatibilityReport()
    runtime = LuaScriptRuntime(report=report)
    runtime.set_roots(scripts.roots)
    ran = 0
    for path in sorted((_require(SIGMA) / "exa" / "anm").glob("*.exa")):
        item = map_object(load_exo(path).objects[0], FrameRate(30), report=CompatibilityReport())
        assert item is not None
        for effect in item.clip.effects:
            entry = scripts.get(effect.kind)
            assert entry is not None, path.name
            if entry.label in NEEDS_FFI:
                continue
            image = np.zeros((64, 64, 4), np.uint8)
            image[16:48, 16:48] = 255
            state = ObjectState(image=image, screen_w=1920, screen_h=1080, totalframe=60)
            _apply_params(state, effect, 0)
            result = runtime.run(
                entry.source, state, header=entry.header, script=entry.label, folder=entry.folder
            )
            assert not result.failed, (path.name, result.message)
            ran += 1
    assert ran == 26
