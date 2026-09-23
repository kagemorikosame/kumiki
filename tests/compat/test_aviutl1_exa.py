"""AviUtl1 世代のエイリアス（``.exa``）

見本の書き方は、配布されている ``.exa`` 67 本（sigma_aviutl_scripts・PSDToolKit・
localfont2・FPS カウンタ）からそのまま写した 実物そのものを通す試験は
``test_real_aviutl1_exa.py`` にあり、置き場が無い機械では飛ぶ ここは CI でも走る
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from sashimono.compat.aviutl import catalog as catalog_module
from sashimono.compat.aviutl.catalog import ScriptCatalog, set_script_catalog
from sashimono.compat.aviutl.control import lua_value, parse_control
from sashimono.compat.aviutl.exo import parse_exo
from sashimono.compat.aviutl.mapping import map_object
from sashimono.compat.aviutl.motion import from_aviutl1, parse_motion
from sashimono.compat.aviutl.objapi import ObjectState
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.aviutl.runtime import LuaScriptRuntime, blank_image
from sashimono.core.model import AnimatedValue
from sashimono.core.timebase import FrameRate
from sashimono.effects.spec import CheckSpec, TextSpec

RATE = FrameRate(30)

#: PSDToolKit の Subtitle.exa の骨組み（テキスト欄は短くした）
TEXT_ALIAS = """[vo]
length=64
camera=0
[vo.0]
_name=テキスト
サイズ=34
font=MS UI Gothic
color=ffffff
color2=000000
type=0
align=4
text=53004b00
[vo.1]
_name=標準描画
X=0.0
Y=0.0
Z=0.0
拡大率=100.00
透明度=0.0
回転=0.00
blend=0
"""

#: 同じものの英語版（PSDToolKit の Subtitle_en.exa の骨組み）
ENGLISH_ALIAS = """[vo.0]
_name=Text
Size=34
font=Segoe UI
text=53004b00
[vo.1]
_name=Standard drawing
X=120.0
Y=0.0
Z=0.0
Zoom%=150.00
Clearness=25.0
Rotation=30.00
blend=0
"""

#: PSDToolKit の wav.exa
AUDIO_ALIAS = """[ao]
length=64
[ao.0]
_name=音声ファイル
再生位置=0.00
再生速度=100.0
ループ再生=0
動画ファイルと連携=0
file=
[ao.1]
_name=標準再生
音量=50.0
左右=0.0
"""

#: sigma_aviutl_scripts の ディザワイプ(時計).exa
SCENE_ALIAS = """[v.0]
_name=シーンチェンジ
反転=0
type=2
filter=0
name=ディザワイプ(時計)@ディザσ
調整=0.00
track1=45.00
param=_1=3;_2=0;_3=4;_4=123;_5=0;_6=0;_7=0;_0=nil;
"""

#: sigma_aviutl_scripts の 効果集σ - 内側シャドー.exa 中身も標準描画も無く、効果だけ
EFFECT_ALIAS = """[vo.0]
_name=アニメーション効果
type=0
filter=0
name=内側シャドー@効果集σ
track0=-40.00
track1=24.00
track2=40.00
track3=10.00
check0=1
param=_1=0x102030;_2=[[a;b.png]];_3=1;_0=nil;
"""

#: 上のエイリアスが呼ぶスクリプト 制御文字は ``@効果集σ.anm`` の書き方のまま
EFFECT_SCRIPT = """@内側シャドー
--track0:X,-2000,2000,-8,1
--track1:Y,-2000,2000,8,1
--track2:濃さ,0,100,40
--track3:拡散,0,500,10,1
--check0:影を元画像で切り抜き,1
--dialog:色/col,_1=0x000000;パターン画像,_2=[[]];└α値適用/chk,_3=1;TRACK,_0=nil;
obj.ox = 0
@四隅丸め
--track0:左上半径,0,2000,32,1
obj.ox = 0
"""


@pytest.fixture
def scripts(tmp_path: Path) -> Iterator[ScriptCatalog]:
    """``@効果集σ.anm`` だけを置いた一覧 終わったら元の一覧へ戻す"""
    root = tmp_path / "scripts"
    root.mkdir()
    (root / "@効果集σ.anm").write_text(EFFECT_SCRIPT, encoding="cp932")
    # 同じ名前のスクリプトを別の種類（カスタムオブジェクト）にも置く
    # PSDToolKit は 多目的スライダー を .anm と .obj の両方に持っている
    (root / "@効果集σ.obj").write_text("@内側シャドー\n--track0:別物,0,1,0\n", encoding="cp932")
    saved = catalog_module._catalog
    created = ScriptCatalog(roots=(root,))
    created.scan()
    set_script_catalog(created)
    yield created
    catalog_module._catalog = saved
    if saved is not None:
        saved.register_all()


class TestSections:
    def test_the_vo_sections_become_one_object(self) -> None:
        # 以前は ``[vo]`` ``[vo.0]`` を全体設定として読み、オブジェクトが 0 個だった
        exo = parse_exo(TEXT_ALIAS)
        assert len(exo.objects) == 1
        assert [entry.name for entry in exo.objects[0].entries] == ["テキスト", "標準描画"]
        assert exo.generation == 1
        assert exo.is_alias

    def test_the_length_is_the_span(self) -> None:
        # ``length=64`` を読まないと 1 フレームのクリップになり、尺を合わせられない
        obj = parse_exo(TEXT_ALIAS).objects[0]
        assert (obj.start, obj.end, obj.duration) == (0, 63, 64)
        assert obj.span_given

    def test_an_alias_without_the_header_has_no_span(self) -> None:
        # sigma の効果のエイリアスは ``[vo]`` を持たない 長さは置く側が決める
        obj = parse_exo(EFFECT_ALIAS).objects[0]
        assert not obj.span_given

    def test_the_audio_and_scene_change_sections_are_read(self) -> None:
        assert [e.name for e in parse_exo(AUDIO_ALIAS).objects[0].entries] == [
            "音声ファイル",
            "標準再生",
        ]
        assert parse_exo(SCENE_ALIAS).objects[0].entries[0].name == "シーンチェンジ"

    def test_the_text_is_mapped(self) -> None:
        mapped = map_object(parse_exo(TEXT_ALIAS).objects[0], RATE, report=CompatibilityReport())
        assert mapped is not None
        assert mapped.clip.source is not None
        assert mapped.clip.source.params["text"] == "SK"
        assert mapped.clip.duration == 64


class TestEnglishNames:
    def test_the_english_names_read_like_the_japanese_ones(self) -> None:
        # 英語版の AviUtl は名前を英語で書く 読まないと中身が「未知のオブジェクト」になり、
        # 位置も拡大も透明度も落ちる
        entries = parse_exo(ENGLISH_ALIAS).objects[0].entries
        assert [entry.name for entry in entries] == ["テキスト", "標準描画"]
        assert entries[0].params["サイズ"] == "34"
        drawing = entries[1].params
        assert (drawing["拡大率"], drawing["透明度"], drawing["回転"]) == (
            "150.00",
            "25.0",
            "30.00",
        )

    def test_the_english_drawing_is_placed(self) -> None:
        report = CompatibilityReport()
        mapped = map_object(parse_exo(ENGLISH_ALIAS).objects[0], RATE, report=report)
        assert mapped is not None
        assert mapped.clip.opacity.static == pytest.approx(0.75)
        transform = next(e for e in mapped.clip.effects if e.kind == "transform")
        scale = transform.params["scale"]
        assert isinstance(scale, AnimatedValue)
        assert scale.static == pytest.approx(150.0)
        assert not report.missing


class TestTrackLines:
    def test_the_method_number_is_not_a_value(self) -> None:
        # ``0.00,0.00,3`` を素直に読むと 0 から 3 へ動く値に見える
        motion = parse_motion(from_aviutl1("0.00,0.00,3"))
        assert motion is not None
        assert motion.values == (0.0, 0.0)
        assert motion.method == "瞬間移動"
        assert not motion.moves

    def test_an_unknown_number_is_recorded(self) -> None:
        # 番号と移動方法の対応を確かめたのは 3 だけ ほかは番号のまま記録へ出る
        report = CompatibilityReport()
        entry_text = "[vo.0]\n_name=図形\nサイズ=100,200,1\n[vo.1]\n_name=標準描画\nX=0.0,50.0,7\n"
        map_object(parse_exo(entry_text).objects[0], RATE, report=report)
        assert "AviUtl の移動方法: AviUtl1 の番号 7" in report.missing

    def test_plain_values_are_left_alone(self) -> None:
        assert from_aviutl1("100.00") == "100.00"
        assert from_aviutl1("ffffff") == "ffffff"


class TestEffectOnly:
    def test_the_effect_connects_to_the_script(self, scripts: ScriptCatalog) -> None:
        # 以前は ``name`` を表示名とそのまま比べていたので、``@`` の付いた実物は
        # 1 本も繋がらず、しかも先頭にあるせいで「未知のオブジェクト」として捨てられた
        report = CompatibilityReport()
        mapped = map_object(parse_exo(EFFECT_ALIAS).objects[0], RATE, report=report)
        assert mapped is not None
        assert mapped.kind == "effects"
        assert not mapped.has_picture
        (effect,) = mapped.clip.effects
        entry = scripts.get(effect.kind)
        assert entry is not None
        assert (entry.label, entry.kind) == ("内側シャドー", "anm")
        assert not report.missing

    def test_the_tracks_and_the_dialog_are_read(self, scripts: ScriptCatalog) -> None:
        del scripts
        mapped = map_object(parse_exo(EFFECT_ALIAS).objects[0], RATE, report=CompatibilityReport())
        assert mapped is not None
        params = mapped.clip.effects[0].params
        track0 = params["track0"]
        assert isinstance(track0, AnimatedValue)
        assert track0.static == -40.0
        assert params["check0"] is True
        colour = params["_1"]
        assert isinstance(colour, tuple)
        assert tuple(round(float(part) * 255) for part in colour[:3]) == (0x10, 0x20, 0x30)
        # ``[[…]]`` の中の ``;`` で切らない
        assert params["_2"] == "a;b.png"
        assert params["_3"] is True
        assert "_0" not in params

    def test_a_missing_script_is_counted_by_name(self, scripts: ScriptCatalog) -> None:
        del scripts
        report = CompatibilityReport()
        text = EFFECT_ALIAS.replace("内側シャドー@効果集σ", "内側シャドー@別の束")
        map_object(parse_exo(text).objects[0], RATE, report=report)
        assert report.missing["アニメーション効果: 内側シャドー@別の束"] == 1


class TestScriptedContents:
    def test_custom_objects_and_scene_changes_are_counted_by_script(self) -> None:
        report = CompatibilityReport()
        map_object(parse_exo(SCENE_ALIAS).objects[0], RATE, report=report)
        assert report.missing["シーンチェンジ: ディザワイプ(時計)@ディザσ"] == 1

    def test_the_audio_volume_is_kept(self) -> None:
        report = CompatibilityReport()
        mapped = map_object(parse_exo(AUDIO_ALIAS).objects[0], RATE, report=report)
        assert mapped is not None
        (volume,) = mapped.clip.effects
        assert volume.kind == "audio_volume"
        level = volume.params["volume"]
        assert isinstance(level, AnimatedValue)
        assert level.static == 50.0
        assert "フィルタ: 標準再生" not in report.missing


class TestDialog:
    def test_a_nil_default_makes_no_field(self) -> None:
        # 欄を作ると 0 が入り、``if _0 then _0[1]`` が数を表として引いて落ちる
        header = parse_control("--dialog:色/col,_1=0x000000;TRACK,_0=nil;")
        assert [spec.name for spec in header.parameters] == ["_1"]

    def test_a_long_string_default_is_text(self) -> None:
        (spec,) = parse_control("--dialog:パターン画像,_2=[[]];").parameters
        assert isinstance(spec, TextSpec)
        assert spec.default == ""

    def test_a_dialog_check_is_a_number_in_lua(self) -> None:
        # 配布スクリプトは ``_3==1`` と比べる 真偽で渡すと Lua では偽になる
        (spec,) = parse_control("--dialog:α値適用/chk,_3=1;").parameters
        assert isinstance(spec, CheckSpec)
        assert lua_value(spec, True) == 1
        assert lua_value(spec, False) == 0
        runtime = LuaScriptRuntime(instruction_limit=100_000)
        state = ObjectState(image=blank_image(8, 8))
        state.values["_3"] = lua_value(spec, True)
        runtime.run("if _3 == 1 then obj.ox = 5 end", state)
        assert state.ox == 5.0

    def test_a_named_check_stays_a_boolean(self) -> None:
        # AviUtl2 の ``--check@名前`` は真偽 こちらまで数にすると ``if bold then`` が
        # 0 でも真になる（Lua では 0 も真）
        (spec,) = parse_control("--check@bold:太字,true").parameters
        assert lua_value(spec, True) is True


class TestPackage:
    def test_package_loaded_is_there_but_empty(self) -> None:
        # sigma のスクリプトは ``package.loaded.bit`` を頭で読む 無いと 1 行目で落ちる
        runtime = LuaScriptRuntime(instruction_limit=100_000)
        state = ObjectState(image=blank_image(8, 8))
        result = runtime.run(
            "if package.loaded.bit == nil and package.path == nil"
            " and package.loadlib == nil then obj.ox = 1 end",
            state,
        )
        assert not result.failed
        assert state.ox == 1.0

    def test_package_loaded_does_not_leak_between_scripts(self) -> None:
        # 同じランタイムでエフェクトを順に走らせるので、前のスクリプトが
        # ``package.loaded`` に置いた物が見えると、積んだ順で結果が変わる
        runtime = LuaScriptRuntime(instruction_limit=100_000)
        state = ObjectState(image=blank_image(8, 8))
        first = runtime.run("package.loaded.leak = 1", state)
        assert not first.failed
        second = runtime.run("if package.loaded.leak == nil then obj.ox = 1 end", state)
        assert not second.failed
        assert state.ox == 1.0


class TestQodoFindings:
    """PR #131 の 2 度目のレビュー（Qodo）で見つかった 2 つ"""

    def test_an_escaped_quote_does_not_close_the_string(self) -> None:
        # ``\"`` を閉じ引用符と取り違えると、中の ``;`` で切れて後ろの欄が崩れる
        header = parse_control('--dialog:本文,_1="a\\";b";色/col,_2=0x102030;')
        assert [spec.name for spec in header.parameters] == ["_1", "_2"]
        text = header.parameters[0]
        assert isinstance(text, TextSpec)
        assert text.default == 'a";b'

    def test_an_escaped_quote_in_the_alias_value(self, scripts: ScriptCatalog) -> None:
        # ``.exa`` の ``param=`` も同じ切り方を通る
        del scripts
        alias = EFFECT_ALIAS.replace("_2=[[a;b.png]]", "_2='c\\';d.png'")
        mapped = map_object(parse_exo(alias).objects[0], RATE, report=CompatibilityReport())
        assert mapped is not None
        params = mapped.clip.effects[0].params
        assert params["_2"] == "c';d.png"
        assert params["_3"] is True

    def test_an_unrelated_section_is_not_picked(self, tmp_path: Path) -> None:
        # ``foo.anm`` に ``@bar`` と ``@baz`` しか無いとき、``name=foo`` で先頭の ``bar`` を
        # 走らせると、頼まれていない効果が黙って掛かる 見つからないと記録する
        root = tmp_path / "scripts"
        root.mkdir()
        (root / "foo.anm").write_text("@bar\nobj.ox = 1\n@baz\nobj.ox = 2\n", "cp932")
        saved = catalog_module._catalog
        created = ScriptCatalog(roots=(root,))
        created.scan()
        set_script_catalog(created)
        try:
            report = CompatibilityReport()
            alias = "[vo.0]\n_name=アニメーション効果\nname=foo\nparam=\n"
            mapped = map_object(parse_exo(alias).objects[0], RATE, report=report)
            assert mapped is not None
            assert mapped.clip.effects == ()
            assert report.missing["アニメーション効果: foo"] == 1
        finally:
            catalog_module._catalog = saved
            if saved is not None:
                saved.register_all()


