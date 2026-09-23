"""AviUtl2 のスクリプトモジュール（``.mod2`` の中身が DLL のもの）を呼ぶ

配布スクリプトの中には、重い処理を DLL に切り出しているものがある
（テレビ字幕の ``TVSubtitle.scan`` は、文字の絵から行ごとの四角を探す）
DLL の中身は公開されておらず、推測で書き直すと必ず外れる
**利用者の AviUtl2 に入っている DLL をそのまま呼ぶ**

呼び方は AviUtl ExEdit2 Plugin SDK の ``module2.h``（MIT ライセンス
Copyright (c) 2025 Kenkun）に従う DLL が出している ``GetScriptModuleTable`` から
関数の一覧を受け取り、呼ぶときは「引数を読む関数」「戻り値を積む関数」を並べた
表（``SCRIPT_MODULE_PARAM``）を渡す DLL はそれを呼んで引数を読み、結果を積む

**読んだ DLL は Sashimono と同じ権限で動く** Lua の閉じ込め（ファイルを触れない、
Python の中身へ届かない）の外に出る そのため

- 読むのは、スクリプトフォルダ（利用者が自分で置いた場所）の中の DLL だけ
  （探す側 :mod:`sashimono.compat.aviutl.runtime` が決めている）
- 設定で切れる（:func:`set_enabled`）
- DLL の中で落ちると Sashimono ごと落ちる 捕まえる手段は無い

Windows の 64bit だけ 配るのは Windows 版だけで、AviUtl2 も 64bit しか無い
"""

from __future__ import annotations

import ctypes
import struct
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "EDIT_SECTION_SLOTS",
    "HOST_VERSION",
    "NativeModule",
    "NativeModuleError",
    "PixelData",
    "enabled",
    "host_version_for",
    "load",
    "set_enabled",
]

#: こちらが名乗る本体の版 ``InitializePlugin`` へ渡し、``RequiredVersion`` と比べる
#:
#: **写した表の並びが、どの版の本体まで含むか**を表す 2010601 は AviUtl2 v2.1.6a
#: 根拠は実物だけ
#:
#: - v2.1.6a の ``aviutl2.exe`` に 32bit の定数 2010601 が 6 か所あり、同じ版が書いた
#:   プロジェクト（.aup2）の見出しも ``version=2010601``
#: - SDK の見本（WindowClient.cpp）の ``RequiredVersion`` は 2003300 で、SDK に
#:   ``RequiredVersion`` が足された日（2026/2/14）は本体の「2.00 beta33」の日
#: - 手元の配布物の値 2003300 2004900 2010100 2010400 も、上の読み方
#:   （2 / 01 / 06 / 01＝a）で本体の履歴にある版に当たる VariableFont の 2004900
#:   （beta49 2026/6/7）は、SDK が ``register_font_collection`` を足した日と同じ
#:
#: 写した SDK（2026/9/19 までの履歴）は v2.1.6a（2026/8/22）より新しいので、
#: v2.1.6a までに足された枠は全部並んでいる 9/19 の分がどの版の番号に当たるかは
#: 手元の本体の履歴に無く分からないため、名乗るのは確かめられる v2.1.6a までにする
HOST_VERSION = 2010601

#: 1 つのモジュールから受け取る関数の数の上限 終わりの印が無い一覧を、
#: どこまでも読みに行かないため 手元の配布物は 1 つ（TVSubtitle は scan だけ）
MAX_FUNCTIONS = 1024

#: PE の機械の種類 x64 以外の DLL は 64bit の Sashimono へ読み込めない
_MACHINE_AMD64 = 0x8664

#: ``EDIT_SECTION``（plugin2.h）の枠の数 先頭の 1 つは ``EDIT_INFO* info``
#: （データへの番地）で、残りの 82 は関数 中身は編集中のプロジェクトを触る物なので、
#: こちらには無い 関数の枠を 82 と数えて表を作ると、先頭をデータとして読む
#: プラグインが関数の機械語を読み、末尾の関数を引くプラグインは表の外を読む
#: 合成フォントが落ちた番地 0x170 が ``get_font``（46 番目の枠 8 バイト刻み）と
#: 一致することで、この並びが実物と合っていることも確かめてある
EDIT_SECTION_SLOTS = 83


