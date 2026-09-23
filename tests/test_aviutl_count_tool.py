"""配布物を数える道具（tools/aviutl_count.py）

数えた結果で埋める順を決めるので、数え方が間違っていると、手を付ける順も間違う
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from sashimono.core.timebase import FrameRate

ROOT = Path(__file__).resolve().parent.parent

#: AviUtl1 のエイリアス 中身が 図形 で、写せない ``AviUtl1 の番号 7`` を 1 つ持つ
EXA = "[vo]\nlength=30\n[vo.0]\n_name=図形\nサイズ=100\n[vo.1]\n_name=標準描画\nX=0.0,50.0,7\n"

#: AviUtl2 のエイリアス 写せない所は無い
OBJECT = "[Object]\nframe=0,29\n[Object.0]\neffect.name=図形\nサイズ=100\n"

#: 全体設定しか無い 開けるが何も置かない
EMPTY = "[exedit]\nwidth=1920\nheight=1080\n"


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "aviutl_count", ROOT / "tools" / "aviutl_count.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def files(tmp_path: Path) -> list[Path]:
    written = []
    for name, body, encoding in (
        ("旧.exa", EXA, "cp932"),
        ("新.object", OBJECT, "utf-8"),
        ("空.exa", EMPTY, "cp932"),
    ):
        path = tmp_path / name
        path.write_text(body, encoding=encoding)
        written.append(path)
    return written


def test_each_suffix_is_counted_on_its_own(tool: ModuleType, files: list[Path]) -> None:
    # まとめた数だけでは、AviUtl1 と AviUtl2 のどちらの読み手の穴なのかが分からない
    tallies = tool.count(files, FrameRate(30))
    assert set(tallies) == {"*", ".exa", ".object"}
    assert (tallies[".exa"].files, tallies[".object"].files, tallies["*"].files) == (2, 1, 3)
    assert tallies[".exa"].missing["AviUtl の移動方法: AviUtl1 の番号 7"] == 1
    assert not tallies[".object"].missing
    assert tallies["*"].missing == tallies[".exa"].missing


def test_a_file_that_places_nothing_is_counted(tool: ModuleType, files: list[Path]) -> None:
    # 以前は、開けても 0 個しか置かない .exa が「読めなかったファイル 0 本」に紛れていた
    tallies = tool.count(files, FrameRate(30))
    assert tallies[".exa"].empty_files == 1
    assert tallies[".exa"].objects == 1
    assert not tallies["*"].broken
