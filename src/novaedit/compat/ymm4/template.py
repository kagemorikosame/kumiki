"""YMM4 のアイテムテンプレート（``.ymmt``）を、こちらのクリップへ写す。

中身は .NET のシリアライザが書いた JSON で、要素の種類が ``$type`` に入る。
包み方は版で変わりうる（そのままアイテム 1 つ、``Items`` の配列、``Item`` に
1 つ）ので、どれで来ても読めるようにしてある。

**振り分けはクラス名だけで行う。** 名前空間もアセンブリ名も版で変わる。
知らない種類が来たら、その名前を :mod:`~novaedit.compat.aviutl.report` に残して
先へ進む。1 種類読めないだけでテンプレート全体が落ちるのは割に合わない。

.. note::

   このマシンに YMM4 は入っていないため、実際に書き出されたファイルでの
   突き合わせができていない。読めなかった項目は必ず記録に残るので、
   実ファイルを 1 つ通せば足りない対応がそのまま一覧に出る。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from novaedit.compat.aviutl.report import CompatibilityReport, global_report
from novaedit.compat.mapped import MappedObject
from novaedit.compat.ymm4.decorations import map_decorations
from novaedit.compat.ymm4.values import animated, colour, number, type_name
from novaedit.core.model import AnimatedValue, Clip, Effect, GeneratedSource, ParamValue
from novaedit.effects.definition import registry

__all__ = ["TEMPLATE_SUFFIXES", "Ymm4ParseError", "load_template", "map_template"]

#: アイテムテンプレートの拡張子。
TEMPLATE_SUFFIXES = (".ymmt",)

#: YMM4 の合成モードと、こちらの呼び名。
_BLEND_MODES: dict[str, str] = {
    "Normal": "normal",
    "通常": "normal",
    "Add": "add",
    "加算": "add",
    "Multiply": "multiply",
    "乗算": "multiply",
    "Screen": "screen",
    "スクリーン": "screen",
}

#: 図形の種類。
_SHAPES: dict[str, str] = {
    "Rectangle": "rect",
    "四角形": "rect",
    "RoundedRectangle": "rounded",
    "角丸四角形": "rounded",
    "Ellipse": "ellipse",
    "Circle": "ellipse",
    "円": "ellipse",
    "Triangle": "triangle",
    "三角形": "triangle",
    "Star": "star",
    "星": "star",
}

#: 素材を参照するアイテム。中身ではなくパスだけを返す。
_MEDIA_ITEMS: dict[str, str] = {
    "VideoItem": "動画ファイル",
    "ImageItem": "画像ファイル",
    "AudioItem": "音声ファイル",
    "VoiceItem": "音声ファイル",
}


class Ymm4ParseError(ValueError):
    """``.ymmt`` として読めない。"""


def load_template(path: Path) -> list[dict[str, Any]]:
    """ファイルを読んで、アイテムの列を返す。

    YMM4 は UTF-8 で書く。BOM が付くことがあるので ``utf-8-sig`` で開く。
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise Ymm4ParseError(f"開けない: {target} ({exc})") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Ymm4ParseError(f"{target.name}: JSON として読めない ({exc})") from exc
    return _items_of(document, target.name)


def _items_of(document: Any, name: str) -> list[dict[str, Any]]:
    """包み方の違いを吸収して、アイテムの列を取り出す。"""
    if isinstance(document, list):
        return [item for item in document if isinstance(item, dict)]
    if not isinstance(document, dict):
        raise Ymm4ParseError(f"{name}: アイテムが見つからない")

    for key in ("Items", "TimelineItems", "Contents"):
        found = document.get(key)
        if isinstance(found, list):
            return [item for item in found if isinstance(item, dict)]
    single = document.get("Item")
    if isinstance(single, dict):
        return [single]
    if "$type" in document:
        return [document]
    raise Ymm4ParseError(f"{name}: アイテムが見つからない")


def map_template(
    items: list[dict[str, Any]], *, report: CompatibilityReport | None = None
) -> list[MappedObject]:
    """アイテムの列をクリップへ。写せなかったものは飛ばす。"""
    log = report if report is not None else global_report
    mapped = [_map_item(item, log) for item in items]
    return [item for item in mapped if item is not None]