class NativeModuleError(RuntimeError):
    """DLL を読めない・呼べない・DLL が失敗を伝えてきた"""


_enabled = True


def enabled() -> bool:
    """DLL のモジュールを読んでよいか 本人の設定"""
    return _enabled


def set_enabled(value: bool) -> None:
    """設定を変える 描画と書き出しの両方がここを見る"""
    global _enabled
    _enabled = bool(value)


@dataclass(eq=False)
class PixelData:
    """``obj.getpixeldata`` の戻り値 DLL へ渡す画素の置き場

    Lua には中身の見えない値として渡る（AviUtl でもポインタで、Lua からは
    読めない） DLL が書き換えることがあるので、並びが連続した複製を持つ
    """

    pixels: np.ndarray
    #: ``"rgba"`` か ``"bgra"``
    order: str = "rgba"

    def __post_init__(self) -> None:
        self.pixels = np.ascontiguousarray(self.pixels, dtype=np.uint8)

    @property
    def width(self) -> int:
        return int(self.pixels.shape[1])

    @property
    def height(self) -> int:
        return int(self.pixels.shape[0])

    @property
    def address(self) -> int:
        return int(self.pixels.ctypes.data)


@dataclass
class _Call:
    """1 回の呼び出しの引数と結果 C から呼ばれる関数がここを見る"""

    args: Sequence[Any]
    results: list[Any] = field(default_factory=list)
    error: str | None = None
    #: C へ返した文字列 呼び出しが終わるまで生かしておく
    strings: list[ctypes.Array[ctypes.c_char]] = field(default_factory=list)

    def arg(self, index: int) -> Any:
        return self.args[index] if 0 <= index < len(self.args) else None

    def string(self, value: Any) -> int | None:
        if not isinstance(value, str):
            return None
        buffer = ctypes.create_string_buffer(value.encode("utf-8"))
        self.strings.append(buffer)
        return ctypes.addressof(buffer)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _key(value: bytes | None) -> str:
    return value.decode("utf-8", "replace") if value else ""


def _item(table: Any, key: Any) -> Any:
    return table.get(key) if isinstance(table, Mapping) else None


def _array(table: Any, index: int) -> Any:
    """配列の要素 C は 0 から、Lua の表は 1 から数える"""
    return _item(table, index + 1)


def _length(table: Any) -> int:
    """Lua の ``#`` と同じ 1 から途切れずに続く数"""
    if not isinstance(table, Mapping):
        return 0
    count = 0
    while (count + 1) in table:
        count += 1
    return count


def _type(value: Any) -> int:
    """``PARAM_TYPE`` の値 module2.h の並び"""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 1
    if isinstance(value, PixelData):
        return 2
    if isinstance(value, int | float):
        return 3
    if isinstance(value, str):
        return 4
    if isinstance(value, Mapping):
        return 5
    return 7


# --- module2.h の並びをそのまま写す ---

_c_str = ctypes.c_char_p
_ptr = ctypes.c_void_p
_i, _d, _b = ctypes.c_int, ctypes.c_double, ctypes.c_bool
_F = ctypes.CFUNCTYPE

