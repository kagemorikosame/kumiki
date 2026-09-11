"""タイムラインのウィジェット

自分でプロジェクトを書き換えない 操作の結果はすべて :class:`Command` として
:attr:`TimelineView.command_requested` から外へ出す UI と AI が同じ入口を通る、
という設計をここでも守るため
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import (
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from kumiki.core.commands import (
    Command,
    MoveClip,
    RemoveClip,
    SetTrackState,
    SplitClip,
    TrimClip,
)
from kumiki.core.model import Clip, ClipId, GroupId, Project, TrackId, TrackKind
from kumiki.engine.cache import MediaAnalyzer
from kumiki.ui.theme import Colors, Metrics
from kumiki.ui.timeline.layout import TimelineLayout
from kumiki.ui.timeline.painter import draw_clip as paint_clip
from kumiki.ui.timeline.painter import (
    draw_playhead,
    draw_ruler,
    draw_track_background,
    draw_track_header,
    track_button_rects,
    visible_clips,
)

__all__ = ["TimelineView"]

#: ホイール 1 段で拡大する倍率
ZOOM_STEP = 1.25


class DragKind(Enum):
    NONE = auto()
    PLAYHEAD = auto()
    MOVE_CLIP = auto()
    TRIM_HEAD = auto()
    TRIM_TAIL = auto()


@dataclass(slots=True)
class DragState:
    """ドラッグ中の状態

    ドラッグ中はプロジェクトを書き換えず、確定した時点で 1 つのコマンドを出す
    途中経過をコマンドにすると、Undo 履歴が中間状態で埋まる
    """

    kind: DragKind = DragKind.NONE
    clip_id: ClipId | None = None
    origin_track: TrackId | None = None
    #: 掴んだ位置と、クリップ先頭とのフレーム差 掴んだ場所を保ったまま動かすため
    grab_offset: int = 0
    #: 現在のプレビュー位置 描画にだけ使う
    preview_start: int = 0
    preview_track: TrackId | None = None
    preview_head_delta: int = 0
    preview_tail_delta: int = 0
    moved: bool = False


class TimelineView(QWidget):
    """クリップを並べて見せ、編集操作を受け付ける"""

    #: 再生ヘッドが動いた 引数はフレーム番号
    playhead_moved = Signal(int)
    #: 選択が変わった 引数はクリップ ID、または空文字列
    selection_changed = Signal(str)
    #: 編集操作が発生した 引数はコマンドの一覧と、履歴に出す操作名
    #:
    #: 常に一覧で渡す 1 回の操作が複数のコマンドになることがあり（分割など）、
    #: それを 1 回の取り消しで戻せるようにするため
    commands_requested = Signal(list, str)

    def __init__(
        self, project: Project, analyzer: MediaAnalyzer, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._analyzer = analyzer
        self._layout = TimelineLayout()
        self._playhead = 0
        self._selected: ClipId | None = None
        self._drag = DragState()
        self._follow_playhead = True

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(160)

    # --- 外から差し替えるもの ---

    @property
    def project(self) -> Project:
        return self._project

    def set_project(self, project: Project) -> None:
        self._project = project
        # 選択していたクリップが消えていれば選択を解く 存在しない ID を
        # 持ち続けると、次の操作で「見つからない」例外になる
        if self._selected is not None and project.timeline.locate_clip(self._selected) is None:
            self._selected = None
            self.selection_changed.emit("")
        self.update()

    @property
    def playhead(self) -> int:
        return self._playhead

    def set_playhead(self, frame: int, *, follow: bool = True) -> None:
        frame = max(0, frame)
        if frame == self._playhead:
            return
        self._playhead = frame
        if follow and self._follow_playhead:
            self._layout = self._layout.ensure_visible(frame, self.width())
        self.update()

    @property
    def selected_clip(self) -> ClipId | None:
        return self._selected

    def select(self, clip_id: ClipId | None) -> None:
        if clip_id == self._selected:
            return
        self._selected = clip_id
        self.selection_changed.emit(clip_id or "")
        self.update()

    def zoom(self, factor: float) -> None:
        """ウィジェットの中央を基準に拡大・縮小する メニューやボタンから"""
        self._layout = self._layout.zoomed(factor, anchor_x=self.width() / 2.0)
        self.update()

    def zoom_to_fit(self) -> None:
        """タイムライン全体が収まる倍率にする"""
        duration = max(1, self._project.duration)
        usable = max(1, self.width() - Metrics.TRACK_HEADER_WIDTH)
        self._layout = TimelineLayout(pixels_per_frame=usable / duration * 0.98)
        self.update()

    # --- 描画 ---

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt の命名規約
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), Colors.TIMELINE_BACKGROUND)

        timeline = self._project.timeline
        width = self.width()

        for band in self._layout.bands(timeline):
            if band.bottom <= Metrics.RULER_HEIGHT or band.top >= self.height():
                continue
            draw_track_background(painter, band, width)

        for band, clip, rect in visible_clips(timeline, self._layout, width):
            media = self._project.find_media(clip.media_id) if clip.media_id is not None else None
            paint_clip(
                painter,
                clip,
                band,
                self._layout,
                self._project.rate,
                media=media,
                filmstrip=self._analyzer.filmstrip(media) if media is not None else None,
                waveform=self._analyzer.waveform(media) if media is not None else None,
                selected=clip.id == self._selected,
                clip_rect=rect,
            )

        self._draw_drag_preview(painter)

        active = {
            track.id
            for kind in (TrackKind.VIDEO, TrackKind.AUDIO)
            for track in timeline.active_tracks(kind)
        }
        for band in self._layout.bands(timeline):
            if band.bottom <= Metrics.RULER_HEIGHT or band.top >= self.height():
                continue
            draw_track_header(painter, band, active=band.track.id in active)

        draw_ruler(painter, self._layout, width, self._project.rate)
        draw_playhead(painter, self._layout, self._playhead, self.height())

    def _draw_drag_preview(self, painter: QPainter) -> None:
        """ドラッグ中の落下先を枠線で示す

        実際のクリップを動かさずに枠だけ出すことで、途中経過が Undo 履歴に
        残らず、かつ落ちる位置は分かる
        """
        if self._drag.kind not in (DragKind.MOVE_CLIP, DragKind.TRIM_HEAD, DragKind.TRIM_TAIL):
            return
        if self._drag.clip_id is None:
            return
        located = self._project.timeline.locate_clip(self._drag.clip_id)
        if located is None:
            return
        _, clip = located

        target_track = self._drag.preview_track or self._drag.origin_track
        band = next(
            (b for b in self._layout.bands(self._project.timeline) if b.track.id == target_track),
            None,
        )
        if band is None:
            return

        if self._drag.kind is DragKind.MOVE_CLIP:
            start, end = self._drag.preview_start, self._drag.preview_start + clip.duration
        else:
            start = clip.timeline_start + self._drag.preview_head_delta
            end = clip.timeline_end + self._drag.preview_tail_delta

        left = self._layout.frame_to_x(start)
        right = self._layout.frame_to_x(end)
        painter.setPen(QPen(Colors.SELECTION, 2, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(int(left), band.top + 1, max(2, int(right - left)), band.height - 3)

    # --- 入力 ---

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt の命名規約
        delta = event.angleDelta().y()
        if delta == 0:
            return

        modifiers = event.modifiers()
        if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            # マウス位置を基準に拡大する 中央基準だと、拡大するたびに
            # 見ていた場所が画面外へ逃げる
            factor = ZOOM_STEP if delta > 0 else 1.0 / ZOOM_STEP
            self._layout = self._layout.zoomed(factor, anchor_x=event.position().x())
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            frames = self._layout.frames_in(self.width()) * 0.15
            self._layout = self._layout.scrolled_to(
                self._layout.scroll_frame - (frames if delta > 0 else -frames)
            )
        else:
            self._layout = self._layout.scrolled_vertically(self._layout.scroll_y - (delta // 4))
        self.update()
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt の命名規約
        if event.button() != Qt.MouseButton.LeftButton:
            return
        position = event.position().toPoint()

        if self._toggle_track_button(position):
            return

        if position.y() < Metrics.RULER_HEIGHT or position.x() < Metrics.TRACK_HEADER_WIDTH:
            self._drag = DragState(kind=DragKind.PLAYHEAD)
            self._scrub(position)
            return

        hit = self._clip_at(position)
        if hit is None:
            self.select(None)
            self._drag = DragState(kind=DragKind.PLAYHEAD)
            self._scrub(position)
            return

        track_id, clip = hit
        self.select(clip.id)

        edge = self._edge_at(position, clip)
        frame = self._layout.frame_at(position.x())
        self._drag = DragState(
            kind=edge,
            clip_id=clip.id,
            origin_track=track_id,
            grab_offset=frame - clip.timeline_start,
            preview_start=clip.timeline_start,
            preview_track=track_id,
        )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt の命名規約
        position = event.position().toPoint()

        if self._drag.kind is DragKind.NONE:
            self._update_cursor(position)
            button = self._track_button_at(position)
            self.setToolTip(button[1] if button is not None else "")
            return

        if self._drag.kind is DragKind.PLAYHEAD:
            self._scrub(position)
            return

        self._drag.moved = True
        frame = self._layout.frame_at(position.x())

        if self._drag.kind is DragKind.MOVE_CLIP:
            self._drag.preview_start = max(0, frame - self._drag.grab_offset)
            band = self._layout.band_at(self._project.timeline, position.y())
            if band is not None and not band.track.locked:
                self._drag.preview_track = band.track.id
        elif self._drag.clip_id is not None:
            located = self._project.timeline.locate_clip(self._drag.clip_id)
            if located is not None:
                _, clip = located
                if self._drag.kind is DragKind.TRIM_HEAD:
                    self._drag.preview_head_delta = min(
                        frame - clip.timeline_start, clip.duration - 1
                    )
                else:
                    self._drag.preview_tail_delta = max(
                        frame - clip.timeline_end, -(clip.duration - 1)
                    )
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt の命名規約
        del event
        drag, self._drag = self._drag, DragState()
        if drag.kind in (DragKind.NONE, DragKind.PLAYHEAD) or not drag.moved:
            self.update()
            return
        if drag.clip_id is None:
            return

        command = self._command_for(drag)
        if command is not None:
            self._request([command], command.label)
        self.update()

    def _command_for(self, drag: DragState) -> Command | None:
        """ドラッグの結果を 1 つのコマンドにまとめる"""
        if drag.clip_id is None:
            return None
        located = self._project.timeline.locate_clip(drag.clip_id)
        if located is None:
            return None
        _, clip = located

        if drag.kind is DragKind.MOVE_CLIP:
            unchanged = (
                drag.preview_start == clip.timeline_start
                and drag.preview_track == drag.origin_track
            )
            if unchanged:
                return None
            return MoveClip(
                drag.clip_id,
                drag.preview_start,
                drag.preview_track if drag.preview_track != drag.origin_track else None,
            )

        if drag.kind is DragKind.TRIM_HEAD and drag.preview_head_delta:
            return TrimClip(drag.clip_id, head_delta=drag.preview_head_delta)
        if drag.kind is DragKind.TRIM_TAIL and drag.preview_tail_delta:
            return TrimClip(drag.clip_id, tail_delta=drag.preview_tail_delta)
        return None

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt の命名規約
        key = event.key()
        rippled = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if key == Qt.Key.Key_S:
            self.split_at_playhead()
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected(ripple=rippled)
        elif key == Qt.Key.Key_Left:
            self.set_playhead(self._playhead - 1)
            self.playhead_moved.emit(self._playhead)
        elif key == Qt.Key.Key_Right:
            self.set_playhead(self._playhead + 1)
            self.playhead_moved.emit(self._playhead)
        elif key == Qt.Key.Key_Home:
            self.set_playhead(0)
            self.playhead_moved.emit(self._playhead)
        elif key == Qt.Key.Key_End:
            self.set_playhead(self._project.duration)
            self.playhead_moved.emit(self._playhead)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # --- 操作 ---

    def split_at_playhead(self) -> None:
        """再生ヘッドの位置で、そこにあるクリップをすべて分割する

        選択の有無に関わらず全トラックを切る Premiere の Ctrl+K や
        AviUtl の分割と同じ感覚

        リンクされた映像・音声は 1 つのコマンドで一緒に割れるので、グループごとに
        1 回だけ発行する 両方に出すと、2 回目は「すでに割れている」失敗になり、
        意味のないエラーがステータスバーに出る
        """
        frame = self._playhead
        targets: list[Clip] = []
        seen: set[GroupId] = set()
        for track in self._project.timeline.tracks:
            if track.locked:
                continue
            for clip in track.clips:
                if not (clip.timeline_start < frame < clip.timeline_end):
                    continue
                if clip.link_group is not None:
                    if clip.link_group in seen:
                        continue
                    seen.add(clip.link_group)
                targets.append(clip)

        self._request([SplitClip(clip.id, frame) for clip in targets], "再生ヘッドで分割")

    def delete_selected(self, *, ripple: bool = False) -> None:
        if self._selected is None:
            return
        command = RemoveClip(self._selected, ripple=ripple)
        self._request([command], command.label)

    def _request(self, commands: list[Command], label: str) -> None:
        if commands:
            self.commands_requested.emit(commands, label)

    # --- 補助 ---

    def _scrub(self, position: QPoint) -> None:
        # スクラブ中は追従を切る 追従したままだと、掴んだ位置が画面中央へ
        # 逃げ続けて操作にならない
        self._follow_playhead = False
        self.set_playhead(self._layout.frame_at(position.x()), follow=False)
        self._follow_playhead = True
        self.playhead_moved.emit(self._playhead)

    def _track_button_at(self, position: QPoint) -> tuple[TrackId, str, str] | None:
        """ヘッダの切り替えボタンの上なら ``(トラック, 説明, 属性名)``"""
        if position.x() >= Metrics.TRACK_HEADER_WIDTH or position.y() < Metrics.RULER_HEIGHT:
            return None
        band = self._layout.band_at(self._project.timeline, position.y())
        if band is None:
            return None
        for attribute, tip, rect in track_button_rects(band):
            if rect.contains(position):
                return band.track.id, tip, attribute
        return None

    def _toggle_track_button(self, position: QPoint) -> bool:
        """ボタンの上なら切り替えのコマンドを出して真を返す

        再生ヘッドは動かさない ミュートを押すたびに見ていた場所が飛ぶと、
        聞き比べのたびに位置を戻すことになる
        """
        hit = self._track_button_at(position)
        if hit is None:
            return False
        track_id, _, attribute = hit
        track = self._project.timeline.find_track(track_id)
        if track is None:
            return False
        command = SetTrackState(track_id, **{attribute: not getattr(track, attribute)})
        self._request([command], command.label)
        return True

    def _clip_at(self, position: QPoint) -> tuple[TrackId, Clip] | None:
        for band, clip, rect in visible_clips(self._project.timeline, self._layout, self.width()):
            if rect.contains(position):
                return band.track.id, clip
        return None

    def _edge_at(self, position: QPoint, clip: Clip) -> DragKind:
        """クリップの端を掴んでいるならトリム、そうでなければ移動"""
        left = self._layout.frame_to_x(clip.timeline_start)
        right = self._layout.frame_to_x(clip.timeline_end)
        if abs(position.x() - left) <= Metrics.TRIM_HANDLE_WIDTH:
            return DragKind.TRIM_HEAD
        if abs(position.x() - right) <= Metrics.TRIM_HANDLE_WIDTH:
            return DragKind.TRIM_TAIL
        return DragKind.MOVE_CLIP

    def _update_cursor(self, position: QPoint) -> None:
        hit = self._clip_at(position)
        if hit is None:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        edge = self._edge_at(position, hit[1])
        self.setCursor(
            Qt.CursorShape.SizeHorCursor
            if edge in (DragKind.TRIM_HEAD, DragKind.TRIM_TAIL)
            else Qt.CursorShape.OpenHandCursor
        )
