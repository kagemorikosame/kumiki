"""オブジェクト設定パネル

選んだクリップの中身とエフェクトを、パラメータ定義から自動で組み立てて見せる
エフェクトを増やしてもここに手を入れる必要は無い

自分ではプロジェクトを書き換えない 操作はすべてコマンドとして外へ出す
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sashimono.core.commands import (
    AddEffect,
    ClearKeyframes,
    Command,
    MoveEffect,
    ParamPath,
    ParamTarget,
    RemoveEffect,
    RemoveKeyframe,
    SetClipProperty,
    SetEffectEnabled,
    SetKeyframe,
    SetParam,
)
from sashimono.core.io import Preset, PresetStore
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    ClipId,
    Effect,
    EffectId,
    ParamValue,
    Project,
)
from sashimono.effects import ParameterSpec, TrackSpec, registry
from sashimono.effects.blending import BLEND_MODES
from sashimono.effects.sources import source_registry
from sashimono.engine.gpu import BlendMode
from sashimono.ui.inspector.widgets import ParameterEditor, TrackEditor, create_editor
from sashimono.ui.theme import Colors

__all__ = ["InspectorPanel"]

#: 合成方法の表示名
BLEND_LABELS = {
    BlendMode.NORMAL: "通常",
    BlendMode.ADD: "加算",
    BlendMode.MULTIPLY: "乗算",
    BlendMode.SCREEN: "スクリーン",
    BlendMode.SUBTRACT: "減算",
    BlendMode.OVERLAY: "オーバーレイ",
    BlendMode.LIGHTEN: "比較(明)",
    BlendMode.DARKEN: "比較(暗)",
    **{mode: label for mode, label in BLEND_MODES if mode in BlendMode.EXTENDED},
}


def _same_effect(
    primary: Clip, other: Clip, effect_id: EffectId, *, after: bool = False
) -> Effect | None:
    """主のクリップのエフェクトに当たる、相手側のエフェクト 同じ種類の同じ順番で探す

    場面切り替えは前の場面と後の場面で別の列を持つので、同じ列の中で探す
    """
    mine = primary.after_effects if after else primary.effects
    theirs = other.after_effects if after else other.effects
    found = next((e for e in mine if e.id == effect_id), None)
    if found is None:
        return None
    # 同じ種類が何個目かを数える 種類の一覧から探すと、2 個目以降でも 0 番目が出る
    index = sum(1 for e in mine[: mine.index(found)] if e.kind == found.kind)
    same = [e for e in theirs if e.kind == found.kind]
    return same[index] if index < len(same) else None


def _moved_path(path: ParamPath, primary: Clip, other: Clip) -> ParamPath | None:
    if path.target is ParamTarget.CLIP:
        return replace(path, clip_id=other.id)
    if path.target is ParamTarget.SOURCE:
        if primary.source is None or other.source is None:
            return None
        if primary.source.kind != other.source.kind:
            return None
        if path.name not in other.source.params:
            return None
        return replace(path, clip_id=other.id)
    if path.effect_id is None:
        return None
    twin = _same_effect(primary, other, path.effect_id, after=path.after)
    if twin is None or path.name not in twin.params:
        return None
    return replace(path, clip_id=other.id, effect_id=twin.id)


def _for_clip(command: Command, primary: Clip, other: Clip) -> Command | None:
    """主のクリップ向けのコマンドを、ほかのクリップ向けに作り直す 当てられなければ ``None``"""
    if isinstance(command, SetClipProperty):
        return replace(command, clip_id=other.id)
    if isinstance(command, SetParam | SetKeyframe | RemoveKeyframe | ClearKeyframes):
        path = _moved_path(command.path, primary, other)
        return None if path is None else replace(command, path=path)
    # エフェクトの追加や並べ替えは、主のクリップだけに当てる（増やすと元へ戻しにくい）
    return None


class InspectorPanel(QWidget):
    """選択中のクリップの設定"""

    #: 編集操作 引数はコマンドの一覧と、履歴に出す操作名
    commands_requested = Signal(list, str)
    #: ドラッグ中の途中経過 履歴に残さずプレビューだけ更新する
    preview_requested = Signal(object)
    #: グラフエディタで開くパラメータが選ばれた
    curve_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project: Project | None = None
        self._clip_id: ClipId | None = None
        self._selection: tuple[ClipId, ...] = ()
        self._presets = PresetStore()
        self._frame = 0
        #: パラメータごとの入力欄 プロジェクトが変わったときに値を入れ直す
        self._editors: dict[tuple[str, str], ParameterEditor] = {}

        self._title = QLabel("クリップを選んでください", self)
        self._title.setStyleSheet(f"color: {Colors.TEXT_MUTED.name()}; padding: 6px 8px;")

        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(8, 4, 8, 8)
        self._body_layout.setSpacing(10)
        self._body_layout.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidget(self._body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._add_button = QPushButton("エフェクトを追加…", self)
        self._add_button.clicked.connect(self._show_effect_menu)
        self._add_button.setEnabled(False)

        self._preset_button = QPushButton("プリセット…", self)
        self._preset_button.clicked.connect(self._show_preset_menu)
        self._preset_button.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(0)
        buttons.addWidget(self._add_button, 1)
        buttons.addWidget(self._preset_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._title)
        layout.addWidget(scroll, 1)
        layout.addLayout(buttons)

    # --- 外から差し替えるもの ---

    def set_project(self, project: Project) -> None:
        self._project = project
        self._rebuild()

    def set_clip(self, clip_id: ClipId | None) -> None:
        self.set_selection((clip_id,) if clip_id is not None else ())

    def set_selection(self, clip_ids: tuple[ClipId, ...]) -> None:
        """選んでいるクリップ 先頭が主のクリップで、設定パネルはそれを出す

        何本も選んでいれば、触った設定を選んだ全部へ当てる（同じ設定を持つものだけ）
        """
        primary = clip_ids[0] if clip_ids else None
        if primary == self._clip_id and tuple(clip_ids) == self._selection:
            return
        self._selection = tuple(clip_ids)
        self._clip_id = primary
        self._rebuild()

    def set_frame(self, frame: int) -> None:
        """再生位置 キーフレームの打点とアニメーション中の表示値に使う"""
        if frame == self._frame:
            return
        self._frame = frame
        self._refresh_animated()

    # --- 組み立て ---

    def _clip(self) -> Clip | None:
        if self._project is None or self._clip_id is None:
            return None
        located = self._project.timeline.locate_clip(self._clip_id)
        return located[1] if located is not None else None

    def _rebuild(self) -> None:
        """中身を作り直す

        値だけ入れ直せば済む場合も多いが、エフェクトの増減や並べ替えを
        差分で追うと取りこぼしが出る 組み直す方が確実で、選択中の 1 クリップ
        ぶんなら十分に速い
        """
        self._editors.clear()
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

        clip = self._clip()
        if clip is None:
            self._title.setText("クリップを選んでください")
            self._add_button.setEnabled(False)
            self._preset_button.setEnabled(False)
            self._body_layout.addStretch(1)
            return

        self._add_button.setEnabled(True)
        self._preset_button.setEnabled(True)
        self._title.setText(self._describe(clip))

        self._body_layout.addWidget(self._build_clip_section(clip))
        if clip.source is not None:
            section = self._build_source_section(clip)
            if section is not None:
                self._body_layout.addWidget(section)

        for index, effect in enumerate(clip.effects):
            self._body_layout.addWidget(self._build_effect_section(clip, effect, index))
        # 場面切り替えは、前の場面（上のエフェクト）と後の場面で別に積む
        if clip.source is not None and clip.source.kind == "transition":
            for index, effect in enumerate(clip.after_effects):
                self._body_layout.addWidget(
                    self._build_effect_section(clip, effect, index, after=True)
                )

        self._body_layout.addStretch(1)
        self._refresh_animated()

    def _describe(self, clip: Clip) -> str:
        if clip.source is not None:
            definition = source_registry.get(clip.source.kind)
            return definition.label if definition is not None else clip.source.kind
        if self._project is not None and clip.media_id is not None:
            media = self._project.find_media(clip.media_id)
            if media is not None:
                return media.name
        return "クリップ"

    def _blend_editor(self, parent: QWidget, clip: Clip) -> QComboBox:
        blend = QComboBox(parent)
        for mode in BlendMode.ALL:
            blend.addItem(BLEND_LABELS.get(mode, mode), mode)
        blend.setCurrentIndex(max(0, blend.findData(clip.blend_mode)))
        blend.currentIndexChanged.connect(
            lambda index: self._emit(
                SetClipProperty(clip.id, "blend_mode", str(blend.itemData(index)))
            )
        )
        return blend

    def _build_clip_section(self, clip: Clip) -> QWidget:
        section = _Section("クリップ")

        # フィルタは下の絵を置き換えるだけで、合成方法を使わない 出しておくと、
        # 選んでも何も変わらない欄を触らせることになる
        if not clip.is_filter:
            section.add_row("合成方法", self._blend_editor(section, clip))

        opacity_spec = TrackSpec("opacity", "不透明度", 0, 1, 1, step=0.01)
        editor = self._make_editor(
            opacity_spec, ParamPath.of_clip(clip.id, "opacity"), clip.opacity
        )
        section.add_row(
            "不透明度",
            editor,
            self._keyframe_button(ParamPath.of_clip(clip.id, "opacity"), clip.opacity),
        )
        if clip.hold_at is not None:
            # 止めた絵は読み込み（YMM4 の素材より長い動画・再生速度 0）で付く 見えないままだと、
            # 絵が動かない理由がどこにも出ず、素材の不具合と取り違える 外す道も置く
            held = QLabel(f"素材の {float(clip.hold_at):.3f} 秒の絵で止める", section)
            held.setStyleSheet("border: none;")
            release = QPushButton("解除", section)
            release.clicked.connect(lambda: self._emit(SetClipProperty(clip.id, "hold_at", None)))
            section.add_row("絵を止める", held, release)
        return section

    def _build_source_section(self, clip: Clip) -> QWidget | None:
        assert clip.source is not None
        definition = source_registry.get(clip.source.kind)
        if definition is None:
            return None

        section = _Section(definition.label)
        if clip.is_filter:
            # 設定の項目を持たないので、何もしない箱に見える 何に効くのかをここで言う
            section.add_note(
                "このトラックより下を重ねた絵に、下に積んだエフェクトを掛けます"
                " 不透明度は掛ける前と後の混ぜ具合です"
                " 部分モザイク・ぼかしと部分フィルタの範囲は、画面の中央から数えます"
                "（右と上が正）"
            )
        for spec in definition.parameters:
            path = ParamPath.of_source(clip.id, spec.name)
            value = clip.source.params.get(spec.name)
            section.add_row(
                spec.label, self._make_editor(spec, path, value), self._keyframe_button(path, value)
            )
        return section

    def _build_effect_section(
        self, clip: Clip, effect: Effect, index: int, *, after: bool = False
    ) -> QWidget:
        definition = registry.get(effect.kind)
        label = definition.label if definition is not None else f"{effect.kind}（未知）"
        if after:
            label = f"{label}（後の場面）"
        stack = clip.after_effects if after else clip.effects
        section = _Section(
            label,
            effect=effect,
            clip_id=clip.id,
            index=index,
            count=len(stack),
            after=after,
        )
        section.action_requested.connect(self._emit)

        if definition is None:
            # 定義の無いエフェクトは触らせない 値の意味が分からないまま
            # 書き換えると、対応する版で開いたときに壊れて見える
            section.add_note("このエフェクトの定義が見つかりません 設定は保持されます")
            return section

        for spec in definition.parameters:
            path = ParamPath.of_effect(clip.id, effect.id, spec.name, after=after)
            value = effect.params.get(spec.name)
            section.add_row(
                spec.label, self._make_editor(spec, path, value), self._keyframe_button(path, value)
            )
        return section

    def _make_editor(
        self, spec: ParameterSpec, path: ParamPath, value: ParamValue | None
    ) -> ParameterEditor:
        editor = create_editor(spec)
        editor.set_value(value)
        editor.value_changed.connect(lambda new: self._on_value_changed(path, new))
        editor.value_previewed.connect(lambda new: self._on_value_previewed(path, new))
        self._editors[(str(path.effect_id or path.target.value), spec.name)] = editor
        return editor

    def _keyframe_button(self, path: ParamPath, value: ParamValue | None) -> QWidget | None:
        """キーフレームの打点ボタン 数値パラメータにだけ付く"""
        if not isinstance(value, AnimatedValue) and value is not None:
            return None

        animated = value if isinstance(value, AnimatedValue) else AnimatedValue()
        button = QToolButton()
        button.setText("◆" if animated.is_animated else "◇")
        button.setToolTip("キーフレームを打つ / 右クリックで解除")
        button.setFixedWidth(24)
        button.setAutoRaise(True)
        if animated.is_animated:
            button.setStyleSheet(f"color: {Colors.ACCENT.name()};")
        button.clicked.connect(lambda: self._toggle_keyframe(path, animated))
        button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        button.customContextMenuRequested.connect(lambda _: self._keyframe_menu(path, animated))
        return button

    # --- 操作 ---

    def _on_value_changed(self, path: ParamPath, value: ParamValue) -> None:
        current = self._current_value(path)
        animating = isinstance(current, AnimatedValue) and current.is_animated
        if animating and isinstance(value, AnimatedValue):
            # アニメーション中の値を触ったら、その位置のキーフレームを動かす
            # 静的値で上書きすると、打ったキーフレームが黙って消える
            self._emit(SetKeyframe(path, self._frame, value.static))
            return
        self._emit(SetParam(path, value))

    def _on_value_previewed(self, path: ParamPath, value: ParamValue) -> None:
        self.preview_requested.emit(SetParam(path, value))

    def _current_value(self, path: ParamPath) -> ParamValue | None:
        from sashimono.core.commands import resolve_param

        if self._project is None:
            return None
        return resolve_param(self._project, path)

    def _toggle_keyframe(self, path: ParamPath, animated: AnimatedValue) -> None:
        existing = next((k for k in animated.keyframes if k.frame == self._frame), None)
        if existing is not None:
            self._emit(RemoveKeyframe(path, self._frame))
        else:
            self._emit(SetKeyframe(path, self._frame, animated.at(self._frame)))

    def _keyframe_menu(self, path: ParamPath, animated: AnimatedValue) -> None:
        menu = QMenu(self)
        curve = menu.addAction("グラフエディタで開く")
        clear = menu.addAction("アニメーションを解除")
        clear.setEnabled(animated.is_animated)

        chosen = menu.exec(self.cursor().pos())
        if chosen is curve:
            self.curve_selected.emit(path)
        elif chosen is clear:
            self._emit(ClearKeyframes(path, self._frame))

    def _show_effect_menu(self) -> None:
        clip = self._clip()
        if clip is None:
            return

        menu = QMenu(self)
        # 場面切り替えは、前の場面と後の場面で積む先が違う
        transition = clip.source is not None and clip.source.kind == "transition"
        roots: dict[bool, QMenu] = {False: menu}
        if transition:
            roots = {False: menu.addMenu("前の場面へ"), True: menu.addMenu("後の場面へ")}
        submenus: dict[tuple[bool, str], QMenu] = {}
        for after, root in roots.items():
            for definition in registry.all():
                submenu = submenus.get((after, definition.category))
                if submenu is None:
                    submenu = root.addMenu(definition.category)
                    submenus[(after, definition.category)] = submenu
                action = submenu.addAction(definition.label)
                action.setData((definition.kind, after))

        chosen = menu.exec(self._add_button.mapToGlobal(self._add_button.rect().bottomLeft()))
        if chosen is None:
            return
        kind, after = chosen.data()
        definition = registry.require(str(kind))
        where = "（後の場面）" if after else ""
        self._emit(
            AddEffect(clip.id, definition.create(), after=bool(after)),
            f"{definition.label}を追加{where}",
        )

    def _show_preset_menu(self) -> None:
        """プリセットの保存と適用

        保存するのはエフェクトの列ごと 見た目のほとんどは複数のエフェクトの
        組み合わせでできているので、1 つずつ保存しても使い物にならない
        """
        clip = self._clip()
        if clip is None:
            return

        menu = QMenu(self)
        save = menu.addAction("この構成を保存…")
        save.setEnabled(bool(clip.effects))
        menu.addSeparator()

        presets = self._presets.all()
        if not presets:
            placeholder = menu.addAction("（保存されたプリセットはありません）")
            placeholder.setEnabled(False)
        else:
            submenus: dict[str, QMenu] = {}
            for preset in presets:
                submenu = submenus.get(preset.category)
                if submenu is None:
                    submenu = menu.addMenu(preset.category)
                    submenus[preset.category] = submenu
                action = submenu.addAction(preset.name)
                action.setData(preset.name)

        chosen = menu.exec(self._preset_button.mapToGlobal(self._preset_button.rect().bottomLeft()))
        if chosen is None:
            return
        if chosen is save:
            self._save_preset(clip)
            return

        selected = next((p for p in presets if p.name == chosen.data()), None)
        if selected is not None:
            self.commands_requested.emit(
                [AddEffect(clip.id, effect) for effect in selected.instantiate()],
                f"プリセット: {selected.name}",
            )

    def _save_preset(self, clip: Clip) -> None:
        name, accepted = QInputDialog.getText(
            self, "プリセットを保存", "名前", text=self._describe(clip)
        )
        if not accepted or not name.strip():
            return
        self._presets.save(Preset(name=name.strip(), effects=clip.effects))

    def _emit(self, command: Command, label: str | None = None) -> None:
        commands = [command, *self._also_for_others(command)]
        text = label or command.label
        if len(commands) > 1:
            text = f"{text}（{len(commands)} 本）"
        self.commands_requested.emit(commands, text)

    def _also_for_others(self, command: Command) -> list[Command]:
        """同じ設定を、選んでいるほかのクリップにも当てるコマンド

        エフェクトのパラメータは「同じ種類の何番目か」で相手を探す 相手が持って
        いなければ飛ばす（無いものを作ると、選んだだけで中身が増える）
        """
        others = [clip_id for clip_id in self._selection if clip_id != self._clip_id]
        if not others or self._project is None:
            return []
        primary = self._clip()
        if primary is None:
            return []
        extra: list[Command] = []
        for clip_id in others:
            located = self._project.timeline.locate_clip(clip_id)
            if located is None:
                continue
            copied = _for_clip(command, primary, located[1])
            if copied is not None:
                extra.append(copied)
        return extra

    def _refresh_animated(self) -> None:
        """キーフレームで決まる値を、今のフレームの値に更新する"""
        clip = self._clip()
        if clip is None:
            return

        for (owner, name), editor in self._editors.items():
            if not isinstance(editor, TrackEditor):
                continue
            value = self._lookup(clip, owner, name)
            if isinstance(value, AnimatedValue) and value.is_animated:
                editor.set_animated_value(value.at(self._frame - clip.timeline_start))

    def _lookup(self, clip: Clip, owner: str, name: str) -> ParamValue | None:
        if owner == "clip":
            return getattr(clip, name, None)
        if owner == "source":
            return clip.source.params.get(name) if clip.source is not None else None
        # 場面切り替えは前の場面と後の場面の 2 列を持つ どちらに積んだものも拾う
        both = (*clip.effects, *clip.after_effects)
        effect = next((e for e in both if e.id == owner), None)
        return effect.params.get(name) if effect is not None else None


class _Section(QFrame):
    """1 つの見出しと、その下のパラメータ行"""

    action_requested = Signal(object)

    def __init__(
        self,
        title: str,
        *,
        effect: Effect | None = None,
        clip_id: ClipId | None = None,
        index: int = 0,
        count: int = 0,
        after: bool = False,
    ) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            f"QFrame {{ background-color: {Colors.PANEL.name()};"
            f" border: 1px solid {Colors.BORDER.name()}; border-radius: 4px; }}"
        )

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(8, 6, 8, 8)
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(6)
        self._grid.setColumnStretch(1, 1)
        self._row = 0

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        label = QLabel(title)
        label.setStyleSheet(f"color: {Colors.TEXT.name()}; font-weight: bold; border: none;")
        header.addWidget(label)
        header.addStretch(1)

        self._after = after
        if effect is not None and clip_id is not None:
            header.addWidget(self._toggle(effect, clip_id))
            header.addWidget(self._move(effect, clip_id, index - 1, "▲", index > 0))
            header.addWidget(self._move(effect, clip_id, index + 1, "▼", index < count - 1))
            header.addWidget(self._remove(effect, clip_id))

        container = QWidget(self)
        container.setStyleSheet("border: none;")
        container.setLayout(header)
        self._grid.addWidget(container, 0, 0, 1, 3)
        self._row = 1

    def add_row(self, label: str, editor: QWidget, extra: QWidget | None = None) -> None:
        text = QLabel(label)
        text.setStyleSheet(f"color: {Colors.TEXT_MUTED.name()}; border: none;")
        text.setFixedWidth(96)
        text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._grid.addWidget(text, self._row, 0)
        self._grid.addWidget(editor, self._row, 1)
        if extra is not None:
            self._grid.addWidget(extra, self._row, 2)
        self._row += 1

    def add_note(self, message: str) -> None:
        note = QLabel(message)
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {Colors.TEXT_MUTED.name()}; border: none;")
        self._grid.addWidget(note, self._row, 0, 1, 3)
        self._row += 1

    def _toggle(self, effect: Effect, clip_id: ClipId) -> QToolButton:
        button = QToolButton()
        button.setCheckable(True)
        button.setChecked(effect.enabled)
        button.setText("有効" if effect.enabled else "無効")
        button.setToolTip("掛ける前と後を見比べる")
        button.setAutoRaise(True)
        button.toggled.connect(
            lambda state: self.action_requested.emit(
                SetEffectEnabled(clip_id, effect.id, bool(state), after=self._after)
            )
        )
        return button

    def _move(
        self, effect: Effect, clip_id: ClipId, index: int, text: str, enabled: bool
    ) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip("順番を変える 掛ける順で結果が変わる")
        button.setAutoRaise(True)
        button.setEnabled(enabled)
        button.clicked.connect(
            lambda: self.action_requested.emit(
                MoveEffect(clip_id, effect.id, index, after=self._after)
            )
        )
        return button

    def _remove(self, effect: Effect, clip_id: ClipId) -> QToolButton:
        button = QToolButton()
        button.setText("✕")
        button.setToolTip("このエフェクトを外す")
        button.setAutoRaise(True)
        button.clicked.connect(
            lambda: self.action_requested.emit(RemoveEffect(clip_id, effect.id, after=self._after))
        )
        return button