#: 並びが 1 つでもずれると、DLL は別の関数を呼ぶ（黙って誤った値を読む）
_SLOTS: tuple[tuple[str, Any], ...] = (
    ("get_param_num", _F(_i)),
    ("get_param_int", _F(_i, _i)),
    ("get_param_double", _F(_d, _i)),
    ("get_param_string", _F(_ptr, _i)),
    ("get_param_data", _F(_ptr, _i)),
    ("get_param_table_int", _F(_i, _i, _c_str)),
    ("get_param_table_double", _F(_d, _i, _c_str)),
    ("get_param_table_string", _F(_ptr, _i, _c_str)),
    ("get_param_array_num", _F(_i, _i)),
    ("get_param_array_int", _F(_i, _i, _i)),
    ("get_param_array_double", _F(_d, _i, _i)),
    ("get_param_array_string", _F(_ptr, _i, _i)),
    ("push_result_int", _F(None, _i)),
    ("push_result_double", _F(None, _d)),
    ("push_result_string", _F(None, _c_str)),
    ("push_result_data", _F(None, _ptr)),
    ("push_result_table_int", _F(None, ctypes.POINTER(_c_str), ctypes.POINTER(_i), _i)),
    ("push_result_table_double", _F(None, ctypes.POINTER(_c_str), ctypes.POINTER(_d), _i)),
    ("push_result_table_string", _F(None, ctypes.POINTER(_c_str), ctypes.POINTER(_c_str), _i)),
    ("push_result_array_int", _F(None, ctypes.POINTER(_i), _i)),
    ("push_result_array_double", _F(None, ctypes.POINTER(_d), _i)),
    ("push_result_array_string", _F(None, ctypes.POINTER(_c_str), _i)),
    ("set_error", _F(None, _c_str)),
    ("get_param_boolean", _F(_b, _i)),
    ("push_result_boolean", _F(None, _b)),
    ("get_param_table_boolean", _F(_b, _i, _c_str)),
    ("push_result_array_boolean", _F(None, ctypes.POINTER(_b), _i)),
    ("push_result_table_boolean", _F(None, ctypes.POINTER(_c_str), ctypes.POINTER(_b), _i)),
    # 編集の情報・関数の返却・メタテーブルは使わない（手元の配布物は呼ばない）
    # 空のまま渡すと、呼ばれた時点で DLL が落ちる 関数の返却とメタテーブルの枠には、
    # 呼ばれたら失敗として記録する関数を置く（:func:`_handlers` の ``refuse``）
    # ``edit`` には :data:`_EDIT_SECTION` を置く こちらは記録せず 0 を返すだけ
    # （描画の 1 コマごとに呼ばれうるので、記録すると数が膨らむだけで役に立たない）
    ("edit", _ptr),
    ("push_result_function", _F(None, _ptr, _ptr)),
    ("deprecated_push_result_meta_table", _F(None, _ptr, _ptr, _ptr)),
    ("userdata", _ptr),
    ("push_result_meta_table", _F(None, _ptr, _ptr)),
    ("get_param_meta_table", _F(_ptr, _i, _ptr)),
    ("get_param_type", _F(_i, _i)),
)


class _Param(ctypes.Structure):
    _fields_ = list(_SLOTS)


_ModuleFunction = _F(None, ctypes.POINTER(_Param))


class _FunctionEntry(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar_p), ("func", _ModuleFunction)]


class _ModuleTable(ctypes.Structure):
    _fields_ = [("information", ctypes.c_wchar_p), ("functions", ctypes.POINTER(_FunctionEntry))]


