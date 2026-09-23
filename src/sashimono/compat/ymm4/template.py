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
（:mod:`sashimono.compat.catalog`）はそれを 1 本ずつ並べる

アイテムの種類は ``$type`` に入る 振り分けは**クラス名だけ**で行う 実物には
``Version=4.32.0.2, Culture=neutral, PublicKeyToken=null`` まで書かれていて、
丸ごと突き合わせると YMM4 が更新されただけで読めなくなる

知らない種類が来たら、その名前を :mod:`~sashimono.compat.aviutl.report` に残して
先へ進む 1 種類読めないだけでテンプレート全体が落ちるのは割に合わない
"""

from __future__ import annotations

import json
import math
import zipfile
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Any

from sashimono.compat.aviutl.report import CompatibilityReport, global_report
from sashimono.compat.mapped import MappedObject, fitted_effect
from sashimono.compat.ymm4.brushes import BLEND_NAMES, brush_effect, is_solid
from sashimono.compat.ymm4.decorations import map_decorations, map_video_effects, with_pivot
from sashimono.compat.ymm4.effects import CenterPoint
from sashimono.compat.ymm4.values import (
    animated,
    brush_colour,
    colour,
    number,
    reporting,
    timespan,
    type_name,
)
from sashimono.core.model import AnimatedValue, Clip, Effect, GeneratedSource, ParamValue
from sashimono.effects.definition import registry
from sashimono.effects.sources import source_registry

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
    持っている 掛かる相手は、グループのレイヤーのすぐ上から ``GroupRange`` 段
    まで 掛け方は「合成する」（``IsComposite``）で 2 通りに分かれる

    * 合成しない — 範囲の中身 1 つずつにエフェクトを掛ける こちらのモデルに
      入れ子は無いので、**中身へエフェクトを移して**平らにする
    * 合成する — 範囲の中身を 1 枚の絵に重ねてから、その絵にエフェクト・反転・
      拡大・不透明度・合成モード・クリッピングを掛ける こちらでは中身を
      シーンにまとめ、グループをそのシーンのクリップとして置く
      （:attr:`MappedObject.children`）

    中身が無いテンプレート（``アニメーション効果/振り子`` のようなもの）は
    **エフェクトだけ**の結果になり、既にあるクリップへ着せて使う
    """
    log = report if report is not None else global_report
    # 値を読む関数の多くは記録を受け取らない 知らない移動方法の形がこの読み込みの
    # 記録に残るよう、入口で置いておく
    with reporting(log):
        return _map_items(list(items), log)


def _map_items(items: list[dict[str, Any]], log: CompatibilityReport) -> list[MappedObject]:
    composites, consumed = _composite_groups(items, log)

    contents: list[tuple[int, MappedObject]] = []
    grouped: list[Effect] = []
    # 入れ物ごとの (レイヤー, 範囲, 長さ, エフェクト) 動く値のキーフレームはその
    # 入れ物の長さの上に並んでいるので、中身の無いテンプレートでも長さを残す
    # 1 にすると、着せるときに尺を合わせられず、動きが着せた先の途中で止まる
    containers: list[_Container] = []
    for item in items:
        if id(item) in consumed:
            continue
        scene = composites.get(id(item))
        if scene is not None:
            contents.append((_layer_of(item), scene))
            continue
        if type_name(item) in _CONTAINER_ITEMS:
            effects = _group_effects(item, log)
            grouped.extend(effects)
            containers.append(
                _Container(
                    layer=_layer_of(item),
                    reach=_reach_of(item),
                    length=max(1, int(number(item.get("Length"), 1.0))),
                    effects=effects,
                )
            )
            continue
        mapped = _map_item(item, log)
        if mapped is not None:
            contents.append((_layer_of(item), mapped))

    if not grouped:
        return [mapped for _, mapped in contents]
    if not contents:
        # 中身のないテンプレート エフェクトだけを**入れ物ごとに**返す
        # 1 つにまとめると、長さの違う入れ物が混ざったときに短い方の動きが
        # 長い方の尺で伸び縮みする（着せる側は 1 つずつ尺を合わせる）
        return [
            MappedObject(
                clip=Clip(
                    timeline_start=0,
                    duration=container.length,
                    effects=tuple(container.effects),
                ),
                layer=1,
                kind="effects",
                has_span=False,
            )
            for container in containers
            if container.effects
        ]
    # 入れ物のエフェクトを中身へ移す 入れ物と中身で長さが違うことがあるので
    # （手元の配布物 97 本のうち 6 本 例: 入れ物 18 中身 300）、動く値の時刻を
    # 中身の長さへ揃えてから移す 揃えずに移すと、入れ物の終わりに置いた点が
    # 中身の途中に残り、エフェクトの終わりの見た目が出ないまま止まる
    #
    # 移すのは中身を範囲に含む入れ物のものだけ 全部へ配ると、リボンのテロップの
    # 文字（範囲の外）に吹き出しの登場の動きが 2 回掛かり、倍の距離を飛んでくる
    return [
        _with_group_effects(
            item,
            [container for container in containers if container.covers(layer)],
            span=max((container.length for container in containers), default=1),
        )
        for layer, item in contents
    ]


