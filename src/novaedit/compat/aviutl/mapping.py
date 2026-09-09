"""``.exo`` / ``.exa`` の中身を、こちらのモデルへ写す。

AviUtl のオブジェクトは「中身 1 つ + フィルタの列」でできている。こちらの
:class:`~novaedit.core.model.Clip` も「生成物または素材 + エフェクトの列」なので、
構造はそのまま対応する。写すのは値の名前と単位だけ。

対応が無いものは**捨てずに記録する**（:mod:`novaedit.compat.aviutl.report`）。
読めなかったことに気付けないまま「なんとなく違う絵」が出るのが一番困る。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from novaedit.compat.aviutl.encoding import decode_utf16_hex
from novaedit.compat.aviutl.exo import ExoEntry, ExoFile, ExoObject
from novaedit.compat.aviutl.report import CompatibilityReport, global_report
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
    "border": {"サイズ": "width"},
    "shadow": {"X": "offset_x", "Y": "offset_y", "濃さ": "opacity"},
    "sharpen": {"強さ": "strength", "範囲": "radius"},
    "noise": {"強さ": "strength"},
    "mosaic": {"サイズ": "size"},
    "crop": {"上": "top", "下": "bottom", "左": "left", "右": "right"},
}


@dataclass(frozen=True, slots=True)
class MappedObject:
    """1 オブジェクトを写した結果。"""

    clip: Clip
    layer: int
    #: 素材ファイルを参照している場合のパス。読み込みは呼び出し側が行う。
    media_path: str = ""
    kind: str = ""


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
            rotation = entry.number("回転", 0.0)
            opacity = AnimatedValue(1.0 - entry.number("透明度", 0.0) / 100.0)
            blend = _blend_of(entry)
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
        timeline_start=max(0, obj.start - 1),
        duration=obj.duration,
        source=source,
        effects=tuple(effects),
        opacity=opacity,
        blend_mode=blend,
    )
    del rate
    return MappedObject(clip=clip, layer=max(1, obj.layer), media_path=media_path, kind=kind)


def _content(entry: ExoEntry, log: CompatibilityReport) -> tuple[GeneratedSource | None, str, str]:
    """中身を生成オブジェクトへ。素材ファイルの場合はパスだけ返す。"""
    if entry.name == "テキスト":
        return _text(entry), "", "text"
    if entry.name == "図形":
        return _figure(entry), "", "shape"
    if entry.name in ("動画ファイル", "画像ファイル", "音声ファイル"):
        return None, entry.params.get("file", ""), entry.name

    if entry.name not in _CONTENT_NAMES:
        log.note_missing(f"オブジェクト: {entry.name}")
    else:
        log.note_missing(f"未対応の中身: {entry.name}")
    return None, "", entry.name


def _text(entry: ExoEntry) -> GeneratedSource:
    params: dict[str, ParamValue] = {
        "text": entry.text(),
        "size": AnimatedValue(entry.number("サイズ", 48.0)),
        "color": _color(entry.params.get("color", "ffffff")),
        "border_color": _color(entry.params.get("color2", "000000")),
        "bold": entry.integer("B"),
        "italic": entry.integer("I"),
        "line_spacing": AnimatedValue(entry.number("spacing_y")),
        "letter_spacing": AnimatedValue(entry.number("spacing_x")),
        "align": _align(entry.integer("align")),
    }
    font = entry.params.get("font")
    if font:
        params["font"] = font
    if entry.integer("type") in (1, 2, 3):
        # 1=影付き 2=影付き(薄) 3=縁取り。縁取りだけは同じ形で出せる。
        params["border_width"] = AnimatedValue(2.0)
    return GeneratedSource(kind="text", params=params)


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


def _filter(entry: ExoEntry, log: CompatibilityReport) -> Effect | None:
    """フィルタをエフェクトへ。"""
    if entry.name == "アニメーション効果":
        return _animation(entry, log)

    kind = _FILTERS.get(entry.name)
    if kind is None:
        log.note_missing(f"フィルタ: {entry.name}")
        return None

    definition = registry.get(kind)
    if definition is None:  # pragma: no cover - 対応表の種別は必ずある
        return None

    params = definition.default_params()
    names = _PARAMS.get(kind, {})
    for source_name, value in entry.params.items():
        target = names.get(source_name)
        if target is None:
            continue
        spec = definition.spec(target)
        if spec is not None:
            params[spec.name] = spec.coerce(_as_number(value))
    return Effect(kind=kind, params=params)


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


def _blend_of(entry: ExoEntry) -> str:
    index = entry.integer("blend")
    mode = _BLEND_MODES[index] if 0 <= index < len(_BLEND_MODES) else "normal"
    # こちらに無い合成方法は通常扱いにする。似た別のもので代用すると、
    # 直したつもりの無い違いが出る。
    return mode if mode in ("normal", "add", "multiply", "screen") else "normal"


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
