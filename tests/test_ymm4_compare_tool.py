"""YMM4 と比べる道具（tools/ymm4_compare.py）のフレームの数え方

比べる道具がずれていると、描き方が合っていても差が出て、合っていない所を
探し回ることになる
"""

from __future__ import annotations

import importlib.util
import sys
from fractions import Fraction
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ymm4_compare", ROOT / "tools" / "ymm4_compare.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_first_exported_frame_is_frame_zero(tool: ModuleType) -> None:
    """YMM4 の書き出しは最初の 1 枚の時刻が 1 フレーム後ろから始まる

    時刻をそのまま番号にすると YMM4 の絵が 1 枚ずつ遅れて並び、場面の切れ目で
    差が 8〜24 跳ねた（ガタッて落ちるは 23.7、頭の時刻を引くと 0.15）
    """
    base = Fraction(1, 30000)
    assert tool.frame_index(1000, 1000, base, 30.0) == 0
    assert tool.frame_index(2000, 1000, base, 30.0) == 1
    # 頭の時刻が分からない動画は、時刻をそのまま使う
    assert tool.frame_index(3000, None, base, 30.0) == 3


def test_references_are_read_in_order_without_keeping_them_all(tool: ModuleType) -> None:
    # 1 万枚を超える書き出しを全部持つとメモリに載らない 若い順に進めて読む
    frames = ((index, np.full((1, 1, 3), index, dtype=np.uint8)) for index in (0, 1, 2, 4))
    references = tool.References(frames)
    first = references.get(1)
    assert first is not None and int(first[0, 0, 0]) == 1
    # 動画に無い番号は None
    assert references.get(3) is None
    last = references.get(4)
    assert last is not None and int(last[0, 0, 0]) == 4
    assert references.get(9) is None


def test_every_frame_of_a_slot_can_be_compared(tool: ModuleType) -> None:
    # 3 枚だけだと、切れ目のように一瞬だけずれる所を見落とす
    case = tool.Case(name="n", file="f", index=0, start=100, length=60)
    assert case.sample_frames() == [102, 129, 156]
    assert case.sample_frames(every=True) == list(range(100, 160))
