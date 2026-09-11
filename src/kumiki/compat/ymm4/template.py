"""YMM4 のアイテムテンプレート（``.ymmt``）を、こちらのクリップへ写す

**``.ymmt`` は ZIP** 中に ``catalog.json`` が 1 つ入っていて、その中身がこの形

.. code-block:: json

    {"FilePath": "C:\\…\\なにか.ymmt",
     "ItemTemplates": [{"Name": "アニメーション効果/振り子",
                        "Path": ["アニメーション効果", "振り子"],
                        "Items": [ … ]}],
     "VideoEffectTemplates": [],
     "AudioEffectTemplates": []}

**1 ファイルに何本も入っている** 手元で確かめた配布物は 17 本と 106 本だった
だから :func:`load_template` はテンプレートの**列**を返し、棚
（:mod:`kumiki.compat.catalog`）はそれを 1 本ずつ並べる

アイテムの種類は ``$type`` に入る 振り分けは**クラス名だけ**で行う 実物には
``Version=4.32.0.2, Culture=neutral, PublicKeyToken=null`` まで書かれていて、
丸ごと突き合わせると YMM4 が更新されただけで読めなくなる

知らない種類が来たら、その名前を :mod:`~kumiki.compat.aviutl.report` に残して
先へ進む 1 種類読めないだけでテンプレート全体が落ちるのは割に合わない
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from kumiki.compat.aviutl.report import CompatibilityReport, global_report
from kumiki.compat.mapped import MappedObject
from kumiki.compat.ymm4.decorations import map_decorations, map_video_effects
from kumiki.compat.ymm4.values import animated, colour, number, type_name
from kumiki.core.model import AnimatedValue, Clip, Effect, GeneratedSource, ParamValue
from kumiki.effects.definition import registry

__all__ = [
    "CATALOG_NAME",
    "TEMPLATE_SUFFIXES",
    "ItemTemplate",
    "Ymm4ParseError",
    "load_template",
    "map_template",
]

#: アイテムテンプレートの拡張子
TEMPLATE_SUFFIXES = (".ymmt",)

#: ZIP の中に入っているファイルの名前
CATALOG_NAME = "catalog.json"

#: YMM4 の合成モードと、こちらの呼び名
#: こちらに無いものは通常扱いにして記録に残す
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

#: ``BasePoint`` の横と縦 ``CenterCenter`` ``LeftTop`` のように 2 つ並ぶ
_HORIZONTAL = (("Left", "left"), ("Right", "right"), ("Center", "center"))
_VERTICAL = (("Top", "top"), ("Bottom", "bottom"), ("Center", "middle"))

#: 図形の種類
_SHAPES: dict[str, str] = {
    "Rectangle": "rect",
    "Square": "rect",
    "RoundedRectangle": "rounded",
    "Ellipse": "ellipse",
    "Circle": "ellipse",
    "Triangle": "triangle",
    "Star": "star",
    "Background": "background",
}

#: 素材を参照するアイテム 中身ではなくパスだけを返す
_MEDIA_ITEMS: dict[str, str] = {
    "VideoItem": "動画ファイル",
    "ImageItem": "画像ファイル",
    "AudioItem": "音声ファイル",
    "VoiceItem": "音声ファイル",
}

#: 中身を持たないアイテム 読めないのではなく、それ自体は絵を持たない
#:
#: ``GroupItem`` はまとめた相手に掛かるエフェクトを持つ入れ物 AviUtl の
#: 「フィルタオブジェクト」に近い :func:`map_template` が中身へ移す
_CONTAINER_ITEMS = frozenset({"GroupItem"})


class Ymm4ParseError(ValueError):
    """``.ymmt`` として読めない"""


@dataclass(frozen=True, slots=True)
class ItemTemplate:
    """カタログに入っているテンプレート 1 本"""

    name: str
    #: ``["アニメーション効果", "振り子"]`` のような分類
    path: tuple[str, ...] = ()
    items: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def folder(self) -> str:
        """分類の先頭 棚の見出しに使う"""
        return self.path[0] if len(self.path) > 1 else ""


def load_template(path: Path) -> list[ItemTemplate]:
    """ファイルを読んで、入っているテンプレートの列を返す"""
    target = Path(path)
    try:
        raw = _read_catalog(target)
    except OSError as exc:
        raise Ymm4ParseError(f"開けない: {target} ({exc})") from exc

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Ymm4ParseError(f"{target.name}: JSON として読めない ({exc})") from exc
    return _templates_of(document, target)


def _read_catalog(path: Path) -> str:
    """``.ymmt`` の中身を取り出す

    ZIP なら ``catalog.json`` を、そうでなければファイルそのものを読む
    BOM が付くことがあるので ``utf-8-sig`` で開く
    """
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".json")]
            chosen = CATALOG_NAME if CATALOG_NAME in names else (names[0] if names else "")
            if not chosen:
                raise Ymm4ParseError(f"{path.name}: {CATALOG_NAME} が入っていない")
            return archive.read(chosen).decode("utf-8-sig")
    return path.read_text(encoding="utf-8-sig")


def _templates_of(document: Any, path: Path) -> list[ItemTemplate]:
    """包み方の違いを吸収して、テンプレートの列を取り出す"""
    if isinstance(document, dict):
        catalogued = document.get("ItemTemplates")
        if isinstance(catalogued, list):
            built = [_template_of(entry) for entry in catalogued if isinstance(entry, dict)]
            found = [item for item in built if item.items]
            if found:
                return found
            raise Ymm4ParseError(f"{path.name}: アイテムの入ったテンプレートが無い")

    # 古い形、あるいはアイテムだけを書き出したもの
    items = _bare_items(document)
    if items:
        return [ItemTemplate(name=path.stem, items=tuple(items))]
    raise Ymm4ParseError(f"{path.name}: アイテムが見つからない")


def _template_of(entry: dict[str, Any]) -> ItemTemplate:
    raw_path = entry.get("Path")
    parts = tuple(str(part) for part in raw_path) if isinstance(raw_path, list) else ()
    name = str(entry.get("Name") or (parts[-1] if parts else ""))
    items = entry.get("Items")
    return ItemTemplate(
        name=name,
        path=parts,
        items=tuple(item for item in items if isinstance(item, dict))
        if isinstance(items, list)
        else (),
    )


def _bare_items(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, list):
        return [item for item in document if isinstance(item, dict)]
    if not isinstance(document, dict):
        return []
    for key in ("Items", "TimelineItems", "Contents"):
        found = document.get(key)
        if isinstance(found, list):
            return [item for item in found if isinstance(item, dict)]
    single = document.get("Item")
    if isinstance(single, dict):
        return [single]
    return [document] if "$type" in document else []


def map_template(
    items: list[dict[str, Any]], *, report: CompatibilityReport | None = None
) -> list[MappedObject]:
    """アイテムの列をクリップへ 写せなかったものは飛ばす

    ``GroupItem`` は中身を持たない入れ物で、まとめた相手に掛かるエフェクトを
    持っている こちらのモデルに入れ子は無いので、**同じテンプレートの中身へ
    エフェクトを移して**平らにする

    手元の配布物はどれも「中身 1 つ + グループ 1 つ」の形（``GroupRange`` は 1）
    だったので、この移し方でずれない 中身が無いテンプレート
    （``アニメーション効果/振り子`` のようなもの）は**エフェクトだけ**の
    結果になり、既にあるクリップへ着せて使う
    """
    log = report if report is not None else global_report

    contents: list[MappedObject] = []
    grouped: list[Effect] = []
    for item in items:
        if type_name(item) in _CONTAINER_ITEMS:
            grouped.extend(_group_effects(item, log))
            continue
        mapped = _map_item(item, log)
        if mapped is not None:
            contents.append(mapped)

    if not grouped:
        return contents
    if not contents:
        # 中身のないテンプレート エフェクトだけを返す
        return [
            MappedObject(
                clip=Clip(timeline_start=0, duration=1, effects=tuple(grouped)),
                layer=1,
                kind="effects",
                has_span=False,
            )
        ]
    return [
        replace(item, clip=replace(item.clip, effects=(*item.clip.effects, *grouped)))
        for item in contents
    ]


def _group_effects(item: dict[str, Any], log: CompatibilityReport) -> list[Effect]:
    """``GroupItem`` が持っているエフェクト 位置の動きも含む

    グループに付いた縁取りは、文字そのものの飾りではなく**まとめた絵の外側**に
    掛かる だからテキストの設定ではなく縁取りエフェクトとして扱う
    """
    length = max(1, int(number(item.get("Length"), 1.0)))
    keyframes = item.get("KeyFrames")
    video = map_video_effects(item.get("VideoEffects"), log, length=length, keyframes=keyframes)

    outlines: list[Effect] = []
    border = registry.get("border")
    width = video.params.get("border_width")
    if border is not None and isinstance(width, AnimatedValue) and width.static > 0:
        outlines.append(border.create(width=width, color=video.params.get("border_color")))

    return [*_placement(item, length, keyframes), *outlines, *video.effects]


def _map_item(item: dict[str, Any], log: CompatibilityReport) -> MappedObject | None:
    name = type_name(item)

    length = max(1, int(number(item.get("Length"), 1.0)))
    keyframes = item.get("KeyFrames")

    source, media_path, kind = _content(item, name, log)
    if source is None and not media_path:
        return None

    effects: list[Effect] = []
    video = map_video_effects(item.get("VideoEffects"), log, length=length, keyframes=keyframes)
    if source is not None and source.kind == "text":
        decorations = map_decorations(
            item.get("Decorations"),
            log,
            size=number(item.get("FontSize"), 64.0),
            style=str(item.get("Style") or ""),
            style_colour=item.get("StyleColor"),
        )
        merged = {**source.params, **decorations.params, **video.params}
        source = GeneratedSource(kind="text", params=merged)
        effects.extend(decorations.effects)
    effects.extend(video.effects)

    effects[0:0] = _placement(item, length, keyframes)

    return MappedObject(
        clip=Clip(
            timeline_start=max(0, int(number(item.get("Frame"), 0.0))),
            duration=length,
            source=source,
            effects=tuple(effects),
            opacity=animated(
                item.get("Opacity"), 100.0, length=length, keyframes=keyframes, scale=0.01
            ),
            blend_mode=_blend_of(item, log),
        ),
        # YMM4 のレイヤーは 0 始まり こちらのトラックは 1 始まり
        layer=max(1, int(number(item.get("Layer"), 0.0)) + 1),
        media_path=media_path,
        kind=kind,
        has_span="Length" in item,
    )


def _blend_of(item: dict[str, Any], log: CompatibilityReport) -> str:
    raw = str(item.get("Blend") or "Normal")
    mode = _BLEND_MODES.get(raw)
    if mode is None:
        log.note_missing(f"YMM4 の合成モード: {raw}")
        return "normal"
    return mode


def _content(
    item: dict[str, Any], name: str, log: CompatibilityReport
) -> tuple[GeneratedSource | None, str, str]:
    if name in ("TextItem", "Text"):
        return _text(item), "", "text"
    if name in ("ShapeItem", "EffectItem", "Shape"):
        return _shape(item, log), "", "shape"

    media = _MEDIA_ITEMS.get(name)
    if media is not None:
        return None, str(item.get("FilePath") or item.get("File") or ""), media

    log.note_missing(f"YMM4 のアイテム: {name or '種類不明'}")
    return None, "", name


def _text(item: dict[str, Any]) -> GeneratedSource:
    length = max(1, int(number(item.get("Length"), 1.0)))
    keyframes = item.get("KeyFrames")
    size = number(item.get("FontSize"), 64.0)
    align, valign = _base_point(item)

    params: dict[str, ParamValue] = {
        "text": str(item.get("Text") or ""),
        "size": animated(item.get("FontSize"), 64.0, length=length, keyframes=keyframes),
        "color": colour(item.get("FontColor"), (1.0, 1.0, 1.0, 1.0)),
        "bold": bool(item.get("Bold")),
        "italic": bool(item.get("Italic")),
        # ``LineHeight2`` は百分率（100 が標準） 画素数だと思って渡すと、
        # 標準のつもりが 100px の行間になる
        "line_spacing": AnimatedValue(
            size * (number(item.get("LineHeight2"), 100.0) - 100.0) / 100.0
        ),
        "letter_spacing": animated(
            item.get("LetterSpacing2"), 0.0, length=length, keyframes=keyframes
        ),
        "align": align,
        "valign": valign,
        "vertical": _is_vertical(item),
    }
    font = item.get("Font")
    if isinstance(font, str) and font:
        params["font"] = font
    return GeneratedSource(kind="text", params=params)


def _base_point(item: dict[str, Any]) -> tuple[str, str]:
    """``BasePoint`` を横と縦に分ける ``CenterCenter`` のように 2 つ並ぶ"""
    raw = str(item.get("BasePoint") or "")
    align = next((value for key, value in _HORIZONTAL if raw.startswith(key)), "center")
    valign = next((value for key, value in _VERTICAL if raw.endswith(key)), "middle")
    return align, valign


def _is_vertical(item: dict[str, Any]) -> bool:
    direction = str(item.get("FontDirection") or item.get("TextDirection") or "")
    return "Vertical" in direction or "縦" in direction


def _shape(item: dict[str, Any], log: CompatibilityReport) -> GeneratedSource:
    parameter = item.get("ShapeParameter")
    parameter = parameter if isinstance(parameter, dict) else {}

    # 種類はプラグイン名に入っている（``BackgroundShapePlugin`` など）
    raw = str(item.get("ShapeType2") or item.get("ShapeType") or item.get("Type") or "")
    plugin = raw.partition(",")[0].rpartition(".")[2]
    shape = next(
        (value for key, value in _SHAPES.items() if plugin.startswith(key)),
        None,
    )
    if shape is None:
        shape = _SHAPES.get(type_name(parameter).removesuffix("ShapeParameter"), "")
    if not shape:
        log.note_missing(f"YMM4 の図形: {plugin or type_name(parameter) or '種類不明'}")
        shape = "rect"

    return GeneratedSource(
        kind="shape",
        params={
            "shape": shape,
            "width": AnimatedValue(number(parameter.get("Width"), 400.0)),
            "height": AnimatedValue(number(parameter.get("Height"), 400.0)),
            "color": colour(parameter.get("Color"), (1.0, 1.0, 1.0, 1.0)),
            "line_width": AnimatedValue(number(parameter.get("StrokeThickness"), 0.0)),
        },
    )


def _placement(item: dict[str, Any], length: int, keyframes: Any) -> list[Effect]:
    """位置・拡大・回転を変形エフェクトへ

    AviUtl 側（:func:`~kumiki.compat.aviutl.mapping.map_object`）と同じ扱いに
    しておく クリップは配置を持たないので、見た目が同じになるエフェクトへ写す
    """
    definition = registry.get("transform")
    if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
        return []

    pos_x = animated(item.get("X"), 0.0, length=length, keyframes=keyframes)
    # YMM4 の Y は下向き こちらは上向き
    pos_y = _negated(animated(item.get("Y"), 0.0, length=length, keyframes=keyframes))
    zoom = animated(item.get("Zoom"), 100.0, length=length, keyframes=keyframes)
    rotation = animated(item.get("Rotation"), 0.0, length=length, keyframes=keyframes)

    resting = ((pos_x, 0.0), (pos_y, 0.0), (zoom, 100.0), (rotation, 0.0))
    if not any(value.is_animated or value.static != rest for value, rest in resting):
        return []

    return [
        definition.create(pos_x=pos_x, pos_y=pos_y, scale=zoom, scale_y=zoom, rotation=rotation)
    ]


def _negated(value: AnimatedValue) -> AnimatedValue:
    return AnimatedValue(
        static=-value.static,
        keyframes=tuple(replace(k, value=-k.value) for k in value.keyframes),
    )
