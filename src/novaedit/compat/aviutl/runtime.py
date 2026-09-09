"""AviUtl スクリプトを走らせる Lua ランタイム。

**Lua 5.1 で動かす。** AviUtl 本体が 5.1 で、配布スクリプトは ``unpack`` や
``setfenv`` のような 5.1 の書き方を普通に使う。5.4 で動かすとそこで落ちるので、
lupa が同梱している LuaJIT 2.1（5.1 互換）を使い、無ければ Lua 5.1 へ落ちる。

**サンドボックスを掛ける。** 配布スクリプトは他人が書いたコードで、それを読み込む
だけで実行される。``io`` ``os`` ``require`` ``dofile`` を外し、ファイルにも
プロセスにも触れないようにしてある。実行時間にも上限を置き、無限ループで
編集画面ごと固まらないようにする。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from novaedit.compat.aviutl.control import ScriptHeader
from novaedit.compat.aviutl.encoding import read_text
from novaedit.compat.aviutl.objapi import (
    DrawCall,
    ObjApi,
    ObjectState,
    hsv_to_number,
    rgb_to_number,
)
from novaedit.compat.aviutl.report import CompatibilityReport, global_report

__all__ = ["LuaError", "LuaScriptRuntime", "ScriptResult", "lua_available"]

#: 1 回の実行に許す命令数。超えたら打ち切る。
#:
#: 実測で、2000 回まわるスクリプトが 0.06ms。この上限に当たるまで走らせても
#: 9ms なので、壊れたスクリプトを掴んでも再生が止まるのではなく重くなるだけで済む。
INSTRUCTION_LIMIT = 5_000_000

#: ``obj`` を Lua のテーブルとして見せるための下準備。
#: メタテーブル経由にすることで、読み書きのすべてを Python 側で受けられる。
#: Python のオブジェクトをそのまま渡す手もあるが、AviUtl のスクリプトは
#: ``obj`` をテーブルとして扱う（``obj.track0`` など）ので、テーブルにしておく。
_BIND_OBJ = """
function(get, set)
  obj = setmetatable({}, {
    __index = function(_, key) return get(key) end,
    __newindex = function(_, key, value) set(key, value) end,
  })
end
"""

#: モジュールとして読む拡張子。テキストの Lua を先に探す。
MODULE_SUFFIXES = (".lua", ".mod", ".mod2")

#: 命令数に上限を掛けて呼ぶための包み。無限ループを書いたスクリプトは実在する。
_GUARD = """
function(fn, limit)
  local co = coroutine.create(fn)
  local hit = false
  debug.sethook(co, function() hit = true; error("時間切れ", 2) end, "", limit)
  local ok, err = coroutine.resume(co)
  debug.sethook(co)
  if not ok then return err, hit end
  return nil, false
