"""AviUtl スクリプトを走らせる Lua ランタイム

**Lua 5.1 で動かす** AviUtl 本体が 5.1 で、配布スクリプトは ``unpack`` や
``setfenv`` のような 5.1 の書き方を普通に使う 5.4 で動かすとそこで落ちるので、
lupa が同梱している LuaJIT 2.1（5.1 互換）を使い、無ければ Lua 5.1 へ落ちる

**サンドボックスを掛ける** 配布スクリプトは他人が書いたコードで、それを読み込む
だけで実行される ``io`` ``os`` ``require`` ``dofile`` を外し、ファイルにも
プロセスにも触れないようにしてある 実行時間にも上限を置き、無限ループで
編集画面ごと固まらないようにする
"""

from __future__ import annotations

import ctypes
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from sashimono.compat.aviutl import native, plugin
from sashimono.compat.aviutl.control import ScriptHeader
from sashimono.compat.aviutl.embedded import EMIT, build_source, has_embedded, literal_text
from sashimono.compat.aviutl.encoding import read_text
from sashimono.compat.aviutl.objapi import (
    DrawCall,
    ObjApi,
    ObjectState,
    hsv_to_number,
    lua_text,
    rgb_to_number,
)
from sashimono.compat.aviutl.report import CompatibilityReport, global_report

__all__ = ["LuaError", "LuaScriptRuntime", "ScriptResult", "lua_available"]

#: 1 回の実行に許す命令数 超えたら打ち切る
#:
#: 実測で、2000 回まわるスクリプトが 0.06ms この上限に当たるまで走らせても
#: 9ms なので、壊れたスクリプトを掴んでも再生が止まるのではなく重くなるだけで済む
INSTRUCTION_LIMIT = 5_000_000

#: ``obj`` を Lua のテーブルとして見せるための下準備
#: メタテーブル経由にすることで、読み書きのすべてを Python 側で受けられる
#: Python のオブジェクトをそのまま渡す手もあるが、AviUtl のスクリプトは
#: ``obj`` をテーブルとして扱う（``obj.track0`` など）ので、テーブルにしておく
_BIND_OBJ = """
function(get, set)
  obj = setmetatable({}, {
    __index = function(_, key) return get(key) end,
    __newindex = function(_, key, value) set(key, value) end,
  })
end
"""

#: モジュールとして読む拡張子 テキストの Lua を先に探す
MODULE_SUFFIXES = (".lua", ".mod", ".mod2")

#: DLL へ渡す表を写すときの深さの上限 配布物が渡すのは 2 段（頂点の表の表）まで
NATIVE_TABLE_DEPTH = 8

#: 書き出しの手前で Lua の中のまま長さを数える Python へ渡してから数えると、
#: Lua に許した大きさの文字列を Python 側にも丸ごと写してから断ることになる
#: 上限はバイト数（UTF-8 の 1 文字は最大 4 バイト） 文字数は Python 側でも数える
_LIMIT_EMIT = """
function(sink, limit)
    local written = 0
    return function(value)
        local text = tostring(value)
        written = written + #text
        if written > limit then
            error("書き出す文字が多すぎます")
        end
        sink(text)
    end
end
"""

