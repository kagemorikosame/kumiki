"""``.exo`` / ``.exa`` の中身を、こちらのモデルへ写す

AviUtl のオブジェクトは「中身 1 つ + フィルタの列」でできている こちらの
:class:`~sashimono.core.model.Clip` も「生成物または素材 + エフェクトの列」なので、
構造はそのまま対応する 写すのは値の名前と単位だけ

対応が無いものは**捨てずに記録する**（:mod:`sashimono.compat.aviutl.report`）
読めなかったことに気付けないまま「なんとなく違う絵」が出るのが一番困る
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from sashimono.compat.aviutl.encoding import decode_utf16_hex
from sashimono.compat.aviutl.exo import ExoEntry, ExoFile, ExoObject
from sashimono.compat.aviutl.motion import (
    FLAG_EXPRESSION,
    FLAG_SCRIPT,
    Motion,
    animated_value,
    parse_motion,
)
from sashimono.compat.aviutl.report import CompatibilityReport, global_report
from sashimono.compat.decoration import decoration_params, find_decoration
from sashimono.compat.mapped import MappedObject
from sashimono.core.commands import AddClip, AddTrack, Command
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    Effect,
    GeneratedSource,
    MediaId,
    ParamValue,
    Project,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.effects.definition import EffectDefinition, registry
from sashimono.effects.spec import (
    IMAGE_SUFFIXES,
    ColorSpec,
    FileSpec,
    ParameterSpec,
    ParamInput,
    TrackSpec,
    ValueSpec,
)

__all__ = ["MappedObject", "map_exo", "map_object", "media_paths"]

#: AviUtl1 の図形の種類（``type`` の番号）
_FIGURES = ("ellipse", "rect", "triangle", "pentagon", "hexagon", "star", "background")

#: AviUtl2 の図形の種類 **名前で書かれる**（``図形の種類=ハート``）
#: AviUtl2 に図形を置いたエイリアスを作らせて読み取った
#: ハート に当たる形はこちらに無いので、記録に残して矩形にする
#:
#: 三角形は**円に内接する形**（頂点が上） AviUtl2 に描かせて測ると、サイズ 400 で
#: 頂点が中心の 200 上・底辺が 100 下・底辺の幅 346 だった（縦横比 ±50 でも同じ形を
#: 縦か横に縮めただけ） 四角に合わせた三角形で写すと、底辺が 100 下へはみ出して
#: 幅も 54 広がる
_FIGURE_NAMES: dict[str, str] = {
    "背景": "background",
    "円": "ellipse",
    "四角形": "rect",
    "三角形": "inscribed_triangle",
    "五角形": "pentagon",
    "六角形": "hexagon",
    "星型": "star",
}

#: 図形の線の太さがこれ以上なら塗りつぶし AviUtl2 の既定値がこの値
#: そのまま線の太さとして渡すと、画面いっぱいの輪郭になる
_FILLED_LINE = 4000.0

#: 合成方法の番号
_BLEND_MODES = (
    "normal",
    "add",
    "subtract",
    "multiply",
    "screen",
    "overlay",
    "lighten",
    "darken",
)

#: **読んだうえで捨てる**項目 記録にも残さない
#:
#: 落としても絵が変わらないと**実物で確かめた**ものだけを並べる
#: 記録に残すと、本当に写せていない項目の並びがこれで埋まって役に立たなくなる
_IGNORED: dict[str, frozenset[str]] = {
    # 拡張色調補正の 色空間 AviUtl2 に YUV と RGB の両方を描かせたが、
    # 出力は 1 バイトも違わなかった（輝度のゲインと彩度のゲインで確認）
    "拡張色調補正": frozenset({"色空間"}),
}

#: 中身として扱う要素の名前 これ以外はフィルタ
_CONTENT_NAMES = frozenset(
    {
        "テキスト",
        "図形",
        "動画ファイル",
        "画像ファイル",
        "音声ファイル",
        "シーン",
        "フレームバッファ",
        "直前のオブジェクト",
        "時間制御",
    }
)

#: 位置と大きさを決める要素 エフェクトではなくクリップの配置として扱う
_DRAW_NAMES = frozenset({"標準描画", "拡張描画"})


@dataclass(frozen=True, slots=True)
class _Param:
    """写し先の項目

    ``convert`` は値の直し方（Y の向きや、透明度と不透明度のように意味が逆のとき）
    ``also`` は同じ値を入れるもう 1 つの項目（AviUtl が縦横をまとめて持つとき）
    """

    name: str
    convert: Callable[[float], float] | None = None
    also: str | None = None


def _flip(value: float) -> float:
    """AviUtl の下向き正の Y を、こちらの上向き正へ"""
    return -value


def _rest(value: float) -> float:
    """透明度（0 で不透明）を不透明度（100 で不透明）へ"""
    return 100.0 - value


def _turn(one: int, other: int) -> int:
    """2 つの角度の近さ 1 周でつながっているので短い側を見る"""
    gap = abs(one - other) % 360
    return min(gap, 360 - gap)


def _quarter(value: float) -> float:
    """90 度単位の回し方（0〜3）を角度へ"""
    return value * 90.0


def _byte_percent(value: float) -> float:
    """AviUtl の 0..255 の刻みを 0..100% へ"""
    return value * 100.0 / 255.0


#: フィルタのパラメータの対応 **AviUtl の効果名で引く**
#:
#: 種別で引くと、同じエフェクトへ写す効果（``座標`` ``拡大率`` ``回転`` は
#: どれも変形）の項目名がぶつかる AviUtl では ``X`` が効果ごとに別の意味を持つ
#:
#: 項目名は AviUtl2 に効果を積んだエイリアスを作らせて読み取った（推測していない）
_PARAMS: dict[str, dict[str, _Param]] = {
    "ぼかし": {"範囲": _Param("radius")},
    "発光": {
        "強さ": _Param("intensity"),
        "しきい値": _Param("threshold"),
        "範囲": _Param("radius"),
    },
    "グロー": {
        "強さ": _Param("intensity"),
        "しきい値": _Param("threshold"),
        "拡散": _Param("radius"),
    },
    "拡散光": {"強さ": _Param("intensity"), "拡散": _Param("radius")},
    "色調補正": {
        "明るさ": _Param("brightness"),
        "コントラスト": _Param("contrast"),
        "色相": _Param("hue"),
        "彩度": _Param("saturation"),
    },
    "クロマキー": {
        "色相範囲": _Param("hue_range"),
        "彩度範囲": _Param("saturation_range"),
        "境界補正": _Param("softness"),
    },
    # AviUtl2 の縁取りは ``サイズ`` ``ぼかし`` ``縁色`` 色は数値ではないので
    # 対応表とは別に扱う（:func:`_filter` を参照）
    # パターン画像 は縁を塗る模様 パスをそのまま渡し、描くときに読む
    "縁取り": {"サイズ": _Param("width"), "パターン画像": _Param("pattern")},
    "枠線": {"サイズ": _Param("width")},
    # 画像合成 X と Y は画像をずらす量（画面の画素のまま、拡大率で縮まない）
    # AviUtl2 に X=50 Y=30 を描かせると、画像は右へ 50、**下へ** 30 動いた
    "画像合成": {
        "X": _Param("offset_x"),
        "Y": _Param("offset_y", _flip),
        "拡大率": _Param("zoom"),
        "画像": _Param("image_file"),
        "ループ画像": _Param("loop"),
    },
    "グラデーション": {
        "強さ": _Param("strength"),
        "中心X": _Param("center_x"),
        "中心Y": _Param("center_y", _flip),
        "角度": _Param("angle"),
        "幅": _Param("span"),
    },
    "影": {"X": _Param("offset_x"), "Y": _Param("offset_y", _flip), "濃さ": _Param("opacity")},
    "シャドー": {
        "X": _Param("offset_x"),
        "Y": _Param("offset_y", _flip),
        "濃さ": _Param("opacity"),
    },
    "ドロップシャドウ": {
        "X": _Param("offset_x"),
        "Y": _Param("offset_y", _flip),
        "濃さ": _Param("opacity"),
        "拡散": _Param("blur"),
    },
    "シャープ": {"強さ": _Param("strength"), "範囲": _Param("radius")},
    "ノイズ": {"強さ": _Param("strength")},
    # 閃光 X と Y は写さない 0 でなければ :func:`_note_dropped` が記録に残す
    # AviUtl2 で X=300 Y=-200 を描かせると、光が動くのではなく画面いっぱいの
    # 薄い靄になった 光のもとをずらして写すと、かえって差が開く（6.9 → 9.9）
    "閃光": {
        "強さ": _Param("strength"),
        "サイズ固定": _Param("fixed_size"),
    },
    "グラデーションマップ": {"強さ": _Param("strength"), "パターン画像": _Param("pattern")},
    # 拡張色調補正 1 つずつ動かした見本を AviUtl2 に描かせて意味を測った
    # ゲインは倍率、オフセットは足し算、リフトは黒の持ち上げ、ガンマは冪
    "拡張色調補正": {
        "輝度::ゲイン": _Param("luma_gain"),
        "輝度::ガンマ": _Param("luma_gamma"),
        "輝度::リフト": _Param("luma_lift"),
        "輝度::オフセット": _Param("luma_offset"),
        "彩度::ゲイン": _Param("sat_gain"),
        "彩度::ガンマ": _Param("sat_gamma"),
        "彩度::リフト": _Param("sat_lift"),
        "彩度::オフセット": _Param("sat_offset"),
        "赤::ゲイン": _Param("red_gain"),
        "赤::ガンマ": _Param("red_gamma"),
        "赤::リフト": _Param("red_lift"),
        "赤::オフセット": _Param("red_offset"),
        "緑::ゲイン": _Param("green_gain"),
        "緑::ガンマ": _Param("green_gamma"),
        "緑::リフト": _Param("green_lift"),
        "緑::オフセット": _Param("green_offset"),
        "青::ゲイン": _Param("blue_gain"),
        "青::ガンマ": _Param("blue_gamma"),
        "青::リフト": _Param("blue_lift"),
        "青::オフセット": _Param("blue_offset"),
        "色相::オフセット": _Param("hue_offset"),
        "飽和する": _Param("clamped"),
    },
    # 領域拡張 四方へ広げる量 絵は入れ物の真ん中に残るので、片側だけ広げるとずれる
    "領域拡張": {
        "上": _Param("top"),
        "下": _Param("bottom"),
        "左": _Param("left"),
        "右": _Param("right"),
    },
    # ミラー 境目調整 は折り返す線を外へ動かす量（鏡像はその倍だけ離れる）
    "ミラー": {
        "透明度": _Param("opacity"),
        "減衰": _Param("falloff"),
        "境目調整": _Param("gap"),
    },
    "音声再生": {"音量": _Param("volume"), "左右": _Param("pan")},
    "音量調整": {"音量": _Param("volume"), "左右": _Param("pan")},
    "音量フェード": {"イン": _Param("fade_in"), "アウト": _Param("fade_out")},
    # モノラル化の 比率 は **0 が元のまま** 逆に読むと既定でステレオが潰れる
    "モノラル化": {"比率": _Param("ratio")},
    "ランダム配置": {
        "数": _Param("count"),
        "範囲": _Param("span"),
        "回転": _Param("angle"),
        "拡散": _Param("spread"),
        "ランダム角度": _Param("random_angle"),
    },
    # ディスプレイスメントマップ 変形X と 変形Y はずらす量 Y は下が正
    "ディスプレイスメントマップ": {
        "サイズ": _Param("size"),
        "ぼかし": _Param("blur"),
        "変形X": _Param("move_x"),
        "変形Y": _Param("move_y", _flip),
    },
    # 特定色域変換 色相まわりは**度** 彩度だけが 0..255 の刻み
    # AviUtl2 に 色相範囲 16 と 90 を描かせて、塗り替わる所の境目から読んだ
    # 0..255 の刻みとして 360/256 を掛けると、範囲 16 が 22 度に広がって
    # 残るはずの色まで塗り替わる
    "特定色域変換": {
        "色相範囲": _Param("hue_range"),
        "彩度範囲": _Param("saturation_range", _byte_percent),
        "境界補正": _Param("feather"),
    },
    "モザイク": {"サイズ": _Param("size")},
    "クリッピング": {
        "上": _Param("top"),
        "下": _Param("bottom"),
        "左": _Param("left"),
        "右": _Param("right"),
    },
    "境界ぼかし": {"範囲": _Param("blur")},
    "エッジ抽出": {"強さ": _Param("strength"), "しきい値": _Param("radius")},
    "凸エッジ": {"幅": _Param("thickness"), "高さ": _Param("elevation"), "角度": _Param("azimuth")},
    "方向ブラー": {"範囲": _Param("radius"), "角度": _Param("angle")},
    "放射ブラー": {
        "範囲": _Param("amount"),
        "X": _Param("center_x"),
        "Y": _Param("center_y", _flip),
    },
    "レンズブラー": {"範囲": _Param("radius"), "光の強さ": _Param("brightness")},
    "色ずれ": {"ずれ幅": _Param("shift"), "角度": _Param("angle"), "強さ": _Param("strength")},
    "カラーキー": {"色差範囲": _Param("tolerance"), "境界補正": _Param("feather")},
    "ルミナンスキー": {"基準輝度": _Param("threshold"), "輝度範囲": _Param("smoothness")},
    "斜めクリッピング": {
        "中心X": _Param("center_x"),
        "中心Y": _Param("center_y", _flip),
        "角度": _Param("angle"),
        "ぼかし": _Param("blur"),
        "幅": _Param("width"),
    },
    "波紋": {
        "中心X": _Param("center_x"),
        "中心Y": _Param("center_y", _flip),
        "幅": _Param("amplitude"),
        "高さ": _Param("wavelength"),
        "速度": _Param("period"),
    },
    "ラスター": {
        "横幅": _Param("wavelength"),
        "高さ": _Param("amplitude"),
        "周期": _Param("period"),
    },
    "極座標変換": {"中心幅": _Param("core"), "渦巻": _Param("twist")},
    "リール回転": {"回転数": _Param("rotation")},
    "砕け散る": {
        "開始時間": _Param("start"),
        "再生速度": _Param("speed"),
        "破片サイズ": _Param("size"),
        "速度": _Param("fly"),
        "重力": _Param("fall"),
        "時間差": _Param("delay"),
        "距離影響": _Param("impact"),
    },
    "粒子化": {
        "開始時間": _Param("preroll"),
        "起点X": _Param("emitter_x"),
        # 放つ位置の Y は YMM4 に合わせて下が正のまま持つ（表示名にも書いてある）
        "起点Y": _Param("emitter_y"),
        "粒子速度": _Param("speed"),
        "角度": _Param("emit_angle"),
        "ゆらぎ": _Param("turbulence"),
    },
    "振動": {
        "X": _Param("range_x"),
        "Y": _Param("range_y"),
        "Z": _Param("range_z"),
        "周期": _Param("interval"),
    },
    # 震える は縦横をまとめて 1 つの振幅で持つ 本人の回答（2026-09-19）で
    # 手ぶれ・振動・震えるは 1 つにまとめる
    "震える": {"振幅": _Param("range_x", also="range_y"), "間隔": _Param("interval")},
    "反復移動": {"距離": _Param("move_x"), "速さ": _Param("interval")},
    "点滅": {"速さ": _Param("interval"), "点滅割合": _Param("opacity")},
    "弾む": {"速さ": _Param("period"), "高さ": _Param("height")},
    "円形配置": {
        "円周": _Param("circumference"),
        "半径": _Param("radius"),
        "数": _Param("count"),
    },
    "画像ループ": {"横回数": _Param("count_x"), "縦回数": _Param("count_y")},
    "単色化": {"強さ": _Param("amount")},
    # 反転は軸ごとの旗 輝度・色相・透明度の反転は当たるものが無いので記録に回る
    "反転": {"上下反転": _Param("vertical"), "左右反転": _Param("horizontal")},
    # 振り子は元の角度を挟んで往復する回転 速さは 1 往復の長さ
    "振り子": {"角度": _Param("angle_z"), "速さ": _Param("interval")},
    # ローテーションは 90 度単位の回し方を数で持つ
    "ローテーション": {"90度回転": _Param("rotation", _quarter)},
    "扇クリッピング": {
        "中心X": _Param("center_x"),
        "中心Y": _Param("center_y", _flip),
        "基準角": _Param("rotation"),
        "範囲角": _Param("span"),
        "ぼかし": _Param("blur"),
    },
    "座標": {"X": _Param("pos_x"), "Y": _Param("pos_y", _flip)},
    "拡大率": {"拡大率": _Param("scale", also="scale_y")},
    "透明度": {"透明度": _Param("amount", _rest)},
    "回転": {"Z": _Param("rotation"), "X": _Param("rotation_x"), "Y": _Param("rotation_y")},
    "リサイズ": {"拡大率": _Param("scale", also="scale_y")},
    # 万華鏡 長さ は鏡の三角の辺 繰り返し回数 は覆う範囲を三角何段ぶんにするか
    # AviUtl2 に長さと繰り返しを変えた見本を描かせ、模様の間隔と端の位置から読んだ
    "万華鏡": {
        "中心X": _Param("center_x"),
        "中心Y": _Param("center_y", _flip),
        "長さ": _Param("span"),
        "回転": _Param("angle"),
        "角数(偶数)": _Param("corners"),
        "繰り返し回数": _Param("repeats"),
        "固定サイズ": _Param("fixed_size"),
        "円形マスク": _Param("circle_mask"),
        "回転同期": _Param("spin_pattern"),
        "領域外を透過": _Param("clip_outside"),
    },
    # 個別オブジェクトの 2 つは、オブジェクト分割 で切ったマスの**位置**を動かす
    # 分け方は :func:`map_object` が前にある オブジェクト分割 から渡す
    "座標の拡大縮小(個別オブジェクト)": {
        "拡大率": _Param("scale"),
        "中心X": _Param("center_x"),
        "中心Y": _Param("center_y", _flip),
    },
    "座標の回転(個別オブジェクト)": {
        "角度": _Param("angle"),
        "中心X": _Param("center_x"),
        "中心Y": _Param("center_y", _flip),
    },
}

#: AviUtl のフィルタ名と、こちらのエフェクト種別
_FILTERS: dict[str, str] = {
    "ぼかし": "blur",
    "発光": "glow",
    "グロー": "glow",
    "拡散光": "glow",
    "色調補正": "color",
    "クロマキー": "chroma_key",
    "縁取り": "border",
    "枠線": "border",
    "画像合成": "image_blend",
    "影": "shadow",
    "シャドー": "shadow",
    "ドロップシャドウ": "shadow",
    "シャープ": "sharpen",
    "ノイズ": "noise",
    "閃光": "flash",
    "グラデーションマップ": "gradient_map",
    "拡張色調補正": "color_grade",
    "特定色域変換": "color_range_shift",
    "領域拡張": "expand_area",
    "ミラー": "mirror",
    "ディスプレイスメントマップ": "displacement_map",
    "ランダム配置": "scatter",
    # 音声 AviUtl2 v2.1.6a の音声フィルタはこの 3 つだけ
    # 音声再生 は音声オブジェクトの置き方（映像の 標準描画 に当たる）で、
    # 項目が 音量調整 と同じなので同じエフェクトへ写す
    "音声再生": "audio_volume",
    "音量調整": "audio_volume",
    "音量フェード": "audio_fade",
    "モノラル化": "audio_monaural",
    "モザイク": "mosaic",
    "マスク": "mask",
    "クリッピング": "crop",
    "リサイズ": "transform",
    "グラデーション": "gradient",
    "境界ぼかし": "border_blur",
    "エッジ抽出": "edge_detect",
    "凸エッジ": "bevel_light",
    "方向ブラー": "directional_blur",
    "放射ブラー": "radial_blur",
    "レンズブラー": "lens_blur",
    "モーションブラー": "after_image",
    "色ずれ": "color_shift",
    # 単色化は「色」と「強さ」を持つ 単色塗り（fill）がそのまま当たる
    "単色化": "fill",
    # 反転は上下・左右の旗 AviUtl の ミラー は鏡像を映す別の効果なので写さない
    # （項目が 透明度・減衰・境目調整・ミラーの方向 で、こちらに当たるものが無い）
    "反転": "flip",
    "カラーキー": "color_key",
    "ルミナンスキー": "luminance_key",
    "斜めクリッピング": "crop_angle",
    "波紋": "ripple",
    "ラスター": "wave",
    "極座標変換": "polar",
    "リール回転": "reel_spin",
    "砕け散る": "crash",
    "粒子化": "particles",
    "振動": "random_move",
    "震える": "random_move",
    "反復移動": "repeat_move",
    "点滅": "repeat_opacity",
    "弾む": "jump",
    "円形配置": "circular_duplicate",
    "画像ループ": "tile",
    "振り子": "repeat_rotate",
    "ローテーション": "transform",
    "扇クリッピング": "shape_mask",
    "座標": "transform",
    "拡大率": "transform",
    "透明度": "opacity",
    "回転": "transform",
    "万華鏡": "kaleidoscope",
    "座標の拡大縮小(個別オブジェクト)": "split_pieces",
    "座標の回転(個別オブジェクト)": "split_pieces",
}

#: 絵を切るだけで、それ自体は絵を変えないフィルタ 後ろの 個別オブジェクト の効果へ分け方を渡す
_SPLIT = "オブジェクト分割"


#: 絵や音をそのまま素材として持つ中身 素材一覧へ載せる側（media_paths）と、
#: クリップを素材と結ぶ側（_content）の両方がここを見る 片方だけに足すと、
#: 一覧には載るのにクリップが空のまま、ということが起きる
_MEDIA_NAMES = frozenset({"動画ファイル", "画像ファイル", "音声ファイル"})

#: 素材一覧へ載せる中身 音声波形表示は自分では素材にならないが、``ファイル`` の音を描く
#: 読み込ませないと、別の機械へ持っていったときに探し直せない
_LISTED_NAMES = _MEDIA_NAMES | {"音声波形表示"}


def media_paths(exo: ExoFile) -> tuple[str, ...]:
    """このファイルが参照している素材のパス

    読み込みは呼び出し側に任せる 互換層はファイルを開かない（開くと、
    素材が見つからないだけで対応付け全体が失敗しうる）
    """
    found: list[str] = []
    for obj in exo.objects:
        content = obj.content
        if content is None or content.name not in _LISTED_NAMES:
            continue
        path = _media_file(content)
        if path and path not in found:
            found.append(path)
    return tuple(found)


def _media_file(entry: ExoEntry) -> str:
    """中身が読み込む素材ファイルのパス

    AviUtl2 は ``ファイル=``、AviUtl1 は ``file=`` と書く（AviUtl2 v2.1.6a に動画・画像・
    音声ファイルを置いて保存させ、3 つとも ``ファイル=`` だと確かめた） ``file`` だけを
    見ていたので、AviUtl2 の素材は素材一覧にもクリップにも載らず、中身の無い
    クリップになっていた
    """
    return entry.value("ファイル", "file").strip()


def map_exo(
    exo: ExoFile,
    project: Project,
    *,
    at_frame: int = 0,
    media: dict[str, MediaId] | None = None,
    report: CompatibilityReport | None = None,
) -> list[Command]:
    """ファイル全体を、タイムラインへ置くコマンドの列にする

    レイヤーはそのままトラックに対応させる AviUtl のレイヤー 1 が一番下なので、
    こちらの映像トラックの並びと同じ向きになる
    """
    log = report if report is not None else global_report
    mapped = [map_object(obj, project.rate, report=log) for obj in exo.objects]
    mapped = [item for item in mapped if item is not None]
    if not mapped:
        return []

    commands: list[Command] = []
    tracks = _tracks_for(project, {item.layer for item in mapped if item is not None}, commands)

    known = media or {}
    for item in mapped:
        if item is None:  # pragma: no cover - 直前で除いている
            continue
        track = tracks[item.layer]
        placed = Clip(
            timeline_start=item.clip.timeline_start + at_frame,
            duration=item.clip.duration,
            media_id=known.get(item.media_path) if item.media_path else None,
            source=item.clip.source,
            source_in=item.clip.source_in,
            speed=item.clip.speed,
            effects=item.clip.effects,
            opacity=item.clip.opacity,
            blend_mode=item.clip.blend_mode,
        )
        commands.append(AddClip(track.id, placed))
    return commands


def map_object(
    obj: ExoObject, rate: FrameRate, *, report: CompatibilityReport | None = None
) -> MappedObject | None:
    """1 オブジェクトをクリップへ 写せなければ ``None``"""
    log = report if report is not None else global_report
    content = obj.content
    if content is None:
        return None

    source, media_path, kind = _content(content, obj.relative_points(), log)
    effects: list[Effect] = []
    opacity = AnimatedValue(1.0)
    blend = "normal"
    # 中間点はオブジェクトの持ち物 トラックバーの値はこの点の数だけ並ぶ
    points = obj.relative_points()
    placement: dict[str, AnimatedValue] | None = None
    # オブジェクト分割 の分け方 切らないうちは 1 マスなので、個別の効果は何も動かさない
    # （AviUtl2 でも分割なしの 個別の拡大 50 は元の絵のままだった）
    grid: dict[str, ParamValue] = {}

    for entry in obj.filters():
        if entry.name == _SPLIT:
            grid = _split_grid(entry, points, log)
            continue
        if entry.name in _DRAW_NAMES:
            placement = _placement(entry, points, log)
            opacity = animated_value(
                entry.params.get("透明度"),
                points=points,
                log=log,
                label="描画設定の透明度",
                convert=lambda value: 1.0 - value / 100.0,
            )
            blend = _blend_of(entry, log)
            continue

        effect = _filter(entry, points, log)
        if effect is not None and effect.kind == "split_pieces":
            effect = replace(effect, params={**effect.params, **grid})
            merged = _merge_pieces(effects[-1], effect, log) if effects else None
            if merged is not None:
                effects[-1] = merged
                continue
        if effect is not None:
            effects.append(effect)

    if (
        placement is not None
        and source is not None
        and source.params.get("shape") == "motion_trail"
    ):
        # 移動軌跡は自分の位置の**動き**をたどって線を引く 位置は図形へ渡し、
        # 変形からは外す 両方に残すと、線を描いた絵をもう一度その位置へずらして
        # 軌跡が 2 倍の所に出る
        source = source.with_param("pos_x", placement["pos_x"]).with_param(
            "pos_y", placement["pos_y"]
        )
        placement = {**placement, "pos_x": AnimatedValue(0.0), "pos_y": AnimatedValue(0.0)}

    # 位置・拡大・回転は変形エフェクトへ AviUtl では描画設定だが、こちらでは
    # クリップの持ち物ではないので、同じ見た目になるエフェクトへ写す
    if placement is not None and any(
        value.is_animated or value.static != _PLACEMENT_DEFAULTS[name]
        for name, value in placement.items()
    ):
        transform = registry.get("transform")
        if transform is not None:
            effects.insert(0, transform.create(**placement))

    source_in, speed = _playback(content, rate, log)
    clip = Clip(
        timeline_start=obj.start,
        duration=obj.duration,
        source=source,
        source_in=source_in,
        speed=speed,
        effects=tuple(effects),
        opacity=opacity,
        blend_mode=blend,
    )
    return MappedObject(
        clip=clip,
        layer=max(1, obj.layer),
        media_path=media_path,
        kind=kind,
        has_span=obj.span_given,
    )


def _split_grid(
    entry: ExoEntry, points: tuple[int, ...], log: CompatibilityReport
) -> dict[str, ParamValue]:
    """オブジェクト分割 の横と縦の数を、個別の効果のパラメータとして読む"""
    grid: dict[str, ParamValue] = {}
    for source_name, target in (("横分割数", "columns"), ("縦分割数", "rows")):
        grid[target] = animated_value(
            entry.params.get(source_name),
            points=points,
            log=log,
            label=f"{_SPLIT}の{source_name}",
            default=1.0,
        )
    # 横と縦の数のほかに使われている項目があれば記録する 黙って捨てると、
    # 写せたつもりのまま違う絵が出る（ほかのフィルタと同じ扱い）
    _note_dropped(entry, {"横分割数", "縦分割数"}, log)
    return grid


def _merge_pieces(previous: Effect, effect: Effect, log: CompatibilityReport) -> Effect | None:
    """続けて積んだ 個別オブジェクト の拡大と回転を 1 つにまとめる

    2 つ目の効果は、1 つ目で動いた後のマスを動かす こちらのエフェクトは
    分けた升目の位置から動かすので、別々に並べると 2 つ目が元の升目を基準に
    読み直し、動いた後の断片が欠けたり元の場所に残ったりする

    値が動かなければ、マスの真ん中の行き先は「拡大・回転 + 平行移動」の 1 つの式に
    畳める（軸の違う変形や、拡大どうしが続いても同じ） 値が動くときは、軸が同じで
    片方が拡大だけ・もう片方が回転だけの組に限って畳む（順番を入れ替えても同じ所へ行く）
    それ以外は畳めないので、分けて並べたうえで記録に残す
    """
    if previous.kind != "split_pieces":
        return None
    if any(previous.params.get(key) != effect.params.get(key) for key in ("columns", "rows")):
        return None
    first, second = _piece_stage(previous), _piece_stage(effect)
    if first is not None and second is not None:
        return replace(previous, params={**previous.params, **_compose(first, second)})
    same = ("center_x", "center_y", "offset_x", "offset_y")
    unit = {"scale": AnimatedValue(100.0), "angle": AnimatedValue(0.0)}
    params = dict(previous.params)
    merged = all(previous.params.get(key) == effect.params.get(key) for key in same)
    for name, idle in unit.items():
        mine, theirs = previous.params.get(name), effect.params.get(name)
        if theirs is None or theirs == idle:
            continue
        if mine != idle:
            merged = False
        params[name] = theirs
    if merged:
        return replace(previous, params=params)
    log.note_missing("個別オブジェクトの効果を、動く値で続けて積んだもの")
    return None


#: 分けたマスを動かす 1 段 拡大率（倍）・時計回りの角度（度）・軸・ずらし（画素 Y は上が正）
_Stage = tuple[float, float, tuple[float, float], tuple[float, float]]


def _piece_stage(effect: Effect) -> _Stage | None:
    """動かない値だけでできていれば、その段の式を返す"""
    numbers: dict[str, float] = {}
    for name, idle in (
        ("scale", 100.0),
        ("angle", 0.0),
        ("center_x", 0.0),
        ("center_y", 0.0),
        ("offset_x", 0.0),
        ("offset_y", 0.0),
    ):
        value = effect.params.get(name, AnimatedValue(idle))
        if not isinstance(value, AnimatedValue) or value.is_animated:
            return None
        numbers[name] = value.static
    return (
        numbers["scale"] / 100.0,
        numbers["angle"],
        (numbers["center_x"], numbers["center_y"]),
        (numbers["offset_x"], numbers["offset_y"]),
    )


def _compose(first: _Stage, second: _Stage) -> dict[str, ParamValue]:
    """2 段を続けたものを、軸をオブジェクトの中心に置いた 1 段で表す

    1 段は ``c → P + s·R(θ)(c − P) + d``（R は時計回り） 続けると
    ``c → s₂s₁·R(θ₂+θ₁)·c + b`` になり、``b = A₂b₁ + b₂``（``bₖ = Pₖ − AₖPₖ + dₖ``）
    """

    def linear(stage: _Stage) -> tuple[tuple[float, float, float, float], tuple[float, float]]:
        scale, angle, (px, py), (dx, dy) = stage
        turn = math.radians(angle)
        # Y が上向きの座標で時計回りに回す行列
        a, b, c, d = (
            scale * math.cos(turn),
            scale * math.sin(turn),
            -scale * math.sin(turn),
            scale * math.cos(turn),
        )
        return (a, b, c, d), (px - (a * px + b * py) + dx, py - (c * px + d * py) + dy)

    _, shift1 = linear(first)
    (a, b, c, d), shift2 = linear(second)
    offset = (
        a * shift1[0] + b * shift1[1] + shift2[0],
        c * shift1[0] + d * shift1[1] + shift2[1],
    )
    return {
        "scale": AnimatedValue(first[0] * second[0] * 100.0),
        "angle": AnimatedValue(first[1] + second[1]),
        "center_x": AnimatedValue(0.0),
        "center_y": AnimatedValue(0.0),
        "offset_x": AnimatedValue(offset[0]),
        "offset_y": AnimatedValue(offset[1]),
    }


#: 変形エフェクトへ写す描画設定と、その既定値（既定のままなら変形を足さない）
_PLACEMENT_DEFAULTS: dict[str, float] = {
    "pos_x": 0.0,
    "pos_y": 0.0,
    "scale": 100.0,
    "scale_y": 100.0,
    "rotation": 0.0,
}


def _placement(
    entry: ExoEntry, points: tuple[int, ...], log: CompatibilityReport
) -> dict[str, AnimatedValue]:
    """描画設定（位置・拡大・回転）を変形エフェクトのパラメータへ

    AviUtl2 は軸ごとに分けて持つ 回転として使えるのは Z 軸だけで、
    X/Y 軸の回転は板を傾ける立体的な変形なのでここでは写せない
    """
    for axis in ("X軸回転", "Y軸回転"):
        motion = entry.motion(axis)
        if motion is not None and (motion.first != 0.0 or motion.moves):
            log.note_missing(f"描画設定: {axis}")

    scale = animated_value(
        entry.params.get("拡大率"), points=points, log=log, label="描画設定の拡大率", default=100.0
    )
    return {
        "pos_x": animated_value(
            entry.params.get("X"), points=points, log=log, label="描画設定の X"
        ),
        # Y は AviUtl が下向き正 こちらは上向き正なので符号を反転する
        "pos_y": animated_value(
            entry.params.get("Y"),
            points=points,
            log=log,
            label="描画設定の Y",
            convert=lambda value: -value,
        ),
        "scale": scale,
        "scale_y": scale,
        "rotation": animated_value(
            entry.params.get("回転") or entry.params.get("Z軸回転"),
            points=points,
            log=log,
            label="描画設定の回転",
        ),
    }


def _varies(motion: Motion) -> bool:
    """時間で変わりうるか

    値が同じでも、式やスクリプトの移動方法なら変わる（``100,100,回転,4|360``）
    値の並びだけ見ると、そういう行を止めたことに気付けない
    """
    return motion.moves or bool(motion.flags & (FLAG_EXPRESSION | FLAG_SCRIPT))


def _spec_value(
    spec: ParameterSpec,
    raw: str,
    points: tuple[int, ...],
    log: CompatibilityReport,
    label: str,
    convert: Callable[[float], float] | None = None,
) -> ParamInput:
    """仕様に合わせて値を渡す形へ

    動きを読めるのはトラックバーだけ チェックや選択肢まで
    :class:`AnimatedValue` に包むと、``coerce`` が型違いとして既定値へ落とす

    ``convert`` は対応表が持つ値の直し（Y の向きなど） 直してから範囲へ収める
    """
    motion = parse_motion(raw)
    if isinstance(spec, TrackSpec | ValueSpec):
        if motion is None:
            # 数として読めない 写し先の既定値をそのまま使う
            # （既定値に変換を掛けると、透明度 100 が不透明度 0 になって全透明になる）
            # 空の値も記録する 既定値へ置き換えたことに変わりはない
            log.note_missing(f"AviUtl の数として読めない値: {label}")
            return spec.default_value()
        if isinstance(spec, ValueSpec):
            # スライダーを持たない数値は動かせない 先頭の値だけ使う
            if _varies(motion):
                log.note_missing(f"AviUtl の動く値を写せない項目: {label}")
            value = motion.first if convert is None else convert(motion.first)
            return value
        adjust = spec.clamp if convert is None else (lambda value: spec.clamp(convert(value)))
        return animated_value(
            raw, points=points, log=log, label=label, convert=adjust, default=spec.default
        )
    if isinstance(spec, ColorSpec):
        # エイリアスの色は ``ffd400`` のような 16 進の文字 そのまま渡すと
        # ``coerce`` が色として読めず、既定の色（多くは黒）へ落ちる
        # テレビ字幕の板が黒の上に黒で描かれ、何も出ていないように見えていた
        parsed = _hex_color(raw)
        if parsed is None:
            # 読めない色は、そのスクリプトが決めた既定の色にして記録する
            # 白へ倒すと、既定が白でない色欄（板の色など）の見た目が変わる
            log.note_missing(f"AviUtl の色として読めない値: {label}")
            return spec.default_value()
        return parsed
    return raw


def _content(
    entry: ExoEntry, points: tuple[int, ...], log: CompatibilityReport
) -> tuple[GeneratedSource | None, str, str]:
    """中身を生成オブジェクトへ 素材ファイルの場合はパスだけ返す"""
    if entry.name == "テキスト":
        return _text(entry, log), "", "text"
    if entry.name == "図形":
        return _figure(entry, log), "", "shape"
    if entry.name == "集中線":
        return _concentration(entry, points, log), "", "shape"
    if entry.name == "扇型":
        return _fan(entry, log), "", "shape"
    if entry.name == "多角形":
        return _polygon(entry, log), "", "shape"
    if entry.name == "カウンター":
        return _counter(entry, log), "", "text"
    if entry.name == "ライン(移動軌跡)":
        return _motion_trail(entry, points, log), "", "shape"
    if entry.name == "星":
        return _star_field(entry, points, log), "", "shape"
    if entry.name == "音声波形表示":
        path = _media_file(entry)
        return _waveform(entry, path, points, log), path, "shape"
    if entry.name in _MEDIA_NAMES:
        return None, _media_file(entry), entry.name

    if entry.name not in _CONTENT_NAMES:
        log.note_missing(f"オブジェクト: {entry.name}")
    else:
        log.note_missing(f"未対応の中身: {entry.name}")
    return None, "", entry.name


#: AviUtl1 の ``type``（文字装飾の番号）と、AviUtl2 での呼び名
#: 番号で持っているのは AviUtl1 だけで、中身は同じものを指す
_DECORATION_BY_INDEX = ("標準文字", "影付き文字", "影付き文字（薄）", "縁取り文字")

#: AviUtl2 の ``文字揃え`` ``中央揃え[下]`` のように横と縦を 1 つにまとめてある
_ALIGNMENTS: dict[str, tuple[str, str]] = {
    "左寄せ[上]": ("left", "top"),
    "中央揃え[上]": ("center", "top"),
    "右寄せ[上]": ("right", "top"),
    "左寄せ[中]": ("left", "middle"),
    "中央揃え[中]": ("center", "middle"),
    "右寄せ[中]": ("right", "middle"),
    "左寄せ[下]": ("left", "bottom"),
    "中央揃え[下]": ("center", "bottom"),
    "右寄せ[下]": ("right", "bottom"),
}


def _text(entry: ExoEntry, log: CompatibilityReport) -> GeneratedSource:
    """テキストオブジェクト 世代でパラメータ名がまるごと違う"""
    size = entry.numeric("サイズ", "size", default=48.0)
    align, valign = _text_alignment(entry)
    params: dict[str, ParamValue] = {
        "text": entry.text(),
        "size": AnimatedValue(size),
        "color": _color(entry.value("文字色", "color", default="ffffff")),
        "bold": entry.integer("B"),
        "italic": entry.integer("I"),
        "line_spacing": AnimatedValue(entry.numeric("行間", "spacing_y")),
        "letter_spacing": AnimatedValue(entry.numeric("字間", "spacing_x")),
        "align": align,
        "valign": valign,
        # 入れ物と太字を AviUtl2 に合わせる 字の形を入れ物にすると、画像合成や
        # 万華鏡の基準が AviUtl2 より 22〜24 画素内側になる（#64）
        # AviUtl1 は測っていないので標準のまま 推測で AviUtl2 の決まりを当てると、
        # 今まで読めていた AviUtl1 の字幕の位置が黙って動く
        "layout": "aviutl" if entry.generation >= 2 else "native",
    }
    font = entry.value("フォント", "font")
    if font:
        params["font"] = font

    params.update(_decoration_of(entry, size, log))
    # テキスト欄に埋め込んだ Lua（``<?...?>``）は本文のまま持つ 時刻で結果が
    # 変わるので、読み込む時点ではなく描くたびに走らせる（engine.render.scripts）
    return GeneratedSource(kind="text", params=params)


def _text_alignment(entry: ExoEntry) -> tuple[str, str]:
    """``文字揃え`` を横と縦に分ける"""
    named = entry.params.get("文字揃え")
    if named is not None:
        return _ALIGNMENTS.get(named.strip(), ("center", "bottom"))
    # AviUtl1 は番号 0..2 が上、3..5 が中、6..8 が下
    index = entry.integer("align")
    return _align(index), ("top", "middle", "bottom")[min(index // 3, 2)]


def _decoration_of(entry: ExoEntry, size: float, log: CompatibilityReport) -> dict[str, ParamValue]:
    """``文字装飾`` を縁取りと影のパラメータへ

    カウンター（カスタムオブジェクト）は同じものを ``装飾タイプ`` と書く
    読まないと、縁取りや影の付いたカウンターが素の文字になる
    """
    name = entry.params.get("文字装飾", entry.params.get("装飾タイプ"))
    if name is None:
        index = entry.integer("type")
        name = _DECORATION_BY_INDEX[index] if 0 <= index < len(_DECORATION_BY_INDEX) else ""

    decoration = find_decoration(name)
    if decoration is None:
        log.note_missing(f"文字装飾: {name}")
        return {}
    colour = _color(entry.value("影・縁色", "color2", default="000000"))
    return decoration_params(decoration, size, (colour[0], colour[1], colour[2], colour[3]))


def _figure(entry: ExoEntry, log: CompatibilityReport) -> GeneratedSource:
    """図形オブジェクト

    世代で書き方が違う AviUtl1 は種類を番号（``type``）と ``color`` で書き、
    AviUtl2 は名前（``図形の種類``）と ``色`` で書く 前者だけを読んでいたので、
    **AviUtl2 の図形は種類も色も落ちて、白い円になっていた**
    """
    size = entry.number("サイズ", 100.0)
    ratio = entry.number("縦横比", 0.0) / 100.0
    width = size * (1.0 - max(0.0, ratio))
    height = size * (1.0 - max(0.0, -ratio))

    for key in ("サイズ", "縦横比", "ライン幅"):
        motion = parse_motion(entry.params.get(key))
        # 値が同じでも式やスクリプトなら時間で変わる（_varies で見る）
        if motion is not None and _varies(motion):
            # 大きさは サイズ と 縦横比 から計算してから渡すので、動きを残せない
            log.note_missing(f"図形の動く{key}")

    named = entry.params.get("図形の種類")
    if named is not None:
        shape = _FIGURE_NAMES.get(named.strip())
        if shape is None:
            log.note_missing(f"図形の種類: {named.strip()}")
            shape = "rect"
    else:
        index = entry.integer("type")
        shape = _FIGURES[index] if 0 <= index < len(_FIGURES) else "rect"

    # 角を丸くする は矩形のときだけ意味がある
    if shape == "rect" and entry.number("角を丸くする") != 0.0:
        shape = "rounded"

    # 線の太さが図形より大きければ塗りつぶし そのまま渡すと画面を覆う輪郭になる
    # 項目が無いか 0 のときも塗りつぶし AviUtl1 の図形には ライン幅 が無く、
    # 輪郭だけにすると、塗ってあった図形が中抜きになる
    line = entry.number("ライン幅")
    filled = line <= 0.0 or line >= min(_FILLED_LINE, max(width, height))
    return GeneratedSource(
        kind="shape",
        params={
            "shape": shape,
            "width": AnimatedValue(max(1.0, width)),
            "height": AnimatedValue(max(1.0, height)),
            "color": _color(entry.value("色", "color", default="ffffff")),
            "line_width": AnimatedValue(0.0 if filled else line),
            "outline_only": not filled,
        },
    )


def _note_frozen(entry: ExoEntry, keys: tuple[str, ...], log: CompatibilityReport) -> None:
    """動きの付いた項目を先頭の値で止めたことを記録する

    生成オブジェクトの値は計算してから渡すものが多く、キーフレームを残せない
    黙って止めると、動くはずの絵が止まったまま気付けない
    """
    for key in keys:
        motion = parse_motion(entry.params.get(key))
        if motion is not None and _varies(motion):
            log.note_missing(f"{entry.name}の動く{key}")


def _fan(entry: ExoEntry, log: CompatibilityReport) -> GeneratedSource:
    """扇型 AviUtl2 のカスタムオブジェクト

    項目は ``中心角`` ``サイズ`` ``ライン幅`` ``色`` AviUtl2 に置かせて読み取った
    """
    _note_frozen(entry, ("中心角", "サイズ", "ライン幅"), log)
    size = max(1.0, entry.number("サイズ", 100.0))
    line = entry.number("ライン幅")
    # 塗りつぶしの判定は、幅と高さに渡すのと同じ値で見る
    filled = line <= 0.0 or line >= min(_FILLED_LINE, size)
    return GeneratedSource(
        kind="shape",
        params={
            "shape": "fan",
            "span": AnimatedValue(entry.number("中心角", 360.0)),
            "width": AnimatedValue(size),
            "height": AnimatedValue(size),
            "color": _color(entry.value("色", default="ffffff")),
            "line_width": AnimatedValue(0.0 if filled else line),
            "outline_only": not filled,
        },
    )


def _polygon(entry: ExoEntry, log: CompatibilityReport) -> GeneratedSource:
    """多角形 AviUtl2 のカスタムオブジェクト

    頂点は ``座標=0,-150,130,75,-130,75`` と x と y を並べて書く（Y は下が正）
    こちらの線の図形（``polyline``）は ``x,y;x,y`` なので組み直す
    """
    _note_frozen(entry, ("ライン幅",), log)
    raw = [part.strip() for part in entry.params.get("座標", "").split(",") if part.strip()]
    # 読めない値だけを捨てると、その後ろの x と y が入れ替わって別の形になる
    # 非有限（NaN や無限大）も同じ 描画の側で落ちて、黙って違う形が出る
    numbers = [value for part in raw if (value := _as_number(part)) is not None]
    usable = len(numbers) == len(raw) and len(numbers) % 2 == 0 and numbers
    pairs: list[tuple[float, float]] = []
    if usable:
        pairs = [(numbers[index], numbers[index + 1]) for index in range(0, len(numbers), 2)]
    else:
        # 組にできない並びは、まるごと諦めて記録に残す
        log.note_missing("多角形の座標（読めない）")

    corners = round(entry.number("頂点数", float(len(pairs))))
    if corners != len(pairs):
        # 頂点数と座標の数が食い違うファイル 余分な点まで描くと形が変わるので、
        # 頂点数が正でかつ少ないときだけそのぶんを使う どちらにしても記録に残す
        log.note_missing("多角形の頂点数と座標の数が合わない")
        if 0 < corners < len(pairs):
            pairs = pairs[:corners]

    # Y は AviUtl が下向き正 こちらは上向き正なので符号を反転する
    points = ";".join(f"{x},{_flip(y)}" for x, y in pairs)

    repeats = entry.number("繰り返し描画数", 1.0)
    if repeats != 1.0:
        log.note_missing("多角形の繰り返し描画")

    line = entry.number("ライン幅")
    filled = entry.number("簡易塗り潰し") != 0.0
    colour = _color(entry.value("色", default="ffffff"))
    return GeneratedSource(
        kind="shape",
        params={
            "shape": "polyline",
            "points": points,
            "closed": True,
            "line_width": AnimatedValue(max(1.0, line)),
            "color": colour,
            # 簡易塗り潰し のときだけ中を塗る 既定は線だけ
            "fill_color": colour if filled else (0.0, 0.0, 0.0, 0.0),
        },
    )


def _counter(entry: ExoEntry, log: CompatibilityReport) -> GeneratedSource:
    """カウンター AviUtl2 のカスタムオブジェクト 数を数えて出すテキスト

    項目は ``初期値`` ``速度`` ``サイズ`` ``表示形式`` ``フォント名``
    ``装飾タイプ`` ``文字色`` ``影・縁色``
    """
    _note_frozen(entry, ("初期値", "速度", "サイズ"), log)
    style = entry.params.get("表示形式", "標準").strip()
    if style != "標準":
        # 書式（時分秒など）の書き方が分からないものは記録に残す
        log.note_missing(f"カウンターの表示形式: {style}")
    params: dict[str, ParamValue] = {
        "text": "",
        "size": AnimatedValue(entry.number("サイズ", 34.0)),
        "font": entry.params.get("フォント名", "").strip(),
        "color": _color(entry.value("文字色", default="ffffff")),
        # ``s`` は 60 で分へ繰り上がる カウンターはただ数を数えるので通算の ``n``
        # 負の初めの値や数え下げもそのまま出せる（:func:`format_time` が符号を付ける）
        "timer_format": "n",
        "timer_start": AnimatedValue(entry.number("初期値")),
        # 速度は 1 秒あたりの進み方 こちらは百分率で持つ
        "timer_rate": AnimatedValue(entry.number("速度", 1.0) * 100.0),
        # テキストと同じく AviUtl2 の入れ物で組む
        "layout": "aviutl",
    }
    # 装飾はテキストと同じ仕組みで写す（縁取りや影が消えないように）
    params.update(_decoration_of(entry, entry.number("サイズ", 34.0), log))
    return GeneratedSource(kind="text", params=params)


def _concentration(
    entry: ExoEntry, points: tuple[int, ...], log: CompatibilityReport
) -> GeneratedSource:
    """集中線 AviUtl2 ではカスタムオブジェクト（図形の仲間）

    項目は ``濃さ`` ``速さ`` ``中心幅`` ``色`` AviUtl2 に置かせて読み取った

    ``濃さ`` は本数でも太さでもなく、その両方に効く AviUtl2 に 40・80・160 を
    描かせて角度の占有率を測ると 18%・約 65%・100% と 2 乗で増えたので、
    本数と 1 本の太さの両方へ掛ける 片方だけに渡すと、濃くしたときに
    線がただ増える（または太るだけの）別の絵になる
    """
    density = animated_value(
        entry.params.get("濃さ"), points=points, log=log, label="集中線の濃さ", default=40.0
    )
    return GeneratedSource(
        kind="shape",
        params={
            "shape": "concentration",
            # AviUtl の集中線は画面いっぱい 中心幅（真ん中の空き）が 0 でも同じ
            # 空きの有無で描き方を変えると、空きを 0 にしたものだけが
            # YMM4 の作り（大きさの円に収まる小さな絵）に落ちる
            "fill_frame": True,
            # 濃さ 40 で 64 本・占有率 18%（実測）に合わせた係数
            # 太さは 2 乗 100% を超えると線が重なり、濃さ 160 で画面が埋まる
            "density": _mapped(density, lambda value: value * 1.6),
            "line_thickness": _mapped(density, _line_thickness),
            "center_gap": animated_value(
                entry.params.get("中心幅"),
                points=points,
                log=log,
                label="集中線の中心幅",
                default=300.0,
            ),
            "flicker": animated_value(
                entry.params.get("速さ"), points=points, log=log, label="集中線の速さ", default=25.0
            ),
            "color": _color(entry.value("色", default="ffffff")),
        },
    )


#: カスタムオブジェクトが読み込む図形（``--figure``）の名前と、こちらの形
#: 三角形は円に内接する形 ライン(移動軌跡) の先端を AviUtl2 に描かせて測ると、
#: 大きさ 48 で高さ 36・底辺 41 だった（四角に合わせた三角形なら 48 と 48）
_SCRIPT_FIGURES: dict[str, str] = {
    "円": "ellipse",
    "四角形": "rect",
    "三角形": "inscribed_triangle",
    "五角形": "pentagon",
    "六角形": "hexagon",
    "星型": "star",
}


def _script_figure(entry: ExoEntry, key: str, default: str, log: CompatibilityReport) -> str:
    """図形の名前を形へ 知らない図形（ハートや、自分で足した画像の図形）は記録して既定へ"""
    named = entry.params.get(key, "").strip()
    if not named:
        return default
    shape = _SCRIPT_FIGURES.get(named)
    if shape is None:
        log.note_missing(f"{entry.name}の{key}: {named}")
        return default
    return shape


def _tracks_of(
    entry: ExoEntry, points: tuple[int, ...], log: CompatibilityReport
) -> Callable[[str, float], AnimatedValue]:
    """カスタムオブジェクトの項目を、名前と既定値だけで動く値として読む道具

    項目ごとに ``animated_value`` の引数を並べると、ラベルの付け忘れで記録に
    どの項目か分からない行が残る 名前を 1 か所で組み立てる
    """

    def track(key: str, default: float) -> AnimatedValue:
        return animated_value(
            entry.params.get(key),
            points=points,
            log=log,
            label=f"{entry.name}の{key}",
            default=default,
        )

    return track


def _motion_trail(
    entry: ExoEntry, points: tuple[int, ...], log: CompatibilityReport
) -> GeneratedSource:
    """ライン(移動軌跡) AviUtl2 のカスタムオブジェクト

    名前は「ライン」だが折れ線ではなく、**オブジェクトが通った跡**を描く
    （本体の ``script.obj2`` の式を読んで確かめた） たどる位置は描画設定の X と Y で、
    それは :func:`map_object` が図形へ渡す ここでは線と先端の見た目だけを読む
    """

    track = _tracks_of(entry, points, log)
    return GeneratedSource(
        kind="shape",
        params={
            "shape": "motion_trail",
            "line_width": track("ライン幅", 16.0),
            "trail_head_size": track("先端", 48.0),
            "trail_head_angle": track("先端角度", 0.0),
            "trail_head_offset": track("先端位置補正", 70.0),
            "trail_head_shape": _script_figure(entry, "先端図形", "inscribed_triangle", log),
            "trail_speed": track("固定速度", 0.0),
            "trail_interval": track("描画間隔", 10.0),
            "trail_min_step": track("最小間隔", 2.0),
            "trail_core": track("主線描画(%)", 100.0),
            "trail_band": track("補助描画(%)", 0.0),
            "color": _color(entry.value("色", default="ffffff")),
        },
    )


def _star_field(
    entry: ExoEntry, points: tuple[int, ...], log: CompatibilityReport
) -> GeneratedSource:
    """星 AviUtl2 のカスタムオブジェクト

    名前は「星」だが星形ではなく、**奥から手前へ流れてくる星空** 本体の
    ``script.obj2`` の式を移した（:func:`sashimono.engine.motion_shapes.star_field`）
    粒の置き場所は乱数なので 1 枚ずつは合わない 数・流れる向き・速さを実物の絵で確かめた
    """

    track = _tracks_of(entry, points, log)
    return GeneratedSource(
        kind="shape",
        params={
            "shape": "starfield",
            "star_count": track("個数", 1500.0),
            "star_speed": track("速度", 6.0),
            "star_spread": track("広がり", 12.0),
            "star_depth": track("奥行き", 20.0),
            "star_size": track("サイズ", 30.0),
            "star_shape": _script_figure(entry, "形状", "ellipse", log),
            "star_fade_in": track("フェードイン時間", 0.15),
            "star_fade_out": track("フェードアウト時間", 0.15),
            "color": _color(entry.value("色", default="ddddff")),
        },
    )


def _waveform(
    entry: ExoEntry, path: str, points: tuple[int, ...], log: CompatibilityReport
) -> GeneratedSource:
    """音声波形表示 AviUtl2 の**メディアオブジェクト**（フィルタではない）

    自分の ``ファイル`` の音を、今の時刻から 1 画素 1 サンプルで横に並べて線にする
    AviUtl2 に描かせた絵を素材のサンプルと突き合わせて読んだ（44.1kHz のプロジェクトで
    横幅 800 の線が 800 サンプル、最後のフレームではクリップの終わりから先が 0 だった）
    再生位置と再生速度は音声ファイルと同じくクリップの切り出しへ写す（:func:`_playback`）

    ``波形のプリセット`` は**読まずに捨てる** UI の上だけの道具で、選ぶとスペクトラム表示・
    ミラー表示・解像度・スペースの値を項目へ書き込み、名前の欄は空に戻る（v2.1.6a で
    Type1〜5 を選んで保存させると、どれも ``波形のプリセット=`` で値の方が変わっていた）
    ファイルに名前だけを書いても絵は変わらなかった（Type1〜5 の 5 本とも既定と同じ絵）
    名前から値を引き直すと、名前を書き換えただけのファイルで AviUtl2 と違う絵になる

    ``ミラー表示`` は線では絵が変わらなかった（差は圧縮の揺れの 0.02） スペクトラムでは
    棒を上下の真ん中に置く（:func:`~sashimono.engine.audio_shapes.spectrum_cells`）
    """
    spectrum = entry.number("スペクトラム表示") != 0.0
    track = _tracks_of(entry, points, log)
    # 再生範囲の 2 つ目の値は、素材をどこまで読むか 始めと同じ値（10,10）の見本は
    # AviUtl2 で何も描かれなかった 読まずに素材の続きを描くと、無いはずの波形が出る
    position = parse_motion(entry.params.get("再生位置"))
    end = -1
    if position is not None and position.method == "再生範囲" and len(position.values) >= 2:
        end = max(0, round(position.values[-1] * 1000.0))
    return GeneratedSource(
        kind="shape",
        params={
            "shape": "waveform",
            "audio_end_ms": end,
            "width": track("横幅", 800.0),
            "height": track("高さ", 400.0),
            "wave_volume": track("音量", 100.0),
            "wave_spectrum": spectrum,
            "wave_mirror": entry.number("ミラー表示") != 0.0,
            # 解像度とスペースは素の値のまま渡す（スペースは升の幅に対する %）
            # 横 16 升・スペース 4 で、幅 50 の升の境目に 2 画素のすき間だった
            "wave_columns": track("横解像度", 0.0),
            "wave_rows": track("縦解像度", 0.0),
            "wave_gap_x": track("横スペース", 0.0),
            "wave_gap_y": track("縦スペース", 0.0),
            "audio_path": path,
            "color": _color(entry.value("波形の色", default="ffffff")),
        },
    )


#: 色として読むパラメータ ``ffffff`` の形で入っている **AviUtl の効果名で引く**
_COLOR_PARAMS: dict[str, dict[str, str]] = {
    "縁取り": {"縁色": "color", "色": "color"},
    "枠線": {"枠色": "color"},
    "グラデーション": {"開始色": "start_color", "終了色": "end_color"},
    "影": {"影色": "color", "色": "color"},
    "シャドー": {"影色": "color", "色": "color"},
    "ドロップシャドウ": {"影色": "color"},
    "クロマキー": {"色": "key_color"},
    "カラーキー": {"基準色": "key_color"},
    "単色化": {"色": "color"},
    "エッジ抽出": {"色": "color"},
    "発光": {"光色": "tint"},
    "グロー": {"光色": "tint"},
    "拡散光": {"光色": "tint"},
    "凸エッジ": {"光色": "color"},
    "閃光": {"光色": "light_color"},
    "グラデーションマップ": {"暗部色": "dark_color", "明部色": "light_color"},
    "特定色域変換": {"変換前の色": "key_color", "変換後の色": "to_color"},
}

#: AviUtl2 の ``合成モード`` 番号ではなく表示名で入っている
_BLEND_NAMES: dict[str, str] = {
    "通常": "normal",
    "加算": "add",
    "減算": "subtract",
    "乗算": "multiply",
    "スクリーン": "screen",
    "オーバーレイ": "overlay",
    "比較(明)": "lighten",
    "比較(暗)": "darken",
}

#: 選択肢として読むパラメータ ``元の名前 -> (こちらの名前, 表示名の対応)``
_SELECT_PARAMS: dict[str, dict[str, tuple[str, dict[str, str]]]] = {
    "グラデーション": {
        "形状": ("shape", {"線形": "linear", "円形": "radial"}),
        # 配布物は加算や乗算で重ねる使い方が多い 通常のままだと見た目が別物になる
        "合成モード": ("blend", _BLEND_NAMES),
    },
    "マスク": {"種類": ("shape", {"矩形": "rect", "円": "ellipse", "楕円": "ellipse"})},
    # 画像合成の 合成モード 名前は AviUtl2 v2.1.6a の一覧から読み、1 つずつ描かせた
    "画像合成": {
        "合成モード": (
            "blend",
            {
                "前方から合成": "front",
                "後方から合成": "back",
                "色情報を上書き": "overwrite",
                "輝度をアルファ値として上書き": "luma_alpha",
                "輝度をアルファ値として乗算": "luma_multiply",
            },
        )
    },
    "ミラー": {
        "ミラーの方向": ("side", {"下側": "bottom", "上側": "top", "左側": "left", "右側": "right"})
    },
    "ディスプレイスメントマップ": {
        "マップの種類": (
            "map_kind",
            {"円": "circle", "四角": "rect", "横": "horizontal", "縦": "vertical"},
        )
    },
    "ルミナンスキー": {
        # 暗い部分を透過 ＝ 明るい所が残る こちらの旗は「暗いところを残す」
        "モード": ("invert", {"暗い部分を透過": "", "明るい部分を透過": "1"}),
    },
}


#: 登場・退場の効果と、こちらのエフェクト種別
#:
#: AviUtl は ``フェード`` ``ワイプ`` だけ「イン」「アウト」を**秒**で持ち、
#: ほかは ``時間`` 1 つで登場だけに掛かる こちらは登場と退場を旗で選び、
#: 時間は 1 つしか持てないので、食い違うときは記録に残す
_APPEARANCE: dict[str, str] = {
    "フェード": "inout_fade",
    "ワイプ": "inout_wipe",
    "画面外から登場": "inout_move",
    "拡大縮小して登場": "inout_zoom",
    "広がって登場": "inout_zoom",
    "起き上がって登場": "inout_getup",
    "弾んで登場": "inout_jump",
    "何処からともなく登場": "inout_blur",
    "ランダム方向から登場": "inout_random_direction",
    "ランダム間隔で落ちながら登場": "inout_fall",
    "点滅して登場": "inout_blink",
}

#: ``ワイプの種類`` の対応 付属の絵は式で作ってある（[[互換性の穴]]）
_WIPE_PATTERNS: dict[str, str] = {
    "ワイプ(横)": "horizontal",
    "ワイプ(縦)": "vertical",
    "ワイプ(円)": "circle",
    "ワイプ(四角)": "square",
    "ワイプ(時計回り)": "clockwise",
}

#: ``画面外から登場`` の角度と向き AviUtl の角度は画面で時計回り
#: （Y が下向き正なので 90 度は下）
_MOVE_DIRECTIONS: dict[int, str] = {0: "right", 90: "bottom", 180: "left", 270: "top"}


def _appearance(
    entry: ExoEntry, points: tuple[int, ...], log: CompatibilityReport
) -> Effect | None:
    """登場・退場の効果をエフェクトへ"""
    kind = _APPEARANCE[entry.name]
    definition = registry.get(kind)
    if definition is None:  # pragma: no cover - 対応表の種別は必ずある
        return None

    params = definition.default_params()
    if entry.name in ("フェード", "ワイプ"):
        enter = entry.number("イン")
        leave = entry.number("アウト")
        if enter > 0.0 and leave > 0.0 and enter != leave:
            # こちらは時間を 1 つしか持てない 長い方に合わせる
            log.note_missing(f"{entry.name}の登場と退場で違う時間")
        seconds = max(enter, leave)
    else:
        enter = leave = 0.0
        seconds = entry.number("時間")
        enter = seconds
    params["effect_in"] = enter > 0.0
    params["effect_out"] = leave > 0.0
    if entry.name in ("フェード", "ワイプ") or not _put_raw(
        definition, params, "effect_time", entry, "時間", points, log
    ):
        _put(definition, params, "effect_time", seconds)

    # 加減速の旗は、両端を滑らかにするイージング（時間制御のプリセットと同じ考え方）
    if entry.number("加減速") != 0.0:
        params["easing"] = "sine"
        params["easing_mode"] = "inout"

    _appearance_extras(entry, definition, params, points, log)
    return Effect(kind=kind, params=params)


def _appearance_extras(
    entry: ExoEntry,
    definition: EffectDefinition,
    params: dict[str, ParamValue],
    points: tuple[int, ...],
    log: CompatibilityReport,
) -> None:
    """効果ごとの追加の項目 写せないものはここで記録に残す"""
    if entry.name == "ワイプ":
        shape = entry.params.get("ワイプの種類", "").strip()
        pattern = _WIPE_PATTERNS.get(shape)
        if pattern is None:
            # 付属の 5 枚以外の絵は作れない フェードで代わりにする
            log.note_missing(f"ワイプの種類: {shape}")
            pattern = "fade"
        params["pattern"] = pattern
        _put_raw(definition, params, "tolerance", entry, "ぼかし", points, log)
        params["reverse_in"] = entry.number("反転(イン)") != 0.0
        params["reverse_out"] = entry.number("反転(アウト)") != 0.0
        return

    if entry.name == "画面外から登場":
        angle = round(entry.number("角度")) % 360
        direction = _MOVE_DIRECTIONS.get(angle)
        if direction is None:
            # 向きは上下左右の 4 つしか持てない 近い方へ丸める
            # 角度は 1 周でつながっているので、差は 0 度をまたぐ側も見る
            # （まっすぐ引き算すると 359 度が右ではなく上になる）
            log.note_missing(f"画面外から登場の角度: {entry.number('角度')}")
            direction = _MOVE_DIRECTIONS[min(_MOVE_DIRECTIONS, key=lambda key: _turn(key, angle))]
        params["direction"] = direction
        if entry.number("数", 1.0) != 1.0:
            log.note_missing("画面外から登場の数（何回も出入りする）")
        if entry.number("ランダム方向") != 0.0:
            log.note_missing("画面外から登場のランダム方向")
        return

    if entry.name == "ランダム方向から登場":
        # 回転 は飛んでくるあいだに回る周の数 ライト は明るさの足し算
        # AviUtl2 に描かせると、ライト 30 のとき途中の明るさが 238 → 190 と落ちた
        _put_raw(definition, params, "spin", entry, "回転", points, log)
        _put_raw(definition, params, "light", entry, "ライト", points, log)
        return

    if entry.name == "ランダム間隔で落ちながら登場":
        # 距離 は落ち始めの高さ 間隔 は落ち始めが遅れる幅（秒）
        # 遅れ 0・距離 200・加減速なしの見本で、真っ直ぐ落ちて濃くなった
        _put_raw(definition, params, "distance_", entry, "距離", points, log)
        _put_raw(definition, params, "interval", entry, "間隔", points, log)
        return

    if entry.name == "点滅して登場":
        # 点滅間隔 はフレーム数 一定にする を外すと区切りごとに長さが揺れる
        _put_raw(definition, params, "interval", entry, "点滅間隔", points, log)
        params["even"] = entry.number("点滅間隔を一定にする") != 0.0
        return

    if entry.name == "拡大縮小して登場":
        _put_raw(definition, params, "zoom", entry, "拡大率", points, log)
        return

    if entry.name == "広がって登場":
        # 隠れたときに片方の軸だけ 0 になる 縦方向の旗で軸が入れ替わる
        vertical = entry.number("縦方向") != 0.0
        _put(definition, params, "zoom", 100.0)
        _put(definition, params, "zoom_x", 100.0 if vertical else 0.0)
        _put(definition, params, "zoom_y", 0.0 if vertical else 100.0)
        return

    if entry.name == "起き上がって登場":
        if entry.number("勢い") != 0.0:
            log.note_missing("起き上がって登場の勢い")
        return

    if entry.name == "弾んで登場":
        _put_raw(definition, params, "height", entry, "高さ", points, log)
        count = entry.number("回数")
        if count > 0.0:
            # 1 回の長さ ＝ 全体の時間 ÷ 回数
            _put(definition, params, "period", entry.number("時間") / count)
        return

    if entry.name == "何処からともなく登場":
        _put_raw(definition, params, "radius", entry, "ぼかし", points, log)
        if entry.number("位置") != 0.0:
            log.note_missing("何処からともなく登場の位置（ずれながら出る）")
        del points
        return


def _put(
    definition: EffectDefinition, params: dict[str, ParamValue], name: str, value: float
) -> None:
    """項目へ 1 つの数を入れる 仕様が無ければ何もしない

    こちらで計算した数（回数から出した 1 回の長さなど）を入れるときに使う
    ファイルの値をそのまま入れるときは :func:`_put_raw` を使う 動きが落ちるため
    """
    spec = definition.spec(name)
    if spec is not None:
        params[spec.name] = spec.coerce(value)


def _put_raw(
    definition: EffectDefinition,
    params: dict[str, ParamValue],
    name: str,
    entry: ExoEntry,
    source: str,
    points: tuple[int, ...],
    log: CompatibilityReport,
    convert: Callable[[float], float] | None = None,
) -> bool:
    """ファイルの項目をそのまま写す 動きが付いていればキーフレームも残す

    返り値は写せたか（項目がファイルに無ければ ``False``）
    """
    spec = definition.spec(name)
    raw = entry.params.get(source)
    if spec is None or raw is None:
        return False
    params[spec.name] = spec.coerce(
        _spec_value(spec, raw, points, log, f"{entry.name}の{source}", convert=convert)
    )
    return True


def _filter(entry: ExoEntry, points: tuple[int, ...], log: CompatibilityReport) -> Effect | None:
    """フィルタをエフェクトへ"""
    if entry.name == "アニメーション効果":
        return _animation(entry, points, log)
    if "@" in entry.name:
        # AviUtl2 のエイリアスはスクリプトを ``表示名@ファイル名`` で書く
        return _script_filter(entry, points, log)
    if entry.name in _APPEARANCE:
        return _appearance(entry, points, log)

    kind = _FILTERS.get(entry.name)
    if kind is None:
        log.note_missing(f"フィルタ: {entry.name}")
        return None

    definition = registry.get(kind)
    if definition is None:  # pragma: no cover - 対応表の種別は必ずある
        return None

    params = definition.default_params()
    if entry.name == "扇クリッピング":
        # 形は扇で固定 中心を通る角度の範囲だけを残す
        params["shape"] = "fan"
    if entry.name == "振り子":
        # 元の角度を挟んで振れる（片側だけに振れるのではない）
        params["centering"] = True
    names = _PARAMS.get(entry.name, {})
    colours = _COLOR_PARAMS.get(entry.name, {})
    choices = _SELECT_PARAMS.get(entry.name, {})
    handled: set[str] = set(_IGNORED.get(entry.name, ()))
    for source_name, value in entry.params.items():
        target = names.get(source_name)
        if target is not None:
            handled.add(source_name)
            for field in (target.name, target.also):
                spec = definition.spec(field) if field is not None else None
                if spec is not None:
                    params[spec.name] = spec.coerce(
                        _spec_value(
                            spec,
                            value,
                            points,
                            log,
                            f"{entry.name}の{source_name}",
                            convert=target.convert,
                        )
                    )
            continue

        colour_target = colours.get(source_name)
        if colour_target is not None:
            handled.add(source_name)
            spec = definition.spec(colour_target)
            if spec is not None:
                params[spec.name] = spec.coerce(_color(value))
            continue

        choice = choices.get(source_name)
        if choice is not None:
            target_name, table = choice
            chosen = table.get(value.strip())
            if chosen is None:
                # 表に無い選択肢 既定値のままになるので、写せたことにしない
                continue
            handled.add(source_name)
            spec = definition.spec(target_name)
            if spec is not None:
                params[spec.name] = spec.coerce(chosen)

    _check_images(entry, definition, params, handled, log)
    _note_dropped(entry, handled, log)
    return Effect(kind=kind, params=params)


def _check_images(
    entry: ExoEntry,
    definition: EffectDefinition,
    params: dict[str, ParamValue],
    handled: set[str],
    log: CompatibilityReport,
) -> None:
    """画像として読むパス（画像合成の 画像、縁取りの パターン画像）を確かめる

    中身は開かない（開くのは描くとき） 在るかどうかと拡張子だけを見る
    エイリアスは作った人の機械のパスをそのまま持っているので、別の機械では
    見つからないことが多い 描くときは画像なしで描くので、黙っていると
    模様の消えた絵が出たことに気付けない

    ``ループ再生`` は動画を繰り返すかどうか 静止画か画像なしなら絵は変わらないので
    写せたことにする 動画は先頭のコマしか使わないので、そのときは項目ごと記録に残る
    """
    movie = False
    images = False
    for spec in definition.parameters:
        if not (isinstance(spec, FileSpec) and spec.texture):
            continue
        images = True
        path = params.get(spec.name)
        if not isinstance(path, str) or not path:
            continue
        if Path(path).suffix.lower() not in IMAGE_SUFFIXES:
            movie = True
            log.note_missing(f"{entry.name}の画像が静止画でない（先頭のコマだけ使う）")
        if not Path(path).is_file():
            log.note_missing(f"{entry.name}の画像が見つからない")
    # 画像を読まないエフェクトでは写せたことにしない（ほかのフィルタの同じ名前の
    # 項目が、写していないのに記録から消える）
    if images and not movie:
        handled.add("ループ再生")


#: 値ではなく並びの区切りに使われる項目 写せなくても困らない
_STRUCTURAL = ("Group", "詳細設定")


def _is_off(value: str) -> bool:
    """その項目が「使っていない」状態か

    空か、値が全部 0 なら、写さなくても見た目は変わらない
    手元の配布物では ``縁取り`` の ``ぼかし`` が 26 本とも 0 で、これを記録に
    出していたせいで、本当に埋めるべき穴が埋もれていた

    **動きが付いていれば 0 でも使っている** ``0,0,回転,4|360`` や
    ``0`` から始まる参照式は、時間が進むと 0 ではなくなる
    移動方法の名前だけでは見ない（``0,0,直線移動,0`` は名前が付いていても動かない）
    加速と減速の旗も同じで、値が動かなければ見た目は変わらない
    """
    text = value.strip()
    if not text:
        return True
    motion = parse_motion(text)
    if motion is None or _varies(motion):
        return False
    return all(number == 0.0 for number in motion.values)


def _note_dropped(entry: ExoEntry, handled: set[str], log: CompatibilityReport) -> None:
    """対応表に無い項目のうち、**使われているもの**を記録する

    効果そのものを写せても、項目を落としていれば見た目は変わる（``震える`` の
    ``角度`` など） 黙って捨てると、写せたつもりのまま違う絵が出る

    **選ぶ項目は 0 でも記録する** AviUtl1 世代は選択肢を名前ではなく番号で書くので、
    ``ミラーの方向=0`` のような値が「使っていない」と見なされて消えていた
    番号がどの選択肢かは実物で確かめていないため、既定値のまま黙って進むと
    向きの違う絵が出たことに気付けない
    """
    choices = _SELECT_PARAMS.get(entry.name, {})
    for source_name, value in entry.params.items():
        if source_name in handled:
            continue
        if source_name.startswith(_STRUCTURAL) or source_name.endswith(".hide"):
            continue
        if source_name not in choices and _is_off(value):
            continue
        log.note_missing(f"{entry.name}の項目: {source_name}")


def _script_filter(
    entry: ExoEntry, points: tuple[int, ...], log: CompatibilityReport
) -> Effect | None:
    """``表示名@ファイル名`` で書かれたスクリプトを繋ぐ

    AviUtl2 のエイリアスはアニメーション効果を専用の名前ではなく、この形で
    直接書く 手元にスクリプトが無ければ、何を要求されたかだけ残す
    """
    from sashimono.compat.aviutl.catalog import script_catalog

    label, _, _file = entry.name.partition("@")
    found = next((item for item in script_catalog().all() if item.label == label), None)
    if found is None:
        log.note_missing(f"スクリプト: {entry.name}")
        return None

    definition = registry.get(found.identifier)
    if definition is None:  # pragma: no cover - 登録済みのはず
        return None

    params = definition.default_params()
    for source_name, value in entry.params.items():
        spec = definition.spec(source_name)
        if spec is None:
            # 制御文字で名前を付けていないスクリプトは track0..3 で並ぶ
            spec = next((s for s in definition.parameters if s.label == source_name), None)
        if spec is not None:
            params[spec.name] = spec.coerce(
                _spec_value(spec, value, points, log, f"{entry.name}の{source_name}")
            )
    return Effect(kind=found.identifier, params=params)


def _animation(entry: ExoEntry, points: tuple[int, ...], log: CompatibilityReport) -> Effect | None:
    """アニメーション効果 スクリプトが手元にあれば繋ぐ"""
    from sashimono.compat.aviutl.catalog import script_catalog

    name = entry.params.get("name", "")
    catalog = script_catalog()
    found = next((item for item in catalog.all() if item.label == name), None)
    if found is None:
        log.note_missing(f"アニメーション効果: {name}")
        return None

    definition = registry.get(found.identifier)
    if definition is None:  # pragma: no cover - 登録済みのはず
        return None

    params = definition.default_params()
    for index in range(4):
        spec = definition.spec(f"track{index}")
        raw = entry.params.get(f"param{index}") or entry.params.get(str(index))
        if spec is not None and raw is not None:
            params[spec.name] = spec.coerce(
                _spec_value(spec, raw, points, log, f"{name}の track{index}")
            )
    return Effect(kind=found.identifier, params=params)


def _tracks_for(project: Project, layers: set[int], commands: list[Command]) -> dict[int, Track]:
    """レイヤー番号に対応する映像トラックを用意する

    間が空いていても埋める AviUtl のレイヤー番号は上下の位置そのものなので、
    空きレイヤーを詰めると重ね順が変わってしまう
    """
    existing = list(project.timeline.video_tracks())
    tracks: dict[int, Track] = {}
    for layer in range(1, max(layers, default=0) + 1):
        if layer - 1 < len(existing):
            tracks[layer] = existing[layer - 1]
            continue
        track = Track(kind=TrackKind.VIDEO, name=f"V{layer}")
        commands.append(AddTrack(track))
        tracks[layer] = track
    return tracks


#: こちらの合成器が持っている方法 AviUtl の合成モードはすべて揃った
_SUPPORTED_BLENDS = frozenset(
    {"normal", "add", "subtract", "multiply", "screen", "overlay", "lighten", "darken"}
)


def _whole_number(raw: str) -> int:
    """整数を表す文字（``1`` ``1.0`` ``1e0``）なら番号 それ以外は -1

    数でない値や ``1.5`` を 0 や 1 と読むと、対応済みの合成に見えて記録から漏れる
    """
    try:
        value = float(raw)
    except ValueError:
        return -1
    if not math.isfinite(value) or not value.is_integer():
        return -1
    return int(value)


def _blend_of(entry: ExoEntry, log: CompatibilityReport) -> str:
    named = entry.params.get("合成モード")
    if named is not None:
        # 知らない名前を既定で「通常」にすると、下の判定で対応済みに見えて
        # 記録に残らない 未知の名前はそのまま渡して記録させる
        mode = _BLEND_NAMES.get(named.strip(), named.strip())
    else:
        raw = entry.params.get("blend", "0").strip()
        index = _whole_number(raw)
        # 表に無い番号（輝度・色差など）も記録に残るよう、番号のまま渡す
        mode = _BLEND_MODES[index] if 0 <= index < len(_BLEND_MODES) else f"番号 {raw}"

    # こちらに無い合成方法は通常扱いにする 似た別のもので代用すると、
    # 直したつもりの無い違いが出る
    if mode not in _SUPPORTED_BLENDS:
        log.note_missing(f"合成モード: {named or mode}")
        return "normal"
    return mode


def _align(index: int) -> str:
    return ("center", "left", "right")[index % 3] if 0 <= index < 9 else "center"


def _line_thickness(density: float) -> float:
    """集中線の ``濃さ`` を、線 1 本の太さ（間隔に対する %）へ

    濃さ 40 のとき 33%（実測の占有率 18% に当たる） そこから 2 乗で増やす
    線形にすると、濃さ 160 で埋まるはずの画面が半分しか埋まらない
    """
    return min((density / 40.0) ** 2 * 33.0, 400.0)


def _mapped(value: AnimatedValue, convert: Callable[[float], float]) -> AnimatedValue:
    """動く値の中身を、キーフレームごと作り直す

    1 つの項目が 2 つの値へ効くとき（集中線の ``濃さ`` は本数と太さの両方）に使う
    元の欄を 2 回読ませると、読めなかったときの記録が二重に残る
    """
    return AnimatedValue(
        static=convert(value.static),
        keyframes=tuple(
            replace(keyframe, value=convert(keyframe.value)) for keyframe in value.keyframes
        ),
    )


def _color(value: str) -> tuple[float, ...]:
    """``ffffff`` の形の色を 0..1 の組へ

    頭の飾りは ``removeprefix`` で 1 つずつ落とす ``lstrip`` は文字の集合として
    削るので、``000000``（黒）が空文字になって「読めない色」＝白へ落ちる
    黒は縁取りと影の既定色なので、配布物のほとんどが白く塗り潰される
    """
    parsed = _hex_color(value)
    return parsed if parsed is not None else (1.0, 1.0, 1.0, 1.0)


def _hex_color(value: str) -> tuple[float, ...] | None:
    """``ffffff`` の形の色を 0..1 の組へ 読めなければ ``None``

    読めないときにどの色へ倒すかは、呼ぶ側が決める（組み込みのフィルタは白、
    スクリプトの色欄はそのスクリプトが決めた既定の色）
    """
    text = value.strip().removeprefix("#")
    for prefix in ("0x", "0X"):
        text = text.removeprefix(prefix)
    try:
        number = int(text, 16)
    except ValueError:
        return None
    return (
        ((number >> 16) & 0xFF) / 255.0,
        ((number >> 8) & 0xFF) / 255.0,
        (number & 0xFF) / 255.0,
        1.0,
    )


def _as_number(value: str) -> float | None:
    """数として読む 読めなければ ``None``

    多角形の座標のように「読めない要素は落とす」場面があるので、0 では返さない
    """
    try:
        number = float(value)
    except ValueError:
        return None
    # NaN と無限大は数として扱わない 座標や大きさに入ると描画の側で落ちる
    return number if math.isfinite(number) else None


#: 0 秒 いちいち ``Fraction(0)`` と書かずに済ませる
ZERO = Fraction(0)


#: 再生の項目で「止まっている」と言い切れる移動方法 これ以外は中身を知らない
_STILL_PLAYBACK = frozenset({"", "移動無し", "再生範囲"})


def _playback_value(
    entry: ExoEntry, key: str, log: CompatibilityReport, label: str
) -> Motion | None:
    """再生の項目を読む 写せないものはここで記録に残す

    :class:`Clip` の切り出し位置と速度は 1 つの値しか持てない 動くもの・式・
    スクリプトの移動方法・知らない移動方法は先頭の値で止まるので、黙って落とさない
    """
    raw = entry.params.get(key)
    if raw is None:
        return None
    motion = parse_motion(raw)
    if motion is None:
        log.note_missing(f"AviUtl の数として読めない{label}")
        return None
    # 再生範囲の 2 つの値は素材の切り出しの始めと終わりで、動きではない
    varies = _varies(motion) if motion.method != "再生範囲" else bool(motion.flags)
    if varies or motion.method not in _STILL_PLAYBACK:
        log.note_missing(f"AviUtl の{label}（1 つの値しか持てない）")
    return motion


def _playback(
    entry: ExoEntry, rate: FrameRate, log: CompatibilityReport
) -> tuple[Fraction, Fraction]:
    """素材の切り出し位置（秒）と再生速度

    AviUtl2 は ``再生位置=0.967,6.151,再生範囲,0`` と**秒**で書く
    AviUtl1 は ``再生位置`` にフレーム番号（1 始まり）で書く（古い ``開始位置``
    という書き方も受ける） 世代 1 の実物は手元に無いので、そちらは確かめていない
    以前はどちらも読めておらず、素材が必ず頭から始まっていた

    :class:`Clip` の切り出し位置と速度は 1 つの値しか持てない 動く再生位置や
    変速は写せないので、記録に残してから先頭の値で止める
    """
    position = _playback_value(entry, "再生位置", log, "動く再生位置")
    if entry.generation >= 2:
        start = Fraction(position.first).limit_denominator(10_000) if position is not None else ZERO
    else:
        frames = position.first if position is not None else float(entry.integer("開始位置", 1))
        start = Fraction(frames - 1).limit_denominator(10_000) * rate.frame_duration

    speed_motion = _playback_value(entry, "再生速度", log, "変速")
    percent = speed_motion.first if speed_motion is not None else 100.0
    if percent <= 0.0:
        # 0 や負の速度は AviUtl では「止める」 こちらは速度に 0 を置けない
        log.note_missing(f"再生速度: {percent}")
        percent = 100.0
    speed = Fraction(percent / 100.0).limit_denominator(1_000)
    return max(ZERO, start), speed


def decode_text_param(value: str) -> str:
    """``.exo`` のテキスト欄を読む 外からも使えるように公開しておく"""
    return decode_utf16_hex(value)