class TestSourceryFindings:
    """PR #131 のレビューで見つかった 2 つ"""

    def test_a_semicolon_inside_a_long_string_default_stays(self) -> None:
        # 本文を ``split(";")`` で切ると ``[[a;b]]`` が割れ、初期値が ``[[a`` になって
        # ``b]]`` が次の項目として読まれ、後ろの色の欄まで崩れる
        header = parse_control("--dialog:パターン画像,_2=[[a;b]];色/col,_1=0x102030;")
        assert [spec.name for spec in header.parameters] == ["_2", "_1"]
        text = header.parameters[0]
        assert isinstance(text, TextSpec)
        assert text.default == "a;b"

    def test_a_single_script_file_is_found_by_its_file_name(self, tmp_path: Path) -> None:
        # ``@`` の無い ``name=ゆれ`` は、ファイル名がそのまま表示名の 1 本きりのスクリプト
        # 中に ``@ゆれ`` の見出しを書いたものまで「見つからない」にしていた
        root = tmp_path / "scripts"
        root.mkdir()
        (root / "ゆれ.anm").write_text("@ゆれ\n--track0:幅,0,100,10\nobj.ox = 0\n", "cp932")
        # 何本も入れるファイル（名前が @ で始まる）の同じ表示名は、@ の無い名前では選ばない
        (root / "@束.anm").write_text("@ゆれ\n--track0:別物,0,1,0\nobj.ox = 0\n", "cp932")
        saved = catalog_module._catalog
        created = ScriptCatalog(roots=(root,))
        created.scan()
        set_script_catalog(created)
        try:
            report = CompatibilityReport()
            alias = "[vo.0]\n_name=アニメーション効果\nname=ゆれ\ntrack0=40.00\nparam=\n"
            mapped = map_object(parse_exo(alias).objects[0], RATE, report=report)
            assert mapped is not None
            assert not report.missing
            (effect,) = mapped.clip.effects
            entry = created.get(effect.kind)
            assert entry is not None
            assert entry.path.name == "ゆれ.anm"
        finally:
            catalog_module._catalog = saved
            if saved is not None:
                saved.register_all()


