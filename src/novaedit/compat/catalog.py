"""外部のテンプレートを 1 つの棚に並べる。

集めるのは 2 種類。

* AviUtl のエイリアス — ``.exa`` ``.exa2`` ``.object``（AviUtl2 世代）
* YMM4 のアイテムテンプレート — ``.ymmt``

読み方は違うが、出てくるものは同じ :class:`~novaedit.compat.mapped.MappedObject`
なので、タイムラインへ置く処理は 1 つで済む。

**字幕テンプレートは「置く」だけでなく「今のクリップに着せる」ことができる。**
配布されている字幕エイリアスは、見本の文字（``字幕テキスト`` など）が入った
テキストオブジェクトとして配られている。そのまま置くと、字幕を打ち直すことに
なる。:func:`restyle` は文字と時間を今のクリップのまま残し、見た目だけを
入れ替える。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from novaedit.compat.aviutl.exo import ExoParseError, load_exo
from novaedit.compat.aviutl.mapping import map_object
from novaedit.compat.aviutl.report import CompatibilityReport, global_report
from novaedit.compat.mapped import MappedObject
from novaedit.compat.ymm4.template import Ymm4ParseError, load_template, map_template
from novaedit.core.commands import AddClip, AddEffect, AddTrack, Command, RemoveEffect, SetSource
from novaedit.core.commands.insert import DEFAULT_GENERATED_FRAMES
from novaedit.core.model import Clip, GeneratedSource, Project, Track, TrackId, TrackKind
from novaedit.core.timebase import FrameRate

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

#: AviUtl 側で読む拡張子。
_AVIUTL_SUFFIXES = (".exa", ".exa2", ".object", ".exo", ".exo2")

#: YMM4 側で読む拡張子。
_YMM4_SUFFIXES = (".ymmt",)

#: 文字だけを差し替えて着せ替えるときに、テンプレート側から**取らない**設定。
#:
#: 文字そのものと文字送りは、今のクリップの持ち物。見た目を変えたいだけなのに
#: 中身まで置き換わったら、それは着せ替えではない。
_KEPT_ON_RESTYLE = frozenset({"text", "reveal"})


@dataclass(frozen=True, slots=True)
class TemplateEntry:
    """棚に並ぶテンプレート 1 つ。"""

    name: str
    path: Path
    #: 置かれていたフォルダ名。配布物はフォルダで分かれているので、そのまま出す。
    folder: str = ""
    #: ``"aviutl"`` か ``"ymm4"``。
    source: str = "aviutl"

    @property
    def label(self) -> str:
        return self.name

    def load(self, *, report: CompatibilityReport | None = None) -> list[MappedObject]:
        """中身を読んで、写した結果を返す。"""
        log = report if report is not None else global_report
        if self.source == "ymm4":
            return map_template(load_template(self.path), report=log)

        exo = load_exo(self.path)
        mapped = [map_object(obj, FrameRate(30, 1), report=log) for obj in exo.objects]
        return [item for item in mapped if item is not None]


class TemplateCatalog:
    """フォルダを走査して並べる。"""

    def __init__(self) -> None:
        self._entries: list[TemplateEntry] = []

    def scan(self, roots: tuple[Path, ...]) -> list[TemplateEntry]:
        """走査してこの棚を入れ替える。読めないファイルは黙って飛ばす。"""
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
                entry = _entry_for(path, root)
                if entry is not None:
                    seen.add(resolved)
                    found.append(entry)
        self._entries = found
        return found

    def all(self) -> tuple[TemplateEntry, ...]:
        return tuple(self._entries)

    def folders(self) -> tuple[str, ...]:
        """出てきたフォルダ名を、並んだ順のまま重複なく。"""
        names: list[str] = []
        for entry in self._entries:
            if entry.folder not in names:
                names.append(entry.folder)
        return tuple(names)

    def find(self, name: str) -> TemplateEntry | None:
        return next((entry for entry in self._entries if entry.name == name), None)


def _entry_for(path: Path, root: Path) -> TemplateEntry | None:
    suffix = path.suffix.lower()
    if suffix in _AVIUTL_SUFFIXES:
        source = "aviutl"
    elif suffix in _YMM4_SUFFIXES:
        source = "ymm4"
    else:
        return None

    relative = path.parent.relative_to(root)
    folder = str(relative) if str(relative) != "." else root.name
    return TemplateEntry(name=path.stem, path=path, folder=folder, source=source)


def default_template_roots() -> tuple[Path, ...]:
    """既定で見に行くフォルダ。

    スクリプトと同じ考え方で、**すでに持っている資産をコピーせずに使える**ことを
    優先する。AviUtl2 や YMM4 が入っていれば、そのフォルダをそのまま見る。
    """
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "NovaEdit" / "templates")

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
    """棚を差し替える。テストと、フォルダ設定を変えたときに使う。"""
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
    """写した結果をタイムラインへ置くコマンドの列。

    ``track_id`` を渡せばそのトラックへまとめて置く。渡さなければ、元の
    レイヤー番号に対応する映像トラックへ置く（無ければ作る）。
    """
    if not objects:
        return []

    commands: list[Command] = []
    tracks = (
        {}
        if track_id is not None
        else _tracks_for(project, {item.layer for item in objects}, commands)
    )

    # 一番早いオブジェクトが ``at_frame`` に来るように、まとめてずらす。
    # エイリアスは元のタイムライン上の位置を持ったままなので、そのまま置くと
    # 指定した場所ではなく元あった場所へ行く。
    origin = min(item.clip.timeline_start for item in objects)

    for item in objects:
        duration = item.clip.duration if item.has_span else default_duration
        placed = replace(
            item.clip,
            timeline_start=item.clip.timeline_start - origin + max(0, at_frame),
            duration=max(1, duration),
        )
        target = track_id if track_id is not None else tracks[item.layer].id
        commands.append(AddClip(target, placed))
    return commands


def restyle(objects: list[MappedObject], clip: Clip) -> list[Command]:
    """テンプレートの見た目を、今あるクリップへ着せる。

    使うのはテキストオブジェクトを持つ最初の 1 つだけ。字幕テンプレートは
    1 オブジェクトで配られるし、複数あってもどれを着せるべきかは決められない。
    """
    template = next(
        (item for item in objects if item.clip.source and item.clip.source.kind == "text"),
        None,
    )
    if template is None or template.clip.source is None:
        return []
    if clip.source is None or clip.source.kind != "text":
        return []

    params = {
        **clip.source.params,
        **{
            name: value
            for name, value in template.clip.source.params.items()
            if name not in _KEPT_ON_RESTYLE
        },
    }

    commands: list[Command] = [SetSource(clip.id, GeneratedSource(kind="text", params=params))]
    commands.extend(RemoveEffect(clip.id, effect.id) for effect in clip.effects)
    commands.extend(AddEffect(clip.id, effect) for effect in template.clip.effects)
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


#: 読み込みに失敗したときに投げられる例外。呼び出し側はこれだけ捕まえればよい。
TemplateError = (ExoParseError, Ymm4ParseError)
