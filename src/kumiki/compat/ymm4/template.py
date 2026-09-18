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
from kumiki.compat.ymm4.brushes import BLEND_NAMES, brush_effect, is_solid
from kumiki.compat.ymm4.decorations import map_decorations, map_video_effects, with_pivot
from kumiki.compat.ymm4.effects import CenterPoint
from kumiki.compat.ymm4.values import animated, brush_colour, colour, number, type_name
from kumiki.core.model import AnimatedValue, Clip, Effect, GeneratedSource, ParamValue
from kumiki.effects.definition import registry
from kumiki.effects.sources import source_registry

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
    "Overlay": "overlay",
    "オーバーレイ": "overlay",
    # YMM4 の名前は Lighter と Darker（Lighten と Darken は YMM4 が読み込みで断る）
    "Lighter": "lighten",
    "比較(明)": "lighten",
    "Darker": "darken",
    "比較(暗)": "darken",
    "Subtract": "subtract",
    "減算": "subtract",
    # 残りの名前は塗りのエフェクトと同じ表で読む
    **BLEND_NAMES,
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
    # YMM4 の三角形は円に内接する（試験の絵で確かめた）
    "Triangle": "inscribed_triangle",
    "Star": "star",
    "Background": "background",
    "Hexagon": "hexagon",
    "Fan": "fan",
    "Arrow": "arrow",
    "Superformula": "superformula",
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
        effect_templates = document.get("VideoEffectTemplates")
        if isinstance(catalogued, list) or isinstance(effect_templates, list):
            built = [
                _template_of(entry)
                for entry in (catalogued if isinstance(catalogued, list) else [])
                if isinstance(entry, dict)
            ]
            built.extend(
                _effect_template_of(entry)
                for entry in (effect_templates if isinstance(effect_templates, list) else [])
                if isinstance(entry, dict)
            )
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


#: 映像エフェクトのテンプレートを包むアイテム エフェクトだけを持つグループと同じ扱いになる
_EFFECT_HOLDER = "YukkuriMovieMaker.Project.Items.GroupItem, YukkuriMovieMaker"


def _effect_template_of(entry: dict[str, Any]) -> ItemTemplate:
    """映像エフェクトのテンプレート（``VideoEffectTemplates``）を 1 本読む

    アイテムを持たず、エフェクトの並びだけが入っている 置くものではなく、既にある
    クリップへ着せて使う エフェクトだけを持つグループとして包めば、アイテムの
    テンプレートと同じ道（:func:`map_template`）で読める 包まずに飛ばすと、
    あおもや式のエフェクト集 15 本が棚に並ばず、読めないことにも気付けなかった
    """
    name = str(entry.get("Name") or "")
    effects = entry.get("Effects")
    if not isinstance(effects, list) or not effects:
        return ItemTemplate(name=name)
    holder = {"$type": _EFFECT_HOLDER, "VideoEffects": effects, "Length": 1}
    return ItemTemplate(name=name, path=("映像エフェクト", name), items=(holder,))


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
    params, chain, final = _video_chain(item, log, length, keyframes)

    outlines: list[Effect] = []
    border = registry.get("border")
    width = params.get("border_width")
    if border is not None and isinstance(width, AnimatedValue) and width.static > 0:
        outlines.append(border.create(width=width, color=params.get("border_color")))

    return [*outlines, *chain, *final]


#: 描画を遅らせる印（DrawLazyEffect）の設定と、その場で当てる配置の項目
_LAZY_PARTS = (
    ("IsXYZ", ("X", "Y")),
    ("IsZoom", ("Zoom",)),
    ("IsRotation", ("Rotation",)),
)
_RESTING = {"X": 0.0, "Y": 0.0, "Zoom": 100.0, "Rotation": 0.0}


