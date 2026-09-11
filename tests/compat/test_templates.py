"""テンプレートの棚と、置く／着せるの 2 通り。

配布されている字幕エイリアスは見本の文字入りで配られる。そのまま置くだけだと
毎回打ち直すことになるので、**今の文字を残して見た目だけ入れ替える**道を
用意してある。ここではその境目を検査する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kumiki.compat.catalog import TemplateCatalog, place, restyle
from kumiki.core.commands import AddClip, AddEffect, AddTrack, RemoveEffect, SetSource
from kumiki.core.model import AnimatedValue, Clip, GeneratedSource, Project
from kumiki.effects import registry


def value_at(value: object, frame: int = 0) -> float:
    """数値パラメータの、その時刻での値。

    :data:`~kumiki.core.model.ParamValue` は数値とは限らないので、
    数値であることをここで 1 度だけ確かめる。
    """
    assert isinstance(value, AnimatedValue)
    return value.at(frame)


ALIAS = (
    "[Object]"
    + chr(10)
    + "frame=0,89"
    + chr(10)
    + "[Object.0]"
    + chr(10)
    + "effect.name=テキスト"
    + chr(10)
    + "サイズ=72.00"
    + chr(10)
    + "フォント=Dela Gothic One"
    + chr(10)
    + "文字色=ffee00"
    + chr(10)
    + "影・縁色=000000"
    + chr(10)
    + "文字装飾=縁取り文字（太）"
    + chr(10)
    + "文字揃え=中央揃え[下]"
    + chr(10)
    + "テキスト=見本の文字"
    + chr(10)
    + "[Object.1]"
    + chr(10)
    + "effect.name=縁取り"
    + chr(10)
    + "サイズ=4"
    + chr(10)
    + "縁色=ff0000"
    + chr(10)
)

NO_SPAN = (
    "[Object]" + chr(10) + "[Object.0]" + chr(10) + "effect.name=テキスト" + chr(10) + "テキスト=あ"
)


@pytest.fixture
def shelf(tmp_path: Path) -> tuple[TemplateCatalog, Path]:
    root = tmp_path / "テンプレート"
    (root / "字幕").mkdir(parents=True)
    (root / "字幕" / "強調.object").write_text(ALIAS, "utf-8")
    (root / "字幕" / "長さなし.object").write_text(NO_SPAN, "utf-8")
    (root / "読まない.txt").write_text("素通り", "utf-8")
    (root / "YMM4").mkdir()
    (root / "YMM4" / "見出し.ymmt").write_text(
        json.dumps(
            {
                "$type": "YukkuriMovieMaker.Project.Items.TextItem, YukkuriMovieMaker",
                "Text": "見出し",
                "FontSize": 80,
                "Length": 60,
            }
        ),
        "utf-8",
    )
    catalog = TemplateCatalog()
    catalog.scan((root,))
    return catalog, root


class TestScanning:
    def test_both_kinds_are_found(self, shelf: tuple[TemplateCatalog, Path]) -> None:
        catalog, _ = shelf
        sources = sorted({entry.source for entry in catalog.all()})
        assert sources == ["aviutl", "ymm4"]

    def test_other_files_are_skipped(self, shelf: tuple[TemplateCatalog, Path]) -> None:
        catalog, _ = shelf
        assert all(entry.name != "読まない" for entry in catalog.all())

    def test_the_folder_name_is_kept(self, shelf: tuple[TemplateCatalog, Path]) -> None:
        # 配布物はフォルダで分かれている。そのまま見出しに使う。
        catalog, _ = shelf
        assert "字幕" in catalog.folders()

    def test_an_absent_folder_is_not_an_error(self, tmp_path: Path) -> None:
        catalog = TemplateCatalog()
        assert catalog.scan((tmp_path / "無い",)) == []


class TestPlacing:
    def test_a_clip_is_added(self, shelf: tuple[TemplateCatalog, Path]) -> None:
        catalog, _ = shelf
        entry = catalog.find("強調")
        assert entry is not None
        commands = place(entry.load(), Project.create(), at_frame=0)
        assert any(isinstance(command, AddClip) for command in commands)

    def test_a_track_is_created_when_there_is_none(
        self, shelf: tuple[TemplateCatalog, Path]
    ) -> None:
        catalog, _ = shelf
        entry = catalog.find("強調")
        assert entry is not None
        commands = place(entry.load(), Project.create())
        assert isinstance(commands[0], AddTrack)

    def test_it_lands_where_asked(self, shelf: tuple[TemplateCatalog, Path]) -> None:
        # エイリアスは元の位置を持ったまま。そのまま置くと指定した場所へ来ない。
        catalog, _ = shelf
        entry = catalog.find("強調")
        assert entry is not None
        commands = place(entry.load(), Project.create(), at_frame=120)
        added = [c for c in commands if isinstance(c, AddClip)]
        assert added[0].clip.timeline_start == 120

    def test_an_alias_without_a_span_gets_the_default_length(
        self, shelf: tuple[TemplateCatalog, Path]
    ) -> None:
        # 1 フレームのクリップを置かれても使えない。
        catalog, _ = shelf
        entry = catalog.find("長さなし")
        assert entry is not None
        commands = place(entry.load(), Project.create(), default_duration=150)
        added = [c for c in commands if isinstance(c, AddClip)]
        assert added[0].clip.duration == 150

    def test_an_alias_with_a_span_keeps_it(self, shelf: tuple[TemplateCatalog, Path]) -> None:
        catalog, _ = shelf
        entry = catalog.find("強調")
        assert entry is not None
        added = [c for c in place(entry.load(), Project.create()) if isinstance(c, AddClip)]
        assert added[0].clip.duration == 90


class TestRestyling:
    def existing(self) -> Clip:
        blur = registry.get("blur")
        assert blur is not None
        return Clip(
            timeline_start=300,
            duration=45,
            source=GeneratedSource(
                kind="text",
                params={"text": "自分で打った字幕", "size": AnimatedValue(30.0)},
            ),
            effects=(blur.create(radius=8),),
        )

    def commands(self, shelf: tuple[TemplateCatalog, Path]) -> list[object]:
        catalog, _ = shelf
        entry = catalog.find("強調")
        assert entry is not None
        return list(restyle(entry.load(), self.existing()))

    def test_the_text_is_kept(self, shelf: tuple[TemplateCatalog, Path]) -> None:
        # 見本の文字で上書きしたら、それは着せ替えではない。
        source = next(c for c in self.commands(shelf) if isinstance(c, SetSource)).source
        assert source is not None
        assert source.params["text"] == "自分で打った字幕"

    def test_the_look_is_taken_from_the_template(self, shelf: tuple[TemplateCatalog, Path]) -> None:
        source = next(c for c in self.commands(shelf) if isinstance(c, SetSource)).source
        assert source is not None
        assert value_at(source.params["size"], 0) == 72.0
        assert source.params["font"] == "Dela Gothic One"
        assert value_at(source.params["border_width"], 0) > 0

    def test_the_old_effects_are_cleared_first(self, shelf: tuple[TemplateCatalog, Path]) -> None:
        # 残すと、前のデザインの縁とテンプレートの縁が二重に付く。
        commands = self.commands(shelf)
        assert any(isinstance(c, RemoveEffect) for c in commands)
        added = [c for c in commands if isinstance(c, AddEffect)]
        assert [c.effect.kind for c in added] == ["border"]

    def test_the_timing_is_untouched(self, shelf: tuple[TemplateCatalog, Path]) -> None:
        # 位置と長さを変えるコマンドは 1 つも出ない。
        assert all(not isinstance(c, AddClip) for c in self.commands(shelf))

    def test_a_non_text_clip_is_refused(self, shelf: tuple[TemplateCatalog, Path]) -> None:
        catalog, _ = shelf
        entry = catalog.find("強調")
        assert entry is not None
        shape = Clip(
            timeline_start=0,
            duration=30,
            source=GeneratedSource(kind="shape", params={"shape": "rect"}),
        )
        assert restyle(entry.load(), shape) == []