#: 命令数に上限を掛けて呼ぶための包み 無限ループを書いたスクリプトは実在する
#:
#: フックはコルーチンごとに掛かる スクリプトが自分で作ったコルーチンに掛けないと、
#: その中の無限ループで編集画面が戻らなくなる ``coroutine.create`` と ``wrap`` を
#: 差し替えて、実行中に作られたコルーチンにも同じフックを掛け、命令数は全体で数える
#:
#: ``debug.sethook`` はここで取っておき、スクリプトからは見えなくする 見えると
#: フックを外して上限から逃げられる
_GUARD = """
(function()
  local sethook = debug.sethook
  local create = coroutine.create
  local resume = coroutine.resume
  local unpack = unpack or table.unpack
  local STEP = 1000
  local active = nil

  local function guarded_create(fn)
    local co = create(fn)
    if active then sethook(co, active, "", STEP) end
    return co
  end

  coroutine.create = guarded_create
  coroutine.wrap = function(fn)
    local co = guarded_create(fn)
    return function(...)
      local results = {resume(co, ...)}
      if not results[1] then error(results[2], 0) end
      return unpack(results, 2)
    end
  end
  debug = {traceback = debug.traceback, getinfo = debug.getinfo}

  -- 文字列のパターン照合は C の中で回るので、命令数のフックが掛からない
  -- ``.-`` を何個も並べたパターンは、文字列の長さの個数乗の手間になり、数百文字でも
  -- 戻らない 繰り返し記号の数から手間を見積もり、重すぎるものは照合せずに断る
  local PATTERN_BUDGET = 1e10
  local function quantifiers(pattern)
    local count, i, n = 0, 1, #pattern
    while i <= n do
      local c = pattern:sub(i, i)
      if c == "%" then
        i = i + 2
      elseif c == "[" then
        i = i + 1
        if pattern:sub(i, i) == "^" then i = i + 1 end
        if pattern:sub(i, i) == "]" then i = i + 1 end
        while i <= n and pattern:sub(i, i) ~= "]" do
          if pattern:sub(i, i) == "%" then i = i + 1 end
          i = i + 1
        end
        i = i + 1
      else
        if c == "*" or c == "+" or c == "-" or c == "?" then count = count + 1 end
        i = i + 1
      end
    end
    return count
  end
  local function checked(original, plain_at)
    return function(s, pattern, ...)
      local plain = plain_at and select(plain_at - 2, ...)
      if not plain and type(s) == "string" and type(pattern) == "string" then
        -- 先頭を決めない（``^`` で始まらない）パターンは、開始位置ごとに試すので
        -- 文字列の長さのぶんだけ手間が増える それも見積もりに入れる
        local starts = pattern:sub(1, 1) == "^" and 1 or (#s + 1)
        if starts * (#s + 1) ^ quantifiers(pattern) > PATTERN_BUDGET then
          error("文字列のパターンが重すぎます（" .. #s .. " 文字）", 2)
        end
      end
      return original(s, pattern, ...)
    end
  end
  -- xpcall の後始末の関数は、エラーを起こした場所（上限のフックの中）で呼ばれる
  -- フックの中では次のフックが掛からないので、そこで無限ループされると止められない
  -- 巻き戻してから呼ぶ形に置き換える 呼ばれる時点のスタックは変わるが、戻り値は同じ
  local protect = pcall
  xpcall = function(fn, handler, ...)
    local results = {protect(fn, ...)}
    if results[1] then return unpack(results) end
    local _, handled = protect(handler, results[2])
    return false, handled
  end

  string.find = checked(string.find, 4)
  string.match = checked(string.match)
  string.gmatch = checked(string.gmatch)
  string.gsub = checked(string.gsub)

  return function(fn, limit)
    local used, hit = 0, false
    local previous = active
    local hook
    hook = function()
      used = used + STEP
      if used > limit then
        hit = true
        -- 超えたあとは命令ごとに止める pcall でエラーを握り潰してループを続けても、
        -- pcall の外へ戻った最初の命令で止まる
        sethook(hook, "", 1)
        error("時間切れ", 2)
      end
    end
    active = hook
    local co = guarded_create(fn)
    local ok, value = resume(co)
    active = previous
    if not ok then return value, hit, nil end
    return nil, false, value
  end
end)()
"""


#: Lua が使ってよいメモリ（バイト） 命令数の上限だけでは、``string.rep`` の 1 回で
#: 巨大な文字列を作られるのを止められない 配布ファイルを開いただけで落ちないように
#: 絵の画素は Python 側に持つので、スクリプトそのものはこれで十分足りる
LUA_MEMORY_LIMIT = 256 * 1024 * 1024

#: テキストの埋め込み Lua が書き出せる文字数 書き出しは Python 側に溜まるので、Lua の
#: メモリ上限が効かない 字幕や説明文でこれを超える文字を 1 つのテキストに出すことは無い
EMBEDDED_TEXT_LIMIT = 100_000


def _attribute_filter(obj: Any, name: Any, is_setting: bool) -> Any:
    """Lua から Python の値の属性を引くときの門番 ``_`` で始まる名前は通さない

    ``register_builtins`` を切っても、Lua へ渡した関数の ``__globals__`` から
    ``__builtins__`` の ``__import__`` まで辿れる スクリプトが使う API に ``_`` で
    始まる名前は無いので、まとめて塞いでも困らない
    """
    del obj, is_setting
    if not isinstance(name, str) or name.startswith("_"):
        raise AttributeError(f"Lua からは参照できない名前です: {name}")
    return name