def _video_chain(
    item: dict[str, Any], log: CompatibilityReport, length: int, keyframes: Any
) -> tuple[dict[str, ParamValue], list[Effect], list[Effect]]:
    """映像エフェクトの並びと、最後に当てる配置（反転と位置・拡大・回転）

    YMM4 はエフェクトを掛けた絵を最後に置く ただし描画を遅らせる印があれば、印の場所で
    印が指す分（位置・拡大・回転）だけを先に当て、残りを最後に当てる（試験で確かめた）
    """
    raw = item.get("VideoEffects")
    entries = raw if isinstance(raw, list) else []
    lazy = next(
        (
            index
            for index, entry in enumerate(entries)
            if isinstance(entry, dict)
            and entry.get("IsEnabled") is not False
            and type_name(entry) == "DrawLazyEffectEffect"
        ),
        None,
    )
    flip = _flip(item)
    if lazy is None:
        video = map_video_effects(entries, log, length=length, keyframes=keyframes)
        final = [*flip, *_placement(item, length, keyframes, video.pivot)]
        return video.params, list(video.effects), final

    marker = entries[lazy]
    first = map_video_effects(entries[:lazy], log, length=length, keyframes=keyframes)
    rest = map_video_effects(entries[lazy + 1 :], log, length=length, keyframes=keyframes)
    early = {key: _RESTING[key] for key in _RESTING}
    late = dict(item)
    for flag, keys in _LAZY_PARTS:
        if marker.get(flag) is True:
            for key in keys:
                early[key] = item.get(key, _RESTING[key])
                late[key] = _RESTING[key]
    placed_early = _placement(early, length, keyframes, first.pivot)
    final = [*flip, *_placement(late, length, keyframes, rest.pivot or first.pivot)]
    params = {**first.params, **rest.params}
    return params, [*first.effects, *placed_early, *rest.effects], final


def _flip(item: dict[str, Any]) -> list[Effect]:
    """アイテムの反転 YMM4 は左右に裏返してから回す（回す向きは変わらない）"""
    if item.get("IsInverted") is not True:
        return []
    definition = registry.get("flip")
    return [] if definition is None else [definition.create(horizontal=True, vertical=False)]


#: 線の図形の塗りに模様を置くときの目印の色 描いたあと、この色の所だけを模様に替える
#: 塗りだけに模様を掛けるには、形（線と塗り）の中で塗りの場所を伝える必要がある
FILL_KEY = (1.0, 0.0, 1.0, 1.0)


def _map_item(item: dict[str, Any], log: CompatibilityReport) -> MappedObject | None:
    name = type_name(item)

    length = max(1, int(number(item.get("Length"), 1.0)))
    keyframes = item.get("KeyFrames")

    if _preview_only(item):
        return None
    if name == "TransitionItem":
        return _transition(item, length, keyframes, log)
    source, media_path, kind = _content(item, name, log)
    if source is None and not media_path:
        return None

    if item.get("IsAlwaysOnTop") is True or item.get("IsZOrderEnabled") is True:
        log.note_missing("YMM4 のアイテムの重なり順の設定（常に手前・Z 順）")
    effects: list[Effect] = []
    if source is not None and source.kind == "shape":
        # 図形のブラシが単色でなければ、白で描いた形を模様で塗る
        parameter = item.get("ShapeParameter")
        brush = parameter.get("Brush") if isinstance(parameter, dict) else None
        fill = parameter.get("FillBrush") if isinstance(parameter, dict) else None
        if not is_solid(brush):
            painted = brush_effect(
                brush, log, length=length, keyframes=keyframes, pattern_only=True
            )
            if painted is not None:
                source = source.with_param("color", (1.0, 1.0, 1.0, 1.0))
                effects.append(painted)
        if not is_solid(fill) and source.params.get("shape") == "polyline":
            # 線の図形の塗りだけを模様にする 目印の色で塗った所を置き換える
            filled = brush_effect(fill, log, length=length, keyframes=keyframes, key_only=True)
            if filled is not None:
                effects.append(filled)
    params, chain, final = _video_chain(item, log, length, keyframes)
    if source is not None and source.kind == "text":
        decorations = map_decorations(
            item.get("Decorations"),
            log,
            size=number(item.get("FontSize"), 64.0),
            style=str(item.get("Style") or ""),
            style_colour=item.get("StyleColor"),
        )
        merged = {**source.params, **decorations.params, **params}
        source = GeneratedSource(kind="text", params=merged)
        effects.extend(decorations.effects)
    effects.extend(chain)

    # YMM4 はエフェクトを掛けた絵を、最後に位置・拡大・回転で置く 先に置くと、
    # 画面の中で動かしたあとの絵にエフェクトが掛かり、回した図形が中心点の前で切れる
    effects.extend(final)

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
            # 上のオブジェクト（すぐ下に描かれる層）の形で切り抜く
            clip_to_below=item.get("IsClippingWithObjectAbove") is True,
        ),
        # YMM4 のレイヤーは 0 始まり こちらのトラックは 1 始まり
        layer=max(1, int(number(item.get("Layer"), 0.0)) + 1),
        media_path=media_path,
        kind=kind,
        has_span="Length" in item,
    )


