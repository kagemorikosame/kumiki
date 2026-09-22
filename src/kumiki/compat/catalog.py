"""外部のテンプレートを 1 つの棚に並べる

集めるのは 2 種類

* AviUtl のエイリアス — ``.exa`` ``.exa2`` ``.object``（AviUtl2 世代）
* YMM4 のアイテムテンプレート — ``.ymmt``

読み方は違うが、出てくるものは同じ :class:`~kumiki.compat.mapped.MappedObject`
なので、タイムラインへ置く処理は 1 つで済む

**字幕テンプレートは「置く」だけでなく「今のクリップに着せる」ことができる**
配布されている字幕エイリアスは、見本の文字（``字幕テキスト`` など）が入った
テキストオブジェクトとして配られている そのまま置くと、字幕を打ち直すことに
なる :func:`restyle` は文字と時間を今のクリップのまま残し、見た目だけを
入れ替える
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from kumiki.compat.aviutl.exo import ExoParseError, load_exo
from kumiki.compat.aviutl.mapping import map_object
from kumiki.compat.aviutl.report import CompatibilityReport, global_report
from kumiki.compat.mapped import MappedObject, fitted_effect, fitted_value
from kumiki.compat.ymm4.template import Ymm4ParseError, load_template, map_template
from kumiki.core.commands import (
    AddClip,
    AddEffect,
    AddScene,
    AddTrack,
    Command,
    InScene,
    RemoveEffect,
    SetSource,
    new_scene,
)
from kumiki.core.commands.insert import DEFAULT_GENERATED_FRAMES
from kumiki.core.model import (
    Clip,
    GeneratedSource,
    Project,
    SceneId,
    Track,
    TrackId,
    TrackKind,
)
from kumiki.core.timebase import FrameRate

__all__ = [
    "TemplateCatalog",
    "TemplateEntry",
    "TemplateError",
    "default_template_roots",
    "place",
    "restyle",
    "set_template_catalog",
    "template_catalog",
]

#: AviUtl 側で読む拡張子
_AVIUTL_SUFFIXES = (".exa", ".exa2", ".object", ".exo", ".exo2")

#: YMM4 側で読む拡張子
_YMM4_SUFFIXES = (".ymmt",)

#: 文字だけを差し替えて着せ替えるときに、テンプレート側から**取らない**設定
#:
#: 文字そのものと文字送りは、今のクリップの持ち物 見た目を変えたいだけなのに
#: 中身まで置き換わったら、それは着せ替えではない
_KEPT_ON_RESTYLE = frozenset({"text", "reveal"})


@dataclass(frozen=True, slots=True)
class TemplateEntry:
    """棚に並ぶテンプレート 1 つ"""

    name: str
    path: Path
    #: 置かれていたフォルダ名 配布物はフォルダで分かれているので、そのまま出す
    folder: str = ""
    #: ``"aviutl"`` か ``"ymm4"``
    source: str = "aviutl"
    #: ``.ymmt`` の中の何本目か
    #:
    #: AviUtl のエイリアスは 1 ファイル 1 本だが、YMM4 のアイテムテンプレートは
    #: **1 ファイルに何本も入っている**（手元の配布物は 17 本と 106 本だった）
    index: int = 0

    @property
    def label(self) -> str:
        return self.name

    def load(self, *, report: CompatibilityReport | None = None) -> list[MappedObject]:
        """中身を読んで、写した結果を返す"""
        log = report if report is not None else global_report
        if self.source == "ymm4":
            templates = load_template(self.path)
            if not 0 <= self.index < len(templates):
                return []
            return map_template(list(templates[self.index].items), report=log)

        exo = load_exo(self.path)
        mapped = [map_object(obj, FrameRate(30, 1), report=log) for obj in exo.objects]
        return [item for item in mapped if item is not None]


class TemplateCatalog:
    """フォルダを走査して並べる"""

    def __init__(self) -> None:
        self._entries: list[TemplateEntry] = []

    def scan(self, roots: tuple[Path, ...]) -> list[TemplateEntry]:
        """走査してこの棚を入れ替える 読めないファイルは黙って飛ばす"""
        found: list[TemplateEntry] = []
        seen: set[Path] = set()
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                resolved = path.resolve()
                if resolved in seen:
                    continue
                entries = _entries_for(path, root)
                if entries:
                    seen.add(resolved)
                    found.extend(entries)
        self._entries = found
        return found

    def all(self) -> tuple[TemplateEntry, ...]:
        return tuple(self._entries)

    def folders(self) -> tuple[str, ...]:
        """出てきたフォルダ名を、並んだ順のまま重複なく"""
        names: list[str] = []
        for entry in self._entries:
            if entry.folder not in names:
                names.append(entry.folder)
        return tuple(names)

    def find(self, name: str) -> TemplateEntry | None:
        return next((entry for entry in self._entries if entry.name == name), None)


def _entries_for(path: Path, root: Path) -> list[TemplateEntry]:
    """1 ファイルから並ぶテンプレート

    AviUtl のエイリアスは 1 本 YMM4 のアイテムテンプレートは中を開いて数える
    """
    suffix = path.suffix.lower()
    relative = path.parent.relative_to(root)
    folder = str(relative) if str(relative) != "." else root.name

    if suffix in _AVIUTL_SUFFIXES:
        return [TemplateEntry(name=path.stem, path=path, folder=folder, source="aviutl")]
    if suffix not in _YMM4_SUFFIXES:
        return []

    try:
        templates = load_template(path)
    except (Ymm4ParseError, OSError):
        # 読めないものは棚に出さない 開くまで中身が分からない形式なので、
        # 一覧に並べてから「読めません」と言うより出さないほうが分かりやすい
        return []

    return [
        TemplateEntry(
            name=template.name or f"{path.stem} {index + 1}",
            path=path,
            # 配布物は ``アニメーション効果/振り子`` のように分類を持っている
            # ファイル名だけで並べると 100 本超が 1 つの見出しに潰れる
            folder=f"{path.stem} / {template.folder}" if template.folder else path.stem,
            source="ymm4",
            index=index,
        )
        for index, template in enumerate(templates)
    ]


def default_template_roots() -> tuple[Path, ...]:
    """既定で見に行くフォルダ

    スクリプトと同じ考え方で、**すでに持っている資産をコピーせずに使える**ことを
    優先する AviUtl2 や YMM4 が入っていれば、そのフォルダをそのまま見る
    """
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "Kumiki" / "templates")

    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        roots.append(Path(program_data) / "aviutl2" / "Alias")

    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "YukkuriMovieMaker" / "ItemTemplate")
    return tuple(roots)


_catalog = TemplateCatalog()


def template_catalog() -> TemplateCatalog:
    return _catalog


def set_template_catalog(catalog: TemplateCatalog) -> None:
    """棚を差し替える テストと、フォルダ設定を変えたときに使う"""
    global _catalog
    _catalog = catalog


def place(
    objects: list[MappedObject],
    project: Project,
    *,
    at_frame: int = 0,
    track_id: TrackId | None = None,
    default_duration: int = DEFAULT_GENERATED_FRAMES,
) -> list[Command]:
    """写した結果をタイムラインへ置くコマンドの列

    ``track_id`` を渡せばそのトラックへまとめて置く 渡さなければ、元の
    レイヤー番号に対応する映像トラックへ置く（無ければ作る）
    """
    # 中身を持たないもの（エフェクトだけのテンプレート）は置けない
    # 空のクリップを置いても何も映らないので、:func:`restyle` で着せて使う
    objects = [item for item in objects if item.has_picture]
    if not objects:
        return []

    commands: list[Command] = []
    tracks = (
        {}
        if track_id is not None
        else _tracks_for(project, {item.layer for item in objects}, commands)
    )

    # 一番早いオブジェクトが ``at_frame`` に来るように、まとめてずらす
    # エイリアスは元のタイムライン上の位置を持ったままなので、そのまま置くと
    # 指定した場所ではなく元あった場所へ行く
    origin = min(item.clip.timeline_start for item in objects)

    for item in objects:
        duration = item.clip.duration if item.has_span else default_duration
        placed = replace(
            item.clip,
            timeline_start=item.clip.timeline_start - origin + max(0, at_frame),
            duration=max(1, duration),
        )
        if item.children:
            placed = replace(
                placed,
                scene_id=_scene_for(item, project, commands),
                # シーンの中の時刻は秒で持つ（素材のクリップと同じ決まり）
                source_in=item.scene_offset * project.rate.frame_duration,
            )
        target = track_id if track_id is not None else tracks[item.layer].id
        commands.append(AddClip(target, placed))
    return commands


def _scene_for(item: MappedObject, project: Project, commands: list[Command]) -> SceneId:
    """まとめて 1 枚にする中身をシーンへ置き、そのシーンを返す

    中身の位置はまとめた入れ物の頭からの時刻で持っているので、そのまま置く
    （``at_frame`` を中身の一番早い位置にして、ずらさない） 頭へ詰めると、
    遅れて出てくる中身が入れ物の頭から出てしまう
    """
    scene = new_scene(project, item.label or "まとめた絵")
    commands.append(AddScene(scene))
    # 中身を置くコマンドはシーンのタイムラインを相手に作る 置き先のトラックを
    # 探すのにメインのトラックを見ると、シーンに無いトラックへ置こうとして落ちる
    inside = replace(project, timeline=scene.timeline)
    earliest = min((child.clip.timeline_start for child in item.children), default=0)
    commands.extend(
        InScene(scene.id, command)
        for command in place(list(item.children), inside, at_frame=earliest)
    )
    return scene.id


def restyle(objects: list[MappedObject], clip: Clip) -> list[Command]:
    """テンプレートの見た目を、今あるクリップへ着せる

    2 通りある

    * **中身のあるテンプレート** — テキストオブジェクトを持つ最初の 1 つを使い、
      文字と時間は今のまま、見た目だけを入れ替える 字幕テンプレートはこれ
    * **エフェクトだけのテンプレート** — YMM4 の「アニメーション効果」のように
      中身を持たないもの 今のクリップに**エフェクトを足す**だけで、
      中身には触らない だからテキスト以外のクリップにも着せられる
    """
    if not objects:
        return []

    effects_only = not any(item.has_picture for item in objects)
    if effects_only:
        added = [
            fitted_effect(effect, item.clip.duration, clip.duration - 1)
            for item in objects
            for effect in item.clip.effects
        ]
        if not added:
            return []
        return [AddEffect(clip.id, effect) for effect in added]

    # 文字はまとめた中身（合成するグループ）の中にあることがある 上だけを見ると、
    # 吹き出しの字幕テンプレートが「文字の無いテンプレート」として断られる
    template = next(
        (
            inner
            for item in objects
            for inner in item.walk()
            if inner.clip.source and inner.clip.source.kind == "text"
        ),
        None,
    )
    if template is None or template.clip.source is None:
        return []
    if clip.source is None or clip.source.kind != "text":
        return []

    span = template.clip.duration
    params = {
        **clip.source.params,
        **{
            name: fitted_value(value, span, clip.duration - 1)
            for name, value in template.clip.source.params.items()
            if name not in _KEPT_ON_RESTYLE
        },
    }

    commands: list[Command] = [SetSource(clip.id, GeneratedSource(kind="text", params=params))]
    commands.extend(RemoveEffect(clip.id, effect.id) for effect in clip.effects)
    commands.extend(
        AddEffect(clip.id, fitted_effect(effect, span, clip.duration - 1))
        for effect in template.clip.effects
    )
    return commands


def _tracks_for(project: Project, layers: set[int], commands: list[Command]) -> dict[int, Track]:
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


#: 読み込みに失敗したときに投げられる例外 呼び出し側はこれだけ捕まえればよい
TemplateError = (ExoParseError, Ymm4ParseError)