def _handlers(call: _Call) -> dict[str, Any]:
    """表に並べる関数 どれも ``call`` の引数を読み、結果を ``call`` へ積む"""

    def as_int(value: Any) -> int:
        number = _number(value)
        return int(number) if number is not None else 0

    def as_double(value: Any) -> float:
        number = _number(value)
        return number if number is not None else 0.0

    def pairs(keys: Any, values: Any, count: int, convert: Callable[[Any], Any]) -> None:
        call.results.append({_key(keys[i]): convert(values[i]) for i in range(max(0, count))})

    def items(values: Any, count: int, convert: Callable[[Any], Any]) -> None:
        call.results.append([convert(values[i]) for i in range(max(0, count))])

    def refuse(what: str) -> Callable[..., Any]:
        def handler(*args: Any) -> None:
            del args
            call.error = call.error or f"{what} には対応していない"

        return handler

    def data(value: Any) -> int | None:
        return value.address if isinstance(value, PixelData) else None

    return {
        "get_param_num": lambda: len(call.args),
        "get_param_int": lambda i: as_int(call.arg(i)),
        "get_param_double": lambda i: as_double(call.arg(i)),
        "get_param_string": lambda i: call.string(call.arg(i)),
        "get_param_data": lambda i: data(call.arg(i)),
        "get_param_table_int": lambda i, k: as_int(_item(call.arg(i), _key(k))),
        "get_param_table_double": lambda i, k: as_double(_item(call.arg(i), _key(k))),
        "get_param_table_string": lambda i, k: call.string(_item(call.arg(i), _key(k))),
        "get_param_array_num": lambda i: _length(call.arg(i)),
        "get_param_array_int": lambda i, k: as_int(_array(call.arg(i), k)),
        "get_param_array_double": lambda i, k: as_double(_array(call.arg(i), k)),
        "get_param_array_string": lambda i, k: call.string(_array(call.arg(i), k)),
        "push_result_int": lambda v: call.results.append(int(v)),
        "push_result_double": lambda v: call.results.append(float(v)),
        "push_result_string": lambda v: call.results.append(_key(v)),
        "push_result_data": lambda v: call.results.append(v),
        "push_result_table_int": lambda k, v, n: pairs(k, v, n, int),
        "push_result_table_double": lambda k, v, n: pairs(k, v, n, float),
        "push_result_table_string": lambda k, v, n: pairs(k, v, n, _key),
        "push_result_array_int": lambda v, n: items(v, n, int),
        "push_result_array_double": lambda v, n: items(v, n, float),
        "push_result_array_string": lambda v, n: items(v, n, _key),
        "set_error": lambda m: setattr(call, "error", _key(m) or "モジュールが失敗を伝えてきた"),
        "get_param_boolean": lambda i: bool(call.arg(i)) if call.arg(i) is not None else False,
        "push_result_boolean": lambda v: call.results.append(bool(v)),
        "get_param_table_boolean": lambda i, k: bool(_item(call.arg(i), _key(k))),
        "push_result_array_boolean": lambda v, n: items(v, n, bool),
        "push_result_table_boolean": lambda k, v, n: pairs(k, v, n, bool),
        "push_result_function": refuse("関数を返すモジュール"),
        "deprecated_push_result_meta_table": refuse("メタテーブルを返すモジュール"),
        "push_result_meta_table": refuse("メタテーブルを返すモジュール"),
        "get_param_meta_table": lambda i, m: None,
        "get_param_type": lambda i: _type(call.arg(i)) if 0 <= i < len(call.args) else -1,
    }


class _Color(ctypes.Structure):
    """``EDIT_INFO::COLOR``（plugin2.h） 1 バイトずつの r g b a"""

    _fields_ = [(name, ctypes.c_ubyte) for name in ("r", "g", "b", "a")]


class _EditInfo(ctypes.Structure):
    """``EDIT_INFO``（plugin2.h） ``EDIT_SECTION`` の先頭の枠が指す先

    並びは plugin2.h の宣言どおり 大きさが違うと、後ろの項目を読むプラグインが
    領域の外を読む
    """

    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("rate", ctypes.c_int),
        ("scale", ctypes.c_int),
        ("sample_rate", ctypes.c_int),
        ("frame", ctypes.c_int),
        ("layer", ctypes.c_int),
        ("frame_max", ctypes.c_int),
        ("layer_max", ctypes.c_int),
        ("display_frame_start", ctypes.c_int),
        ("display_layer_start", ctypes.c_int),
        ("display_frame_num", ctypes.c_int),
        ("display_layer_num", ctypes.c_int),
        ("select_range_start", ctypes.c_int),
        ("select_range_end", ctypes.c_int),
        ("grid_bpm_tempo", ctypes.c_float),
        ("grid_bpm_beat", ctypes.c_int),
        ("grid_bpm_offset", ctypes.c_float),
        ("scene_id", ctypes.c_int),
        ("background", _Color),
    ]


