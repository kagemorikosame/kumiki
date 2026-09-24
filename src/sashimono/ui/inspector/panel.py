"""オブジェクト設定パネル

選んだクリップの中身とエフェクトを、パラメータ定義から自動で組み立てて見せる
エフェクトを増やしてもここに手を入れる必要は無い

自分ではプロジェクトを書き換えない 操作はすべてコマンドとして外へ出す
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from fractions import Fraction

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPainter, QPainterPath, QPen, QPixmap
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
from sashimono.core.commands.fixed import (
    FADE_EFFECT_KIND,
    FLIP_EFFECT_KIND,
    TRANSFORM_EFFECT_KIND,
    VOLUME_EFFECT_KIND,
    fixed_effect,
    takes_picture_items,
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
    Track,
    draws_picture,
    plays_sound,
)
from sashimono.effects import CheckSpec, ParameterSpec, TrackSpec, registry
from sashimono.effects.blending import BLEND_MODES
from sashimono.effects.sources import source_registry
from sashimono.engine.gpu import BlendMode
from sashimono.ui.inspector.header import ClipHeader, identify_clip
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
    if found.fixed:
        # 描画・音声の欄どうしで当てる 何個目かで数えると、相手が同じ種類のふつうの
        # エフェクトを欄より前に持つとき、欄ではなくそちらの値が変わる
        return next((e for e in theirs if e.fixed and e.kind == found.kind), None)
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
        #: 前の版のファイルで、クリップがまだ持っていない描画・音声の欄 既定の値で見せ、
        #: 触ったときに :class:`AddEffect` で足してから値を入れる（1 回の取り消しで戻る）
        #: 開いただけで足すと、見ただけのクリップまで変更が入り、保存を促される
        self._virtual: dict[EffectId, tuple[ClipId, Effect]] = {}

        #: 何のクリップの設定を見ているか（種類・名前・トラック）
        self._title = ClipHeader(self)

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
        located = self._located()
        return located[1] if located is not None else None

    def _located(self) -> tuple[Track, Clip] | None:
        if self._project is None or self._clip_id is None:
            return None
        return self._project.timeline.locate_clip(self._clip_id)

    def _rebuild(self) -> None:
        """中身を作り直す

        値だけ入れ直せば済む場合も多いが、エフェクトの増減や並べ替えを
        差分で追うと取りこぼしが出る 組み直す方が確実で、選択中の 1 クリップ
        ぶんなら十分に速い
        """
        self._editors.clear()
        self._virtual.clear()
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

        located = self._located()
        if located is None:
            self._title.show_identity(None)
            self._add_button.setEnabled(False)
            self._preset_button.setEnabled(False)
            self._body_layout.addStretch(1)
            return
        track, clip = located

        self._add_button.setEnabled(True)
        self._preset_button.setEnabled(True)
        self._show_identity(clip)

        # YMM4 のアイテムの並び 描画 → 中身 → 動画・音声 → 足したエフェクト
        # 絵を描かないクリップ（音声トラック）には描画の組を出さない（出すと、動かしても
        # 何も変わらない合成方法や不透明度が並ぶ） 音を鳴らさないクリップ（映像トラック）には
        # 音量を出さない（リンクした音は音声トラックのクリップの側にある）
        # 混合トラックはクリップが絵と音の両方を持てるので、トラックの種類ではなく
        # draws_picture と plays_sound で決める
        media = (
            self._project.find_media(clip.media_id)
            if self._project is not None and clip.media_id is not None
            else None
        )
        picture = draws_picture(track, clip, media)
        sound = (
            clip.media_id is not None and clip.source is None and plays_sound(track, clip, media)
        )
        shown: set[EffectId] = set()
        if picture:
            self._body_layout.addWidget(self._build_picture_group(clip, shown))
        if clip.source is not None:
            section = self._build_source_section(clip)
            if section is not None:
                self._body_layout.addWidget(section)
        if picture and self._is_movie(clip):
            self._body_layout.addWidget(self._build_movie_group(clip, shown, sound=sound))
        elif sound:
            self._body_layout.addWidget(self._build_sound_group(clip, shown))

        heading = "映像エフェクト" if picture else "音声エフェクト"
        if picture and sound:
            heading = "映像・音声エフェクト"
        self._body_layout.addWidget(_heading(heading))
        for index, effect in enumerate(clip.effects):
            if effect.id in shown:
                continue
            self._body_layout.addWidget(self._build_effect_section(clip, effect, index))
        # 場面切り替えは、前の場面（上のエフェクト）と後の場面で別に積む
        if clip.source is not None and clip.source.kind == "transition":
            for index, effect in enumerate(clip.after_effects):
                self._body_layout.addWidget(
                    self._build_effect_section(clip, effect, index, after=True)
                )

        self._body_layout.addStretch(1)
        self._refresh_animated()

    def _show_identity(self, clip: Clip) -> None:
        identity = identify_clip(self._project, clip.id) if self._project is not None else None
        others = sum(1 for clip_id in self._selection if clip_id != clip.id)
        self._title.show_identity(identity, others=others)

    @property
    def header(self) -> ClipHeader:
        """上の見出し（何のクリップの設定か）"""
        return self._title

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

    # --- 最初から持つ欄（YMM4 の描画・動画・音声の組） ---

    def _fixed_of(self, clip: Clip, kind: str) -> Effect:
        """クリップが持つ ``kind`` の欄 前の版のファイルで持っていなければ、既定の値の仮の物

        仮の物は触ったときに初めてクリップへ足す（:meth:`_send`）
        """
        found = next((e for e in clip.effects if e.fixed and e.kind == kind), None)
        if found is not None:
            return found
        virtual = fixed_effect(kind)
        self._virtual[virtual.id] = (clip.id, virtual)
        return virtual

    def _effect_row(
        self, section: _Section, clip: Clip, effect: Effect, name: str, label: str
    ) -> None:
        """欄のエフェクトの項目を 1 行 表示名は YMM4 の欄の名前にする"""
        definition = registry.get(effect.kind)
        spec = definition.spec(name) if definition is not None else None
        if spec is None:  # pragma: no cover - 固定の項目の定義は必ずある
            return
        path = ParamPath.of_effect(clip.id, effect.id, name)
        value = effect.params.get(name)
        section.add_row(
            label, self._make_editor(spec, path, value), self._keyframe_button(path, value)
        )

    def _fixed_header(self, section: _Section, clip: Clip, effects: Sequence[Effect]) -> None:
        """組の見出しに、欄をまとめて切る切り替えと鍵の印を出す

        欄は外せないが無効にはできる（P1 の決まり） 1 つずつの切り替えを並べると、
        YMM4 の組の中に無い項目が増えて並びが崩れる
        """
        enabled = all(effect.enabled for effect in effects)
        toggle = QToolButton()
        toggle.setObjectName("fixed_toggle")
        toggle.setCheckable(True)
        toggle.setChecked(enabled)
        toggle.setText("有効" if enabled else "無効")
        toggle.setToolTip("この組の欄を掛けるかどうか 無効にすると既定の置き方・鳴り方に戻る")
        toggle.setAutoRaise(True)
        toggle.toggled.connect(
            lambda state: self._send(
                [SetEffectEnabled(clip.id, effect.id, bool(state)) for effect in effects],
                "欄を有効化" if state else "欄を無効化",
            )
        )
        section.add_header_widget(toggle)
        section.add_header_widget(_lock_label())

    def _build_picture_group(self, clip: Clip, shown: set[EffectId]) -> QWidget:
        """描画の組 YMM4 の並び（X・Y・不透明度・拡大率・回転角・合成モード・左右反転・
        クリッピング）のうち、今あるものだけを出す"""
        section = _Section("描画")
        placed = takes_picture_items(clip)
        transform = flip = None
        if placed:
            flip = self._fixed_of(clip, FLIP_EFFECT_KIND)
            transform = self._fixed_of(clip, TRANSFORM_EFFECT_KIND)
            shown.update((flip.id, transform.id))
            self._fixed_header(section, clip, (flip, transform))
            self._effect_row(section, clip, transform, "pos_x", "X")
            self._effect_row(section, clip, transform, "pos_y", "Y")

        opacity_spec = TrackSpec("opacity", "不透明度", 0, 1, 1, step=0.01)
        opacity_path = ParamPath.of_clip(clip.id, "opacity")
        section.add_row(
            "不透明度",
            self._make_editor(opacity_spec, opacity_path, clip.opacity),
            self._keyframe_button(opacity_path, clip.opacity),
        )
        if transform is not None:
            self._effect_row(section, clip, transform, "scale", "拡大率")
            self._effect_row(section, clip, transform, "rotation", "回転角")
        # フィルタは下の絵を置き換えるだけで、合成方法も切り抜きも使わない 出しておくと、
        # 選んでも何も変わらない欄を触らせることになる
        if not clip.is_filter:
            section.add_row("合成モード", self._blend_editor(section, clip))
        if flip is not None:
            self._effect_row(section, clip, flip, "horizontal", "左右反転")
        if not clip.is_filter:
            self._clip_check(section, clip, "clip_to_below", "クリッピング", clip.clip_to_below)
        if self._native_capable(clip):
            # 前の版で置いた物は画面に収めて描いている 見た目を変えずに開くため、勝手には
            # 切り替えない 本人が素材の画素の大きさ（YMM4 の拡大率 100%）へ揃えたいときの道
            # 表示名は短くする 見出しの列は 96 画素で、長いと頭が切れる
            self._clip_check(
                section,
                clip,
                "native_size",
                "画素で置く",
                clip.native_size,
                tooltip="拡大率 100% を素材の画素の大きさにします 外すと画面に収めます",
            )
        return section

    def _clip_check(
        self,
        section: _Section,
        clip: Clip,
        name: str,
        label: str,
        value: bool,
        *,
        tooltip: str = "",
    ) -> None:
        editor = create_editor(CheckSpec(name, label, False))
        editor.setObjectName(f"clip_{name}")
        editor.setToolTip(tooltip)
        editor.set_value(value)
        editor.value_changed.connect(
            lambda state: self._emit(SetClipProperty(clip.id, name, bool(state)))
        )
        section.add_row(label, editor)

    def _native_capable(self, clip: Clip) -> bool:
        """素材の画素の大きさで置けるクリップか（素材の絵を描くもの）"""
        if clip.media_id is None or clip.source is not None or self._project is None:
            return False
        media = self._project.find_media(clip.media_id)
        return media is not None and media.has_video

    def _is_movie(self, clip: Clip) -> bool:
        """動画の組を出すクリップか 静止画には再生の速さも位置も無い"""
        if clip.media_id is None or clip.source is not None or self._project is None:
            return False
        media = self._project.find_media(clip.media_id)
        return media is not None and media.has_video and not media.is_still

    def _build_movie_group(self, clip: Clip, shown: set[EffectId], *, sound: bool) -> QWidget:
        """動画の組 YMM4 の並び（音量・パン・再生速度・再生開始位置）

        音量とパンは ``sound``（混合トラックで音も鳴らすクリップ）のときだけ出す 分ける方式の
        映像のクリップは鳴らないので、出すと動かしても音が変わらない
        """
        section = _Section("動画")
        fade: Effect | None = None
        if sound:
            volume = self._fixed_of(clip, VOLUME_EFFECT_KIND)
            fade = self._fixed_of(clip, FADE_EFFECT_KIND)
            shown.update((volume.id, fade.id))
            self._fixed_header(section, clip, (volume, fade))
            self._effect_row(section, clip, volume, "volume", "音量")
            self._effect_row(section, clip, volume, "pan", "パン")
        self._playback_rows(section, clip)
        if fade is not None:
            self._effect_row(section, clip, fade, "fade_in", "フェードイン")
            self._effect_row(section, clip, fade, "fade_out", "フェードアウト")
        if clip.hold_at is not None:
            # 止めた絵は読み込み（YMM4 の素材より長い動画・再生速度 0）で付く 見えないままだと、
            # 絵が動かない理由がどこにも出ず、素材の不具合と取り違える 外す道も置く
            held = QLabel(f"素材の {float(clip.hold_at):.3f} 秒の絵で止める", section)
            held.setStyleSheet("border: none;")
            release = QPushButton("解除", section)
            release.clicked.connect(lambda: self._emit(SetClipProperty(clip.id, "hold_at", None)))
            section.add_row("絵を止める", held, release)
        return section

    def _build_sound_group(self, clip: Clip, shown: set[EffectId]) -> QWidget:
        """音声の組 YMM4 の並び（音量・パン・再生速度・再生開始位置・フェードイン・
        フェードアウト）"""
        section = _Section("音声")
        volume = self._fixed_of(clip, VOLUME_EFFECT_KIND)
        fade = self._fixed_of(clip, FADE_EFFECT_KIND)
        shown.update((volume.id, fade.id))
        self._fixed_header(section, clip, (volume, fade))
        self._effect_row(section, clip, volume, "volume", "音量")
        self._effect_row(section, clip, volume, "pan", "パン")
        if clip.media_id is not None and clip.source is None:
            self._playback_rows(section, clip)
        self._effect_row(section, clip, fade, "fade_in", "フェードイン")
        self._effect_row(section, clip, fade, "fade_out", "フェードアウト")
        return section

    def _playback_rows(self, section: _Section, clip: Clip) -> None:
        """再生速度（%）と再生開始位置（秒） どちらもクリップ自身の値

        リンクした相手（同じ素材の絵と音）にも同じ値を入れる 片方だけ変えると、絵と音が
        ずれていく
        """
        speed_spec = TrackSpec("speed", "再生速度", 1, 1000, 100, step=1, unit="%")
        speed = create_editor(speed_spec)
        speed.setObjectName("clip_speed")
        speed.set_value(AnimatedValue(float(clip.speed * 100)))
        speed.value_changed.connect(
            lambda value: self._set_linked(clip, "speed", _fraction(value, 100), "再生速度を変更")
        )
        section.add_row("再生速度", speed)

        media = self._project.find_media(clip.media_id) if self._project and clip.media_id else None
        # 上限は素材の長さ 分からない素材は 10 時間まで（スライダーが整数で持てる範囲）
        length = float(media.duration) if media is not None and media.duration > 0 else 36000.0
        start_spec = TrackSpec(
            "source_in",
            "再生開始位置",
            0,
            max(length, float(clip.source_in)),
            0,
            step=0.01,
            unit="秒",
        )
        start = create_editor(start_spec)
        start.setObjectName("clip_source_in")
        start.set_value(AnimatedValue(float(clip.source_in)))
        start.value_changed.connect(
            lambda value: self._set_linked(
                clip, "source_in", _fraction(value, 1), "再生開始位置を変更"
            )
        )
        section.add_row("再生開始位置", start)

    def _set_linked(self, clip: Clip, name: str, value: Fraction, label: str) -> None:
        """クリップ自身の値を、選んだほかのクリップとリンクした相手にも入れる"""
        if name == "speed" and value <= 0:
            return
        base = SetClipProperty(clip.id, name, value)
        commands: list[Command] = [base, *self._also_for_others(base)]
        touched = {c.clip_id for c in commands if isinstance(c, SetClipProperty)}
        for partner in self._link_partners(touched):
            commands.append(SetClipProperty(partner, name, value))
        self._send(commands, label)

    def _link_partners(self, clip_ids: set[ClipId]) -> list[ClipId]:
        if self._project is None:
            return []
        groups = {
            clip.link_group
            for track in self._project.timeline.tracks
            for clip in track.clips
            if clip.id in clip_ids and clip.link_group is not None
        }
        return [
            clip.id
            for track in self._project.timeline.tracks
            for clip in track.clips
            if clip.link_group in groups and clip.id not in clip_ids
        ]

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
        # 隣が固定の項目なら、その向きへは動かせない（命令がまたぐ動きを断る）
        section = _Section(
            label,
            effect=effect,
            clip_id=clip.id,
            index=index,
            up_movable=index > 0 and not stack[index - 1].fixed,
            down_movable=index < len(stack) - 1 and not stack[index + 1].fixed,
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
        pending = self._virtual.get(path.effect_id) if path.effect_id is not None else None
        if pending is not None:
            # まだ無い欄は、値を入れた欄を足した絵で見せる 値だけ変えようとすると、
            # 欄が見つからずにドラッグ中の絵が動かない
            clip_id, effect = pending
            self.preview_requested.emit(AddEffect(clip_id, effect.with_param(path.name, value)))
            return
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
        save.setEnabled(bool(_loose(clip)))
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
        self._presets.save(Preset(name=name.strip(), effects=_loose(clip)))

    def _emit(self, command: Command, label: str | None = None) -> None:
        commands = [command, *self._also_for_others(command)]
        text = label or command.label
        if len(commands) > 1:
            text = f"{text}（{len(commands)} 本）"
        self._send(commands, text)

    def _send(self, commands: list[Command], label: str) -> None:
        """コマンドをまとめて出す（1 回の取り消しで戻る）

        まだクリップに無い欄（:attr:`_virtual`）を指すものがあれば、その前に欄を足す
        足すのと値を入れるのを別々に出すと、取り消しが 2 段になり、1 回戻しただけでは
        既定の値の欄が残る
        """
        materialized: list[Command] = []
        added: set[EffectId] = set()
        for command in commands:
            target = _effect_of(command)
            pending = self._virtual.get(target) if target is not None else None
            if pending is not None and target not in added:
                clip_id, effect = pending
                materialized.append(AddEffect(clip_id, effect))
                added.add(effect.id)
            materialized.append(command)
        self.commands_requested.emit(materialized, label)

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


def _effect_of(command: Command) -> EffectId | None:
    """コマンドが指すエフェクト（値を変える・点を打つ・切り替える物）"""
    if isinstance(command, SetParam | SetKeyframe | RemoveKeyframe | ClearKeyframes):
        return command.path.effect_id
    if isinstance(command, SetEffectEnabled):
        return command.effect_id
    return None


def _loose(clip: Clip) -> tuple[Effect, ...]:
    """足したエフェクト（最初から持つ欄を除く）

    プリセットには欄を入れない 入れると当てるたびに既定のままの配置や反転が
    ふつうのエフェクトとして増え、足した物の一覧が読めなくなる
    """
    return tuple(effect for effect in clip.effects if not effect.fixed)


def _fraction(value: ParamValue, scale: int) -> Fraction:
    """数の入力欄の値を、クリップが持つ分数へ ``scale`` で割る（% を倍率へ）

    小数のまま渡すと保存の所で分数に直せない（SetClipProperty が断る） 入力欄の
    刻みより細かい桁は意味が無いので丸める
    """
    number = value.static if isinstance(value, AnimatedValue) else 0.0
    return Fraction(number).limit_denominator(1_000_000) / scale


def _heading(text: str) -> QLabel:
    """足したエフェクトの一覧の見出し（YMM4 の「映像エフェクト」「音声エフェクト」）"""
    label = QLabel(text)
    label.setObjectName("effects_heading")
    label.setStyleSheet(f"color: {Colors.TEXT_MUTED.name()}; font-weight: bold;")
    return label


def _lock_label() -> QLabel:
    lock = QLabel()
    lock.setObjectName("fixed_lock")
    lock.setPixmap(lock_pixmap(lock.devicePixelRatioF()))
    lock.setAccessibleName("固定の項目")
    lock.setToolTip(
        "クリップが最初から持つ項目です 外すことと並べ替えはできません"
        " 無効にはできます 重ねて掛けたいときは同じエフェクトを追加してください"
    )
    lock.setStyleSheet("border: none;")
    return lock


#: 鍵の印を見せる大きさ（論理画素） 見出しの ▲ ▼ ✕ の文字と同じくらい
LOCK_SIZE = 14

#: 描く細かさ :func:`sashimono.ui.transport.transport_icon` と同じく、大きめに描いて
#: 縮めて見せる 画面の拡大率で引き伸ばしても角がぼけない
_LOCK_SCALE = 4


def lock_pixmap(device_pixel_ratio: float = 1.0) -> QPixmap:
    """固定の項目の鍵の印

    文字（絵文字の鍵）で出すと、Windows ではカラーの絵文字の書体で描かれ、ほかの
    ボタンと揃わない（再生ボタンの ``⏸`` が青い四角になったのと同じ Issue #27）
    書体に頼らず、見出しのボタンの文字と同じ色で自前で描く 16 × 16 の枠で描く
    """
    pixmap = QPixmap(16 * _LOCK_SCALE, 16 * _LOCK_SCALE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(_LOCK_SCALE, _LOCK_SCALE)

    # つる 本体に隠れる所まで伸ばし、付け根に隙間が見えないようにする
    shackle = QPainterPath()
    shackle.moveTo(5, 9)
    shackle.lineTo(5, 6)
    shackle.arcTo(QRectF(5, 2.5, 6, 7), 180, -180)
    shackle.lineTo(11, 9)
    pen = QPen(Colors.TEXT, 1.8)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(shackle)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Colors.TEXT)
    painter.drawRoundedRect(QRectF(3, 7.5, 10, 7), 1.2, 1.2)
    # 鍵穴は抜いて見せる 塗りつぶしの四角だけだと、鍵ではなく箱に見える
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    painter.drawEllipse(QPointF(8, 10.5), 1.2, 1.2)
    painter.drawRect(QRectF(7.5, 10.5, 1, 2.2))
    painter.end()

    icon = QIcon()
    icon.addPixmap(pixmap)
    return icon.pixmap(QSize(LOCK_SIZE, LOCK_SIZE), device_pixel_ratio)


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
        up_movable: bool = False,
        down_movable: bool = False,
        after: bool = False,
    ) -> None:
        super().__init__()
        #: 見出しの言葉 組の並び（描画 → 中身 → 動画・音声 → エフェクト）を試験で見る
        self.heading = title
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
        self._header = header

        self._after = after
        if effect is not None and clip_id is not None:
            header.addWidget(self._toggle(effect, clip_id))
            if effect.fixed:
                # 外すことも並べ替えることもできない 押せないボタンを並べるより、
                # 鍵の印で「最初からある欄」だと示す方が、押せない理由まで伝わる
                header.addWidget(_lock_label())
            else:
                header.addWidget(self._move(effect, clip_id, index - 1, "▲", up_movable))
                header.addWidget(self._move(effect, clip_id, index + 1, "▼", down_movable))
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

    def add_header_widget(self, widget: QWidget) -> None:
        """見出しの右端へ部品を足す（描画・音声の組の切り替えと鍵の印）"""
        self._header.addWidget(widget)

    def _remove(self, effect: Effect, clip_id: ClipId) -> QToolButton:
        button = QToolButton()
        button.setText("✕")
        button.setToolTip("このエフェクトを外す")
        button.setAutoRaise(True)
        button.clicked.connect(
            lambda: self.action_requested.emit(RemoveEffect(clip_id, effect.id, after=self._after))
        )
        return button