#: YMM4 の切り替えの種類と、場面切り替えの切り替え方
_TRANSITION_STYLES = {
    "SwitchTransitionPlugin": "switch",
    "FadeTransitionPlugin": "fade",
    "PushTransitionPlugin": "push",
    "SlideTransitionPlugin": "slide",
    "NoneTransitionPlugin": "overlay",
}


def _transition(
    item: dict[str, Any], length: int, keyframes: Any, log: CompatibilityReport
) -> MappedObject | None:
    """場面切り替え 前の場面のエフェクトはクリップ、後の場面のエフェクトは after_effects へ

    位置・拡大・回転・不透明度はアイテムの設定画面に出ない（配布物ではどれも既定のまま）
    """
    plugin = str(item.get("TransitionType") or "").partition(",")[0].rpartition(".")[2]
    style = _TRANSITION_STYLES.get(plugin)
    if style is None:
        log.note_missing(f"YMM4 の場面切り替えの種類: {plugin or '種類不明'}")
        style = "fade"
    raw = item.get("TransitionParameter")
    parameter = raw if isinstance(raw, dict) else {}
    target = str(parameter.get("Target") or parameter.get("OverlayTarget") or "After")
    if target not in ("Before", "After"):
        log.note_missing(f"YMM4 の場面切り替えの対象: {target}")
    easing = str(parameter.get("EasingType") or "Linear")
    if easing not in _EASING_NAMES:
        log.note_missing(f"YMM4 の場面切り替えのイージング: {easing}")
    easing_mode = str(parameter.get("EasingMode") or "In")
    if easing_mode not in _EASING_MODE_NAMES:
        log.note_missing(f"YMM4 の場面切り替えのイージングの向き: {easing_mode}")
    definition = source_registry.get("transition")
    if definition is None:  # pragma: no cover - 標準の生成オブジェクト
        return None
    source = definition.create(
        style=style,
        # 押し出しの角度は YMM4 が見ていない（90 にしても 0 と同じ絵だった）
        angle=0.0 if style == "push" else number(parameter.get("Angle"), 0.0),
        target="before" if target == "Before" else "after",
        easing=_EASING_NAMES.get(easing, "linear"),
        easing_mode=_EASING_MODE_NAMES.get(easing_mode, "in"),
    )
    before = map_video_effects(
        item.get("BeforeVideoEffects"), log, length=length, keyframes=keyframes
    )
    after = map_video_effects(
        item.get("AfterVideoEffects"), log, length=length, keyframes=keyframes
    )
    if map_video_effects(item.get("VideoEffects"), log, length=length, keyframes=keyframes).effects:
        log.note_missing("YMM4 の場面切り替えのアイテム自体に掛けたエフェクト")
    return MappedObject(
        clip=Clip(
            timeline_start=max(0, int(number(item.get("Frame"), 0.0))),
            duration=length,
            source=source,
            effects=tuple(before.effects),
            after_effects=tuple(after.effects),
        ),
        layer=max(1, int(number(item.get("Layer"), 0.0)) + 1),
        media_path="",
        kind="transition",
    )


#: 切り替えのイージングの名前
_EASING_NAMES = {
    name: name.lower()
    for name in ("Linear", "Sine", "Quad", "Cubic", "Quart", "Quint", "Expo", "Circ", "Back")
} | {"Elastic": "elastic", "Bounce": "bounce", "Jump": "jump"}
_EASING_MODE_NAMES = {"In": "in", "Out": "out", "InOut": "inout"}


