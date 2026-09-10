"""``.exo`` / ``.exa`` の中身を、こちらのモデルへ写す。

AviUtl のオブジェクトは「中身 1 つ + フィルタの列」でできている。こちらの
:class:`~novaedit.core.model.Clip` も「生成物または素材 + エフェクトの列」なので、
構造はそのまま対応する。写すのは値の名前と単位だけ。

対応が無いものは**捨てずに記録する**（:mod:`novaedit.compat.aviutl.report`）。
読めなかったことに気付けないまま「なんとなく違う絵」が出るのが一番困る。
"""

from __future__ import annotations

from fractions import Fraction

from novaedit.compat.aviutl.encoding import decode_utf16_hex
from novaedit.compat.aviutl.exo import ExoEntry, ExoFile, ExoObject
from novaedit.compat.aviutl.report import CompatibilityReport, global_report
from novaedit.compat.decoration import decoration_params, find_decoration
from novaedit.compat.mapped import MappedObject
from novaedit.core.commands import AddClip, AddTrack, Command
from novaedit.core.model import (
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
from novaedit.core.timebase import FrameRate
from novaedit.effects.definition import registry

__all__ = ["MappedObject", "map_exo", "map_object", "media_paths"]

#: AviUtl の図形の種類（``type`` の番号）。
_FIGURES = ("ellipse", "rect", "triangle", "pentagon", "hexagon", "star", "background")

#: 合成方法の番号。
_BLEND_MODES = ("normal", "add", "subtract", "multiply", "screen", "overlay", "lighten")

#: 中身として扱う要素の名前。これ以外はフィルタ。
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

#: 位置と大きさを決める要素。エフェクトではなくクリップの配置として扱う。
_DRAW_NAMES = frozenset({"標準描画", "拡張描画"})

#: AviUtl のフィルタ名と、こちらのエフェクト種別。
_FILTERS: dict[str, str] = {
    "ぼかし": "blur",
    "発光": "glow",
    "色調補正": "color",
    "クロマキー": "chroma_key",
    "縁取り": "border",
    "影": "shadow",
    "シャドー": "shadow",
    "シャープ": "sharpen",
    "ノイズ": "noise",
    "モザイク": "mosaic",
    "マスク": "mask",
    "クリッピング": "crop",
    "リサイズ": "transform",
    "グラデーション": "gradient",
}

#: フィルタのパラメータ名の対応。
_PARAMS: dict[str, dict[str, str]] = {
    "blur": {"範囲": "radius"},
    "glow": {"強さ": "strength", "しきい値": "threshold", "範囲": "radius"},
    "color": {
        "明るさ": "brightness",
        "コントラスト": "contrast",
        "色相": "hue",
        "彩度": "saturation",
    },
    "chroma_key": {"色相範囲": "hue_range", "彩度範囲": "saturation_range", "境界補正": "softness"},
    # AviUtl2 の縁取りは ``サイズ`` ``ぼかし`` ``縁色``。色は数値ではないので
    # 対応表とは別に扱う（:func:`_filter` を参照）。
    "border": {"サイズ": "width"},
    "gradient": {
        "強さ": "strength",
        "中心X": "center_x",
        "中心Y": "center_y",
        "角度": "angle",
        "幅": "span",
    },
    "shadow": {"X": "offset_x", "Y": "offset_y", "濃さ": "opacity"},
    "sharpen": {"強さ": "strength", "範囲": "radius"},
    "noise": {"強さ": "strength"},
    "mosaic": {"サイズ": "size"},
    "crop": {"上": "top", "下": "bottom", "左": "left", "右": "right"},
}


def media_paths(exo: ExoFile) -> tuple[str, ...]:
    """このファイルが参照している素材のパス。

    読み込みは呼び出し側に任せる。互換層はファイルを開かない（開くと、
    素材が見つからないだけで対応付け全体が失敗しうる）。
    """
    found: list[str] = []
    for obj in exo.objects:
        content = obj.content
        if content is None:
            continue
        if content.name in ("動画ファイル", "画像ファイル", "音声ファイル"):
            path = content.params.get("file", "")
            if path and path not in found:
                found.append(path)
    return tuple(found)


def map_exo(
    exo: ExoFile,
    project: Project,
    *,
    at_frame: int = 0,
    media: dict[str, MediaId] | None = None,
    report: CompatibilityReport | None = None,
) -> list[Command]:
    """ファイル全体を、タイムラインへ置くコマンドの列にする。

    レイヤーはそのままトラックに対応させる。AviUtl のレイヤー 1 が一番下なので、
    こちらの映像トラックの並びと同じ向きになる。
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
    """1 オブジェクトをクリップへ。写せなければ ``None``。"""
    log = report if report is not None else global_report
    content = obj.content
    if content is None:
        return None

    source, media_path, kind = _content(content, log)
    effects: list[Effect] = []
    opacity = AnimatedValue(1.0)
    blend = "normal"
    position = (0.0, 0.0)
    scale = 100.0
    rotation = 0.0

    for entry in obj.filters():
        if entry.name in _DRAW_NAMES:
            position = (entry.number("X"), entry.number("Y"))
            scale = entry.number("拡大率", 100.0)
            # AviUtl2 は軸ごとに分けて持つ。回転として使えるのは Z 軸だけで、
            # X/Y 軸の回転は板を傾ける立体的な変形なのでここでは写せない。
            rotation = entry.numeric("回転", "Z軸回転", default=0.0)
            for axis in ("X軸回転", "Y軸回転"):
                if entry.number(axis) != 0.0:
                    log.note_missing(f"描画設定: {axis}")
            opacity = AnimatedValue(1.0 - entry.number("透明度", 0.0) / 100.0)
            blend = _blend_of(entry, log)
            continue

        effect = _filter(entry, log)
        if effect is not None:
            effects.append(effect)

    # 位置・拡大・回転は変形エフェクトへ。AviUtl では描画設定だが、こちらでは
    # クリップの持ち物ではないので、同じ見た目になるエフェクトへ写す。
    if position != (0.0, 0.0) or scale != 100.0 or rotation != 0.0:
        transform = registry.get("transform")
        if transform is not None:
            effects.insert(
                0,
                transform.create(
                    pos_x=position[0],
                    pos_y=-position[1],
                    scale=scale,
                    scale_y=scale,
                    rotation=rotation,
                ),
            )

    clip = Clip(
        timeline_start=obj.start,
        duration=obj.duration,
        source=source,
        effects=tuple(effects),
        opacity=opacity,
        blend_mode=blend,
    )
    del rate
    return MappedObject(
        clip=clip,
        layer=max(1, obj.layer),
        media_path=media_path,
        kind=kind,
        has_span=obj.span_given,
    )


def _content(entry: ExoEntry, log: CompatibilityReport) -> tuple[GeneratedSource | None, str, str]:
    """中身を生成オブジェクトへ。素材ファイルの場合はパスだけ返す。"""
    if entry.name == "テキスト":
        return _text(entry, log), "", "text"
    if entry.name == "図形":
        return _figure(entry), "", "shape"
    if entry.name in ("動画ファイル", "画像ファイル", "音声ファイル"):
        return None, entry.params.get("file", ""), entry.name

    if entry.name not in _CONTENT_NAMES:
        log.note_missing(f"オブジェクト: {entry.name}")
    else:
        log.note_missing(f"未対応の中身: {entry.name}")
    return None, "", entry.name


#: AviUtl1 の ``type``（文字装飾の番号）と、AviUtl2 での呼び名。
#: 番号で持っているのは AviUtl1 だけで、中身は同じものを指す。
_DECORATION_BY_INDEX = ("標準文字", "影付き文字", "影付き文字（薄）", "縁取り文字")

#: AviUtl2 の ``文字揃え``。``中央揃え[下]`` のように横と縦を 1 つにまとめてある。
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
    """テキストオブジェクト。世代でパラメータ名がまるごと違う。"""
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
    }
    font = entry.value("フォント", "font")
    if font:
        params["font"] = font

    params.update(_decoration_of(entry, size, log))
    if "<?" in str(params["text"]):
        # テキスト欄に Lua を埋め込む書き方（``<?...?>``）。文字として出すと
        # 意味が違うので、そのまま出さずに何が来たかだけ残す。
        log.note_missing("テキスト欄に埋め込まれた Lua（<?...?>）")
    return GeneratedSource(kind="text", params=params)


def _text_alignment(entry: ExoEntry) -> tuple[str, str]:
    """``文字揃え`` を横と縦に分ける。"""
    named = entry.params.get("文字揃え")
    if named is not None:
        return _ALIGNMENTS.get(named.strip(), ("center", "bottom"))
    # AviUtl1 は番号。0..2 が上、3..5 が中、6..8 が下。
    index = entry.integer("align")
    return _align(index), ("top", "middle", "bottom")[min(index // 3, 2)]


def _decoration_of(entry: ExoEntry, size: float, log: CompatibilityReport) -> dict[str, ParamValue]:
    """``文字装飾`` を縁取りと影のパラメータへ。"""
    name = entry.params.get("文字装飾")
    if name is None:
        index = entry.integer("type")
        name = _DECORATION_BY_INDEX[index] if 0 <= index < len(_DECORATION_BY_INDEX) else ""

    decoration = find_decoration(name)
    if decoration is None:
        log.note_missing(f"文字装飾: {name}")
        return {}
    colour = _color(entry.value("影・縁色", "color2", default="000000"))
    return decoration_params(decoration, size, (colour[0], colour[1], colour[2], colour[3]))


def _figure(entry: ExoEntry) -> GeneratedSource:
    index = entry.integer("type")
    size = entry.number("サイズ", 100.0)
    ratio = entry.number("縦横比", 0.0) / 100.0
    width = size * (1.0 - max(0.0, ratio))
    height = size * (1.0 - max(0.0, -ratio))
    return GeneratedSource(
        kind="shape",
        params={
            "shape": _FIGURES[index] if 0 <= index < len(_FIGURES) else "rect",
            "width": AnimatedValue(max(1.0, width)),
            "height": AnimatedValue(max(1.0, height)),
            "color": _color(entry.params.get("color", "ffffff")),
            "line_width": AnimatedValue(entry.number("ライン幅")),
        },
    )


#: 色として読むパラメータ。``ffffff`` の形で入っている。
_COLOR_PARAMS: dict[str, dict[str, str]] = {
    "border": {"縁色": "color", "色": "color"},
    "gradient": {"開始色": "start_color", "終了色": "end_color"},
    "shadow": {"影色": "color", "色": "color"},
    "chroma_key": {"色": "key_color"},
}

#: 選択肢として読むパラメータ。``元の名前 -> (こちらの名前, 表示名の対応)``。
_SELECT_PARAMS: dict[str, dict[str, tuple[str, dict[str, str]]]] = {
    "gradient": {"形状": ("shape", {"線形": "linear", "円形": "radial"})},
    "mask": {"種類": ("shape", {"矩形": "rect", "円": "ellipse", "楕円": "ellipse"})},
}


def _filter(entry: ExoEntry, log: CompatibilityReport) -> Effect | None:
    """フィルタをエフェクトへ。"""
    if entry.name == "アニメーション効果":
        return _animation(entry, log)
    if "@" in entry.name:
        # AviUtl2 のエイリアスはスクリプトを ``表示名@ファイル名`` で書く。
        return _script_filter(entry, log)

    kind = _FILTERS.get(entry.name)
    if kind is None:
        log.note_missing(f"フィルタ: {entry.name}")
        return None

    definition = registry.get(kind)
    if definition is None:  # pragma: no cover - 対応表の種別は必ずある
        return None

    params = definition.default_params()
    names = _PARAMS.get(kind, {})
    colours = _COLOR_PARAMS.get(kind, {})
    choices = _SELECT_PARAMS.get(kind, {})
    for source_name, value in entry.params.items():
        target = names.get(source_name)
        if target is not None:
            spec = definition.spec(target)
            if spec is not None:
                params[spec.name] = spec.coerce(_as_number(value))
            continue

        colour_target = colours.get(source_name)
        if colour_target is not None:
            spec = definition.spec(colour_target)
            if spec is not None:
                params[spec.name] = spec.coerce(_color(value))
            continue

        choice = choices.get(source_name)
        if choice is not None:
            target_name, table = choice
            spec = definition.spec(target_name)
            if spec is not None:
                params[spec.name] = spec.coerce(table.get(value.strip(), ""))
    return Effect(kind=kind, params=params)


def _script_filter(entry: ExoEntry, log: CompatibilityReport) -> Effect | None:
    """``表示名@ファイル名`` で書かれたスクリプトを繋ぐ。

    AviUtl2 のエイリアスはアニメーション効果を専用の名前ではなく、この形で
    直接書く。手元にスクリプトが無ければ、何を要求されたかだけ残す。
    """
    from novaedit.compat.aviutl.catalog import script_catalog

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
            # 制御文字で名前を付けていないスクリプトは track0..3 で並ぶ。
            spec = next((s for s in definition.parameters if s.label == source_name), None)
        if spec is not None:
            params[spec.name] = spec.coerce(_as_number(value))
    return Effect(kind=found.identifier, params=params)


def _animation(entry: ExoEntry, log: CompatibilityReport) -> Effect | None:
    """アニメーション効果。スクリプトが手元にあれば繋ぐ。"""
    from novaedit.compat.aviutl.catalog import script_catalog

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
            params[spec.name] = spec.coerce(_as_number(raw))
    return Effect(kind=found.identifier, params=params)


def _tracks_for(project: Project, layers: set[int], commands: list[Command]) -> dict[int, Track]:
    """レイヤー番号に対応する映像トラックを用意する。

    間が空いていても埋める。AviUtl のレイヤー番号は上下の位置そのものなので、
    空きレイヤーを詰めると重ね順が変わってしまう。
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


#: AviUtl2 の ``合成モード``。番号ではなく表示名で入っている。
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

#: こちらの合成器が持っている方法。
_SUPPORTED_BLENDS = frozenset({"normal", "add", "multiply", "screen"})


def _blend_of(entry: ExoEntry, log: CompatibilityReport) -> str:
    named = entry.params.get("合成モード")
    if named is not None:
        mode = _BLEND_NAMES.get(named.strip(), "normal")
    else:
        index = entry.integer("blend")
        mode = _BLEND_MODES[index] if 0 <= index < len(_BLEND_MODES) else "normal"

    # こちらに無い合成方法は通常扱いにする。似た別のもので代用すると、
    # 直したつもりの無い違いが出る。
    if mode not in _SUPPORTED_BLENDS:
        log.note_missing(f"合成モード: {named or mode}")
        return "normal"
    return mode


def _align(index: int) -> str:
    return ("center", "left", "right")[index % 3] if 0 <= index < 9 else "center"


def _color(value: str) -> tuple[float, ...]:
    """``ffffff`` の形の色を 0..1 の組へ。"""
    text = value.strip().lstrip("#").lstrip("0xX")
    try:
        number = int(text, 16)
    except ValueError:
        return (1.0, 1.0, 1.0, 1.0)
    return (
        ((number >> 16) & 0xFF) / 255.0,
        ((number >> 8) & 0xFF) / 255.0,
        (number & 0xFF) / 255.0,
        1.0,
    )


def _as_number(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0


def _source_in(entry: ExoEntry) -> Fraction:  # pragma: no cover - 素材対応は次の段階
    return Fraction(entry.integer("開始位置", 1) - 1, 1)


def decode_text_param(value: str) -> str:
    """``.exo`` のテキスト欄を読む。外からも使えるように公開しておく。"""
    return decode_utf16_hex(value)