@dataclass(frozen=True, slots=True)
class _Container:
    """合成しないグループ 範囲の中身へエフェクトを配る"""

    layer: int
    #: 掛かる段の数 ``None`` は上の段すべて
    reach: int | None
    length: int
    effects: list[Effect]

    def covers(self, layer: int) -> bool:
        return layer > self.layer and (self.reach is None or layer <= self.layer + self.reach)


def _layer_of(item: dict[str, Any]) -> int:
    return int(number(item.get("Layer"), 0.0))


def _reach_of(item: dict[str, Any]) -> int | None:
    """グループが掛かる段の数

    ``GroupRange`` を持たないグループは、映像エフェクトのテンプレートを包んだ入れ物
    （:func:`_effect_template_of`）と、それだけを書き出した配布物（あおもや式の
    アニメーション効果 18 本）だけだった どれもレイヤーも持たず、着せる先の段が
    決まっていないので、上の段すべてに掛かるものとして読む 1 段とすると、
    比べる道具が下地を置いた段によっては何にも掛からなくなる
    """
    if "GroupRange" not in item:
        return None
    return max(1, int(number(item.get("GroupRange"), 1.0)))


def _composite_groups(
    items: list[dict[str, Any]], log: CompatibilityReport
) -> tuple[dict[int, MappedObject], set[int]]:
    """合成するグループと、その範囲の中身を 1 つのまとめた絵にする

    返すのは「グループの ``id`` → まとめた絵」と、まとめた絵の中へ入った中身の ``id``
    入れ子のグループは、外側（レイヤー番号の小さい方 YMM4 の画面では上の段）から
    順にまとめる 内側のグループは中身と一緒に外側の絵の中へ入り、そこでもう一度
    この関数を通る

    中身に数えるのは、範囲の段にあって**時間もグループと重なる**ものだけ
    グループが終わった後に始まるアイテムまでまとめると、シーンのクリップの長さで
    切られて消える

    範囲に中身が 1 つも無い合成するグループは、ふつうの入れ物として残す
    （ペイントトランジションの「この範囲内に次の場面を置いてください」という空の枠）
    空のシーンを置いても何も映らず、トラックとシーンが増えるだけになる
    """
    made: dict[int, MappedObject] = {}
    consumed: set[int] = set()
    for group in sorted(
        (item for item in items if _is_composite(item)),
        key=_layer_of,
    ):
        if id(group) in consumed:
            continue
        reach = _reach_of(group)
        low = _layer_of(group)
        members = [
            item
            for item in items
            if item is not group
            and id(item) not in consumed
            and _layer_of(item) > low
            and (reach is None or _layer_of(item) <= low + reach)
            and _overlaps(item, group)
        ]
        _note_crossing(members, low, reach, log)
        scene = _composite(group, members, log)
        if scene is None:
            continue
        made[id(group)] = scene
        consumed.update(id(item) for item in members)
    return made, consumed


