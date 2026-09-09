"""AviUtl 互換。

- :mod:`~novaedit.compat.aviutl.exo` — ``.exo`` / ``.exa`` の読み込み
- :mod:`~novaedit.compat.aviutl.control` — 制御文字からパラメータ定義へ
- :mod:`~novaedit.compat.aviutl.catalog` — スクリプトの走査と登録
- :mod:`~novaedit.compat.aviutl.runtime` — Lua ランタイム（サンドボックス付き）
- :mod:`~novaedit.compat.aviutl.objapi` — ``obj`` API
- :mod:`~novaedit.compat.aviutl.report` — 未対応 API の記録

Lua は既定で入っている（``lupa``）。互換機能を使わない起動で読み込まれることは
無いよう、重い import はすべて関数の中に閉じてある。
"""

from novaedit.compat.aviutl.catalog import PREFIX, ScriptCatalog, ScriptEntry
from novaedit.compat.aviutl.control import ScriptHeader, parse_control, split_scripts
from novaedit.compat.aviutl.exo import ExoFile, ExoObject, ExoParseError, load_exo, parse_exo
from novaedit.compat.aviutl.report import CompatibilityReport, global_report

__all__ = [
    "PREFIX",
    "CompatibilityReport",
    "ExoFile",
    "ExoObject",
    "ExoParseError",
    "ScriptCatalog",
    "ScriptEntry",
    "ScriptHeader",
    "global_report",
    "load_exo",
    "parse_control",
    "parse_exo",
    "split_scripts",
]


def is_script_effect(kind: str) -> bool:
    """そのエフェクト種別が AviUtl スクリプトか。"""
    return kind.startswith(PREFIX)