#: ``EDIT_SECTION`` の代わりに渡す表
#:
#: 空（nullptr）のまま渡すと、編集の情報を見に来たモジュールが nullptr の
#: 先を読んで Sashimono ごと落ちる（合成フォントの ``decorate_layout`` に
#: 書体名を渡すと、``get_font`` を引きに来て落ちた）
#:
#: 先頭の枠（``info``）は 0 で埋めた ``EDIT_INFO`` を指す 関数の番地を置くと、
#: ``edit->info->width`` を読むプラグインが関数の機械語を解像度として読む
#: 範囲選択の 2 つだけは -1 にする plugin2.h で「未選択の場合は -1」と
#: 決まっていて、0 にすると 0 フレーム目を選んでいることになる
#:
#: 残りの 82 の関数の枠は、どれも同じ「何もせず 0 を返す」関数を指す
#: x64 の呼び出し規約では引数はレジスタに載り、後始末は呼ぶ側がするので、
#: 引数を取らない関数をどの枠に置いても安全に戻れる 返すのは 0＝「無い・
#: 失敗した」で、plugin2.h のどの関数もこの値を「取れなかった」として扱える
#: （返り値は最大 8 バイト 小数を返す関数は EDIT_SECTION に無い）
_EditStub = ctypes.CFUNCTYPE(ctypes.c_int64)


def _edit_section_stub() -> ctypes.Array[ctypes.c_void_p]:
    info = _EditInfo()
    info.select_range_start = -1
    info.select_range_end = -1
    stub = _EditStub(lambda: 0)
    address = ctypes.cast(stub, ctypes.c_void_p).value
    slots = [ctypes.addressof(info)] + [address] * (EDIT_SECTION_SLOTS - 1)
    table = (ctypes.c_void_p * EDIT_SECTION_SLOTS)(*slots)
    # 関数と EDIT_INFO を表にぶら下げて、表が生きている間は回収されないようにする
    # 表に入っているのは番地だけなので、これが無いと指す先だけが先に消える
    table._stub = stub  # type: ignore[attr-defined]
    table._info = info  # type: ignore[attr-defined]
    return table


_EDIT_SECTION = _edit_section_stub()


def _build_param(call: _Call) -> tuple[_Param, list[Any]]:
    """表を組み立てる 返す 2 つ目は、呼び出しが終わるまで生かしておく物"""
    keep: list[Any] = []
    handlers = _handlers(call)
    values: dict[str, Any] = {}
    for name, kind in _SLOTS:
        if name == "edit":
            values[name] = ctypes.cast(_EDIT_SECTION, ctypes.c_void_p)
            continue
        if name == "userdata":
            values[name] = None
            continue
        function = kind(handlers[name])
        keep.append(function)
        values[name] = function
    return _Param(**values), keep


class NativeModule:
    """読み込んだ 1 つの DLL"""

    def __init__(self, path: Path, library: Any, table: _ModuleTable) -> None:
        self.path = path
        self._library = library
        self.information = table.information or ""
        self._functions: dict[str, Any] = {}
        if not table.functions:
            raise NativeModuleError(f"{path.name} が関数の一覧を持っていない")
        # 上限の数まで受け取り、その次が終わりの印（名前が空の行）であることも見る
        # 上限ちょうどの数の関数を持つ正しい一覧を断らないため、1 つ多く見る
        for index in range(MAX_FUNCTIONS + 1):
            entry = table.functions[index]
            if not entry.name:
                break
            if index == MAX_FUNCTIONS:
                # 終わりの印が無い一覧 読み続けると、関係の無い場所まで
                # 関数として読みに行く
                raise NativeModuleError(f"{path.name} の関数の一覧に終わりが無い")
            if not entry.func:
                raise NativeModuleError(f"{path.name} の {entry.name} に中身が無い")
            self._functions[entry.name] = entry.func
        # 同じ DLL を描画と書き出しが同時に呼ぶことがある 中が同時に呼ばれる
        # ことに耐えるかは分からないので、1 つずつにする
        self._lock = threading.Lock()

    @classmethod
    def from_address(cls, path: Path, library: Any, address: int) -> NativeModule:
        """``SCRIPT_MODULE_TABLE*`` の番地から作る

        汎用プラグイン（``.aux2``）は表をファイルから出さず、登録の引数として
        渡してくる :mod:`sashimono.compat.aviutl.plugin` がそれを受け取る
        """
        table = ctypes.cast(address, ctypes.POINTER(_ModuleTable)).contents
        return cls(path, library, table)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._functions)

    def call(self, name: str, args: Sequence[Any]) -> list[Any]:
        """関数を呼ぶ 戻り値は DLL が積んだ順

        DLL が ``set_error`` で失敗を伝えてきたら :class:`NativeModuleError`
        """
        function = self._functions.get(name)
        if function is None:
            raise NativeModuleError(f"{self.path.name} に {name} という関数が無い")
        call = _Call(args=list(args))
        param, keep = _build_param(call)
        with self._lock:
            function(ctypes.byref(param))
        del keep
        if call.error is not None:
            raise NativeModuleError(f"{self.path.name} の {name}: {call.error}")
        return call.results


