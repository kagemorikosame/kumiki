"""生成オブジェクトの定義 テキストと図形

素材を持たないクリップの中身 エフェクトと同じパラメータ仕様に載せてあるので、
設定 UI もプリセットも同じ実装で扱える

描画はここではなく :mod:`kumiki.engine.sources` が行う テキストの整形と縁取りは
Qt の描画系に任せるのが現実的で、その依存をこの層に持ち込みたくない
"""

from __future__ import annotations

from dataclasses import dataclass

from kumiki.core.model import GeneratedSource, ParamValue
from kumiki.effects.easing import EASING_KINDS, EASING_MODES
from kumiki.effects.spec import (
    CheckSpec,
    ColorSpec,
    FontSpec,
    ParameterSpec,
    ParamInput,
    SelectSpec,
    TextSpec,
    TrackSpec,
)

__all__ = [
    "FRAMEBUFFER",
    "SHAPE",
    "TEXT",
    "TRANSITION",
    "SourceDefinition",
    "source_registry",
]


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    """1 種類の生成オブジェクト"""

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
        # AviUtl の「文字揃え」は横と縦の 2 つを 1 つにまとめた呼び方をする
        # （``中央揃え[下]`` など） こちらは別々に持つ まとめると、片方だけを
        # 変えたいときに全部の組み合わせを並べることになる
        SelectSpec(
            "valign",
            "縦の基準",
            (("top", "上"), ("middle", "中"), ("bottom", "下")),
            "middle",
        ),
        TrackSpec("line_spacing", "行間", -50, 200, 0, step=1, unit="px"),
        TrackSpec("letter_spacing", "字間", -20, 100, 0, step=1, unit="px"),
        TrackSpec("border_width", "縁取りの太さ", 0, 64, 0, step=1, unit="px"),
        ColorSpec("border_color", "縁取りの色", (0.0, 0.0, 0.0, 1.0)),
        # 影は「文字の飾り」として文字と一緒に描く クリップ全体に掛ける
        # 影エフェクトとは別物で、こちらは 1 文字ずつの輪郭に付く
        TrackSpec("shadow_x", "影の X", -200, 200, 0, step=1, unit="px"),
        TrackSpec("shadow_y", "影の Y", -200, 200, 0, step=1, unit="px"),
        TrackSpec("shadow_blur", "影のぼかし", 0, 64, 0, step=1, unit="px"),
        ColorSpec("shadow_color", "影の色", (0.0, 0.0, 0.0, 1.0)),
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
                ("inscribed_triangle", "三角形（円に内接）"),
                ("fan", "扇"),
                ("arrow", "矢印"),
                ("superformula", "スーパーフォーミュラ"),
                ("polyline", "線"),
            ),
            "rect",
        ),
        TrackSpec("width", "幅", 1, 8000, 400, step=1, unit="px"),
        TrackSpec("height", "高さ", 1, 8000, 400, step=1, unit="px"),
        ColorSpec("color", "色", (1.0, 1.0, 1.0, 1.0)),
        TrackSpec("corner_radius", "角の丸み", 0, 500, 24, step=1, unit="px"),
        TrackSpec("line_width", "線の太さ", 0, 200, 0, step=1, unit="px"),
        CheckSpec("outline_only", "線のみ", False),
        TrackSpec("span", "扇の角度", 0, 360, 360, unit="度"),
        TrackSpec("bar_length", "矢印の軸の長さ", 0, 1000, 50, unit="%"),
        TrackSpec("bar_thickness", "矢印の軸の太さ", 0, 1000, 50, unit="%"),
        TrackSpec("formula_m", "スーパーフォーミュラ M", 0, 100, 4, step=0.1),
        TrackSpec("formula_n", "スーパーフォーミュラ N", 0.05, 100, 1, step=0.05),
        TextSpec("points", "線の点（x,y;x,y 中心から、下が正）", "", multiline=False),
        SelectSpec(
            "line_type", "線の種類", (("straight", "直線"), ("quadratic", "2 次ベジェ")), "straight"
        ),
        CheckSpec("closed", "線を閉じる", False),
        ColorSpec("fill_color", "線の中の色", (1.0, 1.0, 1.0, 0.0)),
        TextSpec("dash", "破線（線の太さに対する長さ、カンマ区切り）", "", multiline=False),
        TrackSpec("pos_x", "X", -4000, 4000, 0, step=1, unit="px"),
        TrackSpec("pos_y", "Y", -4000, 4000, 0, step=1, unit="px"),
        TrackSpec("rotation", "回転", -3600, 3600, 0, unit="度"),
    ),
)


#: それまでに重ねた画面を、そのまま素材として使う（YMM4 の ``FrameBufferItem``）
#: 下にある絵へぼかしや色調補正を掛けた帯を作るのに使われる 絵は CPU では作らず、
#: レンダラが GPU の中で写し取る（:mod:`kumiki.engine.render.renderer`）
FRAMEBUFFER = SourceDefinition(kind="framebuffer", label="フレームバッファ")


#: 下のトラックの絵を、前の場面から後の場面へ切り替える（YMM4 の ``TransitionItem``）
#: 前の場面はクリップに掛けたエフェクト、後の場面は ``Clip.after_effects`` を通す
#: 絵はレンダラが GPU の中で作る（:mod:`kumiki.engine.render.renderer`）
TRANSITION = SourceDefinition(
    kind="transition",
    label="場面切り替え",
    parameters=(
        SelectSpec(
            "style",
            "切り替え方",
            (
                ("switch", "切り替え"),
                ("fade", "クロスフェード"),
                ("push", "押し出し"),
                ("slide", "スライド"),
                ("overlay", "重ねる"),
            ),
            "fade",
        ),
        TrackSpec("angle", "向き", -360, 360, 0, unit="度"),
        SelectSpec(
            "target", "動かす・手前にする場面", (("before", "前"), ("after", "後")), "after"
        ),
        SelectSpec("easing", "イージング", EASING_KINDS, "linear"),
        SelectSpec("easing_mode", "イージングの向き", EASING_MODES, "in"),
    ),
)


class SourceRegistry:
    """生成オブジェクトの一覧"""

    def __init__(self, definitions: tuple[SourceDefinition, ...]) -> None:
        self._definitions = {definition.kind: definition for definition in definitions}

    def get(self, kind: str) -> SourceDefinition | None:
        return self._definitions.get(kind)

    def all(self) -> tuple[SourceDefinition, ...]:
        return tuple(self._definitions.values())

    def __contains__(self, kind: object) -> bool:
        return kind in self._definitions


source_registry = SourceRegistry((TEXT, SHAPE, FRAMEBUFFER, TRANSITION))