def _span_of(item: dict[str, Any]) -> tuple[int, int]:
    start = int(number(item.get("Frame"), 0.0))
    return start, start + max(1, int(number(item.get("Length"), 1.0)))


def _overlaps(item: dict[str, Any], group: dict[str, Any]) -> bool:
    start, end = _span_of(item)
    group_start, group_end = _span_of(group)
    return start < group_end and group_start < end


def _note_crossing(
    members: list[dict[str, Any]], low: int, reach: int | None, log: CompatibilityReport
) -> None:
    """合成するグループの中のグループが、外側の範囲を越えて掛かっていれば記録する

    内側のグループはまとめた絵の中でしか働かないので、外側の範囲の外の段には何も
    掛けられない YMM4 がこの形をどう描くかは確かめていない（配布物 230 本に合成する
    グループを越える形は無かった） 黙って捨てず、互換性レポートに残す
    """
    if reach is None:
        return
    for item in members:
        if type_name(item) not in _CONTAINER_ITEMS:
            continue
        # ``GroupRange`` を持たない内側のグループは上の段すべてに掛かる扱いなので、
        # 範囲のある外側の中では必ず越えている
        inner = _reach_of(item)
        if inner is None or _layer_of(item) + inner > low + reach:
            log.note_missing("YMM4 の合成するグループの範囲を越える内側のグループ")


def _is_composite(item: dict[str, Any]) -> bool:
    return type_name(item) in _CONTAINER_ITEMS and item.get("IsComposite") is True


def _composite(
    group: dict[str, Any], members: list[dict[str, Any]], log: CompatibilityReport
) -> MappedObject | None:
    """合成するグループ 1 つを、中身をまとめたシーンのクリップへ

    中身のレイヤーはグループからの相対へ直す シーンのトラックは 1 から始まるので、
    グループのすぐ上の段が 1 本目になる

    時刻は、グループと中身のうち一番早く始まるものからの相対にする グループより
    先に始まった中身は、シーンの中でもその分だけ先に始まり、グループのクリップは
    シーンの途中（:attr:`MappedObject.scene_offset`）から映す グループの頭へ
    詰めると、グループが始まった時点で進んでいるはずの動きが頭から描き直される

    グループ自身の反転・拡大・回転・エフェクトは、まとめた絵（画面の大きさ）に
    掛かるので、画面の中心を軸に効く YMM4 の書き出しと比べて確かめた
    （吹き出し風ワイプの縁取りと影と登場の動きを 1 枚に掛けると、真ん中の
    フレームの差が 2.6 から 0.7 へ、リボンのテロップの入りが 8.4 から 4.8 へ下がる）
    """
    center = str(group.get("CompositeCenter") or "ScreenCenter")
    if center != "ScreenCenter":
        log.note_missing(f"YMM4 のグループの合成の中心: {center}")
    start = int(number(group.get("Frame"), 0.0))
    earliest = min((_span_of(item)[0] for item in members), default=start)
    origin = min(start, earliest)
    base = _layer_of(group) + 1
    rebased = [
        {
            **item,
            "Layer": _layer_of(item) - base,
            "Frame": _span_of(item)[0] - origin,
        }
        for item in members
    ]
    children = tuple(_map_items(rebased, log))
    if not any(child.has_picture for child in children):
        return None

    length = max(1, int(number(group.get("Length"), 1.0)))
    keyframes = group.get("KeyFrames")
    remark = str(group.get("Remark") or "").strip().splitlines()
    return MappedObject(
        clip=Clip(
            timeline_start=max(0, start),
            duration=length,
            effects=tuple(_group_effects(group, log)),
            opacity=animated(
                group.get("Opacity"), 100.0, length=length, keyframes=keyframes, scale=0.01
            ),
            blend_mode=_blend_of(group, log),
            clip_to_below=group.get("IsClippingWithObjectAbove") is True,
        ),
        layer=max(1, base),
        kind="scene",
        children=children,
        label=remark[0] if remark else "合成したグループ",
        scene_offset=start - origin,
    )


