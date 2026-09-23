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

1. ``RequiredVersion()``（任意）で必要な本体の版を聞く こちらが写した並び
   （:data:`native.HOST_VERSION`）より新しい版を求めるプラグインは読まない
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

- 読むのは利用者の AviUtl2 の ``Plugin`` フォルダの中だけ（直下と 1 段下
  :func:`plugin_files`）
- 設定（:func:`native.set_enabled`）で切れる
- 読めない・登録が無い・Windows でない場合は落とさずに記録へ残す

**1 つ読むために全部を読むことになる** 何を登録するかは読んでみるまで
分からないため ``obj.module`` が呼ばれて初めて読むので、AviUtl2 のスクリプトを
使わない限り動かない とはいえ副作用はある 手元の 5 本を通して観察したところ

- ``comfont`` だけがスクリプトモジュールを登録した（``compositefont``）
- ``AIEdit`` ``WhisperAutoSub`` は ``register_window_client`` に本物の
  ウィンドウハンドルを渡してきた＝読んだ時点でウィンドウを作っている
- ``VariableFont`` はフィルタプラグインを登録しようとした（こちらは受け取らない）

どれも落ちはしなかったが、**他人の DLL を走らせている**ことに変わりはない
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import weakref
from pathlib import Path
from typing import Any

from sashimono.compat.aviutl.native import (
    NativeModule,
    NativeModuleError,
    enabled,
    host_version_for,
    is_native_x64,
)
from sashimono.compat.aviutl.report import CompatibilityReport, global_report

