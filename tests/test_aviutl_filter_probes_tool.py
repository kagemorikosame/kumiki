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