class TestCodeRabbitFindings:
    """PR #131 の CodeRabbit のレビューで見つかったもの"""

    def test_an_unknown_figure_falls_back_and_is_recorded(self) -> None:
        # 一覧に無い図形名をそのまま既定値にすると SelectSpec が例外を投げる
        header = parse_control('--dialog:角図形/fig,_1="ハート";色/col,_2=0x0;')
        figure = header.parameters[0]
        assert figure.default_value() == "円"
        assert [spec.name for spec in header.parameters] == ["_1", "_2"]
        assert "--dialog の図形: ハート" in header.unknown

    def test_a_non_finite_number_does_not_break_the_header(self) -> None:
        # nan はスライダーの範囲の検査で例外になる
        header = parse_control("--dialog:値,_1=nan;")
        (spec,) = header.parameters
        assert spec.default_value() == AnimatedValue(0.0)
        assert any("_1=nan" in line for line in header.unknown)

    def test_one_broken_script_does_not_stop_the_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 走査が 1 本の例外で止まると、ほかのスクリプトが全部使えなくなる
        (tmp_path / "図形.anm").write_text('--dialog:角図形/fig,_1="ハート";\n', "cp932")
        (tmp_path / "良い.anm").write_text("--track0:量,0,10,1\n", "cp932")
        report = CompatibilityReport()
        catalog = ScriptCatalog(roots=(tmp_path,), report=report)
        assert {entry.label for entry in catalog.scan()} == {"図形", "良い"}

        from sashimono.compat.aviutl import control

        real = control.parse_control

        def fussy(text: str, *, name: str = "") -> control.ScriptHeader:
            if "ハート" in text:
                raise ValueError("壊れた制御文字")
            return real(text, name=name)

        monkeypatch.setattr(control, "parse_control", fussy)
        assert {entry.label for entry in catalog.scan()} == {"良い"}
        assert "図形.anm" in report.failures

    def test_a_broken_section_does_not_take_its_neighbours(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 1 本のファイルに何本も入れた配布物で、後ろの 1 節が崩れているだけで
        # 前の読めていた節まで一覧から消えていた
        (tmp_path / "@束.anm").write_text(
            "@良い\n--track0:量,0,10,1\nobj.ox = 0\n@壊れ\n--track0:量,0,10,1\nobj.ox = 0\n"
            "@後ろ\n--track0:量,0,10,1\nobj.ox = 0\n",
            "cp932",
        )
        from sashimono.compat.aviutl import control

        real = control.parse_control

        def fussy(text: str, *, name: str = "") -> control.ScriptHeader:
            if name == "壊れ":
                raise ValueError("範囲の崩れた値")
            return real(text, name=name)

        monkeypatch.setattr(control, "parse_control", fussy)
        report = CompatibilityReport()
        catalog = ScriptCatalog(roots=(tmp_path,), report=report)
        assert {entry.label for entry in catalog.scan()} == {"良い", "後ろ"}
        assert "@束.anm@壊れ" in report.failures

    def test_a_non_finite_value_control_does_not_break_the_header(self) -> None:
        # int(nan) は ValueError、int(inf) は OverflowError 後者は節ごとの受け止めも
        # すり抜けて走査を止める
        for raw in ("nan", "inf"):
            (spec,) = parse_control(f"--value@x:値,{raw}").parameters
            assert spec.default_value() == 0