def _preview_only(item: dict[str, Any]) -> bool:
    """編集中の画面にだけ映すアイテムか YMM4 は書き出した動画に出さない

    目印や下書きに使われる 読み込むと書き出しに映り込む
    """
    effects = item.get("VideoEffects")
    return isinstance(effects, list) and any(
        isinstance(entry, dict)
        and entry.get("IsEnabled") is not False
        and type_name(entry) == "ShowOnlyPreviewEffect"
        for entry in effects
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
    if name in ("ShapeItem", "Shape"):
        return _shape(item, log), "", "shape"

    if name == "EffectItem":
        # 下のレイヤーの絵にエフェクトを掛けるアイテム（範囲は図形で決める） 図形として
        # 読むと、範囲の図形（多くは画面全体の背景）がそのまま画面を塗りつぶす
        # 写し取った画面にエフェクトを掛けるフレームバッファと同じ形で読む
        plugin = str(item.get("ShapeType2") or "").partition(",")[0].rpartition(".")[2]
        if plugin and not plugin.startswith("Background"):
            log.note_missing(f"YMM4 のエフェクトアイテムの範囲: {plugin}")
        if number(item.get("Blur"), 0.0) > 0 or item.get("InvertMask") is True:
            log.note_missing("YMM4 のエフェクトアイテムの範囲のぼかしと反転")
        return GeneratedSource(kind="framebuffer"), "", "framebuffer"

    if name == "FrameBufferItem":
        # それまでに重ねた画面を素材にする 中身の設定は持たない
        return GeneratedSource(kind="framebuffer"), "", "framebuffer"

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
    if plugin.startswith("LineShape"):
        return _line(parameter, log)
    if plugin.startswith("PenShape"):
        return _pen(parameter, item, log)
    if plugin.startswith("TimerShape"):
        return _timer(parameter, item)
    if plugin.startswith("ConcentrationLineShape"):
        return _concentration(parameter, item)
    shape = next(
        (value for key, value in _SHAPES.items() if plugin.startswith(key)),
        None,
    )
    if shape is None:
        shape = _SHAPES.get(type_name(parameter).removesuffix("ShapeParameter"), "")
    if not shape:
        log.note_missing(f"YMM4 の図形: {plugin or type_name(parameter) or '種類不明'}")
        shape = "rect"

    # 色はブラシ（``Brush.Parameter.Color``）に入っている 古い形だけが直に ``Color`` を持つ
    # ブラシを見ないと、配布物の図形がすべて白で出る
    fallback = colour(parameter.get("Color"), (1.0, 1.0, 1.0, 1.0))
    # 単色以外のブラシは、アイテムを写すとき（_map_item）に模様で塗るエフェクトを足す
    colour_value = brush_colour(parameter.get("Brush"), fallback)

    length = max(1, int(number(item.get("Length"), 1.0)))
    keyframes = item.get("KeyFrames")

    def track(key: str, default: float) -> AnimatedValue:
        # 大きさや線の太さも動く（斜めに伸びる帯のトランジションなど） 先頭の値だけ
        # 読むと、0 から伸びる図形がずっと見えない
        return animated(parameter.get(key), default, length=length, keyframes=keyframes)

    width = track("Width", 400.0)
    height = track("Height", 400.0)
    if str(parameter.get("SizeMode") or "") in ("Size", "SizeAspect"):
        # 大きさ 1 つと縦横比で決める形 縦横比は -100〜100 で、正なら縦長
        size = track("Size", 100.0)
        aspect = number(parameter.get("AspectRate"), 0.0) / 100.0
        width = _scaled(size, 1.0 - max(0.0, aspect))
        height = _scaled(size, 1.0 + min(0.0, aspect))

    params: dict[str, ParamValue] = {
        "shape": shape,
        "width": width,
        "height": height,
        "color": colour_value,
    }
    # YMM4 の線の太さは図形の内側へ描く 半分の大きさを超えれば塗りつぶしと同じ
    # （配布物は塗りつぶしに 4000 や 10000 を入れている） こちらの線は輪郭の上に
    # 中心を置いて描くので、そのまま渡すと外側へ数千画素はみ出して画面を覆う
    # 塗りつぶしなら線を付けず、枠だけなら線の太さの分だけ内側へ縮めて描く
    thickness = max(0.0, number(parameter.get("StrokeThickness"), 0.0))
    if shape != "background" and 0.0 < thickness * 2.0 < min(_peak(width), _peak(height)):
        params["outline_only"] = True
        params["line_width"] = AnimatedValue(thickness)
        params["width"] = _shifted(width, -thickness)
        params["height"] = _shifted(height, -thickness)
    if params["shape"] == "fan":
        params["span"] = track("CenterAngle", 360.0)
    elif params["shape"] == "arrow":
        params["bar_length"] = track("BarLength", 50.0)
        params["bar_thickness"] = track("BarThickness", 50.0)
    elif params["shape"] == "superformula":
        params["formula_m"] = track("M", 4.0)
        params["formula_n"] = track("N", 1.0)
    round_value = number(parameter.get("Round"), 0.0)
    if shape == "rect" and round_value > 0:
        params["shape"] = "rounded"
        params["corner_radius"] = track("Round", 0.0)
    return GeneratedSource(kind="shape", params=params)


#: 破線の種類と、線の太さを 1 とした長さの並び（Direct2D の決まった模様）
_DASHES = {
    "Solid": "",
    "Dash": "2,2",
    "Dot": "0,2",
    "DashDot": "2,2,0,2",
    "DashDotDot": "2,2,0,2,0,2",
}


def _pen(
    parameter: dict[str, Any], item: dict[str, Any], log: CompatibilityReport
) -> GeneratedSource:
    """手描きの線（ペン） 点は中心からの画素で Y は下が正

    ``Offset`` と ``Length`` は線のどこからどこまでを描くかの割合 太さは描いたときの
    ペンの幅（``DrawingAttributes.Width``）に ``Thickness`` の割合を掛ける
    """
    length = max(1, int(number(item.get("Length"), 1.0)))
    keyframes = item.get("KeyFrames")

    def track(key: str, default: float) -> AnimatedValue:
        return animated(parameter.get(key), default, length=length, keyframes=keyframes)

    strokes = parameter.get("Strokes")
    strokes = strokes if isinstance(strokes, list) else []
    if len(strokes) > 1:
        log.note_missing(f"YMM4 のペンの図形の線の本数（{len(strokes)} 本のうち 1 本目だけ描いた）")
    first = strokes[0] if strokes and isinstance(strokes[0], dict) else {}
    attributes = first.get("DrawingAttributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    # 点は画面の左上を原点にした画素 こちらの線は絵の中心が原点で Y は上が正
    # （配布物の点は 1920x1080 の画面で描かれている）
    points = [
        f"{number(point.get('X'), 0.0) - 960.0:g},{540.0 - number(point.get('Y'), 0.0):g}"
        for point in first.get("StylusPoints") or []
        if isinstance(point, dict)
    ]
    offset = track("Offset", 0.0)
    span = track("Length", 100.0)
    return GeneratedSource(
        kind="shape",
        params={
            "shape": "polyline",
            "points": ";".join(points),
            "closed": False,
            "color": colour(attributes.get("Color"), (1.0, 1.0, 1.0, 1.0)),
            "line_width": _scaled(
                track("Thickness", 100.0), number(attributes.get("Width"), 10.0) / 100.0
            ),
            "trim_start": offset,
            "trim_end": _summed(offset, span),
            "fill_color": (1.0, 1.0, 1.0, 0.0),
        },
    )


def _timer(parameter: dict[str, Any], item: dict[str, Any]) -> GeneratedSource:
    """時間を数える図形 文字として描く 数え下げはクリップの終わりで初めの値になる"""
    length = max(1, int(number(item.get("Length"), 1.0)))
    keyframes = item.get("KeyFrames")
    size = number(parameter.get("FontSize"), 64.0)
    align, valign = _base_point(parameter)
    params: dict[str, ParamValue] = {
        "timer_format": str(parameter.get("Format") or "s"),
        "timer_start": animated(
            parameter.get("InitialValue"), 0.0, length=length, keyframes=keyframes
        ),
        "timer_rate": animated(
            parameter.get("PlaybackRate"), 100.0, length=length, keyframes=keyframes
        ),
        "timer_countdown": str(parameter.get("Direction") or "") == "CountDown",
        "timer_length": length,
        "size": AnimatedValue(size),
        "color": colour(parameter.get("FontColor"), (1.0, 1.0, 1.0, 1.0)),
        "bold": bool(parameter.get("Bold")),
        "italic": bool(parameter.get("Italic")),
        "letter_spacing": animated(
            parameter.get("LetterSpacing2"), 0.0, length=length, keyframes=keyframes
        ),
        "align": align,
        "valign": valign,
    }
    font = parameter.get("Font")
    if isinstance(font, str) and font:
        params["font"] = font
    return GeneratedSource(kind="text", params=params)


def _concentration(parameter: dict[str, Any], item: dict[str, Any]) -> GeneratedSource:
    """集中線 大きさは線が届く円の直径、中心の幅はぼかし、速さは選び直す回数"""
    length = max(1, int(number(item.get("Length"), 1.0)))
    keyframes = item.get("KeyFrames")

    def track(key: str, default: float) -> AnimatedValue:
        return animated(parameter.get(key), default, length=length, keyframes=keyframes)

    size = track("Size", 1000.0)
    return GeneratedSource(
        kind="shape",
        params={
            "shape": "concentration",
            "width": size,
            "height": size,
            "color": colour(parameter.get("Stroke"), (1.0, 1.0, 1.0, 1.0)),
            "density": track("Density", 80.0),
            "line_thickness": track("Thickness", 50.0),
            "line_length": track("Length", 70.0),
            "softness": track("CenterWidth", 50.0),
            "flicker": track("Speed", 5.0),
        },
    )


def _summed(first: AnimatedValue, second: AnimatedValue) -> AnimatedValue:
    """2 つの動く値を足す 片方だけが動くときは、動く側の各点で足す"""
    if not second.is_animated:
        return AnimatedValue(
            first.static + second.static,
            tuple(replace(k, value=k.value + second.static) for k in first.keyframes),
        )
    return AnimatedValue(
        first.static + second.static,
        tuple(replace(k, value=k.value + first.static) for k in second.keyframes),
    )


def _line(parameter: dict[str, Any], log: CompatibilityReport) -> GeneratedSource:
    """線の図形 点は中心からの画素で Y は下が正 閉じていれば中を塗る"""
    points: list[str] = []
    for point in parameter.get("Points") or []:
        if not isinstance(point, dict):
            continue
        x_value = animated(point.get("X"), 0.0)
        y_value = animated(point.get("Y"), 0.0)
        if x_value.is_animated or y_value.is_animated:
            log.note_missing("YMM4 の線の図形の点の動き（先頭の位置で描いた）")
        x = x_value.keyframes[0].value if x_value.is_animated else x_value.static
        y = y_value.keyframes[0].value if y_value.is_animated else y_value.static
        # YMM4 の点は下が正 こちらは上が正
        points.append(f"{x:g},{-y:g}")
    style = str(parameter.get("DashStyle") or "Solid")
    dash = _DASHES.get(style)
    if dash is None:
        dash = str(parameter.get("DashPattern") or "")
    fill = parameter.get("FillBrush")
    # 単色以外の塗りは、目印の色で塗っておいて、アイテムを読む所で模様に置き換える
    fill_colour = brush_colour(fill, (1.0, 1.0, 1.0, 0.0)) if is_solid(fill) else FILL_KEY
    return GeneratedSource(
        kind="shape",
        params={
            "shape": "polyline",
            "points": ";".join(points),
            "trim_end": animated(parameter.get("LengthRate"), 100.0),
            "line_type": "quadratic"
            if str(parameter.get("LineType") or "") == "QuadraticBezier"
            else "straight",
            "closed": parameter.get("IsClosed") is True,
            "fill_color": fill_colour,
            "color": brush_colour(parameter.get("Brush"), (1.0, 1.0, 1.0, 1.0)),
            "line_width": AnimatedValue(
                number(animated(parameter.get("Thickness"), 1.0).static, 1.0)
            ),
            "dash": dash,
        },
    )


def _peak(value: AnimatedValue) -> float:
    return max((k.value for k in value.keyframes), default=value.static)


def _shifted(value: AnimatedValue, delta: float) -> AnimatedValue:
    return replace(
        value,
        static=value.static + delta,
        keyframes=tuple(replace(k, value=k.value + delta) for k in value.keyframes),
    )


def _scaled(value: AnimatedValue, factor: float) -> AnimatedValue:
    return replace(
        value,
        static=value.static * factor,
        keyframes=tuple(replace(k, value=k.value * factor) for k in value.keyframes),
    )


def _placement(
    item: dict[str, Any], length: int, keyframes: Any, pivot: CenterPoint | None = None
) -> list[Effect]:
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

    moves = pivot is not None and not pivot.keep
    resting = ((pos_x, 0.0), (pos_y, 0.0), (zoom, 100.0), (rotation, 0.0))
    if not moves and not any(value.is_animated or value.static != rest for value, rest in resting):
        return []

    placed = definition.create(
        pos_x=pos_x,
        pos_y=pos_y,
        scale=zoom,
        scale_y=zoom,
        rotation=rotation,
        # 中心点で「位置を保つ」を切ると、選んだ点がアイテムの位置へ来る
        move_to_pivot=moves,
    )
    # 中心点はアイテム自身の拡大と回転の支点にもなる
    return [placed if pivot is None else with_pivot(placed, pivot)]


def _negated(value: AnimatedValue) -> AnimatedValue:
    return AnimatedValue(
        static=-value.static,
        keyframes=tuple(replace(k, value=-k.value) for k in value.keyframes),
    )