_loaded: dict[Path, NativeModule] = {}
_loading = threading.Lock()


def is_native_x64(path: Path) -> bool:
    """64bit の Windows の DLL か 違う物を読むと、読み込みの時点で落ちる"""
    try:
        with path.open("rb") as file:
            head = file.read(0x40)
            if head[:2] != b"MZ" or len(head) < 0x40:
                return False
            # PE の見出しの位置は先頭の 0x3C に書いてある 決まった範囲だけを
            # 読んで探すと、前置き（DOS スタブ）の長い正しい DLL を断ることになる
            offset = struct.unpack_from("<I", head, 0x3C)[0]
            file.seek(offset)
            header = file.read(6)
    except OSError:
        return False
    if len(header) < 6 or header[:4] != b"PE\0\0":
        return False
    machine: int = struct.unpack_from("<H", header, 4)[0]
    return machine == _MACHINE_AMD64


def load(path: Path) -> NativeModule:
    """DLL を読む 同じ DLL は 1 度だけ読む（初期化を何度も呼ばない）"""
    if not enabled():
        raise NativeModuleError("DLL のモジュールを読まない設定になっている")
    if sys.platform != "win32":
        raise NativeModuleError("DLL のモジュールは Windows でしか読めない")
    resolved = path.resolve()
    with _loading:
        existing = _loaded.get(resolved)
        if existing is not None:
            return existing
        if not is_native_x64(resolved):
            raise NativeModuleError(f"{path.name} は 64bit の Windows の DLL ではない")
        try:
            library = ctypes.WinDLL(str(resolved))
        except OSError as exc:
            raise NativeModuleError(f"{path.name} を読めない: {exc}") from exc
        _initialize(library, path)
        getter = getattr(library, "GetScriptModuleTable", None)
        if getter is None:
            raise NativeModuleError(f"{path.name} はスクリプトモジュールではない")
        getter.restype = ctypes.POINTER(_ModuleTable)
        table = getter()
        if not table:
            raise NativeModuleError(f"{path.name} が関数の一覧を返さない")
        module = NativeModule(path, library, table.contents)
        _loaded[resolved] = module
        return module


def host_version_for(library: Any, path: Path) -> int:
    """初期化へ渡す本体の版 こちらより新しい本体を求める DLL は断る

    求められた版をそのまま名乗ると、DLL はその版で増えた枠（表の後ろ）が
    あるものとして、こちらの表の外を呼ぶかもしれない 呼ばれた先は
    でたらめな番地で、Sashimono ごと落ちる 読まずに断る方がましで、
    断った理由は呼ぶ側が記録へ残す
    """
    required = getattr(library, "RequiredVersion", None)
    if required is not None:
        required.restype = ctypes.c_uint32
        needed = int(required())
        if needed > HOST_VERSION:
            raise NativeModuleError(
                f"{path.name} は本体の版 {needed} 以降を求めている"
                f"（こちらが写した並びは {HOST_VERSION} まで）"
            )
    return HOST_VERSION


def _initialize(library: Any, path: Path) -> None:
    """初期化を呼ぶ 断られたら使わない（使える状態ではない、という意味）"""
    version = host_version_for(library, path)
    initialize = getattr(library, "InitializePlugin", None)
    if initialize is None:
        return
    initialize.argtypes = [ctypes.c_uint32]
    initialize.restype = ctypes.c_bool
    if not initialize(version):
        raise NativeModuleError(f"{path.name} が初期化を断った")