def _map_item(item: dict[str, Any], log: CompatibilityReport) -> MappedObject | None:
    name = type_name(item)
    source, media_path, kind = _content(item, name, log)
    if source is None and not media_path:
        return None

    effects: list[Effect] = []
    if source is not None and source.kind == "text":
        decorations = map_decorations(
            item.get("Decorations"), log, size=number(item.get("FontSize"), 64.0)
        )
        source = GeneratedSource(kind="text", params={**source.params, **decorations.params})
        effects.extend(decorations.effects)

    effects[0:0] = _placement(item)

    length = max(1, int(number(item.get("Length"), 1.0)))
    return MappedObject(
        clip=Clip(
            timeline_start=max(0, int(number(item.get("Frame"), 0.0))),
            duration=length,
            source=source,
            effects=tuple(effects),
            opacity=_opacity(item),
            blend_mode=_BLEND_MODES.get(str(item.get("Blend") or "Normal"), "normal"),
        ),
        # YMM4 のレイヤーは 0 始まり。こちらのトラックは 1 始まり。
        layer=max(1, int(number(item.get("Layer"), 0.0)) + 1),
        media_path=media_path,
        kind=kind,
        has_span="Length" in item,
    )


def _opacity(item: dict[str, Any]) -> AnimatedValue:
    """不透明度。YMM4 は 0..100 で持つ。"""
    return AnimatedValue(min(max(number(item.get("Opacity"), 100.0) / 100.0, 0.0), 1.0))


def _content(
    item: dict[str, Any], name: str, log: CompatibilityReport
) -> tuple[GeneratedSource | None, str, str]:
    if name in ("TextItem", "Text"):
        return _text(item), "", "text"
    if name in ("ShapeItem", "Shape"):
        return _shape(item, log), "", "shape"

    media = _MEDIA_ITEMS.get(name)
    if media is not None:
        return None, str(item.get("FilePath") or item.get("File") or ""), media

    log.note_missing(f"YMM4 のアイテム: {name or '種類不明'}")
    return None, "", name


def _text(item: dict[str, Any]) -> GeneratedSource:
    params: dict[str, ParamValue] = {
        "text": str(item.get("Text") or ""),
        "size": animated(item.get("FontSize"), 64.0),
        "color": colour(item.get("FontColor"), (1.0, 1.0, 1.0, 1.0)),
        "bold": bool(item.get("IsBold")),
        "italic": bool(item.get("IsItalic")),
        "line_spacing": animated(item.get("LineHeight2"), 0.0),
        "letter_spacing": animated(item.get("LetterSpacing2"), 0.0),
        "align": _align(item),
        "vertical": _is_vertical(item),
    }
    font = item.get("Font")
    if isinstance(font, str) and font:
        params["font"] = font
    return GeneratedSource(kind="text", params=params)


def _align(item: dict[str, Any]) -> str:
    raw = str(item.get("BasePoint") or item.get("HorizontalAlignment") or "")
    if "Left" in raw or "左" in raw:
        return "left"
    if "Right" in raw or "右" in raw:
        return "right"
    return "center"


def _is_vertical(item: dict[str, Any]) -> bool:
    direction = str(item.get("FontDirection") or item.get("TextDirection") or "")
    return "Vertical" in direction or "縦" in direction


def _shape(item: dict[str, Any], log: CompatibilityReport) -> GeneratedSource:
    raw = str(item.get("Type") or item.get("ShapeType") or "Rectangle")
    shape = _SHAPES.get(raw)
    if shape is None:
        log.note_missing(f"YMM4 の図形: {raw}")
        shape = "rect"
    return GeneratedSource(
        kind="shape",
        params={
            "shape": shape,
            "width": animated(item.get("Width"), 400.0),
            "height": animated(item.get("Height"), 400.0),
            "color": colour(item.get("Color"), (1.0, 1.0, 1.0, 1.0)),
        },
    )


def _placement(item: dict[str, Any]) -> list[Effect]:
    """位置・拡大・回転を変形エフェクトへ。

    AviUtl 側（:func:`~novaedit.compat.aviutl.mapping.map_object`）と同じ扱いに
    しておく。クリップは配置を持たないので、見た目が同じになるエフェクトへ写す。
    """
    definition = registry.get("transform")
    if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
        return []

    pos_x = animated(item.get("X"), 0.0)
    pos_y = animated(item.get("Y"), 0.0)
    zoom = animated(item.get("Zoom"), 100.0)
    rotation = animated(item.get("Rotation"), 0.0)
    if not any(
        value.is_animated or value.static != rest
        for value, rest in ((pos_x, 0.0), (pos_y, 0.0), (zoom, 100.0), (rotation, 0.0))
    ):
        return []

    return [
        definition.create(
            pos_x=pos_x,
            # YMM4 の Y は下向き。こちらは上向き。
            pos_y=_negated(pos_y),
            scale=zoom,
            scale_y=zoom,
            rotation=rotation,
        )
    ]


def _negated(value: AnimatedValue) -> AnimatedValue:
    return AnimatedValue(
        static=-value.static,
        keyframes=tuple(replace(k, value=-k.value) for k in value.keyframes),
    )