def _new_runtime(module: Any) -> Any:
    """メモリ上限つきでランタイムを作る

    ``register_builtins`` も切る 既定のままだと、``os`` や ``io`` を消しても
    ``python.builtins.__import__`` から Python の何でも呼べてしまい、配布ファイルを
    開いただけで手元のファイルを読み書きされうる

    上限を付けられない版（LuaJIT）は使わない 埋め込み Lua はプロジェクトの文字から
    走るので、上限の無いランタイムでは 1 回の ``string.rep`` でソフトごと落とせる
    """
    try:
        return module.LuaRuntime(
            unpack_returned_tuples=True,
            register_eval=False,
            register_builtins=False,
            attribute_filter=_attribute_filter,
            max_memory=LUA_MEMORY_LIMIT,
        )
    except (TypeError, ValueError, RuntimeError) as exc:
        raise LuaError(f"メモリの上限を付けられない Lua です: {exc}") from exc


class LuaError(RuntimeError):
    """スクリプトの読み込みか実行に失敗した"""


@dataclass(frozen=True, slots=True)
class ScriptResult:
    """1 回の実行の結果"""

    draws: tuple[DrawCall, ...]
    #: 実行が失敗したか 失敗しても描画は元の画像で 1 回行う
    failed: bool = False
    message: str = ""
    #: 実行後の状態 値を取り出したいときに使う
    state: ObjectState | None = field(default=None, repr=False)


def lua_available() -> bool:
    """Lua ランタイムを用意できるか"""
    return _load_module() is not None


def _load_module() -> Any:
    """lupa の中から、AviUtl に一番近い Lua を選ぶ

    素の Lua 5.1 が第一候補 AviUtl 本体が使っているのがこれで、文法も
    標準ライブラリの中身も完全に一致する

    LuaJIT を先に選ばないのには理由がある JIT が有効だと、コンパイル済みの
    ループの中で ``debug`` のフックが呼ばれない 無限ループを打ち切る仕組みが
    そこだけ効かなくなるので、使うときは JIT を切る（:meth:`_install_globals`）
    """
    from importlib import import_module

    for name in ("lua51", "luajit21", "luajit20", "lua54"):
        try:
            return import_module(f"lupa.{name}")
        except ImportError:
            continue
    return None


