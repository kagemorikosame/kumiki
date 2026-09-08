"""オブジェクト設定パネル。

選んだクリップの中身とエフェクトを、パラメータ定義から自動で組み立てて見せる。
エフェクトを増やしてもここに手を入れる必要は無い。

自分ではプロジェクトを書き換えない。操作はすべてコマンドとして外へ出す。
"""

from __future__ import annotations

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

from novaedit.core.commands import (
    AddEffect,
    ClearKeyframes,
    Command,
    MoveEffect,
    ParamPath,
    RemoveEffect,
    RemoveKeyframe,
    SetClipProperty,
    SetEffectEnabled,
    SetKeyframe,
    SetParam,
)
from novaedit.core.io import Preset, PresetStore
from novaedit.core.model import AnimatedValue, Clip, ClipId, Effect, ParamValue, Project
from novaedit.effects import ParameterSpec, TrackSpec, registry
from novaedit.effects.sources import source_registry
from novaedit.engine.gpu import BlendMode
from novaedit.ui.inspector.widgets import ParameterEditor, TrackEditor, create_editor
from novaedit.ui.theme import Colors

__all__ = ["InspectorPanel"]

#: 合成方法の表示名。
BLEND_LABELS = {
    BlendMode.NORMAL: "通常",
    BlendMode.ADD: "加算",
    BlendMode.MULTIPLY: "乗算",
    BlendMode.SCREEN: "スクリーン",
}


class InspectorPanel(QWidget):
    """選択中のクリップの設定。"""

    #: 編集操作。引数はコマンドの一覧と、履歴に出す操作名。
    commands_requested = Signal(list, str)
    #: ドラッグ中の途中経過。履歴に残さずプレビューだけ更新する。
    preview_requested = Signal(object)
    #: グラフエディタで開くパラメータが選ばれた。
    curve_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project: Project | None = None
        self._clip_id: ClipId | None = None
        self._presets = PresetStore()
        self._frame = 0
        #: パラメータごとの入力欄。プロジェクトが変わったときに値を入れ直す。
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
        if clip_id == self._clip_id:
            return
        self._clip_id = clip_id
        self._rebuild()

    def set_frame(self, frame: int) -> None:
        """再生位置。キーフレームの打点とアニメーション中の表示値に使う。"""
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
        """中身を作り直す。

        値だけ入れ直せば済む場合も多いが、エフェクトの増減や並べ替えを
        差分で追うと取りこぼしが出る。組み直す方が確実で、選択中の 1 クリップ
        ぶんなら十分に速い。
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

    def _build_clip_section(self, clip: Clip) -> QWidget:
        section = _Section("クリップ")

        blend = QComboBox(section)
        for mode in BlendMode.ALL:
            blend.addItem(BLEND_LABELS.get(mode, mode), mode)
        blend.setCurrentIndex(max(0, blend.findData(clip.blend_mode)))
        blend.currentIndexChanged.connect(
            lambda index: self._emit(
                SetClipProperty(clip.id, "blend_mode", str(blend.itemData(index)))
            )
        )
        section.add_row("合成方法", blend)

        opacity_spec = TrackSpec("opacity", "不透明度", 0, 1, 1, step=0.01)
        editor = self._make_editor(
            opacity_spec, ParamPath.of_clip(clip.id, "opacity"), clip.opacity
        )
        section.add_row(
            "不透明度",
            editor,
            self._keyframe_button(ParamPath.of_clip(clip.id, "opacity"), clip.opacity),
        )
        return section

    def _build_source_section(self, clip: Clip) -> QWidget | None:
        assert clip.source is not None
        definition = source_registry.get(clip.source.kind)
        if definition is None:
            return None

        section = _Section(definition.label)
        for spec in definition.parameters:
            path = ParamPath.of_source(clip.id, spec.name)
            value = clip.source.params.get(spec.name)
            section.add_row(
                spec.label, self._make_editor(spec, path, value), self._keyframe_button(path, value)
            )
        return section

    def _build_effect_section(self, clip: Clip, effect: Effect, index: int) -> QWidget:
        definition = registry.get(effect.kind)
        label = definition.label if definition is not None else f"{effect.kind}（未知）"
        section = _Section(
            label, effect=effect, clip_id=clip.id, index=index, count=len(clip.effects)
        )
        section.action_requested.connect(self._emit)

        if definition is None:
            # 定義の無いエフェクトは触らせない。値の意味が分からないまま
            # 書き換えると、対応する版で開いたときに壊れて見える。
            section.add_note("このエフェクトの定義が見つかりません。設定は保持されます。")
            return section

        for spec in definition.parameters:
            path = ParamPath.of_effect(clip.id, effect.id, spec.name)
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
        """キーフレームの打点ボタン。数値パラメータにだけ付く。"""
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
            # アニメーション中の値を触ったら、その位置のキーフレームを動かす。
            # 静的値で上書きすると、打ったキーフレームが黙って消える。
            self._emit(SetKeyframe(path, self._frame, value.static))
            return
        self._emit(SetParam(path, value))

    def _on_value_previewed(self, path: ParamPath, value: ParamValue) -> None:
        self.preview_requested.emit(SetParam(path, value))

    def _current_value(self, path: ParamPath) -> ParamValue | None:
        from novaedit.core.commands import resolve_param

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
        submenus: dict[str, QMenu] = {}
        for definition in registry.all():
            submenu = submenus.get(definition.category)
            if submenu is None:
                submenu = menu.addMenu(definition.category)
                submenus[definition.category] = submenu
            action = submenu.addAction(definition.label)
            action.setData(definition.kind)

        chosen = menu.exec(self._add_button.mapToGlobal(self._add_button.rect().bottomLeft()))
        if chosen is None:
            return
        definition = registry.require(str(chosen.data()))
        self._emit(AddEffect(clip.id, definition.create()), f"{definition.label}を追加")

    def _show_preset_menu(self) -> None:
        """プリセットの保存と適用。

        保存するのはエフェクトの列ごと。見た目のほとんどは複数のエフェクトの
        組み合わせでできているので、1 つずつ保存しても使い物にならない。
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
        self.commands_requested.emit([command], label or command.label)

    def _refresh_animated(self) -> None:
        """キーフレームで決まる値を、今のフレームの値に更新する。"""
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
        effect = next((e for e in clip.effects if e.id == owner), None)
        return effect.params.get(name) if effect is not None else None


class _Section(QFrame):
    """1 つの見出しと、その下のパラメータ行。"""

    action_requested = Signal(object)

    def __init__(
        self,
        title: str,
        *,
        effect: Effect | None = None,
        clip_id: ClipId | None = None,
        index: int = 0,
        count: int = 0,
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
                SetEffectEnabled(clip_id, effect.id, bool(state))
            )
        )
        return button

    def _move(
        self, effect: Effect, clip_id: ClipId, index: int, text: str, enabled: bool
    ) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip("順番を変える。掛ける順で結果が変わる")
        button.setAutoRaise(True)
        button.setEnabled(enabled)
        button.clicked.connect(
            lambda: self.action_requested.emit(MoveEffect(clip_id, effect.id, index))
        )
        return button

    def _remove(self, effect: Effect, clip_id: ClipId) -> QToolButton:
        button = QToolButton()
        button.setText("✕")
        button.setToolTip("このエフェクトを外す")
        button.setAutoRaise(True)
        button.clicked.connect(lambda: self.action_requested.emit(RemoveEffect(clip_id, effect.id)))
        return button
