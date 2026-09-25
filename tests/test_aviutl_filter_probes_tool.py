"""``tools/aviutl_filter_probes.py`` の並べ方と、Sashimono の側の写し方（PR #221 の指摘）

AviUtl2 は起動しない 書いたプロジェクトと、測る側が作る Sashimono のオブジェクトだけを見る
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.core.timebase import FrameRate

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "aviutl_filter_probes", ROOT / "tools" / "aviutl_filter_probes.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _names(work: Path) -> list[str]:
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    return [str(case["name"]) for case in manifest["cases"]]


def test_the_scene_section_is_written_only_with_scene_probes(
    tool: ModuleType, tmp_path: Path
) -> None:
    """シーン 1 の節は書き方がまだ違い、中身がルートのシーンの頭に置かれる

    どの見本でも足していたころは、頭の 60 フレームの見本に動く四角が重なり、3 回目の
    レンズブラー（光の強さ 0）の見本が読めなかった
    """
    assert tool.command_build(tmp_path / "plain") == 0
    assert "[scene.1]" not in (tmp_path / "plain" / "compare.aup2").read_text(encoding="utf-8")
    assert tool.command_build(tmp_path / "lens", "lb", third=True) == 0
    assert "[scene.1]" not in (tmp_path / "lens" / "compare.aup2").read_text(encoding="utf-8")
    assert tool.command_build(tmp_path / "scene", "sc", third=True) == 0
    assert "[scene.1]" in (tmp_path / "scene" / "compare.aup2").read_text(encoding="utf-8")


def test_the_measure_side_uses_each_probe_length(tool: ModuleType, tmp_path: Path) -> None:
    """60 フレームの見本を決まった 6 フレームで写すと、比べる真ん中のコマに何も無い"""
    assert tool.command_build(tmp_path, "po07", third=True) == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    (case,) = manifest["cases"]
    assert case["length"] == 60
    objects = tool.layer_objects(case, tmp_path, FrameRate(60), CompatibilityReport())
    assert objects
    assert all(obj.clip.duration == 60 for obj in objects)


def test_clipping_by_the_object_above_reaches_the_clip(tool: ModuleType, tmp_path: Path) -> None:
    """見出しの ``clipping.upper=1`` を読まないと、AviUtl2 が切った見本を Sashimono だけ切らない"""
    assert tool.command_build(tmp_path, "po06", third=True) == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    (case,) = manifest["cases"]
    objects = tool.layer_objects(case, tmp_path, FrameRate(60), CompatibilityReport())
    clipped = {obj.layer: obj.clip.clip_to_below for obj in objects}
    assert clipped == {1: False, 2: True, 3: False}


@pytest.mark.parametrize("only", ["", "tc"])
def test_the_crashing_time_control_is_left_out_by_default(
    tool: ModuleType, tmp_path: Path, only: str
) -> None:
    """時間制御の見本は、3 回目も 4 回目も並べたプロジェクトで AviUtl2 を落とした

    ``--fourth`` だけで並べたプロジェクトを案内どおりに開くと落ちる（PR #225）
    """
    status = tool.command_build(tmp_path, only, fourth=True)
    if only:
        assert status == 1
    else:
        assert status == 0
        assert not any(name.startswith("tc") for name in _names(tmp_path))


def test_the_crashing_time_control_is_written_only_when_asked(
    tool: ModuleType, tmp_path: Path
) -> None:
    """落ちる所を確かめ直すときは ``--crashing`` で並べられる"""
    assert tool.command_build(tmp_path, "tc", fourth=True, crashing=True) == 0
    assert _names(tmp_path) == ["tc02_linear_double", "tc02_range"]


def test_a_single_scene_probe_keeps_clear_of_the_misplaced_scene(
    tool: ModuleType, tmp_path: Path
) -> None:
    """シーン 1 の中身はルートの頭（0〜59 フレーム）に置かれる

    頭の空きを見本にしていたころは、``--only sc03`` で選ぶと空きが外れ、sc03 が 0 から
    始まって動く四角と重なった（PR #225）
    """
    assert tool.command_build(tmp_path, "sc03", fourth=True) == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    (case,) = manifest["cases"]
    assert case["name"] == "sc03_plain"
    assert case["start"] >= tool.SCENE_LENGTH


def test_the_third_and_fourth_rounds_cannot_be_asked_together(
    tool: ModuleType, tmp_path: Path
) -> None:
    """両方を付けると 4 回目だけを並べ、3 回目を黙って捨てていた（PR #225）"""
    with pytest.raises(SystemExit) as caught:
        tool.main(["--work", str(tmp_path), "build", "--third", "--fourth"])
    assert caught.value.code == 2
    assert not (tmp_path / "compare.aup2").exists()


def test_a_crashing_project_left_from_before_is_removed(tool: ModuleType, tmp_path: Path) -> None:
    """--crashing で並べたあと、付けずに --only tc で並べ直すと何も選ばれない

    そのとき前の落ちるプロジェクトを残すと、案内どおりに開いて AviUtl2 が落ちる（PR #225）
    """
    assert tool.command_build(tmp_path, "tc", fourth=True, crashing=True) == 0
    assert tool.command_build(tmp_path, "tc", fourth=True) == 1
    assert not (tmp_path / "compare.aup2").exists()
    assert not (tmp_path / "manifest.json").exists()


def test_the_told_length_covers_the_scene_lead_and_long_probes(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """本数 x 6 で数えると、頭の空きのある 60 フレームの sc03 を 10 フレームと案内していた

    実際は 64 フレーム目から 60 フレーム並び、長さは 124 フレームになる（PR #225）
    """
    assert tool.command_build(tmp_path, "sc03", fourth=True) == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    (case,) = manifest["cases"]
    last = case["start"] + case["length"]
    assert f"（{last} フレーム）" in capsys.readouterr().out
