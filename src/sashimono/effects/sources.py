"""生成オブジェクトの定義 テキストと図形

素材を持たないクリップの中身 エフェクトと同じパラメータ仕様に載せてあるので、
設定 UI もプリセットも同じ実装で扱える

描画はここではなく :mod:`sashimono.engine.sources` が行う テキストの整形と縁取りは
Qt の描画系に任せるのが現実的で、その依存をこの層に持ち込みたくない
"""

from __future__ import annotations

from dataclasses import dataclass

from sashimono.core.model import GeneratedSource, ParamValue
from sashimono.effects.easing import EASING_KINDS, EASING_MODES
from sashimono.effects.spec import (
    CheckSpec,
    ColorSpec,
    FontSpec,
    ParameterSpec,
    ParamInput,
    SelectSpec,
    TextSpec,
    TrackSpec,
    ValueSpec,
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
        # タイマー 書式が空でなければ、文字の代わりに時間を出す（YMM4 のタイマーの図形）
        # 書式は .NET の時間の書式（h m s f、\\ で文字をそのまま出す）
        TextSpec("timer_format", "タイマーの書式", "", multiline=False),
        TrackSpec("timer_start", "タイマーの初めの値", -360000, 360000, 0, step=0.01, unit="秒"),
        TrackSpec("timer_rate", "タイマーの速さ", -10000, 10000, 100, unit="%"),
        CheckSpec("timer_countdown", "数え下げる", False),
        ValueSpec("timer_length", "数え下げる長さ", 0, minimum=0, maximum=10**9),
        TrackSpec("pos_x", "X", -4000, 4000, 0, step=1, unit="px"),
        TrackSpec("pos_y", "Y", -4000, 4000, 0, step=1, unit="px"),
    ),
)


