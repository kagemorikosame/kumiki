"""タイムラインの右クリックと「＋ トラック追加」から、物を足すメニュー（Issue #27）

右クリックした所（フレームとトラック）へ置く 再生ヘッドの位置へ置くと、置きたい所まで
再生ヘッドを動かしてからメニューを開き直すことになる

メニューは作るだけで、足すのはビュー（:class:`~sashimono.ui.timeline.view.TimelineView`）の
信号を通す ビューと同じく、ここでもプロジェクトを直接書き換えない

外から持ってくる一覧（AviUtl のスクリプト・テンプレートの棚・保存したエイリアス）は
:class:`AddSources` にまとめて差し替えられるようにする 試験で本人の AviUtl2 の置き場を
読みに行かないため それぞれサブメニューを開いたときに初めて読む 右クリックのたびに
何百本ものスクリプトやテンプレートを読むと、メニューが出るまで待たされる
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox, QWidget

from sashimono.compat.aviutl.catalog import KIND_LABELS, ScriptEntry, script_catalog
from sashimono.compat.catalog import (
    TemplateEntry,
    default_template_roots,
    template_catalog,
)
from sashimono.core.commands import (
    AddEffect,
    Command,
    RemoveTrack,
    insert_clip,
    insert_filter,
    insert_generated,
    insert_scene,
)
from sashimono.core.commands.insert import DEFAULT_GENERATED_FRAMES, is_effect_track, new_track
from sashimono.core.io.aliases import Alias, AliasStore, alias_refusal
from sashimono.core.io.serialize import ProjectFileError
from sashimono.core.model import (
    Clip,
    ClipId,
    GeneratedSource,
    Project,
    SceneId,
    Track,
    TrackId,
    TrackKind,
)
from sashimono.effects.definition import EffectDefinition, registry
from sashimono.effects.sources import SHAPE, TEXT, TRANSITION, source_registry

if TYPE_CHECKING:
    from sashimono.ui.timeline.view import TimelineView

__all__ = [
    "TRACK_CHOICES",
    "AddSources",
    "TimelineAddMenus",
    "effects_for",
]

#: 「＋ トラック追加」の選択肢（表示、種類、エフェクトトラックか）
TRACK_CHOICES: tuple[tuple[str, TrackKind, bool], ...] = (
    ("映像トラック", TrackKind.VIDEO, False),
    ("音声トラック", TrackKind.AUDIO, False),
    ("エフェクトトラック（フィルタ用）", TrackKind.VIDEO, True),
)

#: カスタムオブジェクト（中身を作るスクリプト）の分類 エフェクトの一覧には出さない
#: クリップに掛けると、今の絵を捨てて別の物を描くので、掛けたつもりの絵が消える
_CUSTOM_OBJECT = KIND_LABELS["obj"]


def _custom_objects() -> Sequence[ScriptEntry]:
    return script_catalog().of_kind("obj")


def _shelf_templates() -> Sequence[TemplateEntry]:
    """テンプレートの棚 まだ一度も読んでいなければ、既定の置き場を読む

    読んであれば読み直さない 棚のダイアログの〔読み直す〕と同じ一覧を見せる
    """
    catalog = template_catalog()
    if not catalog.all():
        catalog.scan(default_template_roots())
    return catalog.all()


def _ask_name(parent: QWidget, suggestion: str) -> str | None:
    name, accepted = QInputDialog.getText(
        parent, "エイリアスとして保存", "エイリアスの名前", text=suggestion
    )
    return name.strip() if accepted and name.strip() else None


def _confirm_overwrite(parent: QWidget, name: str) -> bool:
    answer = QMessageBox.question(
        parent,
        "エイリアスとして保存",
        f"エイリアス「{name}」はもうあります 上書きしますか",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


@dataclass
class AddSources:
    """〔追加〕に並べる物の出どころ 試験では置き場の無い物へ差し替える"""

    #: カスタムオブジェクトとして置けるスクリプト（``.obj`` ``.obj2``）
    custom_objects: Callable[[], Sequence[ScriptEntry]] = _custom_objects
    #: テンプレートの棚（AviUtl のエイリアス・YMM4 のアイテムテンプレート）
    templates: Callable[[], Sequence[TemplateEntry]] = _shelf_templates
    #: 自分で保存したエイリアス
    aliases: AliasStore = field(default_factory=AliasStore)
    #: エイリアスの名前を尋ねる 断られたら ``None``
    ask_name: Callable[[QWidget, str], str | None] = _ask_name
    #: 同じ名前のエイリアスを上書きしてよいか 断られたら偽
    confirm_overwrite: Callable[[QWidget, str], bool] = _confirm_overwrite


def effects_for(kind: TrackKind) -> tuple[EffectDefinition, ...]:
    """そのトラックの種類のクリップへ掛けられるエフェクト

    音のエフェクトは音声トラックのクリップだけ、映像のエフェクトは映像トラックのクリップだけ
    逆に掛けると、積めても何も起きない
    """
    audio = kind is TrackKind.AUDIO
    return tuple(
        definition
        for definition in registry.all()
        if (definition.audio_process is not None) == audio and definition.category != _CUSTOM_OBJECT
    )


class TimelineAddMenus:
    """ビューの右クリックメニューへ、足す項目を差し込む係"""

    def __init__(self, view: TimelineView) -> None:
        self._view = view

    @property
    def _project(self) -> Project:
        return self._view.project

    # --- メニュー ---

    def track_add_menu(self, parent: QWidget | None = None, *, title: str = "") -> QMenu:
        """映像・音声・エフェクトのどれを足すかを選ぶメニュー"""
        menu = QMenu(title, parent)
        for label, kind, effect in TRACK_CHOICES:
            _action(menu, label, functools.partial(self.add_track, kind, effect=effect))
        return menu

    def add_empty_items(self, menu: QMenu, frame: int, track: Track | None) -> None:
        """空いた所の右クリック 〔追加〕のサブメニューを足す"""
        track_id = track.id if track is not None else None
        if track is not None and is_effect_track(track):
            # フィルタを置くために足したトラック 一番使う物をサブメニューの外へ出す
            _action(menu, "フィルタを置く", functools.partial(self.place_filter, frame, track_id))
            menu.addSeparator()
        add = menu.addMenu("追加")
        _action(
            add,
            "テキスト",
            functools.partial(self.place_source, TEXT.create(), "テキストを追加", frame, track_id),
        )
        _action(
            add,
            "図形",
            functools.partial(self.place_source, SHAPE.create(), "図形を追加", frame, track_id),
        )
        _action(
            add,
            "場面切り替え",
            functools.partial(
                self.place_source, TRANSITION.create(), "場面切り替えを追加", frame, track_id
            ),
        )
        _action(add, "フィルタ", functools.partial(self.place_filter, frame, track_id))
        add.addSeparator()
        custom = add.addMenu("カスタムオブジェクト")
        _lazily(custom, functools.partial(self._fill_custom_objects, custom, frame, track_id))
        aliases = add.addMenu("エイリアス")
        _lazily(aliases, functools.partial(self._fill_aliases, aliases, frame, track_id))
        scenes = add.addMenu("シーン")
        self._fill_scenes(scenes, frame, track_id)

    def add_clip_items(self, menu: QMenu, track: Track, clip: Clip) -> None:
        """クリップの上の右クリック エフェクトを掛けるのと、エイリアスとして保存"""
        effects = menu.addMenu("エフェクトを追加")
        self._fill_effects(effects, track.kind)
        save = _action(menu, "エイリアスとして保存…", functools.partial(self.save_alias, clip.id))
        reason = alias_refusal(clip)
        save.setEnabled(reason is None)
        if reason is not None:
            save.setToolTip(reason)
        menu.setToolTipsVisible(True)

    def add_track_items(self, menu: QMenu, track: Track | None) -> None:
        """トラックを足す・消す トラックの外（最後のトラックの下）でも足せる"""
        menu.addSeparator()
        menu.addMenu(self.track_add_menu(menu, title="トラックを追加"))
        if track is None:
            return
        name = track.name or "トラック"
        remove = _action(
            menu, f"トラックを削除（{name}）", functools.partial(self.remove_track, track.id)
        )
        # クリップごと消すと、見えていない所のクリップまで黙って消える 空のときだけにする
        remove.setEnabled(not track.clips)

    def _fill_effects(self, menu: QMenu, kind: TrackKind) -> None:
        definitions = effects_for(kind)
        if not definitions:
            _placeholder(menu, "（掛けられるエフェクトがありません）")
            return
        submenus: dict[str, QMenu] = {}
        for definition in definitions:
            submenu = submenus.get(definition.category)
            if submenu is None:
                submenu = menu.addMenu(definition.category or "その他")
                submenus[definition.category] = submenu
            _action(submenu, definition.label, functools.partial(self.add_effect, definition.kind))

    def _fill_custom_objects(self, menu: QMenu, frame: int, track_id: TrackId | None) -> None:
        entries = self._view.add_sources.custom_objects()
        if not entries:
            _placeholder(menu, "（読み込めたカスタムオブジェクトがありません）")
            return
        for entry in entries:
            _action(
                menu,
                entry.label,
                functools.partial(self.place_custom_object, entry, frame, track_id),
            )

    def _fill_aliases(self, menu: QMenu, frame: int, track_id: TrackId | None) -> None:
        sources = self._view.add_sources
        saved = sources.aliases.all()
        mine = menu.addMenu("保存したもの")
        if saved:
            for alias in saved:
                _action(
                    mine, alias.name, functools.partial(self.place_alias, alias, frame, track_id)
                )
        else:
            _placeholder(mine, "（まだありません 右クリックの〔エイリアスとして保存…〕で作れます）")

        shelf = sources.templates()
        if not shelf:
            return
        menu.addSeparator()
        folders: dict[str, QMenu] = {}
        for entry in shelf:
            folder = folders.get(entry.folder)
            if folder is None:
                folder = menu.addMenu(entry.folder or "テンプレート")
                folders[entry.folder] = folder
            action = _action(
                folder,
                entry.label,
                functools.partial(self._view.template_requested.emit, entry, frame, track_id or ""),
            )
            if entry.error:
                # 読めない物も並べる 消すと、置いたはずのファイルが無い理由が分からない
                action.setEnabled(False)
                action.setToolTip(entry.error)
                folder.setToolTipsVisible(True)

    def _fill_scenes(self, menu: QMenu, frame: int, track_id: TrackId | None) -> None:
        # 開いているシーン自身は置けない（入れ子が自分へ戻る）
        scenes = [s for s in self._project.scenes if s.id != self._view.open_scene]
        if not scenes:
            _placeholder(menu, "（置けるシーンがありません）")
            return
        for scene in scenes:
            _action(
                menu,
                scene.name or "無題のシーン",
                functools.partial(self.place_scene, scene.id, frame, track_id),
            )

    # --- 操作 ---

    def add_track(self, kind: TrackKind, *, effect: bool = False) -> None:
        command = new_track(self._project, kind, effect=effect)
        self._view.request([command], command.label)

    def remove_track(self, track_id: TrackId) -> None:
        track = self._project.timeline.find_track(track_id)
        if track is None:
            return
        if track.clips:
            self._view.status_message.emit(
                "クリップのあるトラックは消せません（先に空にしてください）"
            )
            return
        command = RemoveTrack(track_id)
        self._view.request([command], f"トラックを削除: {track.name or track.kind.value}")

    def place_source(
        self, source: GeneratedSource, label: str, frame: int, track_id: TrackId | None
    ) -> None:
        self._view.place(
            insert_generated(self._project, source, at_frame=frame, track_id=track_id), label
        )

    def place_filter(self, frame: int, track_id: TrackId | None) -> None:
        """フィルタを置く エフェクトは積まない（何を掛けたいかは設定パネルで選ぶ）"""
        self._view.place(
            insert_filter(self._project, at_frame=frame, track_id=track_id), "フィルタを追加"
        )

    def place_custom_object(self, entry: ScriptEntry, frame: int, track_id: TrackId | None) -> None:
        """カスタムオブジェクトを置く 空のテキストにスクリプトを 1 つ積んだクリップ

        AviUtl のカスタムオブジェクトは、何も持たないオブジェクトから始めて、
        スクリプトが ``obj.load`` で中身を作る こちらのスクリプトはクリップの絵を
        受け取って描き直すので、何も描かない生成オブジェクト（空のテキスト）を土台にする
        専用の種類を作ると保存の形が変わり、古い版で開けなくなる
        """
        definition = registry.get(entry.identifier) or entry.definition()
        clip = Clip(
            timeline_start=0,
            duration=DEFAULT_GENERATED_FRAMES,
            source=TEXT.create(text=""),
            effects=(definition.create(),),
        )
        self._view.place(
            insert_clip(self._project, clip, at_frame=frame, track_id=track_id),
            f"{entry.label}を追加",
        )

    def place_alias(self, alias: Alias, frame: int, track_id: TrackId | None) -> None:
        self._view.place(
            insert_clip(self._project, alias.instantiate(), at_frame=frame, track_id=track_id),
            f"エイリアスを置く: {alias.name}",
        )

    def place_scene(self, scene_id: SceneId, frame: int, track_id: TrackId | None) -> None:
        scene = self._project.find_scene(scene_id)
        if scene is None:
            return
        self._view.place(
            insert_scene(self._project, scene_id, at_frame=frame, track_id=track_id),
            f"シーンを置く: {scene.name}",
        )

    def add_effect(self, kind: str) -> None:
        """選んでいるクリップへエフェクトを掛ける 何本選んでいても取り消しは 1 回

        右クリックしたクリップと同じ種類（映像か音声か）のクリップにだけ掛ける
        映像と音声を一緒に選んでいるときに、音声のクリップへ映像のエフェクトを積んでも
        何も起きず、設定パネルに効かない項目が増えるだけになる
        """
        definition = registry.get(kind)
        if definition is None:
            return
        audio = definition.audio_process is not None
        timeline = self._project.timeline
        targets: list[ClipId] = []
        for clip_id in self._view.selected_clips:
            located = timeline.locate_clip(clip_id)
            if located is not None and (located[0].kind is TrackKind.AUDIO) == audio:
                targets.append(clip_id)
        commands: list[Command] = [AddEffect(c, definition.create()) for c in targets]
        label = f"{definition.label}を追加"
        if len(commands) > 1:
            label = f"{label}（{len(commands)} 本）"
        self._view.request(commands, label)

    def save_alias(self, clip_id: ClipId) -> bool:
        """クリップの中身を名前を付けて保存する 置いた位置は持たない"""
        located = self._project.timeline.locate_clip(clip_id)
        if located is None:
            return False
        clip = located[1]
        reason = alias_refusal(clip)
        if reason is not None:
            self._view.status_message.emit(reason)
            return False
        sources = self._view.add_sources
        name = sources.ask_name(self._view, _suggested_name(clip))
        if name is None:
            return False
        # 同じ名前があると黙って差し替わり、前に保存した物が消える 既定の名前は
        # テキストの頭から作るので、同じ文言の別の見た目を続けて保存すると重なりやすい
        if sources.aliases.exists(name) and not sources.confirm_overwrite(self._view, name):
            return False
        try:
            sources.aliases.save(Alias.of(name, clip))
        except (OSError, ValueError, ProjectFileError) as exc:
            self._view.status_message.emit(f"エイリアスを保存できなかった: {exc}")
            return False
        self._view.status_message.emit(f"エイリアス「{name}」を保存した")
        return True


def _suggested_name(clip: Clip) -> str:
    """名前の既定 テキストなら中身の頭 並んだときに見分けが付く"""
    source = clip.source
    if source is None:
        return "エイリアス"
    definition = source_registry.get(source.kind)
    label = definition.label if definition is not None else source.kind
    text = source.params.get("text")
    if isinstance(text, str) and text.strip():
        return f"{label} {text.strip().splitlines()[0][:16]}"
    return label


def _lazily(menu: QMenu, fill: Callable[[], None]) -> None:
    """開いたときに一度だけ中身を作る 開くまで置き場を読まない"""

    def populate() -> None:
        if menu.isEmpty():
            fill()

    menu.aboutToShow.connect(populate)


def _placeholder(menu: QMenu, text: str) -> None:
    menu.addAction(text).setEnabled(False)


def _action(menu: QMenu, text: str, slot: Callable[[], object]) -> QAction:
    """メニューに項目を足す ``triggered`` の引数（押されたかどうか）は捨てる"""
    action = menu.addAction(text)
    action.triggered.connect(lambda _checked=False: slot())
    return action
