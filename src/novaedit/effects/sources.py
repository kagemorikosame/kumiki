"""生成オブジェクトの定義。テキストと図形。

素材を持たないクリップの中身。エフェクトと同じパラメータ仕様に載せてあるので、
設定 UI もプリセットも同じ実装で扱える。

描画はここではなく :mod:`novaedit.engine.sources` が行う。テキストの整形と縁取りは
Qt の描画系に任せるのが現実的で、その依存をこの層に持ち込みたくない。
"""

from __future__ import annotations

from dataclasses import dataclass

from novaedit.core.model import GeneratedSource, ParamValue
from novaedit.effects.spec import (
    CheckSpec,
    ColorSpec,
    FontSpec,
    ParameterSpec,
    ParamInput,
    SelectSpec,
    TextSpec,
    TrackSpec,
)

__all__ = ["SHAPE", "TEXT", "SourceDefinition", "source_registry"]


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    """1 種類の生成オブジェクト。"""

    kind: str
    label: str
    parameters: tuple[ParameterSpec, ...] = ()

    def spec(self, name: str) -> ParameterSpec | None:
        for parameter in self.parameters:
            if parameter.name == name:
                return parameter
        return None

    def default_params(self) -> dict[str, ParamValue]:
        return {spec.name: spec.default_value() for spec in self.parameters}

    def create(self, **overrides: ParamInput) -> GeneratedSource:
        params = self.default_params()
        for name, value in overrides.items():
            spec = self.spec(name)
            if spec is not None:
                params[name] = spec.coerce(value)
        return GeneratedSource(kind=self.kind, params=params)


TEXT = SourceDefinition(
    kind="text",
    label="テキスト",
    parameters=(
        TextSpec("text", "文字", "テキスト"),
        FontSpec("font", "フォント"),
        TrackSpec("size", "サイズ", 4, 512, 64, step=1, unit="px"),
        ColorSpec("color", "色", (1.0, 1.0, 1.0, 1.0)),
        CheckSpec("bold", "太字", False),
        CheckSpec("italic", "斜体", False),
        SelectSpec(
            "align",
            "行揃え",
            (("left", "左"), ("center", "中央"), ("right", "右")),
            "center",
        ),
        TrackSpec("line_spacing", "行間", -50, 200, 0, step=1, unit="px"),
        TrackSpec("letter_spacing", "字間", -20, 100, 0, step=1, unit="px"),
        TrackSpec("border_width", "縁取りの太さ", 0, 32, 0, step=1, unit="px"),
        ColorSpec("border_color", "縁取りの色", (0.0, 0.0, 0.0, 1.0)),
        CheckSpec("vertical", "縦書き", False),
        TrackSpec("reveal", "文字送り", 0, 100, 100, step=1, unit="%"),
        TrackSpec("pos_x", "X", -4000, 4000, 0, step=1, unit="px"),
        TrackSpec("pos_y", "Y", -4000, 4000, 0, step=1, unit="px"),
    ),
)


SHAPE = SourceDefinition(
    kind="shape",
    label="図形",
    parameters=(
        SelectSpec(
            "shape",
            "種類",
            (
                ("rect", "矩形"),
                ("rounded", "角丸矩形"),
                ("ellipse", "楕円"),
                ("triangle", "三角形"),
                ("pentagon", "五角形"),
                ("hexagon", "六角形"),
                ("star", "星"),
                ("background", "背景"),
            ),
            "rect",
        ),
        TrackSpec("width", "幅", 1, 8000, 400, step=1, unit="px"),
        TrackSpec("height", "高さ", 1, 8000, 400, step=1, unit="px"),
        ColorSpec("color", "色", (1.0, 1.0, 1.0, 1.0)),
        TrackSpec("corner_radius", "角の丸み", 0, 500, 24, step=1, unit="px"),
        TrackSpec("line_width", "線の太さ", 0, 200, 0, step=1, unit="px"),
        CheckSpec("outline_only", "線のみ", False),
        TrackSpec("pos_x", "X", -4000, 4000, 0, step=1, unit="px"),
        TrackSpec("pos_y", "Y", -4000, 4000, 0, step=1, unit="px"),
        TrackSpec("rotation", "回転", -3600, 3600, 0, unit="度"),
    ),
)


class SourceRegistry:
    """生成オブジェクトの一覧。"""

    def __init__(self, definitions: tuple[SourceDefinition, ...]) -> None:
        self._definitions = {definition.kind: definition for definition in definitions}

    def get(self, kind: str) -> SourceDefinition | None:
        return self._definitions.get(kind)

    def all(self) -> tuple[SourceDefinition, ...]:
        return tuple(self._definitions.values())

    def __contains__(self, kind: object) -> bool:
        return kind in self._definitions


source_registry = SourceRegistry((TEXT, SHAPE))
