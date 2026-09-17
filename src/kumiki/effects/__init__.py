"""エフェクトの定義

パラメータの仕様は AviUtl のスクリプト制御文字と 1 対 1 に対応させてある
自前のエフェクトも配布スクリプトも同じ形に載るので、設定 UI の自動生成と
プリセットの保存が 1 つの実装で済む
"""

# 読み込んだ時点で標準エフェクトを一覧へ入れる 使う側が登録を忘れると、
# プロジェクトを開いたときに全部「未知のエフェクト」になる
from kumiki.effects import builtin as _builtin
from kumiki.effects import motion as _motion
from kumiki.effects import optics as _optics
from kumiki.effects import paint as _paint
from kumiki.effects import stylize as _stylize
from kumiki.effects.definition import EffectDefinition, EffectRegistry, registry
from kumiki.effects.spec import (
    CheckSpec,
    ColorSpec,
    FileSpec,
    FontSpec,
    ParameterGroup,
    ParameterKind,
    ParameterSpec,
    ParamInput,
    SelectSpec,
    TextSpec,
    TrackSpec,
    ValueSpec,
)

_builtin.register_builtin_effects()
_motion.register_motion_effects()
_stylize.register_stylize_effects()
_paint.register_paint_effects()
_optics.register_optics_effects()

__all__ = [
    "CheckSpec",
    "ColorSpec",
    "EffectDefinition",
    "EffectRegistry",
    "FileSpec",
    "FontSpec",
    "ParamInput",
    "ParameterGroup",
    "ParameterKind",
    "ParameterSpec",
    "SelectSpec",
    "TextSpec",
    "TrackSpec",
    "ValueSpec",
    "registry",
]
