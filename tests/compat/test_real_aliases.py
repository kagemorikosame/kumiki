"""このマシンに実際に置かれている配布エイリアスを、全部読ませてみる。

``%PROGRAMDATA%\\aviutl2\\Alias`` にある実物が対象。合成した見本ではなく、
人が配ったものをそのまま通すのが目的なので、無ければ黙って飛ばす。

ここが落ちたときに疑うのは対応表であって、エイリアスではない。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from novaedit.compat.aviutl.exo import load_exo
from novaedit.compat.aviutl.mapping import map_object
from novaedit.compat.aviutl.report import CompatibilityReport
from novaedit.compat.mapped import MappedObject
from novaedit.core.timebase import FrameRate


def alias_root() -> Path | None:
    program_data = os.environ.get("PROGRAMDATA")
    if not program_data:
        return None
    root = Path(program_data) / "aviutl2" / "Alias"
    return root if root.is_dir() else None


ROOT = alias_root()
FILES = sorted(ROOT.rglob("*.object")) if ROOT is not None else []

pytestmark = pytest.mark.skipif(not FILES, reason="このマシンに AviUtl2 の配布エイリアスが無い")


@pytest.fixture(scope="module")
def mapped() -> list[tuple[Path, MappedObject]]:
    rate = FrameRate(30)
    report = CompatibilityReport()
    results: list[tuple[Path, MappedObject]] = []
    for path in FILES:
        for obj in load_exo(path).objects:
            item = map_object(obj, rate, report=report)
            if item is not None:
                results.append((path, item))
    return results


def test_every_file_maps_to_at_least_one_clip(mapped: list[tuple[Path, MappedObject]]) -> None:
    written = {path for path, _ in mapped}
    missing = [path.name for path in FILES if path not in written]
    assert not missing, f"写せなかった: {missing}"


def test_they_are_all_read_as_the_second_generation() -> None:
    # ``.object`` は AviUtl2 世代。1 本でも 1 世代として読まれていたら、
    # テキスト欄を 16 進として復号しようとして中身が消える。
    assert all(load_exo(path).generation == 2 for path in FILES)


def test_the_text_survives(mapped: list[tuple[Path, MappedObject]]) -> None:
    # 空文字になっていたら、テキスト欄の読み方が世代と合っていない。
    texts = [
        item.clip.source.params.get("text")
        for _, item in mapped
        if item.clip.source is not None and item.clip.source.kind == "text"
    ]
    assert texts
    assert all(isinstance(text, str) and text for text in texts)


def test_the_fonts_survive(mapped: list[tuple[Path, MappedObject]]) -> None:
    # 配布物はフォント指定が肝。既定フォントに落ちていたら見た目が別物になる。
    named = [
        item
        for _, item in mapped
        if item.clip.source is not None and "font" in item.clip.source.params
    ]
    assert len(named) >= len(FILES) - 3  # 数本はフォント欄が空のものがある


def test_no_alias_collapses_to_a_single_frame(mapped: list[tuple[Path, MappedObject]]) -> None:
    # 長さを持つと書いてあるのに 1 フレームなら、``frame=`` の読み方が違う。
    with_span = [item for _, item in mapped if item.has_span]
    assert with_span
    assert all(item.clip.duration > 1 for item in with_span)


def test_the_decorated_ones_actually_get_a_decoration(
    mapped: list[tuple[Path, MappedObject]],
) -> None:
    """``文字装飾`` が付いているものは、縁か影のどちらかを持つ。"""
    decorated = 0
    for path, item in mapped:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        if "文字装飾=標準文字" in raw or "文字装飾=" not in raw:
            continue
        source = item.clip.source
        assert source is not None
        assert "border_width" in source.params or "shadow_x" in source.params, path.name
        decorated += 1
    assert decorated > 0