#: 移動軌跡の先端と星空の粒に使える形 AviUtl の図形（``obj.load("figure")``）に当たる
#: 三角形は円に内接する形 AviUtl2 の先端を測ると、大きさ 48 で高さ 36・底辺 41 だった
_FIGURE_CHOICES = (
    ("ellipse", "円"),
    ("rect", "四角形"),
    ("inscribed_triangle", "三角形"),
    ("pentagon", "五角形"),
    ("hexagon", "六角形"),
    ("star", "星型"),
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
                ("concentration", "集中線"),
                ("motion_trail", "移動軌跡"),
                ("starfield", "星空"),
                ("waveform", "音声波形"),
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
        TextSpec("points", "線の点（x,y;x,y 中心から）", "", multiline=False),
        SelectSpec(
            "line_type", "線の種類", (("straight", "直線"), ("quadratic", "2 次ベジェ")), "straight"
        ),
        CheckSpec("closed", "線を閉じる", False),
        ColorSpec("fill_color", "線の中の色", (1.0, 1.0, 1.0, 0.0)),
        TextSpec("dash", "破線（線の太さに対する長さ、カンマ区切り）", "", multiline=False),
        TrackSpec("trim_start", "線を描き始める位置", 0, 100, 0, unit="%"),
        TrackSpec("trim_end", "線を描き終える位置", 0, 100, 100, unit="%"),
        TrackSpec("density", "集中線の本数", 1, 1000, 80, step=1),
        # 100% で線が隙間なく並ぶ それより上は重なる（AviUtl の濃い集中線がここを使う）
        TrackSpec("line_thickness", "集中線の太さ", 0, 400, 50, unit="%"),
        TrackSpec("line_length", "集中線の長さ", 0, 100, 70, unit="%"),
        TrackSpec("softness", "集中線のぼかし", 0, 100, 50, unit="%"),
        TrackSpec("center_gap", "集中線の真ん中の空き", 0, 4000, 0, step=1, unit="px"),
        # AviUtl の集中線は画面いっぱい YMM4 のものは「幅」の円に収まる
        # 空きの有無で分けると、空きを 0 にした AviUtl の集中線が
        # 小さな円に縮んでしまうので、届く先は別の項目で持つ
        CheckSpec("fill_frame", "集中線を画面いっぱいに", False),
        TrackSpec("flicker", "集中線の切り替え", 0, 240, 5, unit="回/秒"),
        # 移動軌跡（AviUtl2 の ``ライン(移動軌跡)``） X と Y の動きをたどって線を引く
        # 線は ``線の太さ`` の円を一定の間隔で押して作る 間隔を広げると点線になる
        TrackSpec("trail_interval", "軌跡の点の間隔", 0, 1000, 10, unit="%"),
        TrackSpec("trail_min_step", "軌跡の点の最小間隔", 0, 1000, 2, step=1, unit="px"),
        TrackSpec("trail_core", "軌跡の点の大きさ", 0, 100, 100, unit="%"),
        TrackSpec("trail_band", "軌跡の点をつなぐ帯の太さ", 0, 100, 0, unit="%"),
        # 0 なら動きの時刻どおり 正の値なら、1 フレームにその画素ずつ道をたどって伸びる
        TrackSpec(
            "trail_speed", "軌跡の伸びる速さ（0 で動きどおり）", 0, 100, 0, unit="px/フレーム"
        ),
        TrackSpec("trail_head_size", "軌跡の先端の大きさ", 0, 500, 48, step=1, unit="px"),
        TrackSpec("trail_head_angle", "軌跡の先端の角度", 0, 360, 0, unit="度"),
        # 50% で図形の中心が今の位置に来る 大きいほど進む向きへ出る
        TrackSpec("trail_head_offset", "軌跡の先端の位置", -500, 500, 70, unit="%"),
        SelectSpec("trail_head_shape", "軌跡の先端の形", _FIGURE_CHOICES, "inscribed_triangle"),
        # 星空（AviUtl2 の ``星``） 粒が奥から手前へ流れてくる
        TrackSpec("star_count", "星の数", 1, 5000, 1500, step=1),
        # 負にすると手前から奥へ流れる
        TrackSpec("star_speed", "星の速さ", -50, 50, 6, step=0.1),
        TrackSpec("star_spread", "星の広がり", 0, 50, 12, step=0.1),
        TrackSpec("star_depth", "星の奥行き", 0, 50, 20, step=0.1),
        TrackSpec("star_size", "星の大きさ", 1, 100, 30, step=1, unit="px"),
        SelectSpec("star_shape", "星の形", _FIGURE_CHOICES, "ellipse"),
        TrackSpec("star_fade_in", "星のフェードイン", 0, 10, 0.15, step=0.01, unit="秒"),
        TrackSpec("star_fade_out", "星のフェードアウト", 0, 10, 0.15, step=0.01, unit="秒"),
        # 音声波形（AviUtl2 の ``音声波形表示``） 素材の音を今の時刻から 1 画素 1 サンプルで描く
        # クリップが素材を持てばそちらを使い、無ければこの道の音を読む
        TextSpec("audio_path", "音声波形の音声ファイル", "", multiline=False),
        TrackSpec("wave_volume", "音声波形の音量", 0, 500, 100, unit="%"),
        # 周波数ごとの大きさを下から塗る（AviUtl2 の スペクトラム表示）
        CheckSpec("wave_spectrum", "音声波形をスペクトラムにする", False),
        # 0 なら 1 画素ずつ 数を決めると、その升目の数の絵に描いてから引き伸ばす
        TrackSpec("wave_columns", "音声波形の横の升目", 0, 4000, 0, step=1),
        TrackSpec("wave_rows", "音声波形の縦の升目", 0, 4000, 0, step=1),
        TrackSpec("wave_gap_x", "音声波形の升目の横のすき間", 0, 100, 0, unit="%"),
        TrackSpec("wave_gap_y", "音声波形の升目の縦のすき間", 0, 100, 0, unit="%"),
        # 素材のこのミリ秒より先は描かない（AviUtl の 再生範囲 の終わり） 負なら素材の終わりまで
        # 整数しか持てない項目なのでミリ秒 秒で持つと 80.448 秒が 80 秒に切れる
        ValueSpec(
            "audio_end_ms",
            "音声波形を読む終わり（ミリ秒 負で最後まで）",
            -1,
            minimum=-1,
            maximum=10**10,
        ),
        TrackSpec("pos_x", "X", -4000, 4000, 0, step=1, unit="px"),
        TrackSpec("pos_y", "Y", -4000, 4000, 0, step=1, unit="px"),
        TrackSpec("rotation", "回転", -3600, 3600, 0, unit="度"),
    ),
)


#: それまでに重ねた画面を、そのまま素材として使う（YMM4 の ``FrameBufferItem``）
#: 下にある絵へぼかしや色調補正を掛けた帯を作るのに使われる 絵は CPU では作らず、
#: レンダラが GPU の中で写し取る（:mod:`sashimono.engine.render.renderer`）
FRAMEBUFFER = SourceDefinition(kind="framebuffer", label="フレームバッファ")


#: 下のトラックの絵を、前の場面から後の場面へ切り替える（YMM4 の ``TransitionItem``）
#: 前の場面はクリップに掛けたエフェクト、後の場面は ``Clip.after_effects`` を通す
#: 絵はレンダラが GPU の中で作る（:mod:`sashimono.engine.render.renderer`）
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
