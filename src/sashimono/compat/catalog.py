"""外部のテンプレートを 1 つの棚に並べる

集めるのは 2 種類

* AviUtl のエイリアス — ``.exa`` ``.exa2`` ``.object``（AviUtl2 世代）
* YMM4 のアイテムテンプレート — ``.ymmt``

読み方は違うが、出てくるものは同じ :class:`~sashimono.compat.mapped.MappedObject`
なので、タイムラインへ置く処理は 1 つで済む

**字幕テンプレートは「置く」だけでなく「今のクリップに着せる」ことができる**
配布されている字幕エイリアスは、見本の文字（``字幕テキスト`` など）が入った
テキストオブジェクトとして配られている そのまま置くと、字幕を打ち直すことに
なる :func:`restyle` は文字と時間を今のクリップのまま残し、見た目だけを
入れ替える
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath

from sashimono.compat.aviutl.exo import ExoParseError, load_exo
from sashimono.compat.aviutl.mapping import map_object
from sashimono.compat.aviutl.report import CompatibilityReport, global_report
from sashimono.compat.mapped import MappedObject, fitted_effect, fitted_value
from sashimono.compat.ymm4.template import Ymm4ParseError, load_template, map_template
from sashimono.core import userdirs
from sashimono.core.commands import (
    AddClip,
    AddEffect,
    AddMedia,
    AddScene,
    AddTrack,
    Command,
    InScene,
    RemoveEffect,
    SetSource,
    new_scene,
)
from sashimono.core.commands.insert import DEFAULT_GENERATED_FRAMES
from sashimono.core.model import (
    Clip,
    GeneratedSource,
    MediaItem,
    Project,
    SceneId,
    Track,
    TrackId,
    TrackKind,
)
from sashimono.core.timebase import FrameRate

__all__ = [
    "MediaPlan",
    "Probe",
    "TemplateCatalog",
    "TemplateEntry",
    "TemplateError",
    "default_template_roots",
    "gather_media",
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
    # APPDATA の有無で分けない（スクリプトの置き場と同じ理由 引き継ぎで写した先と揃える）
    roots.append(userdirs.config_root() / "templates")

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


#: 素材ファイルを開いて :class:`MediaItem` にする関数 開けなければ ``None``
#:
#: 互換層はファイルを開かない（:func:`~sashimono.compat.aviutl.mapping.media_paths` と
#: 同じ決まり） 開く道具は呼び出し側が渡す
type Probe = Callable[[Path], MediaItem | None]


@dataclass(frozen=True, slots=True)
class MediaPlan:
    """テンプレートが参照している素材を、プロジェクトへ登録する段取り"""

    #: 新しく登録する素材の :class:`AddMedia` 置くコマンドより先に実行する
    commands: tuple[Command, ...] = ()
    #: テンプレートに書かれたパス → 使う素材 すでに登録済みの素材も入る
    media: Mapping[str, MediaItem] = field(default_factory=dict)
    #: 見つからなかった・開けなかったパス
    missing: tuple[str, ...] = ()

    @property
    def added(self) -> tuple[MediaItem, ...]:
        """新しく登録する素材 解析や控えの作成を頼む相手"""
        return tuple(c.item for c in self.commands if isinstance(c, AddMedia))


def _media_paths(objects: list[MappedObject]) -> list[str]:
    """素材として登録するパス まとめた中身（シーン）の中も見る

    中身を自分で描くもの（音声波形など）は除く 絵は描いて作るので素材を
    クリップに結ばない 結ぶと、音声しか無い素材を映像トラックへ置くことになり
    置く時点で断られる
    """
    # 辞書で順序を保ったまま重複を落とす 一覧で ``in`` を引くと数が増えるほど遅くなる
    return list(
        dict.fromkeys(
            inner.media_path
            for item in objects
            for inner in item.walk()
            if inner.media_path and inner.clip.source is None
        )
    )


def _same_file(path: Path) -> str:
    """同じファイルかどうかを見分ける鍵

    Windows ではパスの大文字小文字を区別しない 素の文字列で比べると、
    同じ画像を書き方違いで 2 つの素材として登録してしまう
    """
    return os.path.normcase(str(path.resolve()))


def gather_media(
    objects: list[MappedObject],
    project: Project,
    probe: Probe,
    *,
    near: Path | None = None,
) -> MediaPlan:
    """テンプレートの画像・音声・動画を素材として登録する段取りを作る

    これを通さずに :func:`place` すると、素材を参照するクリップは ``media_id`` を
    持たず、置いても描かれず鳴らない

    * **同じファイルは 1 つの素材にする** プロジェクトにすでにあれば、それを使う
      テンプレートを 2 度置くたびに素材が増えると、素材一覧が同じ名前で埋まる
    * 書かれたパスに無ければ ``near``（テンプレートの置き場）で同じ名前を探す
      配布物のパスは作者の機械のもの（``C:\\Users\\作者\\…``）で、受け取った側の
      機械にはまず無い 素材を同じフォルダに添えて配る作者はいる
    """
    known = {_same_file(item.path): item for item in project.media}
    chosen: dict[str, MediaItem] = {}
    commands: list[Command] = []
    missing: list[str] = []
    for raw in _media_paths(objects):
        # 書かれたパスは Windows の形 ``Path`` で名前を取ると、Windows 以外では
        # ``\\`` を区切りと見ずにパス全体を名前として探しに行く
        candidates = [Path(raw)]
        if near is not None:
            candidates.append(near / PureWindowsPath(raw).name)
        path = next((c for c in candidates if c.is_file()), None)
        if path is None:
            missing.append(raw)
            continue
        key = _same_file(path)
        media = known.get(key)
        if media is None:
            media = probe(path)
            if media is None:
                missing.append(raw)
                continue
            commands.append(AddMedia(media))
            known[key] = media
        chosen[raw] = media
    return MediaPlan(commands=tuple(commands), media=chosen, missing=tuple(missing))


def place(
    objects: list[MappedObject],
    project: Project,
    *,
    at_frame: int = 0,
    track_id: TrackId | None = None,
    default_duration: int = DEFAULT_GENERATED_FRAMES,
    media: Mapping[str, MediaItem] | None = None,
) -> list[Command]:
    """写した結果をタイムラインへ置くコマンドの列

    ``track_id`` を渡せばそのトラックへまとめて置く 渡さなければ、元の
    レイヤー番号に対応する映像トラックへ置く（無ければ作る）

    ``media`` は :func:`gather_media` で登録する素材 素材を参照するクリップへ
    ``media_id`` を結ぶ 音声しか無い素材は映像トラックでは鳴らないので、
    ``track_id`` を渡していても音声トラックへ置く
    """
    # 中身を持たないもの（エフェクトだけのテンプレート）は置けない
    # 空のクリップを置いても何も映らないので、:func:`restyle` で着せて使う
    objects = [item for item in objects if item.has_picture]
    if not objects:
        return []

    known = media or {}
    heard = [item for item in objects if _is_sound(item, known)]
    seen = [item for item in objects if not _is_sound(item, known)]

    # 一番早いオブジェクトが ``at_frame`` に来るように、まとめてずらす
    # エイリアスは元のタイムライン上の位置を持ったままなので、そのまま置くと
    # 指定した場所ではなく元あった場所へ行く
    origin = min(item.clip.timeline_start for item in objects)

    def timed(item: MappedObject) -> Clip:
        duration = item.clip.duration if item.has_span else default_duration
        return replace(
            item.clip,
            timeline_start=item.clip.timeline_start - origin + max(0, at_frame),
            duration=max(1, duration),
        )

    commands: list[Command] = []
    tracks = (
        {}
        if track_id is not None or not seen
        else _tracks_for(project, {item.layer for item in seen}, commands)
    )
    sound_tracks = _sound_tracks_for(project, [(item, timed(item)) for item in heard], commands)

    for item in objects:
        placed = timed(item)
        linked = _media_of(item, known)
        if linked is not None:
            placed = replace(placed, media_id=linked.id)
        if item.children:
            placed = replace(
                placed,
                scene_id=_scene_for(item, project, commands, known),
                # シーンの中の時刻は秒で持つ（素材のクリップと同じ決まり）
                source_in=item.scene_offset * project.rate.frame_duration,
            )
        if _is_sound(item, known):
            target = sound_tracks[id(item)].id
        else:
            target = track_id if track_id is not None else tracks[item.layer].id
        commands.append(AddClip(target, placed))
    return commands


def _media_of(item: MappedObject, known: Mapping[str, MediaItem]) -> MediaItem | None:
    """このクリップに結ぶ素材 中身を描くもの（音声波形など）には結ばない"""
    if not item.media_path or item.clip.source is not None:
        return None
    return known.get(item.media_path)


def _is_sound(item: MappedObject, known: Mapping[str, MediaItem]) -> bool:
    """音声トラックへ置くものか

    素材が見つかっていれば、映像を持つかどうかで決める（映像も持つ動画は映像トラック）
    見つからなければ種類の名前で決める 素材の無い音声を映像トラックへ置くと、
    あとで素材を足しても映像トラックでは鳴らない
    """
    # 音も持つ動画は映像トラックへ置くだけで、音声のクリップは作っていない
    # （こちらの素材の読み込み :func:`insert_media` は映像と音声へ分けてリンクする）
    # YMM4 の動画アイテムは配布物 230 本で 1 度も使われておらず、実物で確かめるまでは
    # 分ける側へ寄せない 形式の推測で書くと外れる（Issue #89）
    linked = _media_of(item, known)
    if linked is not None:
        return not (linked.has_video or linked.is_still)
    return item.kind == "音声ファイル" and item.clip.source is None


def _sound_tracks_for(
    project: Project, sounds: list[tuple[MappedObject, Clip]], commands: list[Command]
) -> dict[int, Track]:
    """音声を置く音声トラック（``id(元のオブジェクト)`` → トラック）

    元のレイヤーの低い順に、1 つずつ空いている音声トラックを上から探す
    映像と違い、レイヤー番号をそのままトラックの番号にしない YMM4 は映像と音声を
    同じレイヤーの並びに置くので、10 段目の効果音のために音声トラックを 10 本作ることになる

    使うのは、ロックもミュートもされておらず、置く範囲がほかの音（元からある音と、
    今回先に割り当てた音の両方）と重ならないトラックだけ 重なる所やロックされた
    トラックへ置くと ``AddClip`` が断り、1 回の Undo にまとめた配置が画像も素材の登録も
    含めて全部取り消される ミュートされたトラックでは置けても鳴らない
    空きが無ければ新しく作る
    """
    pool: list[tuple[Track, list[Clip]]] = [
        (track, list(track.clips))
        for track in project.timeline.audio_tracks()
        if not track.locked and not track.muted
    ]
    count = len(list(project.timeline.audio_tracks()))
    chosen: dict[int, Track] = {}
    for item, clip in sorted(sounds, key=lambda pair: (pair[0].layer, pair[1].timeline_start)):
        start, end = clip.timeline_start, clip.timeline_end
        slot = next(
            (
                (track, used)
                for track, used in pool
                if not any(other.overlaps(start, end) for other in used)
            ),
            None,
        )
        if slot is None:
            count += 1
            slot = (Track(kind=TrackKind.AUDIO, name=f"A{count}"), [])
            commands.append(AddTrack(slot[0]))
            pool.append(slot)
        slot[1].append(clip)
        chosen[id(item)] = slot[0]
    return chosen


def _scene_for(
    item: MappedObject,
    project: Project,
    commands: list[Command],
    media: Mapping[str, MediaItem],
) -> SceneId:
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
        for command in place(list(item.children), inside, at_frame=earliest, media=media)
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