class LuaScriptRuntime:
    """スクリプトを走らせる係

    1 つのランタイムを使い回す 作り直すと標準ライブラリの用意だけで数ミリ秒
    かかり、フレームごとに走らせる用途では効いてくる

    スレッドセーフではない 描画系統ごとに 1 つ持つこと
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
        self._lua = _new_runtime(module)
        #: 値の種類を Lua の見方で調べる（表かどうか） DLL へ渡す前に写すため
        self._lua_type = module.lua_type
        # 入れ子で取れる錠にする テキストの展開は、大域変数の差し替えと実行を
        # 1 つの錠の中で行い、その中から run を呼ぶ
        self._lock = threading.RLock()
        self._install_globals()
        # どちらも 1 度だけ組み立てる フレームごとに作り直すと、
        # コンパイルの時間がそのまま描画の遅れになる
        self._bind_obj = self._lua.eval(_BIND_OBJ)
        self._guard = self._lua.eval(_GUARD)
        self._limit_emit = self._lua.eval(_LIMIT_EMIT)
        #: Lua の ``tostring`` いま取っておく スクリプトが差し替えたあとに引くと、
        #: 差し替えた方で文字にしてしまう（書き出しは本体の決まりに従わせる）
        self._tostring = self._lua.eval("tostring")
        #: 前回置いた大域変数 次の実行で消すために覚えておく
        self._injected: set[str] = set()
        #: いま走らせているスクリプトのフォルダ モジュールの探索に使う
        self._folder: Path | None = None
        #: モジュールを探すフォルダ
        self._roots: tuple[Path, ...] = ()
        #: 読み込み済みのモジュール 名前ごとに 1 度だけ実行する
        #: 読んだモジュール **見つけたファイルの場所**で引く 名前で引くと、
        #: 別の配布物の同じ名前のモジュール（DLL を含む）を、後から走った
        #: スクリプトにも渡してしまう
        self._modules: dict[tuple[object, ...], Any] = {}
        #: どこで見つかったか スクリプトのフォルダと名前ごとに 1 度だけ探す
        #: 毎コマ呼ばれる所なので、毎回フォルダを探し直すと重くなる
        self._found: dict[tuple[object, ...], Path | None] = {}

    # --- 準備 ---

    def _install_globals(self) -> None:
        """標準の環境を整え、危ないものを外す"""
        # LuaJIT に落ちた場合は JIT を切る 切らないと、コンパイルされた
        # ループの中で命令数のフックが呼ばれず、実行時間の上限が効かない
        self._lua.execute("if jit then jit.off() end")
        globals_table = self._lua.globals()

        # ファイルとプロセスへの入口を閉じる 配布スクリプトは読み込むだけで
        # 走るので、ここを開けたままにはできない ``debug`` は上限の包み（_GUARD）が
        # ``sethook`` を取っておいてから、中身を絞る
        for name in ("io", "os", "package", "require", "dofile", "loadfile", "load", "loadstring"):
            globals_table[name] = None
        # ``package`` は中身の無い ``loaded`` だけを戻す sigma のスクリプトは読み込みの
        # 頭で ``package.loaded.bit or pcall(require,"bit")`` と書いており、``package`` が
        # nil だとそこで落ちて、効果が 1 本も動かなかった（ffi の要らない配布物 26 本） 本物の
        # ``package`` は探索先（``path`` ``cpath``）とローダーを持つので戻さない
        self._lua.execute("package = { loaded = {} }")

        globals_table["RGB"] = rgb_to_number
        globals_table["HSV"] = hsv_to_number
        globals_table["OR"] = lambda a, b: int(a) | int(b)
        globals_table["AND"] = lambda a, b: int(a) & int(b)
        globals_table["XOR"] = lambda a, b: int(a) ^ int(b)
        globals_table["NOT"] = lambda a: ~int(a)
        globals_table["SHIFT"] = _shift
        globals_table["debug_print"] = self._debug_print
        # 配布スクリプトは共通処理を別ファイルへ切り出している 読めないと
        # 本体の 1 行目で落ちるので、スクリプトフォルダの中だけを許して通す
        globals_table["require"] = self.load_module

    def _debug_print(self, *args: Any) -> None:
        """``debug_print`` の行き先 互換性の記録に混ぜておく"""
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
        emit: Any = None,
    ) -> ScriptResult:
        """スクリプトを 1 回走らせる

        失敗しても例外にしない 1 つのスクリプトの失敗でフレーム全体が真っ黒に
        なるより、そのオブジェクトだけ素通しで出る方が編集を続けられる

        ``emit`` を渡すと ``obj.mes`` が絵ではなく本文を書き出す
        テキスト欄に埋め込んだ Lua（:meth:`expand_text`）だけが使う
        """
        api = ObjApi(
            state,
            report=self._report,
            script=script,
            render_source=self._render_source,
            load_module=self.load_module,
            load_script_module=self.load_script_module,
            emit=emit,
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

    def expand_text(self, text: str, state: ObjectState, *, script: str = "テキスト") -> str:
        """テキスト欄に埋め込んだ Lua（``<?…?>``）を走らせ、画面に出す文字を返す

        ``obj`` はスクリプトと同じように使える（``obj.time`` で数え上げるなど）
        失敗したら Lua の部分を除いた文字を返す 理由は記録に残る
        """
        if not has_embedded(text):
            return text
        output: list[str] = []
        # 差し替えから戻すまでを錠の中で行う 外で差し替えると、その間に別のスレッドの
        # 描画が走ったとき、書き出しが別のテキストへ混ざる
        with self._lock:
            globals_table = self._lua.globals()
            written = 0

            def emit(value: Any) -> None:
                nonlocal written
                piece = str(value)
                written += len(piece)
                if written > EMBEDDED_TEXT_LIMIT:
                    raise LuaError(f"書き出す文字が多すぎます（{EMBEDDED_TEXT_LIMIT} 文字まで）")
                output.append(piece)

            def emit_value(value: Any) -> None:
                """``obj.mes`` の値を Lua の決まりで文字にしてから書き出す

                ``mes``（:func:`~sashimono.compat.aviutl.embedded.build_source` が
                作る方）は Lua の ``tostring`` を通る 同じテキスト欄の中で
                ``obj.mes`` だけ Python の ``str`` を通すと、``true`` が
                ``True``、``nil`` が ``None`` と出て、書き方で結果が変わる
                """
                emit(self._tostring(value))

            globals_table[EMIT] = self._limit_emit(emit, EMBEDDED_TEXT_LIMIT * 4)
            try:
                result = self.run(build_source(text), state, script=script, emit=emit_value)
            finally:
                # 残すと、次に走るスクリプトの ``mes`` がテキストの書き出しを呼ぶ
                globals_table[EMIT] = None
                globals_table["mes"] = None
        if result.failed:
            return literal_text(text)
        return "".join(output)

    def _prepare(self, api: ObjApi, header: ScriptHeader | None, state: ObjectState) -> None:
        """``obj`` を繋ぎ、名前付きの値を大域変数へ置き、``--param`` を流し込む"""
        self._bind_obj(api.get, api.set)
        self._install_values(state)
        if header is None or not header.setup:
            return
        try:
            self._lua.execute(header.setup)
        except Exception as exc:
            raise LuaError(f"--param の実行に失敗: {exc}") from exc

    def _install_values(self, state: ObjectState) -> None:
        """名前付きパラメータを大域変数として置く

        ``--track@offset_x:…`` と書いたスクリプトは、``obj.offset_x`` ではなく
        **素の ``offset_x``** を読む 実際に配布されているスクリプトはどれも
        この書き方なので、ここを用意しないと ``nil`` との演算で必ず落ちる

        前回置いた分は消す 残っていると、別のスクリプトが自分では設定して
        いない値を読めてしまい、動いたり動かなかったりする
        """
        globals_table = self._lua.globals()
        for name in self._injected - set(state.values):
            globals_table[name] = None
        self._injected = set(state.values)
        for name, value in state.values.items():
            # 読めないバイトを持つ文字は、元のバイト列にして置く（lua_text を参照）
            globals_table[name] = lua_text(value)

    # --- モジュール ---

    def set_roots(self, roots: tuple[Path, ...]) -> None:
        """モジュールを探すフォルダ スクリプトの置き場と同じ"""
        self._roots = roots
        # 探す場所が変わったので、見つけた場所の控えは使えない
        self._found.clear()

    def _locate(self, name: str, suffixes: tuple[str, ...]) -> Path | None:
        """モジュールの場所 スクリプトのフォルダと名前ごとに 1 度だけ探す"""
        key = (self._folder, name, suffixes)
        if key not in self._found:
            self._found[key] = self._find_module(name, suffixes=suffixes)
        return self._found[key]

    def load_module(self, name: str = "") -> Any:
        """``obj.module`` と ``require`` の実体

        探すのは**スクリプトフォルダの中だけ** 任意のパスを開けるようにすると、
        配布スクリプトを読み込んだだけでディスクの中身を読まれうる

        読むのは Lua のソースだけ AviUtl2 の ``.mod2`` は中身が Windows の DLL
        であることがある それは AviUtl2 の Lua に合わせて作られた native
        モジュールで、こちらでは動かせない 黙って ``nil`` を返すと「なぜか
        動かない」で終わるので、理由を記録に残す
        """
        key = str(name)
        path = self._locate(key, MODULE_SUFFIXES)
        if path is None:
            missing = ("missing", self._folder, key)
            if missing not in self._modules:
                # 1 度だけ記録する 毎コマ記録すると、1 本の不足が何百回にも数えられる
                self._report.note_missing(f'モジュール "{key}" が見つかりません')
                self._modules[missing] = None
            return None

        cache = ("lua", path.resolve())
        if cache in self._modules:
            return self._modules[cache]

        value = None
        if _is_native(path):
            # DLL は ``obj.module`` で読む（``load_script_module``） ``require`` は Lua の
            # ファイルを読む物で、DLL を渡されても Lua として走らせられない
            self._report.note_missing(
                f'モジュール "{key}" は DLL（{path.name}）なので require では読めない'
                "（obj.module で読む）"
            )
        else:
            value = self._run_file(path)
        self._modules[cache] = value
        return value

    def _run_file(self, path: Path) -> Any:
        """Lua のモジュールのファイルを走らせ、返した値を受け取る 読めなければ ``nil``"""
        try:
            text, _ = read_text(path)
            # 上限の包みの中で走らせる Python から直に実行すると命令数のフックが
            # 掛からず、モジュールの無限ループで固まる
            return self._call_guarded(self._compile(text, path.name))
        except LuaError as exc:
            self._report.note_failure(path.name, f"モジュールを読めない: {exc}")
        except Exception as exc:
            self._report.note_failure(path.name, f"モジュールを読めない: {exc}")
        return None

    def load_script_module(self, name: str = "") -> Any:
        """``obj.module`` の実体 スクリプトモジュール（``.mod2``）を読む

        ``require`` とは探す物が違う 仕様書（lua.txt）では ``obj.module`` は
        ``.mod2`` の関数を取り出す物 同じ名前の ``.lua`` も置いてある配布物が
        あり（テレビ字幕は ``TVSubtitle.lua`` と ``TVSubtitle.mod2``）、``require``
        と同じく ``.lua`` を先に拾うと、``obj.module`` に Lua の方が返って
        DLL の関数（``scan``）が nil になる

        ``.mod2`` が無ければ ``require`` と同じ探し方に戻す これまで ``obj.module``
        で ``.lua`` を読んでいたスクリプトを壊さない
        """
        key = str(name)
        provided = self._plugin_module(key)
        if provided is not None:
            return provided
        path = self._locate(key, (".mod2",))
        if path is None:
            return self.load_module(key)

        # 設定の入り切りも鍵に入れる 入れないと、切ったあとも読んであった DLL の
        # 関数が返り続け、切った意味が無い
        cache = ("mod2", path.resolve(), native.enabled())
        if cache in self._modules:
            return self._modules[cache]

        # 見つけた .mod2 そのものを読む 名前で探し直すと、同じ名前の .lua を拾う
        value = self._native_module(path, key) if _is_native(path) else self._run_file(path)
        self._modules[cache] = value
        return value

    def _plugin_module(self, name: str) -> Any:
        """汎用プラグイン（``.aux2``）が名前を付けて登録したモジュール

        ファイルとしては存在しないので、先にここを見ないと
        「見つかりません」で終わる（合成フォントの ``compositefont``）
        プラグインを読まない設定のときと、AviUtl2 が入っていないときは ``None``
        で、ファイルを探す従来の道へ落ちる
        """
        if not native.enabled():
            return None
        try:
            modules = plugin.script_modules(report=self._report)
        except Exception as exc:  # pragma: no cover - 読み込みは実物が要る
            # プラグインの読み込みは他人の DLL を走らせる 何が出てくるか
            # 分からないので、ここで止めてスクリプト側は素の道へ進ませる
            self._report.note_missing(f"汎用プラグインを読めない: {exc}")
            return None
        module = modules.get(name)
        if module is None:
            return None
        cache = ("aux2", module.path.resolve(), name)
        if cache not in self._modules:
            functions = {
                function: self._native_function(module, function) for function in module.names
            }
            self._modules[cache] = self._lua.table_from(functions)
        return self._modules[cache]

    def _native_module(self, path: Path, name: str) -> Any:
        """DLL のモジュールを読み、Lua から呼べる表にする 読めなければ ``nil``"""
        if not native.enabled():
            self._report.note_missing(
                f'モジュール "{name}" は DLL（{path.name}）で、読まない設定になっている'
            )
            return None
        try:
            module = native.load(path)
        except (native.NativeModuleError, OSError, ValueError, ctypes.ArgumentError) as exc:
            # 関数の一覧が壊れている DLL もある 投げ返すとスクリプトごと落ちるので、
            # 読めなかったこととして記録して nil を返す
            self._report.note_missing(f'モジュール "{name}" を読めない: {exc}')
            return None
        functions = {function: self._native_function(module, function) for function in module.names}
        return self._lua.table_from(functions)

    def _native_function(self, module: native.NativeModule, name: str) -> Any:
        """DLL の関数 1 つを、Lua から呼べる形にする"""

        def call(*args: Any) -> Any:
            # 呼ぶたびに設定を見る スクリプトが関数の表を大域変数へ取っておくと、
            # 設定を切ったあとも同じ表から DLL を呼べてしまう
            if not native.enabled():
                raise LuaError(f"{module.path.name} の {name}: DLL のモジュールを読まない設定")
            converted = [self._from_lua(value) for value in args]
            try:
                results = module.call(name, converted)
            except native.NativeModuleError as exc:
                # Lua のエラーにする スクリプトの失敗として記録され、
                # そのオブジェクトだけ素通しで出る（フレーム全体は落とさない）
                raise LuaError(str(exc)) from exc
            values = tuple(self._to_lua(value) for value in results)
            if not values:
                return None
            return values[0] if len(values) == 1 else values

        return call

    def _from_lua(self, value: Any, depth: int = 0) -> Any:
        """Lua の値を、DLL へ渡せる形へ写す 表は辞書にする（配列は 1 から）

        深さに上限を掛ける 自分自身を指す表を渡されると、写しきれずに固まる
        """
        if self._lua_type(value) != "table":
            return value
        if depth >= NATIVE_TABLE_DEPTH:
            # 黙って空の表にすると、DLL は切れた引数を正しい値として読む
            # 呼ぶのをやめて、スクリプトの失敗として記録する
            raise LuaError(
                f"DLL へ渡す表が深すぎる（{NATIVE_TABLE_DEPTH} 段まで）"
                " 自分自身を指す表かもしれない"
            )
        return {key: self._from_lua(item, depth + 1) for key, item in value.items()}

    def _to_lua(self, value: Any) -> Any:
        """DLL の戻り値を Lua の値にする 配列は 1 から数える表"""
        if isinstance(value, list | dict):
            return self._lua.table_from(value)
        return value

    def _find_module(
        self, name: str, *, suffixes: tuple[str, ...] = MODULE_SUFFIXES
    ) -> Path | None:
        """モジュールのファイルを探す

        AviUtl2 の配布物では ``.mod2``、素の Lua では ``.lua`` が使われる
        スクリプト自身のフォルダを先に見るのは、同じ名前のモジュールが別の
        配布物にもありうるため
        """
        separators = ("..", ":", chr(92), "/")
        if not name or any(part in name for part in separators):
            return None

        folders = [self._folder] if self._folder is not None else []
        folders.extend(self._roots)
        for folder in folders:
            if folder is None:
                continue
            # ``require`` は ``.lua`` を先に見る 同じ名前で ``.mod2``（DLL）と
            # ``.lua`` の両方が置かれている配布物があり、``require`` が読むのは後者
            for suffix in suffixes:
                candidate = folder / f"{name}{suffix}"
                if candidate.exists():
                    return candidate
            # 1 段下も見る 配布物はフォルダごと置かれることが多い
            for suffix in suffixes:
                found = next(folder.glob(f"*/{name}{suffix}"), None)
                if found is not None:
                    return found
        return None

    def _compile(self, source: str, script: str) -> Any:
        try:
            return self._lua.eval(f"function() {_strip_bom(source)} end")
        except Exception as exc:
            raise LuaError(f"{script or 'スクリプト'} を読めません: {exc}") from exc

    def _call_guarded(self, function: Any) -> Any:
        """命令数に上限を掛けて呼ぶ

        無限ループを書いたスクリプトは実在する 掛けておかないと、編集画面が
        戻ってこなくなる
        """
        try:
            message, timed_out, value = self._guard(function, self._instruction_limit)
        except Exception as exc:
            raise LuaError(str(exc)) from exc
        if message is not None:
            raise LuaError("実行が長すぎます" if timed_out else str(message))
        return value


def _is_native(path: Path) -> bool:
    """中身が実行ファイルか、コンパイル済みの Lua か

    AviUtl2 の ``.mod2`` は Windows の DLL であることがある テキストとして
    読むと文字化けした塊を Lua に食わせることになり、意味の分からない
    構文エラーが出る 先に見分ける
    """
    try:
        head = path.open("rb").read(4)
    except OSError:
        return False
    return head[:2] == b"MZ" or head[:4] == b"" + bytes([0x1B]) + b"Lua"


def _shift(value: Any, amount: Any) -> int:
    """``SHIFT()`` 正で左、負で右へ算術シフト"""
    number = int(value)
    count = int(amount)
    return number << count if count >= 0 else number >> -count


def _strip_bom(source: str) -> str:
    return source.lstrip("﻿")


def blank_image(width: int, height: int) -> np.ndarray:
    """透明な画像 スクリプトが自分で中身を作る場合の出発点"""
    return np.zeros((max(1, height), max(1, width), 4), dtype=np.uint8)
