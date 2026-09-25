"""``--dialog`` の ``local 名前=初期値`` をスクリプトのローカル変数として渡すこと（#190）

PSDToolKit の 吹き出し は ``--dialog:余白 横,local mlr=24;…`` と書き、本文で ``mlr`` を読む
AviUtl はこの形を本文の頭で宣言したローカル変数として渡す ``local mlr`` という名前の
大域変数として置くと、本文の ``mlr`` は nil のままで、吹き出し が 1 行目の計算で落ちていた
"""

from __future__ import annotations

import numpy as np

from sashimono.compat.aviutl.control import parse_control
from sashimono.compat.aviutl.objapi import ObjectState
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.aviutl.runtime import LuaScriptRuntime

SOURCE = """--dialog:余白 横,local mlr=24;色/col,local col=0xffffff;普通,plain=3
obj.ox = mlr
obj.oy = col
obj.oz = plain
"""


def _state(values: dict[str, object]) -> ObjectState:
    return ObjectState(image=np.zeros((1, 1, 4), np.uint8), values=values)


def _run(runtime: LuaScriptRuntime, source: str, values: dict[str, object]) -> ObjectState:
    state = _state(values)
    result = runtime.run(source, state, header=parse_control(source))
    assert not result.failed, result.message
    return state


class TestDialogLocals:
    def test_local_items_reach_the_body(self) -> None:
        runtime = LuaScriptRuntime(report=CompatibilityReport())
        state = _run(runtime, SOURCE, {"local mlr": 30, "local col": 0xFF0000, "plain": 5})
        assert (state.ox, state.oy, state.oz) == (30.0, float(0xFF0000), 5.0)

    def test_they_do_not_leak_into_the_next_script(self) -> None:
        # ローカル変数なので、同じランタイムで次に走るスクリプトからは見えない
        runtime = LuaScriptRuntime(report=CompatibilityReport())
        _run(runtime, SOURCE, {"local mlr": 30, "local col": 1, "plain": 5})
        after = _run(runtime, "obj.ox = mlr == nil and 1 or 0", {})
        assert after.ox == 1.0

    def test_line_numbers_in_errors_stay_the_same(self) -> None:
        # 頭に宣言を足しても、失敗したときの行番号がスクリプトの行とずれない
        runtime = LuaScriptRuntime(report=CompatibilityReport())
        state = _state({"local mlr": 1})
        result = runtime.run("local a = 1\nerror('x')", state)
        assert result.failed and ":2:" in result.message