__all__ = [
    "PLUGIN_SUFFIX",
    "app_data_path",
    "default_plugin_roots",
    "forget",
    "plugin_files",
    "script_modules",
    "set_app_data_path",
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
        #: 名前の無い登録を受けた回数 こちらからは引けないので使わないが、
        #: 数えて記録に残す 黙って捨てると、そのモジュールを使うスクリプトが
        #: 「見つかりません」で止まったときに原因を追えない
        self.unnamed = 0
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
            handler = _refuse(kind)
        function = kind(handler)
        self._keep.append(function)
        return function

    def _register_named(self, table: int | None, name: str | None) -> None:
        if table and name:
            self.modules[str(name)] = int(table)

    def _register_unnamed(self, table: int | None) -> None:
        # 名前の無い登録 SDK は名前の決め方を書いていないので、こちらからは
        # 引けない 名前付きだけを使う（実物の comfont.aux2 は名前付きで呼ぶ）
        if table:
            self.unnamed += 1


def _refuse(kind: Any) -> Any:
    """登録だけ受け取って何もしない窓口を作る

    返す値は「無い」 整数の窓口だけ 0 で、あとは nullptr
    整数の窓口へ ``None`` を返すと ctypes が変換に失敗し、拾えない例外として
    捨てられる 呼んだ側へ何が返るかはその時の成り行き任せになる
    （実物の AIEdit は ``get_color_code`` ``get_layout_size`` を呼ぶ）

    文字を返す ``translate`` も nullptr にする 訳す表を持っていないため
    実物（SrtImporter）は受け取った nullptr をそのままメニュー名として
    登録し直すだけで、落ちはしない こちらはメニューを出さないので困らない
    """
    if kind._restype_ is _I:
        return lambda *args: 0
    return lambda *args: None


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

#: プラグインへ渡す ``app_data_path`` の差し替え先 ``None`` なら利用者の AviUtl2 の置き場
_app_data: Path | None = None


def set_app_data_path(path: Path | None) -> None:
    """プラグインへ渡す ``app_data_path`` を差し替える ``None`` で元へ戻す

    合成フォントの見本の設定を試すための口 利用者の
    ``%PROGRAMDATA%\\aviutl2\\compositefont\\profiles.json`` を書き換えずに、
    作業フォルダの設定で組ませて確かめられる

    **まだ読んでいないプラグインにだけ効く** 渡した道はプラグインが初期化で
    受け取って持ち続け、同じ DLL は 2 度初期化しない（:func:`forget` を参照）
    読んだ後に差し替えても、プラグインは前の道を見続ける
    """
    global _app_data
    with _lock:
        _app_data = path


def app_data_path() -> Path | None:
    """プラグインへ渡す ``app_data_path`` 差し替えが無ければ利用者の AviUtl2 の置き場"""
    if _app_data is not None:
        return _app_data
    program_data = os.environ.get("PROGRAMDATA")
    return Path(program_data) / "aviutl2" if program_data else None


_modules: dict[str, NativeModule] | None = None
#: 読んだときに記録した「読めなかった理由」と回数 読み結果と一緒に覚えておく
#: 覚えないと、2 つ目からの記録の器（別のスクリプトの実行）には理由が届かず、
#: 「モジュールが見つかりません」だけが残って原因を追えない
_diagnostics: list[tuple[str, int]] = []
#: 理由をもう伝えた記録の器 器は数を数えるので、描くたびに流し直すと回数が
#: 膨らむ 同じ器には 1 度だけ伝える 器は比べる作り（eq）で hash を持たないので、
#: id を鍵に器そのものを弱く持つ（器が消えれば外れ、同じ id の別の器と取り違えない）
_told: weakref.WeakValueDictionary[int, CompatibilityReport] = weakref.WeakValueDictionary()
#: 伝えたときの器の「消した回数」 器が消されたら、同じ器でも伝え直す
#: 伝え直さないと、互換性の記録の画面で消したあとは理由が二度と戻らない
_told_at: dict[int, int] = {}
_loaded: dict[Path, Any] = {}
#: 読めたが一部を使えなかった理由 プラグインごと（実体のパス）に覚える
#: `_register` からは記録の器に触れないので、ここへ置いて `_scan` が記録する
_problems: dict[Path, list[str]] = {}
_lock = threading.RLock()


def forget() -> None:
    """探し直す 設定を変えたときと、試験で場所を差し替えたときに使う

    すでに読み込んだ DLL は放さない 同じ DLL を 2 度初期化すると、
    プラグインの中の状態が二重になる
    """
    global _modules, _diagnostics
    with _lock:
        _modules = None
        _diagnostics = []
        _told.clear()
        _told_at.clear()


def script_modules(
    roots: tuple[Path, ...] | None = None, *, report: CompatibilityReport | None = None
) -> dict[str, NativeModule]:
    """汎用プラグインが登録したスクリプトモジュール 名前で引ける

    一度だけ読む 見つからない・読めない場合は空の辞書で、呼ぶ側は
    「そういうモジュールは無い」として先へ進む
    """
    global _modules, _diagnostics
    target = report if report is not None else global_report
    with _lock:
        if roots is not None:
            return _scan(roots, target)
        if _modules is None:
            collector = CompatibilityReport()
            _modules = _scan(default_plugin_roots(), collector)
            _diagnostics = list(collector.missing.items())
            _told.clear()
            _told_at.clear()
        key = id(target)
        if _told.get(key) is not target or _told_at.get(key) != target.cleared:
            for line, count in _diagnostics:
                for _ in range(count):
                    target.note_missing(line)
            if _told.get(key) is not target:
                # 器が消えたら回数も外す 外さないと、一時の器を作るたびに
                # 整数の鍵だけが残り続け、長く動かすほど覚える量が増える
                weakref.finalize(target, _told_at.pop, key, None)
            _told[key] = target
            _told_at[key] = target.cleared
        return _modules


def plugin_files(root: Path) -> tuple[Path, ...]:
    """1 つの置き場にある汎用プラグイン 直下と 1 段下を見る

    実物の置かれ方が 2 通りある 手元の AviUtl2 では ``comfont.aux2`` が直下、
    ``AIEdit`` ``SrtImporter`` ``VariableFont`` ``WhisperAutoSub`` は
    それぞれ自分のフォルダの中 直下しか見ないと、フォルダごと配られた
    プラグインが 1 つも見つからない

    SDK の更新履歴（2026/1/25）にも「プラグインの配置場所を Plugin フォルダの
    一つ下のフォルダも対象とするようにした」とある 2 段以上下は SDK にも
    書かれておらず、手元の配布物にも例が無いので見ない 深く潜るほど、
    関係の無い DLL を掴んで走らせる危険も増える
    """
    if not root.is_dir():
        return ()
    found: list[Path] = []
    seen: set[Path] = set()
    # 直下を先に見る 同じ名前が両方にあっても、直下の物を採る（AviUtl2 の
    # 一覧でも直下が先に出る） ``seen`` は同じ実体を 2 度読まないため
    for pattern in (f"*{PLUGIN_SUFFIX}", f"*/*{PLUGIN_SUFFIX}"):
        for path in sorted(root.glob(pattern)):
            key = path.resolve()
            if key in seen:
                continue
            seen.add(key)
            found.append(path)
    return tuple(found)


def _scan(roots: tuple[Path, ...], report: CompatibilityReport) -> dict[str, NativeModule]:
    if not enabled() or sys.platform != "win32":
        return {}
    found: dict[str, NativeModule] = {}
    count = 0
    for root in roots:
        for path in plugin_files(root):
            if count >= MAX_PLUGINS:
                return found
            count += 1
            try:
                loaded = _load(path)
            except (NativeModuleError, OSError, ValueError, ctypes.ArgumentError) as exc:
                # 読めないプラグインは飛ばす 1 つの失敗で残り全部を
                # 諦めると、関係の無いプラグインのせいで合成フォントが消える
                # 黙って飛ばすと「なぜか合成フォントが効かない」で終わるので、
                # どのプラグインがどう失敗したかを残す
                report.note_missing(f"汎用プラグイン {path.name} を読めない: {exc}")
                continue
            for line in _problems.get(path.resolve(), ()):
                report.note_missing(line)
            for name, module in loaded.items():
                if name in found:
                    # 先に見つけた方（直下が先）を残す 後から上書きすると、
                    # 1 段下のプラグインが同じ名前を登録しただけで obj.module に
                    # 別の実装が渡る どちらを本体が選ぶかは SDK に書かれていないので、
                    # 取り違えに気付けるよう記録に残す
                    report.note_missing(
                        f"汎用プラグイン {path.name} のモジュール {name} は名前が重なるので使わない"
                    )
                    continue
                found[name] = module
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
        modules = _register(library, path, _problems.setdefault(resolved, []))
        _loaded[resolved] = modules
        return dict(modules)


def _register(library: Any, path: Path, problems: list[str]) -> dict[str, NativeModule]:
    # こちらより新しい本体を求めるプラグインは、ここで断る（理由は _scan が記録する）
    version = host_version_for(library, path)

    # 窓口の構造体そのものも抱えておく 渡すのはその番地なので、ここで手放すと
    # 初期化のあとに記録や設定を引きに来たプラグインが、解放済みの所を読む
    logger = _logger()
    _keep.append(logger)
    initialize_logger = getattr(library, "InitializeLogger", None)
    if initialize_logger is not None:
        initialize_logger.argtypes = [ctypes.POINTER(_LogHandle)]
        initialize_logger(ctypes.byref(logger))

    config = _config()
    _keep.append(config)
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
        except NativeModuleError as exc:
            # 1 つのモジュールの一覧が壊れていても、同じプラグインの
            # 別のモジュールは使える 理由は _scan が記録へ残す
            problems.append(f"汎用プラグイン {path.name} のモジュール {name} を使えない: {exc}")
    if host.unnamed:
        problems.append(
            f"汎用プラグイン {path.name} の名前の無いモジュール {host.unnamed} 個"
            "（名前が分からず引けない）"
        )
    return modules


def _logger() -> _LogHandle:
    """記録の窓口 プラグインの言い分は捨てる

    画面へ出す先が無い（編集中の 1 コマに何度も呼ばれる） 窓口ごと渡さないと
    記録したいプラグインがそこで落ちるので、受け皿だけ用意する
    """
    values: dict[str, Any] = {}
    for name, kind in _LOG_SLOTS:
        function = kind(_refuse(kind))
        _keep.append(function)
        values[name] = function
    return _LogHandle(**values)


def _config() -> _ConfigHandle:
    """設定の窓口 ``app_data_path`` だけ本物を渡す

    合成フォントはここを起点に自分の設定（``compositefont\\profiles.json``）を
    読む 別の場所を指すと、利用者が AviUtl2 で作った書体の組み合わせが
    見えなくなる 残りの窓口（翻訳・配色・配置）は画面用なので 0 を返す
    見本の設定を試すときだけ :func:`set_app_data_path` で作業フォルダを指す
    """
    values: dict[str, Any] = {"app_data_path": str(app_data_path() or "")}
    for name, kind in _CONFIG_SLOTS:
        function = kind(_refuse(kind))
        _keep.append(function)
        values[name] = function
    return _ConfigHandle(**values)
