"""AviUtl2 の汎用プラグイン（``.aux2``）が出すスクリプトモジュールを使う

``obj.module("名前")`` で取れるモジュールには 2 種類ある スクリプト置き場の
``.mod2``（:mod:`sashimono.compat.aviutl.native` が読む）と、**汎用プラグインが
名前を付けて登録した物** 後者はファイルとして存在しないので、名前でファイルを
探しても永遠に見つからない

合成フォント（``comfont.aux2``）がこれで、配布エイリアス「合成フォントテキスト」
「合成フォント字幕」は ``obj.module("compositefont")`` を引く 見つからないと
1 行目で落ちて、オブジェクトが真っ黒のまま出る

読み方は AviUtl ExEdit2 Plugin SDK の ``plugin2.h``（MIT ライセンス
Copyright (c) 2025 Kenkun）に従う

1. ``RequiredVersion()``（任意）で必要な本体の版を聞く
2. ``InitializeLogger`` ``InitializeConfig``（任意）で記録と設定の窓口を渡す
3. ``InitializePlugin(版)`` で初期化する
4. ``RegisterPlugin(HOST_APP_TABLE*)`` で本体側の窓口を渡す プラグインはここで
   ``register_script_module_name(表, L"名前")`` を呼んでモジュールを登録する

3 と 4 の間に ``GetCommonPluginTable()`` を呼んで名前と説明を受け取れる
**順番は守る** 実物（comfont.aux2）は ``InitializePlugin`` より先に
``GetCommonPluginTable`` を呼ぶと「Plugin not initialized」で止まり、
そのあとの初期化まで道連れに失敗する

``HOST_APP_TABLE`` の中身は、こちらには無い物ばかり（編集中のプロジェクト・
メニュー・ウィンドウ） どれも「登録を受け取るが何もしない」で返す
プラグインが実際に使えるのは、登録したスクリプトモジュールの関数だけになる

**読んだプラグインは Sashimono と同じ権限で動く** :mod:`native` と同じ倒し方で

- 読むのは利用者の AviUtl2 の ``Plugin`` フォルダの中だけ
- 設定（:func:`native.set_enabled`）で切れる
- 読めない・登録が無い・Windows でない場合は落とさずに記録へ残す
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path
from typing import Any

from sashimono.compat.aviutl.native import (
    HOST_VERSION,
    NativeModule,
    NativeModuleError,
    enabled,
    is_native_x64,
)

__all__ = [
    "PLUGIN_SUFFIX",
    "default_plugin_roots",
    "forget",
    "script_modules",
]

#: 汎用プラグインの拡張子
PLUGIN_SUFFIX = ".aux2"

#: 1 つのフォルダから読むプラグインの数の上限 壊れたフォルダを指されたときに、
#: 何百個の DLL を順に読み込んで固まらないため
MAX_PLUGINS = 64

_W = ctypes.c_wchar_p
_A = ctypes.c_char_p
_P = ctypes.c_void_p
_I = ctypes.c_int
_B = ctypes.c_bool
_F = ctypes.CFUNCTYPE

#: ``HOST_APP_TABLE``（plugin2.h）の並び 1 つでもずれると、プラグインは
#: 別の窓口を呼ぶ（引数の数が合わないまま実行され、落ち方が読めなくなる）
_HOST_SLOTS: tuple[tuple[str, Any], ...] = (
    ("set_plugin_information", _F(None, _W)),
    ("register_input_plugin", _F(None, _P)),
    ("register_output_plugin", _F(None, _P)),
    ("register_filter_plugin", _F(None, _P)),
    ("register_script_module", _F(None, _P)),
    ("register_import_menu", _F(None, _W, _P)),
    ("register_export_menu", _F(None, _W, _P)),
    ("register_window_client", _F(None, _W, _P)),
    ("create_edit_handle", _F(_P)),
    ("register_project_load_handler", _F(None, _P)),
    ("register_project_save_handler", _F(None, _P)),
    ("register_layer_menu", _F(None, _W, _P)),
    ("register_object_menu", _F(None, _W, _P)),
    ("register_config_menu", _F(None, _W, _P)),
    ("register_edit_menu", _F(None, _W, _P)),
    ("register_clear_cache_handler", _F(None, _P)),
    ("register_change_scene_handler", _F(None, _P)),
    ("register_import_menu_param", _F(None, _W, _P, _P)),
    ("register_export_menu_param", _F(None, _W, _P, _P)),
    ("register_layer_menu_param", _F(None, _W, _P, _P)),
    ("register_object_menu_param", _F(None, _W, _P, _P)),
    ("register_edit_menu_param", _F(None, _W, _P, _P)),
    ("register_file_drop_handler", _F(None, _W, _W, _P)),
    ("register_file_drop_param_handler", _F(None, _W, _W, _P, _P)),
    ("register_object_item_menu", _F(None, _W, _B, _P)),
    ("register_object_item_menu_param", _F(None, _W, _B, _P, _P)),
    ("register_script_module_name", _F(None, _P, _W)),
    ("register_font_collection", _F(None, _P)),
    ("register_event_listener", _F(None, _I, _P, _P)),
)


class _HostAppTable(ctypes.Structure):
    _fields_ = list(_HOST_SLOTS)


class _LogHandle(ctypes.Structure):
    """``LOG_HANDLE``（logger2.h） どの枠も ``(自分, 文字列)`` を取る"""


#: ``LOG_HANDLE`` の並び どの枠も ``(自分, 文字列)`` を取る
_LOG_SLOTS: tuple[tuple[str, Any], ...] = tuple(
    (name, _F(None, ctypes.POINTER(_LogHandle), _W))
    for name in ("log", "info", "warn", "error", "verbose")
)

_LogHandle._fields_ = list(_LOG_SLOTS)


class _ConfigHandle(ctypes.Structure):
    """``CONFIG_HANDLE``（config2.h） 先頭だけが値で、残りは関数"""


#: ``CONFIG_HANDLE`` の関数の並び 先頭の ``app_data_path`` は値なので別に置く
_CONFIG_SLOTS: tuple[tuple[str, Any], ...] = (
    ("translate", _F(_W, ctypes.POINTER(_ConfigHandle), _W)),
    ("get_language_text", _F(_W, ctypes.POINTER(_ConfigHandle), _W, _W)),
    ("get_font_info", _F(_P, ctypes.POINTER(_ConfigHandle), _A)),
    ("get_color_code", _F(_I, ctypes.POINTER(_ConfigHandle), _A)),
    ("get_layout_size", _F(_I, ctypes.POINTER(_ConfigHandle), _A)),
    ("get_color_code_index", _F(_I, ctypes.POINTER(_ConfigHandle), _A, _I)),
)

_ConfigHandle._fields_ = [("app_data_path", _W), *_CONFIG_SLOTS]


class _Host:
    """1 つのプラグインへ渡す本体側の窓口

    登録を受け取るだけで何もしない 受け取った関数の実体（ctypes の
    コールバック）はプラグインが持ち続けるので、ここで抱えて回収させない
    """

    def __init__(self) -> None:
        self.modules: dict[str, int] = {}
        self._keep: list[Any] = []
        self.table = _HostAppTable(**{name: self._slot(name, kind) for name, kind in _HOST_SLOTS})

    def _slot(self, name: str, kind: Any) -> Any:
        handler: Any
        if name == "register_script_module_name":
            handler = self._register_named
        elif name == "register_script_module":
            handler = self._register_unnamed
        else:
            # 何を渡されても捨てる 返り値のある窓口（create_edit_handle）は
            # nullptr を返す＝「編集中のプロジェクトは無い」
            handler = _refuse
        function = kind(handler)
        self._keep.append(function)
        return function

    def _register_named(self, table: int | None, name: str | None) -> None:
        if table and name:
            self.modules[str(name)] = int(table)

    def _register_unnamed(self, table: int | None) -> None:
        # 名前の無い登録 SDK は名前の決め方を書いていないので、こちらからは
        # 引けない 名前付きだけを使う（実物の comfont.aux2 は名前付きで呼ぶ）
        del table


def _refuse(*args: Any) -> None:
    """登録だけ受け取って何もしない 値を返す窓口には nullptr／0 が返る"""
    del args


def default_plugin_roots() -> tuple[Path, ...]:
    """汎用プラグインを探す場所 AviUtl2 が入っていればその ``Plugin``

    スクリプトと違って、こちらのアプリの中に置き場は作らない 汎用プラグインは
    AviUtl2 本体の器を前提に書かれていて、Sashimono が全部を用意できるわけでは
    ないため 「AviUtl2 にあるから使える」だけにとどめる
    """
    program_data = os.environ.get("PROGRAMDATA")
    if not program_data:
        return ()
    return (Path(program_data) / "aviutl2" / "Plugin",)


#: プラグインへ渡した窓口の置き場 プロセスが終わるまで放さない
_keep: list[Any] = []


_modules: dict[str, NativeModule] | None = None
_loaded: dict[Path, Any] = {}
_lock = threading.RLock()


def forget() -> None:
    """探し直す 設定を変えたときと、試験で場所を差し替えたときに使う

    すでに読み込んだ DLL は放さない 同じ DLL を 2 度初期化すると、
    プラグインの中の状態が二重になる
    """
    global _modules
    with _lock:
        _modules = None


def script_modules(roots: tuple[Path, ...] | None = None) -> dict[str, NativeModule]:
    """汎用プラグインが登録したスクリプトモジュール 名前で引ける

    一度だけ読む 見つからない・読めない場合は空の辞書で、呼ぶ側は
    「そういうモジュールは無い」として先へ進む
    """
    global _modules
    with _lock:
        if _modules is None or roots is not None:
            found = _scan(roots if roots is not None else default_plugin_roots())
            if roots is not None:
                return found
            _modules = found
        return _modules


def _scan(roots: tuple[Path, ...]) -> dict[str, NativeModule]:
    if not enabled() or sys.platform != "win32":
        return {}
    found: dict[str, NativeModule] = {}
    count = 0
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob(f"*{PLUGIN_SUFFIX}")):
            if count >= MAX_PLUGINS:
                return found
            count += 1
            try:
                found.update(_load(path))
            except (NativeModuleError, OSError, ValueError, ctypes.ArgumentError):
                # 読めないプラグインは飛ばす 1 つの失敗で残り全部を
                # 諦めると、関係の無いプラグインのせいで合成フォントが消える
                continue
    return found


def _load(path: Path) -> dict[str, NativeModule]:
    """プラグインを 1 つ読み、登録されたモジュールを名前で返す"""
    resolved = path.resolve()
    with _lock:
        cached = _loaded.get(resolved)
        if cached is not None:
            return dict(cached)
        if not is_native_x64(resolved):
            raise NativeModuleError(f"{path.name} は 64bit の Windows の DLL ではない")
        try:
            library = ctypes.WinDLL(str(resolved))
        except OSError as exc:
            raise NativeModuleError(f"{path.name} を読めない: {exc}") from exc
        modules = _register(library, path)
        _loaded[resolved] = modules
        return dict(modules)


def _register(library: Any, path: Path) -> dict[str, NativeModule]:
    version = HOST_VERSION
    required = getattr(library, "RequiredVersion", None)
    if required is not None:
        required.restype = ctypes.c_uint32
        version = max(version, int(required()))

    logger = _logger()
    initialize_logger = getattr(library, "InitializeLogger", None)
    if initialize_logger is not None:
        initialize_logger.argtypes = [ctypes.POINTER(_LogHandle)]
        initialize_logger(ctypes.byref(logger))

    config = _config()
    initialize_config = getattr(library, "InitializeConfig", None)
    if initialize_config is not None:
        initialize_config.argtypes = [ctypes.POINTER(_ConfigHandle)]
        initialize_config(ctypes.byref(config))

    initialize = getattr(library, "InitializePlugin", None)
    if initialize is not None:
        initialize.argtypes = [ctypes.c_uint32]
        initialize.restype = ctypes.c_bool
        if not initialize(version):
            raise NativeModuleError(f"{path.name} が初期化を断った")

    register = getattr(library, "RegisterPlugin", None)
    if register is None:
        raise NativeModuleError(f"{path.name} は汎用プラグインではない")
    host = _Host()
    register.argtypes = [ctypes.POINTER(_HostAppTable)]
    register(ctypes.byref(host.table))
    # プラグインは窓口の表を持ち続ける 参照を手放すと、あとで呼ばれたときに
    # 回収済みのコールバックへ飛ぶ
    _keep.append(host)

    modules: dict[str, NativeModule] = {}
    for name, address in host.modules.items():
        try:
            modules[name] = NativeModule.from_address(path, library, address)
        except NativeModuleError:
            # 1 つのモジュールの一覧が壊れていても、同じプラグインの
            # 別のモジュールは使える
            continue
    return modules


def _logger() -> _LogHandle:
    """記録の窓口 プラグインの言い分は捨てる

    画面へ出す先が無い（編集中の 1 コマに何度も呼ばれる） 窓口ごと渡さないと
    記録したいプラグインがそこで落ちるので、受け皿だけ用意する
    """
    values: dict[str, Any] = {}
    for name, kind in _LOG_SLOTS:
        function = kind(_refuse)
        _keep.append(function)
        values[name] = function
    return _LogHandle(**values)


def _config() -> _ConfigHandle:
    """設定の窓口 ``app_data_path`` だけ本物を渡す

    合成フォントはここを起点に自分の設定（``compositefont\\profiles.json``）を
    読む 別の場所を指すと、利用者が AviUtl2 で作った書体の組み合わせが
    見えなくなる 残りの窓口（翻訳・配色・配置）は画面用なので 0 を返す
    """
    program_data = os.environ.get("PROGRAMDATA")
    path = str(Path(program_data) / "aviutl2") if program_data else ""
    values: dict[str, Any] = {"app_data_path": path}
    for name, kind in _CONFIG_SLOTS:
        function = kind(_refuse)
        _keep.append(function)
        values[name] = function
    return _ConfigHandle(**values)