end
"""


class LuaError(RuntimeError):
    """スクリプトの読み込みか実行に失敗した。"""


@dataclass(frozen=True, slots=True)
class ScriptResult:
    """1 回の実行の結果。"""

    draws: tuple[DrawCall, ...]
    #: 実行が失敗したか。失敗しても描画は元の画像で 1 回行う。
    failed: bool = False
    message: str = ""
    #: 実行後の状態。値を取り出したいときに使う。
    state: ObjectState | None = field(default=None, repr=False)


def lua_available() -> bool:
    """Lua ランタイムを用意できるか。"""
    return _load_module() is not None


def _load_module() -> Any:
    """lupa の中から、AviUtl に一番近い Lua を選ぶ。

    素の Lua 5.1 が第一候補。AviUtl 本体が使っているのがこれで、文法も
    標準ライブラリの中身も完全に一致する。

    LuaJIT を先に選ばないのには理由がある。JIT が有効だと、コンパイル済みの
    ループの中で ``debug`` のフックが呼ばれない。無限ループを打ち切る仕組みが
    そこだけ効かなくなるので、使うときは JIT を切る（:meth:`_install_globals`）。
    """
    from importlib import import_module

    for name in ("lua51", "luajit21", "luajit20", "lua54"):
        try:
            return import_module(f"lupa.{name}")
        except ImportError:
            continue
    return None


class LuaScriptRuntime:
    """スクリプトを走らせる係。

    1 つのランタイムを使い回す。作り直すと標準ライブラリの用意だけで数ミリ秒
    かかり、フレームごとに走らせる用途では効いてくる。

    スレッドセーフではない。描画系統ごとに 1 つ持つこと。
    """

    def __init__(
        self,
        *,
        report: CompatibilityReport | None = None,
        render_source: Any = None,
        instruction_limit: int = INSTRUCTION_LIMIT,
    ) -> None:
        module = _load_module()
        if module is None:  # pragma: no cover - lupa は既定で入っている
            raise LuaError("Lua ランタイム（lupa）が見つかりません")

        self._report = report if report is not None else global_report
        self._render_source = render_source
        self._instruction_limit = instruction_limit
        self._lua = module.LuaRuntime(unpack_returned_tuples=True, register_eval=False)
        self._lock = threading.Lock()
        self._install_globals()
        # どちらも 1 度だけ組み立てる。フレームごとに作り直すと、
        # コンパイルの時間がそのまま描画の遅れになる。
        self._bind_obj = self._lua.eval(_BIND_OBJ)
        self._guard = self._lua.eval(_GUARD)
        #: 前回置いた大域変数。次の実行で消すために覚えておく。
        self._injected: set[str] = set()
        #: いま走らせているスクリプトのフォルダ。モジュールの探索に使う。
        self._folder: Path | None = None
        #: モジュールを探すフォルダ。
        self._roots: tuple[Path, ...] = ()
        #: 読み込み済みのモジュール。名前ごとに 1 度だけ実行する。
        self._modules: dict[str, Any] = {}

    # --- 準備 ---

    def _install_globals(self) -> None:
        """標準の環境を整え、危ないものを外す。"""
        # LuaJIT に落ちた場合は JIT を切る。切らないと、コンパイルされた
        # ループの中で命令数のフックが呼ばれず、実行時間の上限が効かない。
        self._lua.execute("if jit then jit.off() end")
        globals_table = self._lua.globals()

        # ファイルとプロセスへの入口を閉じる。配布スクリプトは読み込むだけで
        # 走るので、ここを開けたままにはできない。``debug`` は残す。
        # 実行時間の上限を ``debug.sethook`` で掛けているため。
        for name in ("io", "os", "package", "require", "dofile", "loadfile", "load", "loadstring"):
            globals_table[name] = None

        globals_table["RGB"] = rgb_to_number
        globals_table["HSV"] = hsv_to_number
        globals_table["OR"] = lambda a, b: int(a) | int(b)
        globals_table["AND"] = lambda a, b: int(a) & int(b)
        globals_table["XOR"] = lambda a, b: int(a) ^ int(b)
        globals_table["NOT"] = lambda a: ~int(a)
        globals_table["SHIFT"] = _shift
        globals_table["debug_print"] = self._debug_print
        # 配布スクリプトは共通処理を別ファイルへ切り出している。読めないと
        # 本体の 1 行目で落ちるので、スクリプトフォルダの中だけを許して通す。
        globals_table["require"] = self.load_module

    def _debug_print(self, *args: Any) -> None:
        """``debug_print`` の行き先。互換性の記録に混ぜておく。"""
        text = " ".join(str(value) for value in args)
        self._report.note_missing(f"debug_print: {text[:80]}")

    # --- 実行 ---

    def run(
        self,
        source: str,
        state: ObjectState,
        *,
        header: ScriptHeader | None = None,
        script: str = "",
        folder: Path | None = None,
    ) -> ScriptResult:
        """スクリプトを 1 回走らせる。

        失敗しても例外にしない。1 つのスクリプトの失敗でフレーム全体が真っ黒に
        なるより、そのオブジェクトだけ素通しで出る方が編集を続けられる。
        """
        api = ObjApi(
            state,
            report=self._report,
            script=script,
            render_source=self._render_source,
            load_module=self.load_module,
        )
        with self._lock:
            self._folder = folder
            try:
                self._prepare(api, header, state)
                function = self._compile(source, script)
                self._call_guarded(function)
            except LuaError as exc:
                self._report.note_failure(script or "スクリプト", str(exc))
                return ScriptResult(
                    draws=(state.snapshot(),), failed=True, message=str(exc), state=state
                )
        return ScriptResult(draws=state.result(), state=state)

    def _prepare(self, api: ObjApi, header: ScriptHeader | None, state: ObjectState) -> None:
        """``obj`` を繋ぎ、名前付きの値を大域変数へ置き、``--param`` を流し込む。"""
        self._bind_obj(api.get, api.set)
        self._install_values(state)
        if header is None or not header.setup:
            return
        try:
            self._lua.execute(header.setup)
        except Exception as exc:
            raise LuaError(f"--param の実行に失敗: {exc}") from exc

    def _install_values(self, state: ObjectState) -> None:
        """名前付きパラメータを大域変数として置く。

        ``--track@offset_x:…`` と書いたスクリプトは、``obj.offset_x`` ではなく
        **素の ``offset_x``** を読む。実際に配布されているスクリプトはどれも
        この書き方なので、ここを用意しないと ``nil`` との演算で必ず落ちる。

        前回置いた分は消す。残っていると、別のスクリプトが自分では設定して
        いない値を読めてしまい、動いたり動かなかったりする。
        """
        globals_table = self._lua.globals()
        for name in self._injected - set(state.values):
            globals_table[name] = None
        self._injected = set(state.values)
        for name, value in state.values.items():
            globals_table[name] = value

    # --- モジュール ---

    def set_roots(self, roots: tuple[Path, ...]) -> None:
        """モジュールを探すフォルダ。スクリプトの置き場と同じ。"""
        self._roots = roots

    def load_module(self, name: str = "") -> Any:
        """``obj.module`` と ``require`` の実体。

        探すのは**スクリプトフォルダの中だけ**。任意のパスを開けるようにすると、
        配布スクリプトを読み込んだだけでディスクの中身を読まれうる。

        読むのは Lua のソースだけ。AviUtl2 の ``.mod2`` は中身が Windows の DLL
        であることがある。それは AviUtl2 の Lua に合わせて作られた native
        モジュールで、こちらでは動かせない。黙って ``nil`` を返すと「なぜか
        動かない」で終わるので、理由を記録に残す。
        """
        key = str(name)
        if key in self._modules:
            return self._modules[key]

        path = self._find_module(key)
        if path is None:
            self._report.note_missing(f'モジュール "{key}" が見つかりません')
            self._modules[key] = None
            return None

        value = None
        if _is_native(path):
            self._report.note_missing(
                f'モジュール "{key}" は native（{path.name} の中身が DLL）なので使えません'
            )
        else:
            try:
                text, _ = read_text(path)
                value = self._lua.execute(_strip_bom(text))
            except Exception as exc:
                self._report.note_failure(path.name, f"モジュールを読めない: {exc}")
        self._modules[key] = value
        return value

    def _find_module(self, name: str) -> Path | None:
        """モジュールのファイルを探す。

        AviUtl2 の配布物では ``.mod2``、素の Lua では ``.lua`` が使われる。
        スクリプト自身のフォルダを先に見るのは、同じ名前のモジュールが別の
        配布物にもありうるため。
        """
        separators = ("..", ":", chr(92), "/")
        if not name or any(part in name for part in separators):
            return None

        folders = [self._folder] if self._folder is not None else []
        folders.extend(self._roots)
        for folder in folders:
            if folder is None:
                continue
            # ``.lua`` を先に見る。同じ名前で ``.mod2``（DLL）と ``.lua`` の
            # 両方が置かれている配布物があり、こちらで動くのは後者だけ。
            for suffix in MODULE_SUFFIXES:
                candidate = folder / f"{name}{suffix}"
                if candidate.exists():
                    return candidate
            # 1 段下も見る。配布物はフォルダごと置かれることが多い。
            for suffix in MODULE_SUFFIXES:
                found = next(folder.glob(f"*/{name}{suffix}"), None)
                if found is not None:
                    return found
        return None

    def _compile(self, source: str, script: str) -> Any:
        try:
            return self._lua.eval(f"function() {_strip_bom(source)} end")
        except Exception as exc:
            raise LuaError(f"{script or 'スクリプト'} を読めません: {exc}") from exc

    def _call_guarded(self, function: Any) -> None:
        """命令数に上限を掛けて呼ぶ。

        無限ループを書いたスクリプトは実在する。掛けておかないと、編集画面が
        戻ってこなくなる。
        """
        try:
            message, timed_out = self._guard(function, self._instruction_limit)
        except Exception as exc:
            raise LuaError(str(exc)) from exc
        if message is not None:
            raise LuaError("実行が長すぎます" if timed_out else str(message))


def _is_native(path: Path) -> bool:
    """中身が実行ファイルか、コンパイル済みの Lua か。

    AviUtl2 の ``.mod2`` は Windows の DLL であることがある。テキストとして
    読むと文字化けした塊を Lua に食わせることになり、意味の分からない
    構文エラーが出る。先に見分ける。
    """
    try:
        head = path.open("rb").read(4)
    except OSError:
        return False
    return head[:2] == b"MZ" or head[:4] == b"" + bytes([0x1B]) + b"Lua"


def _shift(value: Any, amount: Any) -> int:
    """``SHIFT()``。正で左、負で右へ算術シフト。"""
    number = int(value)
    count = int(amount)
    return number << count if count >= 0 else number >> -count


def _strip_bom(source: str) -> str:
    return source.lstrip("﻿")


def blank_image(width: int, height: int) -> np.ndarray:
    """透明な画像。スクリプトが自分で中身を作る場合の出発点。"""
    return np.zeros((max(1, height), max(1, width), 4), dtype=np.uint8)