def _with_group_effects(
    item: MappedObject, containers: list[_Container], *, span: int
) -> MappedObject:
    """入れ物のエフェクトを 1 つの中身へ移す 動く値の時刻は中身の長さへ揃える

    揃える先は中身の長さちょうど YMM4 は最後の点を長さの位置に置く

    中身が長さを持っていないことがある（``レトロなカウントダウン3秒`` は
    入れ物 90 に対して中身 1） そのまま 1 に揃えると 90 フレームの動きが
    2 フレームに潰れ、置くときに既定の長さまで伸ばされても動きは戻らない
    長さが分かるのは入れ物の側だけなので、そちらを中身の長さとして使う
    ``span`` は、範囲に入っていない入れ物も含めた一番長い入れ物の長さ 中身の長さを
    借りるのは範囲と関係なく、テンプレート全体の尺を決めるため
    """
    known = item.has_span or span <= 1
    duration = item.clip.duration if known else span
    moved = [
        fitted_effect(effect, container.length, duration)
        for container in containers
        for effect in container.effects
    ]
    return replace(
        item,
        clip=replace(item.clip, duration=duration, effects=(*item.clip.effects, *moved)),
        # 入れ物から長さを借りたなら、長さの分かるものとして扱う
        has_span=item.has_span or not known,
    )


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

    speed, silenced = _playback_rate(item, name, log, length=length, keyframes=keyframes)
    return MappedObject(
        clip=Clip(
            timeline_start=max(0, int(number(item.get("Frame"), 0.0))),
            duration=length,
            source=source,
            source_in=_content_offset(item, log) if media_path else Fraction(0),
            speed=speed,
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
        # 動画アイテムは 1 つで映像と音の両方を持つ 音声トラックへも展開しないと鳴らない
        with_sound=name == "VideoItem",
        audio_effects=_audio_effects(
            item, name, log, length=length, keyframes=keyframes, silenced=silenced
        ),
    )


def _content_offset(item: dict[str, Any], log: CompatibilityReport) -> Fraction:
    """素材のどこから再生するか（``ContentOffset``）を秒で

    切り出して使っているテンプレートは、ここを落とすと絵も音も違う所から始まる
    実物（この機械の YMM4 プロジェクト 16 本）の動画 214 個・音声 125 個のうち
    240 個が 0 以外だった

    **効かせるのは素材を持つアイテムだけ** 配布物 230 本を数えると、0 以外なのは
    テキスト 33・図形 25・フレームバッファ 6・グループ 4 と、素材を読まない物ばかりで、
    YMM4 でも絵は動かない（書き出しに残っている既定の値） こちらで効かせると、
    グループ（入れ子のシーン）の時刻だけがずれる

    負の値はこちらの :class:`~sashimono.core.model.Clip` が受け取らない（素材の
    手前から再生することになる） 0 として置き、数えて残す 読めない形も同じ
    """
    raw = item.get("ContentOffset")
    if raw is None or raw == "":
        return Fraction(0)
    offset = timespan(raw)
    if offset is None:
        log.note_missing(f"YMM4 の素材の開始位置（ContentOffset）の書き方: {raw!r}")
        return Fraction(0)
    if offset < 0:
        log.note_missing("YMM4 の素材の開始位置（ContentOffset）が負")
        return Fraction(0)
    return offset


#: 音を持つアイテム 映像を持つのは ``VideoItem`` だけ
_SOUND_ITEMS = frozenset({"VideoItem", "AudioItem", "VoiceItem"})


def _audio_effects(
    item: dict[str, Any],
    name: str,
    log: CompatibilityReport,
    *,
    length: int,
    keyframes: Any,
    silenced: bool = False,
) -> tuple[Effect, ...]:
    """音の設定を、音声トラックのクリップへ掛けるエフェクトにする

    項目の並びは YMM4 自身が書いたものを数えて決めた 手元の YMM4（4.48.0.3）が
    ``user/setting/…/ItemSettings.json`` の ``DefaultItems.VideoItem`` へ書き出す
    既定の動画アイテムと、実際のプロジェクト 16 本に入っていた動画アイテム 214 個・
    音声アイテム 125 個が同じ並びだった（``Volume`` ``Pan`` ``PlaybackRate``
    ``ContentOffset`` ``IsLooped`` ``AudioTrackIndex`` ``AudioEffects`` ``Echo*``）
    アイテムテンプレートの中身も同じ形で入る（同じ設定ファイルの ``Templates`` が、
    ``.ymmt`` の ``catalog.json`` と同じ ``Name`` ``Path`` ``Items`` を持つ）

    写し方は YMM4 本体に書き出させて測って決めた（2026-09-23 YMM4 4.56.1.1
    440Hz の正弦波 基準の枠との最大振幅の比 表は docs/development.md の「音を測る」）

    * ``Volume`` は振幅比の百分率 50 で 0.501・25 で 0.250・0 で無音 YMM4 に音を消す
      印は無く、実物でも音を消した 18 個は ``Volume`` が 0 だった こちらの
      ``audio_volume`` の ``volume`` も振幅比の百分率なので、そのまま渡す
    * ``Pan`` は -100〜100 で負が左 -100 で左 1.00・右 0.00、-50 で左 1.00・右 0.50
      近い側はそのままで、遠い側だけ直線で下がる ``audio_volume`` の ``pan`` と同じ作り
      なので、そのまま渡す
    * ``PlaybackRate`` はクリップの ``speed`` へ（:func:`_playback_rate`）
      0 のとき（``silenced``）は鳴らさないので、音量を 0 にする

    写せないものは数えて残す
    """
    if name not in _SOUND_ITEMS:
        return ()

    def read(key: str, default: float) -> AnimatedValue:
        return animated(item.get(key), default, length=length, keyframes=keyframes)

    def differs(value: AnimatedValue, default: float) -> bool:
        """既定と違う値を持つか 動く値は途中の点まで見る

        先頭の値だけを見ると（``number``）、0 から動き出す定位のように、
        始まりが既定と同じものを取りこぼし、左右へ振る定位が真ん中のまま鳴る
        """
        if value.keyframes:
            return any(point.value != default for point in value.keyframes)
        return value.static != default

    if int(number(item.get("AudioTrackIndex"), 0.0)) != 0:
        log.note_missing("YMM4 の音声トラックの選択（AudioTrackIndex）")
    if item.get("IsLooped") is True:
        log.note_missing("YMM4 の素材の繰り返し（IsLooped）")
    if item.get("EchoIsEnabled") is True:
        log.note_missing("YMM4 のエコー")
    for entry in item.get("AudioEffects") or []:
        # 切ってあるエフェクトは鳴り方に関わらない 数えると、直す順番を決めるときに
        # 効いていないものが上位に来る 映像エフェクトの読み方（map_video_effects）と同じ
        # 実物の音声エフェクト 3 個はどれも IsEnabled を持っていた
        if isinstance(entry, dict) and entry.get("IsEnabled") is not False:
            log.note_missing(f"YMM4 の音声エフェクト: {type_name(entry) or '種類不明'}")

    volume = AnimatedValue(0.0) if silenced else read("Volume", 100.0)
    pan = read("Pan", 0.0)
    if not differs(volume, 100.0) and not differs(pan, 0.0):
        # 既定のままなら何も掛けない 音量 100% のエフェクトが並ぶと、
        # 何を変えたテンプレートなのかが設定画面から読めなくなる
        return ()
    definition = registry.get("audio_volume")
    return () if definition is None else (definition.create(volume=volume, pan=pan),)


def _playback_rate(
    item: dict[str, Any],
    name: str,
    log: CompatibilityReport,
    *,
    length: int,
    keyframes: Any,
) -> tuple[Fraction, bool]:
    """再生速度（``PlaybackRate``）を、クリップの ``speed`` と「鳴らさないか」の組にする

    YMM4 に書き出させて測った（2026-09-23 YMM4 4.56.1.1 2 秒の 440Hz の正弦波を
    4 秒の枠へ置いた） 50 で 3.98 秒・220Hz、200 で 1.00 秒・880Hz、最大振幅は
    変わらない テープのように速さと一緒に高さも変わり、こちらの ``speed``
    （ミキサーの線形の並べ直し）と同じ作り ``Length`` はタイムライン上の長さの
    ままで、素材は ``Length × rate`` だけ進む 50 で 2 秒の素材が 4 秒の枠に収まった
    のがそれで、:class:`~sashimono.core.model.Clip` の ``duration`` と ``speed`` の
    決まり（素材を ``duration × speed`` 読む）と合うので、長さは変えずに渡す

    **0 は鳴らない**（測ると無音で、鳴っている長さも 0） こちらの ``speed`` は
    正の数しか取らないので、``speed`` は 1 のまま置き、音量を 0 にして止める
    （2 つ目の値） クリップを置かない形にしないのは、音声アイテムだと置いた物が
    まるごと消え、あとで速さを直そうにも手を掛ける所が無くなるため YMM4 でも
    音を消したアイテムは ``Volume`` 0 で持つので、同じ形になる
    動画アイテムの ``speed`` は映像のクリップにも効く 絵の速さも YMM4 に書き出させて
    測った（2026-09-23 YMM4 4.56.1.1 ``tools/ymm4_compare.py`` の ``video-rate-build``
    書き出しの各フレームを素材のフレームと突き合わせ、経過フレームに対する傾きを取った）
    100 で 1.00 倍・50 で 0.50 倍・200 で 2.00 倍と、こちらの ``speed`` と一致したので
    数えない 映像と音で分けないのは、リンクした 2 本の速さが違うと絵と音がずれていくため

    **動画アイテムの 0 は、素材の頭（``ContentOffset`` の位置）の絵で止まる**（同じ測り）
    こちらには止めた絵を表す仕組みが無い（``speed`` は正の数だけで、止める効果も
    クリップの持ち方も無い 静止画の素材だけが時刻 0 を読む）ので、絵は等倍で動かし、
    数えて残す 実物の 0 は 5 個あり、どれも動画アイテム（mp4 4 個・webp 1 個）

    NaN や無限大は分数にできないので、等倍として置き数えて残す

    音を持たないアイテムは見ない 実物ではどれも 100 で、``speed`` を持たせると
    テキストや図形の動きの時刻まで変わる

    手元のファイル 99 本（配布物・この機械のプロジェクト・測るために作った試料）の
    アイテム 1156 個の ``PlaybackRate`` はどれもただの数だった 形が来たら先頭の値を
    使い、動くなら数えて残す（``speed`` は動かせない）

    新しい版の書き出しは、ほかに動く値の ``PlaybackRate2`` と、音の速さの変え方
    ``PlaybackRateAudioProcessingMode`` も持つ（実物の音を持つアイテム 138 個は
    どれも ``Resampling``、``PlaybackRate2`` は動かず ``PlaybackRate`` と同じ値だった）
    読むのは ``PlaybackRate`` のまま 測ったとき YMM4 4.56.1.1 は ``PlaybackRate``
    だけのアイテムで速さを変えた 2 つが食い違う・``PlaybackRate2`` が動く・
    ``Resampling`` 以外（高さを変えない変え方かもしれない）は、どちらが効くのか
    測っていないので数えて残す
    """
    if name not in _SOUND_ITEMS:
        return Fraction(1), False
    raw = item.get("PlaybackRate")
    if raw is not None and not _readable_number(raw):
        # 文字や中身の無い Values は number が既定の 100 へ丸めるので、黙っていると
        # 壊れた値が等倍として写り、互換性レポートにも出ない NaN と同じく数えて残す
        log.note_missing(f"YMM4 の再生速度（PlaybackRate）が読めない値: {raw!r}")
        return Fraction(1), False
    rate = number(raw, 100.0)

    # 動きはほかの項目と同じくアイテムの長さと中間点で読む 既定の長さ 1 で読むと、
    # 3 点目以降が同じフレームに重なって捨てられ、途中の値だけが違う動きを数え落とす
    def read(value: Any, default: float) -> AnimatedValue:
        return animated(value, default, length=length, keyframes=keyframes)

    if any(point.value != rate for point in read(raw, 100.0).keyframes):
        log.note_missing("YMM4 の再生速度（PlaybackRate）の動き（先頭の値で写した）")
    newer = item.get("PlaybackRate2")
    if newer is not None and not _readable_number(newer):
        log.note_missing(f"YMM4 の再生速度（PlaybackRate2）が読めない値: {newer!r}")
    elif newer is not None:
        moving = read(newer, rate)
        if moving.keyframes and any(point.value != rate for point in moving.keyframes):
            log.note_missing("YMM4 の再生速度（PlaybackRate2）の動き")
        elif not moving.keyframes and moving.static != rate:
            log.note_missing("YMM4 の再生速度の食い違い（PlaybackRate と PlaybackRate2）")
    mode = item.get("PlaybackRateAudioProcessingMode")
    if mode is not None and mode != "Resampling":
        log.note_missing(f"YMM4 の再生速度の音の変え方: {mode}")
    if not math.isfinite(rate):
        # NaN や無限大は分数にできず、そのまま渡すと ValueError で読み込みごと止まり、
        # 同じテンプレートの正常なアイテムまで写せなくなる 等倍として置き、数えて残す
        log.note_missing(f"YMM4 の再生速度（PlaybackRate）が読めない値: {rate!r}")
        return Fraction(1), False
    if rate == 0:
        if name == "VideoItem":
            log.note_missing("YMM4 の再生速度 0 の動画の止まった絵（等倍で動かした）")
        return Fraction(1), True
    if rate < 0:
        # 負の値は実物に無く、YMM4 でどう鳴るかも測っていない
        log.note_missing("YMM4 の再生速度（PlaybackRate）が負")
        return Fraction(1), False
    # 2 進の小数のまま分数にすると 102.1 が長い分母の分数になる 書かれた 10 進で持つ
    return Fraction(repr(rate)) / 100, False


def _readable_number(value: Any) -> bool:
    """数として読める形か ただの数・数の文字・値を 1 つ以上持つ動く値

    ``number`` と ``animated`` は読めない形を既定値へ丸める 丸めた後では、書かれて
    いた値が既定だったのか壊れていたのか見分けられないので、丸める前に見る
    真偽値は数に読めるが（``True`` が 1）、速さとして書かれることは無いので断る
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return True
    if isinstance(value, str):
        try:
            float(value)
        except ValueError:
            return False
        return True
    if isinstance(value, dict):
        values = value.get("Values")
        return (
            isinstance(values, list)
            and bool(values)
            and all(
                isinstance(entry, dict) and _readable_number(entry.get("Value")) for entry in values
            )
        )
    return False


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
        # 線の位置は中央のまま 大きさを縮めて内側に見せる書き方で YMM4 の見本と
        # 合わせてあるので、内側に引く（#87 の既定）と二重に細って合わなくなる
        params["line_align"] = "center"
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

    AviUtl 側（:func:`~sashimono.compat.aviutl.mapping.map_object`）と同じ扱いに
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
