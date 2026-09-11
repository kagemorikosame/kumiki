"""``.exo`` の中身をこちらのモデルへ写す。

値が写っているかだけでなく、**写せなかったものが記録に残るか**も見る。
黙って捨てると「なんとなく違う絵」が出て、原因を追えない。
"""

from __future__ import annotations

from kumiki.compat.aviutl.encoding import encode_utf16_hex
from kumiki.compat.aviutl.exo import parse_exo
from kumiki.compat.aviutl.mapping import MappedObject, map_exo, map_object
from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.core.model import Project, ProjectSettings
from kumiki.core.timebase import FrameRate

RATE = FrameRate(30)


def build(*entries: str, start: int = 1, end: int = 60, layer: int = 1) -> str:
    body = [f"[0]\nstart={start}\nend={end}\nlayer={layer}\n"]
    body.extend(f"[0.{index}]\n{entry}\n" for index, entry in enumerate(entries))
    return "".join(body)


def one(text: str) -> MappedObject:
    mapped = map_object(parse_exo(text).objects[0], RATE, report=CompatibilityReport())
    assert mapped is not None
    return mapped


class TestText:
    def test_a_text_object_becomes_a_text_source(self) -> None:
        hexed = encode_utf16_hex("こんにちは", length=8)
        mapped = one(build(f"_name=テキスト\nサイズ=64\ncolor=ff0000\ntext={hexed}"))
        source = mapped.clip.source
        assert source is not None
        assert source.kind == "text"
        assert source.params["text"] == "こんにちは"
        assert float(source.params["size"].at(0)) == 64.0  # type: ignore[union-attr]
        assert source.params["color"] == (1.0, 0.0, 0.0, 1.0)

    def test_the_font_is_carried_over(self) -> None:
        mapped = one(build("_name=テキスト\nfont=Meiryo\nサイズ=32"))
        source = mapped.clip.source
        assert source is not None
        assert source.params["font"] == "Meiryo"

    def test_bold_and_alignment(self) -> None:
        mapped = one(build("_name=テキスト\nB=1\nalign=1"))
        source = mapped.clip.source
        assert source is not None
        assert source.params["bold"] == 1
        assert source.params["align"] == "left"


class TestFigure:
    def test_the_figure_number_picks_the_shape(self) -> None:
        # 0=円 1=四角形 2=三角形 3=五角形 4=六角形 5=星形
        for index, expected in enumerate(
            ["ellipse", "rect", "triangle", "pentagon", "hexagon", "star"]
        ):
            mapped = one(build(f"_name=図形\ntype={index}\nサイズ=100"))
            source = mapped.clip.source
            assert source is not None
            assert source.params["shape"] == expected

    def test_the_aspect_ratio_changes_the_size(self) -> None:
        mapped = one(build("_name=図形\ntype=1\nサイズ=100\n縦横比=50"))
        source = mapped.clip.source
        assert source is not None
        assert float(source.params["width"].at(0)) == 50.0  # type: ignore[union-attr]
        assert float(source.params["height"].at(0)) == 100.0  # type: ignore[union-attr]


class TestDrawSettings:
    def test_position_and_scale_become_a_transform(self) -> None:
        mapped = one(
            build(
                "_name=図形\ntype=1\nサイズ=100",
                "_name=標準描画\nX=120.0\nY=-40.0\n拡大率=150.0\n回転=30.0\n透明度=0.0\nblend=0",
            )
        )
        effects = mapped.clip.effects
        assert effects[0].kind == "transform"
        assert float(effects[0].params["pos_x"].at(0)) == 120.0  # type: ignore[union-attr]
        # AviUtl の Y は下が正、こちらは上が正。符号が入れ替わる。
        assert float(effects[0].params["pos_y"].at(0)) == 40.0  # type: ignore[union-attr]
        assert float(effects[0].params["scale"].at(0)) == 150.0  # type: ignore[union-attr]

    def test_transparency_becomes_opacity(self) -> None:
        mapped = one(build("_name=図形", "_name=標準描画\n透明度=25.0"))
        assert float(mapped.clip.opacity.at(0)) == 0.75

    def test_the_blend_number_is_translated(self) -> None:
        mapped = one(build("_name=図形", "_name=標準描画\nblend=1"))
        assert mapped.clip.blend_mode == "add"

    def test_an_unsupported_blend_falls_back_to_normal(self) -> None:
        # 似た別のもので代用すると、直したつもりの無い違いが出る。
        mapped = one(build("_name=図形", "_name=標準描画\nblend=5"))
        assert mapped.clip.blend_mode == "normal"


class TestFilters:
    def test_known_filters_become_effects(self) -> None:
        mapped = one(build("_name=図形", "_name=ぼかし\n範囲=20", "_name=標準描画"))
        effects = mapped.clip.effects
        assert [effect.kind for effect in effects] == ["blur"]
        assert float(effects[0].params["radius"].at(0)) == 20.0  # type: ignore[union-attr]

    def test_unknown_filters_are_recorded(self) -> None:
        report = CompatibilityReport()
        mapped = map_object(
            parse_exo(build("_name=図形", "_name=まだ無いフィルタ\n値=1")).objects[0],
            RATE,
            report=report,
        )
        assert mapped is not None
        assert mapped.clip.effects == ()
        assert any("まだ無いフィルタ" in line for line in report.lines())

    def test_unknown_content_is_recorded(self) -> None:
        report = CompatibilityReport()
        map_object(parse_exo(build("_name=謎のオブジェクト")).objects[0], RATE, report=report)
        assert any("謎のオブジェクト" in line for line in report.lines())


class TestTiming:
    def test_frames_are_converted_to_zero_based(self) -> None:
        # AviUtl は 1 始まりで終端を含む。1..60 は 0 から 60 フレーム。
        mapped = one(build("_name=図形", start=1, end=60))
        assert (mapped.clip.timeline_start, mapped.clip.duration) == (0, 60)

    def test_a_later_object(self) -> None:
        mapped = one(build("_name=図形", start=31, end=90))
        assert (mapped.clip.timeline_start, mapped.clip.duration) == (30, 60)


class TestWholeFile:
    def test_layers_become_tracks(self) -> None:
        text = (
            "[exedit]\nwidth=1920\nheight=1080\nrate=30\nscale=1\n"
            + build("_name=図形\ntype=1", layer=1)
            + "[1]\nstart=1\nend=30\nlayer=3\n[1.0]\n_name=テキスト\nサイズ=40\n"
        )
        project = Project.create(ProjectSettings(frame_rate=RATE))
        commands = map_exo(parse_exo(text), project, report=CompatibilityReport())

        applied = project
        for command in commands:
            applied = command.apply(applied)
        assert len(applied.timeline.tracks) == 3
        assert len(applied.timeline.tracks[0].clips) == 1
        assert len(applied.timeline.tracks[2].clips) == 1

    def test_objects_can_be_dropped_at_a_position(self) -> None:
        project = Project.create(ProjectSettings(frame_rate=RATE))
        commands = map_exo(
            parse_exo(build("_name=図形\ntype=1")),
            project,
            at_frame=90,
            report=CompatibilityReport(),
        )
        applied = project
        for command in commands:
            applied = command.apply(applied)
        assert applied.timeline.tracks[0].clips[0].timeline_start == 90

    def test_a_file_with_nothing_mappable_gives_no_commands(self) -> None:
        project = Project.create(ProjectSettings(frame_rate=RATE))
        empty = "[exedit]\nwidth=1920\nheight=1080\n"
        assert map_exo(parse_exo(empty), project, report=CompatibilityReport()) == []
