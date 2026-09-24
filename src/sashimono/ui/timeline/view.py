"""タイムラインのウィジェット

自分でプロジェクトを書き換えない 操作の結果はすべて :class:`Command` として
:attr:`TimelineView.command_requested` から外へ出す UI と AI が同じ入口を通る、
という設計をここでも守るため
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum, auto

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QContextMenuEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import QMenu, QWidget

from sashimono.core.clipboard import ClipboardContent, copy_clips, cut_commands, paste_commands
from sashimono.core.commands import (
    AddClip,
    Command,
    GroupClips,
    MoveClip,
    MoveClips,
    RemoveClip,
    RemoveClips,
    SetTrackHeights,
    SetTrackState,
    SplitClip,
    TrimClip,
    TrimClips,
    UngroupClips,
)
from sashimono.core.commands.edit import DEFAULT_TRACK_HEIGHT, MAX_TRACK_HEIGHT, MIN_TRACK_HEIGHT
from sashimono.core.model import Clip, ClipId, GroupId, Project, TrackId, TrackKind
from sashimono.engine.cache import MediaAnalyzer
from sashimono.ui.theme import Colors, Metrics
from sashimono.ui.timeline.layout import TimelineLayout, TrackBand
from sashimono.ui.timeline.painter import (
    DETAIL_MIN_WIDTH,
    clip_rect_for,
    clips_in_range,
    draw_dense_clips,
    draw_playhead,
    draw_ruler,
    draw_track_background,
    draw_track_header,
    track_button_rects,
)
from sashimono.ui.timeline.painter import draw_clip as paint_clip
from sashimono.ui.timeline.work_area import WorkAreaEditor

__all__ = ["TimelineView"]

#: ホイール 1 段で拡大する倍率
ZOOM_STEP = 1.25

#: 全トラックの高さを 1 段で変える量（画素） 細かいと何度も回すことになり、
#: 粗いと最小（28）から最大（240）までが数段で終わって合わせにくい
HEIGHT_STEP = 12

#: 境目を掴める幅（上下それぞれ、画素） 狭いと掴めず、広いと名前の行の
#: ボタンに食い込む（最小の高さ 28 のトラックでもボタンが押せる幅にしてある）
RESIZE_GRAB = 3

#: 高さを続けて変えたとみなす間隔（秒） この間隔より短く続けたホイールやキーは
#: 取り消しの 1 段にまとめる 長いと、少し間を置いて別の気持ちで変えたぶんまで
#: 一緒に戻る
HEIGHT_MERGE_SECONDS = 1.0

#: 囲んで選ぶと決めるまでに動かす距離（画素） クリックのつもりの手ぶれで
#: 選択が消えないようにする
MARQUEE_THRESHOLD = 4

#: ヘッダのボタン（TRACK_BUTTONS）の説明は長いので、メニュー用の短い名前を別に持つ
_TRACK_TOGGLES = (("muted", "ミュート"), ("solo", "ソロ"), ("locked", "ロック"))


class DragKind(Enum):
    NONE = auto()
    PLAYHEAD = auto()
    MOVE_CLIP = auto()
    TRIM_HEAD = auto()
    TRIM_TAIL = auto()
    RESIZE_TRACK = auto()
    MARQUEE = auto()


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
    #: 高さは掴んだ位置からの差で決める マウスの位置そのもので決めると、境目から
    #: 少し離れて掴んだだけで高さが跳ぶ
    resize_track: TrackId | None = None
    grab_y: int = 0
    origin_height: int = 0
    #: 選んでいる何本かをまとめて動かしている 行き先のトラックは変えない
    #: （何本もが別々のトラックにいるとき、どこへ移すのかが決まらない）
    group: bool = False
    #: 囲んで選ぶときの始点と今の位置、始める前の選択（Ctrl を押していれば足す）
    marquee_from: QPoint | None = None
    marquee_to: QPoint | None = None
    marquee_base: tuple[ClipId, ...] = ()


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
    #: :attr:`commands_requested` と同じだが、直前の同じ操作の続き 取り消しの段を
    #: 増やさずに直前の段へまとめてもらう（ホイールで高さを変え続けるときなど）
    commands_continued = Signal(list, str)
    #: シーンを置いたクリップをダブルクリックした 引数はシーンの ID
    #: 中を開くのは窓の仕事（どのシーンを編集中かは窓が持つ）
    scene_open_requested = Signal(str)
    #: ビューは窓を知らない（テストで単体で作れるように） 知らせは信号で外へ出し、
    #: ステータスバーに出すのは窓の仕事にする
    status_message = Signal(str)

    def __init__(
        self, project: Project, analyzer: MediaAnalyzer, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._analyzer = analyzer
        self._layout = TimelineLayout()
        self._playhead = 0
        #: 選んでいるクリップ 最後の 1 本が「主」で、設定パネルと AI の既定の
        #: 対象になる 何本選んでも、設定パネルに出せるのは 1 本だけのため
        self._selection: tuple[ClipId, ...] = ()
        #: Shift+クリックで範囲を決めるときの起点 最後に選んだクリップ（選び方は
        #: 問わない AI が選んだものも含む） Shift での範囲選択そのものは起点を
        #: 動かさない 動かすと、Shift を押したまま範囲を広げ直せない
        self._anchor: ClipId | None = None
        self._last_height_change = -HEIGHT_MERGE_SECONDS
        self._drag = DragState()
        self._follow_playhead = True
        self._clipboard: ClipboardContent | None = None
        #: 高さのドラッグ中だけ持つ、掴む前のプロジェクト 途中の高さは描画のため
        #: だけに当て、離したときにこれへ戻してからコマンドを出す
        self._resize_base: Project | None = None
        #: 書き出し範囲の Shift+ドラッグと、その帯 ほかのドラッグとは別に持つ
        self._work_area = WorkAreaEditor(self._request)

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(160)
        # 中身は自前で描いているので、読み上げソフトにはこの名前しか伝わらない
        self.setAccessibleName("タイムライン")
        self.setAccessibleDescription(
            "選んだクリップのトラックは Shift+M でミュート、Shift+S でソロ、Shift+L でロック"
        )

    # --- 外から差し替えるもの ---

    @property
    def project(self) -> Project:
        return self._project

    def set_project(self, project: Project) -> None:
        self._project = project
        # 消えたクリップは選択から外す 存在しない ID を持ち続けると、次の操作で
        # 「見つからない」例外になる
        remaining = tuple(c for c in self._selection if project.timeline.locate_clip(c))
        if remaining != self._selection:
            self.set_selection(remaining)
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
        """主に選んでいるクリップ（最後に選んだもの）"""
        return self._selection[-1] if self._selection else None

    @property
    def selected_clips(self) -> tuple[ClipId, ...]:
        """選んでいるクリップすべて 選んだ順"""
        return self._selection

    def select(self, clip_id: ClipId | None) -> None:
        """1 本だけを選ぶ ``None`` なら選択を解く"""
        self.set_selection((clip_id,) if clip_id is not None else ())
        self._anchor = clip_id

    def set_selection(self, clip_ids: Iterable[ClipId]) -> None:
        """選択を丸ごと入れ替える 重なった ID は 1 つにする（最後の位置を残す）"""
        ordered = tuple(reversed(dict.fromkeys(reversed(tuple(clip_ids)))))
        if ordered == self._selection:
            return
        self._selection = ordered
        self._anchor = self.selected_clip
        # 主のクリップが同じでも知らせる 選択から外したクリップへ、設定パネルの
        # まとめ当てが届いてしまう
        self.selection_changed.emit(self.selected_clip or "")
        self.update()

    def select_all(self) -> None:
        """全トラックのクリップを選ぶ ロックしたトラックも含める（見るだけなら困らない）"""
        self.set_selection(c.id for t in self._project.timeline.tracks for c in t.clips)
        if len(self._selection) > 1:
            self.status_message.emit(f"{len(self._selection)} 本を選択")

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

        start_frame, end_frame = self._layout.visible_range(width)
        scale = self._layout.pixels_per_frame
        selected = frozenset(self._selection)
        for band in self._layout.bands(timeline):
            if band.bottom <= Metrics.RULER_HEIGHT or band.top >= self.height():
                continue
            # 名前が入らない幅のクリップは、まとめて色の帯にする 1 本ずつ描くと
            # 全体表示で数千本を描くことになり、60fps の予算に収まらない
            dense: list[Clip] = []
            for clip in clips_in_range(band.track, start_frame, end_frame):
                if clip.duration * scale < DETAIL_MIN_WIDTH:
                    dense.append(clip)
                    continue
                rect = clip_rect_for(clip, band, self._layout, width)
                if rect is not None:
                    self._paint_detailed(painter, band, clip, rect, clip.id in selected)
            draw_dense_clips(painter, band, dense, self._layout, width, selected)

        self._draw_drag_preview(painter)
        self._work_area.paint_tracks(
            painter, self._layout, width, self.height(), timeline.work_area
        )

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
        self._work_area.paint_ruler(painter, self._layout, width, timeline.work_area)
        draw_playhead(painter, self._layout, self._playhead, self.height())

    def _paint_detailed(
        self, painter: QPainter, band: TrackBand, clip: Clip, rect: QRect, selected: bool
    ) -> None:
        media = self._project.find_media(clip.media_id) if clip.media_id is not None else None
        scene = self._project.find_scene(clip.scene_id) if clip.scene_id is not None else None
        paint_clip(
            painter,
            clip,
            band,
            self._layout,
            self._project.rate,
            media=media,
            filmstrip=self._analyzer.filmstrip(media) if media is not None else None,
            waveform=self._analyzer.waveform(media) if media is not None else None,
            selected=selected,
            clip_rect=rect,
            scene_name=scene.name
            if scene is not None
            else ("（消えたシーン）" if clip.scene_id else None),
        )

    def _draw_drag_preview(self, painter: QPainter) -> None:
        """ドラッグ中の落下先を枠線で示す

        実際のクリップを動かさずに枠だけ出すことで、途中経過が Undo 履歴に
        残らず、かつ落ちる位置は分かる
        """
        if self._drag.kind is DragKind.MARQUEE:
            self._draw_marquee(painter)
            return
        if self._drag.kind not in (DragKind.MOVE_CLIP, DragKind.TRIM_HEAD, DragKind.TRIM_TAIL):
            return
        if self._drag.clip_id is None:
            return
        located = self._project.timeline.locate_clip(self._drag.clip_id)
        if located is None:
            return
        _, clip = located
        bands = {b.track.id: b for b in self._layout.bands(self._project.timeline)}
        painter.setPen(QPen(Colors.SELECTION, 2, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if self._drag.group:
            # 何本かをまとめて動かすときは、動く全員の落下先を出す 掴んだ 1 本の枠
            # だけだと、ほかのクリップがどこへ落ちるか分からない
            delta = self._drag.preview_start - clip.timeline_start
            for track_id, member in self._moving_members():
                if track_id in bands:
                    self._dash_rect(
                        painter,
                        bands[track_id],
                        member.timeline_start + delta,
                        member.timeline_end + delta,
                    )
            return

        target_track = self._drag.preview_track or self._drag.origin_track
        band = bands.get(target_track) if target_track is not None else None
        if band is None:
            return
        if self._drag.kind is DragKind.MOVE_CLIP:
            start, end = self._drag.preview_start, self._drag.preview_start + clip.duration
        else:
            start = clip.timeline_start + self._drag.preview_head_delta
            end = clip.timeline_end + self._drag.preview_tail_delta
        self._dash_rect(painter, band, start, end)

    def _dash_rect(self, painter: QPainter, band: TrackBand, start: int, end: int) -> None:
        left = self._layout.frame_to_x(start)
        right = self._layout.frame_to_x(end)
        painter.drawRect(int(left), band.top + 1, max(2, int(right - left)), band.height - 3)

    def _draw_marquee(self, painter: QPainter) -> None:
        origin, current = self._drag.marquee_from, self._drag.marquee_to
        if origin is None or current is None or origin == current:
            return
        fill = QColor(Colors.SELECTION)
        fill.setAlpha(40)
        painter.setPen(QPen(Colors.SELECTION, 1, Qt.PenStyle.DashLine))
        painter.setBrush(fill)
        painter.drawRect(QRect(origin, current).normalized())

    def _trimmable_selection(self) -> tuple[ClipId, ...]:
        """選んだうち、端を動かせるもの ロックしたトラックのものは外す

        動かすときと違い、リンクした相手のトラックは見ない トリムは相手のトラックが
        ロックされていれば、その相手だけが元の長さで残る（:class:`TrimClip` の決まり）
        """
        timeline = self._project.timeline
        return tuple(
            clip_id
            for clip_id in self._selection
            if (located := timeline.locate_clip(clip_id)) is not None and not located[0].locked
        )

    def _movable_selection(self) -> tuple[ClipId, ...]:
        """選んだうち、ロックしていないトラックのもの

        Ctrl+A はロックしたトラックのクリップも選ぶ（見るだけなら困らない） それを
        そのまま :class:`MoveClips` へ渡すと、ほかのクリップまで動かせなくなる
        動かすときは、ロックしたトラックのものを最初から外す
        """
        timeline = self._project.timeline
        movable: list[ClipId] = []
        for clip_id in self._selection:
            located = timeline.locate_clip(clip_id)
            if located is None or located[0].locked:
                continue
            # リンクした相手がロックしたトラックにいても外す :class:`MoveClips` は
            # そういう組を断るので、残すと枠では動いて見えたのに離すと何も動かない
            link = located[1].link_group
            if link is not None and any(t.locked for t, _ in timeline.linked_clips(link)):
                continue
            movable.append(clip_id)
        return tuple(movable)

    def _moving_members(self) -> list[tuple[TrackId, Clip]]:
        """まとめて動かすときに動くクリップ 動かせる選択とリンクした相手

        :class:`MoveClips` と同じ決まりで集める
        """
        timeline = self._project.timeline
        found: dict[ClipId, tuple[TrackId, Clip]] = {}
        for clip_id in self._movable_selection():
            located = timeline.locate_clip(clip_id)
            if located is None:
                continue
            track, clip = located
            members = (
                list(timeline.linked_clips(clip.link_group))
                if clip.link_group is not None
                else [(track, clip)]
            )
            for member_track, member in members:
                found.setdefault(member.id, (member_track.id, member))
        return list(found.values())

    def _group_floor(self) -> int:
        """まとめて動かすとき、掴んだクリップを置ける最も前の位置

        掴んだ 1 本だけで 0 に止めると、それより前にいるほかのクリップが先頭より前へ
        出る 枠では動かせたように見えるのに、離すと断られる
        """
        if self._drag.clip_id is None:
            return 0
        located = self._project.timeline.locate_clip(self._drag.clip_id)
        members = self._moving_members()
        if located is None or not members:
            return 0
        earliest = min(member.timeline_start for _, member in members)
        return located[1].timeline_start - earliest

    # --- 入力 ---

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt の命名規約
        delta = event.angleDelta().y()
        if delta == 0:
            return

        modifiers = event.modifiers()
        over_header = event.position().x() < Metrics.TRACK_HEADER_WIDTH
        if modifiers & Qt.KeyboardModifier.ControlModifier and over_header:
            # ヘッダの上では全トラックの高さを変える タイムラインの上の Ctrl+ホイールは
            # 横の拡大なので、どちらを変えたいかをマウスの位置で分ける
            self.adjust_track_heights(HEIGHT_STEP if delta > 0 else -HEIGHT_STEP)
            event.accept()
            return
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
        if self._work_area.press(self._layout, position, event.modifiers()):
            return

        resizing = self._resize_band_at(position)
        if resizing is not None:
            self._resize_base = self._project
            self._drag = DragState(
                kind=DragKind.RESIZE_TRACK,
                resize_track=resizing.track.id,
                grab_y=position.y(),
                origin_height=resizing.track.height,
            )
            return

        if self._toggle_track_button(position):
            return

        if position.y() < Metrics.RULER_HEIGHT or position.x() < Metrics.TRACK_HEADER_WIDTH:
            self._drag = DragState(kind=DragKind.PLAYHEAD)
            self._scrub(position)
            return

        modifiers = event.modifiers()
        adding = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        ranged = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        hit = self._clip_at(position)
        if hit is None:
            # 空いた所は、再生ヘッドをそこへ動かし、ドラッグすれば囲んで選ぶ
            # Ctrl を押していれば今の選択に足す（押していなければ選び直し）
            base = self._selection if adding else ()
            if not adding:
                self.select(None)
            self._drag = DragState(
                kind=DragKind.MARQUEE,
                marquee_from=position,
                marquee_to=position,
                marquee_base=base,
            )
            self._scrub(position)
            return

        track_id, clip = hit
        if adding:
            # 足したクリップはそのまま掴んで動かせる 外したクリップは掴まない
            # （選んでいないものを動かすことになる） グループはまとめて足し引きする
            self._toggle(clip.id)
            if clip.id not in self._selection:
                return
        elif ranged and self._anchor is not None:
            self._select_range(self._anchor, clip.id)
            return
        elif clip.id in self._selection:
            # 選んだ何本かのうちの 1 本を掴んだ 選び直すと、まとめて動かせない
            self.set_selection((*self._selection, clip.id))
            self._anchor = clip.id
        else:
            # グループに入っていれば、仲間ごと選ぶ 掴んだ 1 本が主
            self.set_selection((*self._group_of(clip.id), clip.id))
            self._anchor = clip.id

        edge = self._edge_at(position, clip)
        frame = self._layout.frame_at(position.x())
        self._drag = DragState(
            kind=edge,
            clip_id=clip.id,
            origin_track=track_id,
            grab_offset=frame - clip.timeline_start,
            preview_start=clip.timeline_start,
            preview_track=track_id,
            # 何本も選んでいれば、動かすのもトリムもまとめて当てる
            group=edge in (DragKind.MOVE_CLIP, DragKind.TRIM_HEAD, DragKind.TRIM_TAIL)
            and len(self._selection) > 1,
        )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt の命名規約
        position = event.position().toPoint()
        if self._work_area.dragging:
            self._work_area.move(self._layout, position.x())
            self.update()
            return

        if self._drag.kind is DragKind.NONE:
            self._update_cursor(position)
            button = self._track_button_at(position)
            self.setToolTip(button[1] if button is not None else "")
            return

        if self._drag.kind is DragKind.PLAYHEAD:
            self._scrub(position)
            return

        if self._drag.kind is DragKind.RESIZE_TRACK:
            self._preview_height(position.y())
            return

        if self._drag.kind is DragKind.MARQUEE:
            self._update_marquee(position)
            return

        self._drag.moved = True
        frame = self._layout.frame_at(position.x())

        if self._drag.kind is DragKind.MOVE_CLIP:
            floor = self._group_floor() if self._drag.group else 0
            self._drag.preview_start = max(floor, frame - self._drag.grab_offset)
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
        if self._work_area.dragging:
            self._work_area.release(self._project.timeline.work_area)
            self.update()
            return
        drag, self._drag = self._drag, DragState()
        if drag.kind is DragKind.RESIZE_TRACK:
            self._finish_resize(drag)
            return
        if drag.kind is DragKind.MARQUEE:
            # 選択は動かしている間に決まっている 枠を消すだけ
            if len(self._selection) > 1:
                self.status_message.emit(f"{len(self._selection)} 本を選択")
            self.update()
            return
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

        if drag.kind is DragKind.MOVE_CLIP and drag.group:
            delta = drag.preview_start - clip.timeline_start
            movable = self._movable_selection()
            # 掴んだクリップ自身が動かせない（ロックしている）なら何もしない 動かすと、
            # 掴んだものはその場に残り、選んだほかのクリップだけが動く
            if drag.clip_id not in movable:
                return None
            tracks = self._track_delta(drag)
            if not delta and not tracks:
                return None
            return MoveClips(movable, delta, track_delta=tracks)

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
            if drag.group:
                return TrimClips(self._trimmable_selection(), head_delta=drag.preview_head_delta)
            return TrimClip(drag.clip_id, head_delta=drag.preview_head_delta)
        if drag.kind is DragKind.TRIM_TAIL and drag.preview_tail_delta:
            if drag.group:
                return TrimClips(self._trimmable_selection(), tail_delta=drag.preview_tail_delta)
            return TrimClip(drag.clip_id, tail_delta=drag.preview_tail_delta)
        return None

    def _track_delta(self, drag: DragState) -> int:
        """掴んだクリップが何本ぶんトラックを跨いだか 同じ種類の並びで数える"""
        if drag.preview_track is None or drag.preview_track == drag.origin_track:
            return 0
        timeline = self._project.timeline
        origin = timeline.find_track(drag.origin_track) if drag.origin_track else None
        target = timeline.find_track(drag.preview_track)
        if origin is None or target is None or origin.kind is not target.kind:
            return 0
        same = [t.id for t in timeline.tracks if t.kind is origin.kind]
        return same.index(target.id) - same.index(origin.id)

    def _preview_height(self, y: int) -> None:
        """ドラッグ中の高さを描画にだけ当てる 履歴には載せない"""
        base, track_id = self._resize_base, self._drag.resize_track
        if base is None or track_id is None:
            return
        height = self._drag.origin_height + (y - self._drag.grab_y)
        self._drag.moved = True
        self._project = SetTrackHeights(((track_id, height),)).apply(base)
        self.update()

    def _finish_resize(self, drag: DragState) -> None:
        base, self._resize_base = self._resize_base, None
        if base is None or drag.resize_track is None:
            return
        preview, self._project = self._project, base
        track = preview.timeline.find_track(drag.resize_track)
        if drag.moved and track is not None and track.height != drag.origin_height:
            command = SetTrackHeights(((drag.resize_track, track.height),))
            self._request([command], command.label)
        self.update()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802 - Qt の命名規約
        self.build_context_menu(event.pos()).exec(event.globalPos())

    def build_context_menu(self, position: QPoint) -> QMenu:
        """右クリックメニュー 表示と中身を分けてあるのはテストのため

        クリップの上ならそのクリップを選び直してから出す 選んでいた別のクリップが
        対象になると、見ていないものを消すことになる
        """
        menu = QMenu(self)
        hit = self._clip_at(position)
        count = ""
        if hit is not None:
            # 選んでいる何本かの上なら、その何本かが対象 選んでいない所なら、
            # そのクリップだけを選び直す
            if hit[1].id not in self._selection:
                # 左クリックと同じくグループは仲間ごと 1 本だけだと、削除や切り取りで束が裂ける
                self.set_selection((*self._group_of(hit[1].id), hit[1].id))
            if len(self._selection) > 1:
                count = f"（{len(self._selection)} 本）"
            _action(menu, "再生ヘッドで分割", self.split_at_playhead)
            menu.addSeparator()
            _action(menu, f"コピー{count}", self.copy_selected)
            _action(menu, f"切り取り{count}", self.cut_selected)
        paste = _action(menu, "貼り付け（再生ヘッドの位置）", self.paste_at_playhead)
        paste.setEnabled(self._clipboard is not None)
        if hit is not None:
            menu.addSeparator()
            _action(menu, f"削除{count}", self.delete_selected)
            _action(menu, f"削除して詰める{count}", lambda: self.delete_selected(ripple=True))
            menu.addSeparator()
            group = _action(menu, "グループ化", self.group_selected)
            group.setEnabled(len(self._selection) > 1)
            ungroup = _action(menu, "グループ解除", self.ungroup_selected)
            ungroup.setEnabled(self._selection_has_group())
            if hit[1].scene_id is not None:
                scene_id = hit[1].scene_id
                _action(
                    menu,
                    "シーンを開く",
                    functools.partial(self.scene_open_requested.emit, str(scene_id)),
                )

        band = (
            self._layout.band_at(self._project.timeline, position.y())
            if position.y() >= Metrics.RULER_HEIGHT
            else None
        )
        if band is not None:
            menu.addSeparator()
            track = band.track
            name = track.name or "トラック"
            for attribute, label in _TRACK_TOGGLES:
                toggle = _action(
                    menu, f"{name} を{label}", functools.partial(self._flip, track.id, attribute)
                )
                toggle.setCheckable(True)
                toggle.setChecked(bool(getattr(track, attribute)))
            _action(menu, f"{name} の高さを戻す", functools.partial(self._reset_height, track.id))
        self._work_area.add_menu_actions(
            menu, self._layout, position, self._project.timeline.work_area
        )
        return menu

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt の命名規約
        hit = self._clip_at(event.position().toPoint())
        if hit is not None and hit[1].scene_id is not None:
            self.scene_open_requested.emit(str(hit[1].scene_id))
            return
        super().mouseDoubleClickEvent(event)

    def group_selected(self) -> bool:
        """選んでいるクリップを束ねる 2 本以上要る"""
        if len(self._selection) < 2:
            self.status_message.emit("グループ化するには 2 本以上選んでください")
            return False
        command = GroupClips(self._selection)
        self._request([command], command.label)
        return True

    def ungroup_selected(self) -> bool:
        if not self._selection_has_group():
            self.status_message.emit("グループに入っているクリップを選んでください")
            return False
        command = UngroupClips(self._selection)
        self._request([command], command.label)
        return True

    def _selection_has_group(self) -> bool:
        timeline = self._project.timeline
        for clip_id in self._selection:
            located = timeline.locate_clip(clip_id)
            if located is not None and located[1].group_id is not None:
                return True
        return False

    def _reset_height(self, track_id: TrackId) -> None:
        command = SetTrackHeights(((track_id, DEFAULT_TRACK_HEIGHT),))
        self._request([command], command.label)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt の命名規約
        key = event.key()
        rippled = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if key == Qt.Key.Key_S:
            self.split_at_playhead()
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected(ripple=rippled)
        elif key == Qt.Key.Key_Escape:
            self.select(None)
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

    def toggle_selected_track(self, attribute: str) -> bool:
        """選んでいるクリップのトラックの ``muted`` ``solo`` ``locked`` を切り替える

        ヘッダのボタンは描いた矩形でフォーカスが来ないので、キーボードからはこちらを
        使う 選択が無ければ何もしない（どのトラックか分からないまま切り替えない）
        """
        primary = self.selected_clip
        if primary is None:
            return False
        located = self._project.timeline.locate_clip(primary)
        if located is None:
            return False
        track = located[0]
        command = SetTrackState(track.id, **{attribute: not getattr(track, attribute)})
        self._request([command], command.label)
        return True

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
        if not self._selection:
            return
        command: Command = (
            RemoveClips(self._selection, ripple=ripple)
            if len(self._selection) > 1
            else RemoveClip(self._selection[0], ripple=ripple)
        )
        self._request([command], command.label)

    @property
    def has_clipboard(self) -> bool:
        return self._clipboard is not None

    def copy_selected(self) -> bool:
        """選んでいるクリップをコピーする リンクした相手も一緒に入る"""
        if not self._selection:
            return False
        content = copy_clips(self._project, self._selection)
        if not content.clips:
            return False
        self._clipboard = content
        self.status_message.emit(f"{len(content.clips)} 本をコピーした")
        return True

    def cut_selected(self) -> bool:
        """コピーしてから消す 隙間は詰めない（詰めたければ「削除して詰める」）"""
        if not self.copy_selected() or self._clipboard is None:
            return False
        self._request(cut_commands(self._project, self._clipboard), "切り取り")
        return True

    def paste_at_playhead(self) -> bool:
        """再生ヘッドの位置へ貼り付けて、貼ったクリップを選ぶ"""
        if self._clipboard is None:
            self.status_message.emit("コピーしたクリップがありません")
            return False
        try:
            commands = paste_commands(self._project, self._clipboard, self._playhead)
        except ValueError as exc:
            self.status_message.emit(str(exc))
            return False
        self._request(commands, "貼り付け")
        # 実行は受け取った側で済んでいる 貼ったものを全部選んでおくと、そのまま
        # まとめて動かせる 1 本だけ選ぶと、残りを探して選び直すことになる
        pasted = [c.clip.id for c in commands if isinstance(c, AddClip)]
        landed = [c for c in pasted if self._project.timeline.locate_clip(c) is not None]
        if landed:
            self.set_selection(landed)
            self._anchor = landed[-1]
        return True

    def adjust_track_heights(self, delta: int) -> None:
        """全トラックの高さを ``delta`` 画素ずつ変える

        続けて変えたぶん（:data:`HEIGHT_MERGE_SECONDS` 以内）は取り消しの 1 段に
        まとめてもらう ホイールを 10 段回して、戻すのに 10 回取り消すのは重い
        """
        tracks = self._project.timeline.tracks
        wanted = [min(max(t.height + delta, MIN_TRACK_HEIGHT), MAX_TRACK_HEIGHT) for t in tracks]
        if wanted == [t.height for t in tracks]:
            # 上限や下限に張り付いて変わらないときは、続けた操作をそこで区切る
            # 区切らないと、張り付いたまま回したあとすぐ反対へ回したぶんが、
            # 張り付く前の段へまとまり、1 回の取り消しでそこまで戻る
            self._last_height_change = -HEIGHT_MERGE_SECONDS
            return
        now = time.monotonic()
        continued = now - self._last_height_change < HEIGHT_MERGE_SECONDS
        self._last_height_change = now
        command = SetTrackHeights(tuple((t.id, t.height + delta) for t in tracks))
        signal = self.commands_continued if continued else self.commands_requested
        signal.emit([command], command.label)

    def reset_track_heights(self) -> None:
        tracks = self._project.timeline.tracks
        if tracks:
            command = SetTrackHeights(tuple((t.id, DEFAULT_TRACK_HEIGHT) for t in tracks))
            self._request([command], command.label)

    def _flip(self, track_id: TrackId, attribute: str) -> None:
        track = self._project.timeline.find_track(track_id)
        if track is not None:
            command = SetTrackState(track_id, **{attribute: not getattr(track, attribute)})
            self._request([command], command.label)

    def clear_work_area(self) -> bool:
        """書き出し範囲を解除する 編集メニューから 範囲が無ければ知らせて偽を返す"""
        if self._work_area.clear(self._project.timeline.work_area):
            return True
        self.status_message.emit("書き出し範囲は指定されていません")
        return False

    def _request(self, commands: list[Command], label: str) -> None:
        if commands:
            self.commands_requested.emit(commands, label)

    # --- 補助 ---

    def _toggle(self, clip_id: ClipId) -> None:
        """Ctrl+クリック 選んでいれば外し、いなければ足す

        起点は :meth:`set_selection` が残った最後の 1 本へ移す 外したクリップを
        起点にすると、次の Shift+クリックが選んでいないクリップから範囲を取る
        """
        # 足す順はタイムラインの並びを保つ 集合から並べると選んだ順が毎回変わる
        members = self._group_of(clip_id)
        if clip_id in self._selection:
            removed = set(members)
            self.set_selection(c for c in self._selection if c not in removed)
        else:
            self.set_selection((*self._selection, *(c for c in members if c != clip_id), clip_id))

    def _select_range(self, anchor: ClipId, target: ClipId) -> None:
        """Shift+クリック 起点と今のクリップを両隅にした範囲をまとめて選ぶ

        縦は 2 本のトラックの間、横は 2 本の端から端まで 同じトラックなら、その間に
        並ぶクリップが全部入る 起点は動かさないので、Shift を押したまま別の
        クリップを押せば範囲を広げ直せる

        「間」は画面に見えている並びで決める 映像トラックは下から積むので、
        モデルの並び（``timeline.tracks``）とは順が違う
        """
        timeline = self._project.timeline
        first, last = timeline.locate_clip(anchor), timeline.locate_clip(target)
        if first is None or last is None:
            self.select(target)
            return
        shown = [band.track for band in self._layout.bands(timeline)]
        order = [track.id for track in shown]
        top, bottom = sorted((order.index(first[0].id), order.index(last[0].id)))
        start = min(first[1].timeline_start, last[1].timeline_start)
        end = max(first[1].timeline_end, last[1].timeline_end)
        # グループは仲間ごと入れる 一部だけ選ぶと、そのまま動かしたときに束が裂ける
        chosen = [
            member
            for track in shown[top : bottom + 1]
            for clip in track.clips
            if clip.overlaps(start, end)
            for member in self._group_of(clip.id)
        ]
        self.set_selection((*chosen, *self._group_of(target), target))
        self._anchor = anchor

    def _update_marquee(self, position: QPoint) -> None:
        origin = self._drag.marquee_from
        if origin is None:
            return
        if not self._drag.moved:
            if (position - origin).manhattanLength() < MARQUEE_THRESHOLD:
                return
            self._drag.moved = True
        self._drag.marquee_to = position
        rect = QRect(origin, position).normalized()
        caught = [
            member for clip_id in self._clips_in_rect(rect) for member in self._group_of(clip_id)
        ]
        self.set_selection((*self._drag.marquee_base, *caught))
        self.update()

    def _group_of(self, clip_id: ClipId) -> tuple[ClipId, ...]:
        """グループの仲間（自分を含む） グループに入っていなければ自分だけ"""
        timeline = self._project.timeline
        located = timeline.locate_clip(clip_id)
        if located is None or located[1].group_id is None:
            return (clip_id,)
        return tuple(clip.id for _, clip in timeline.grouped_clips(located[1].group_id))

    def _clips_in_rect(self, rect: QRect) -> list[ClipId]:
        """枠に少しでも掛かったクリップ 全部を収めなくても選べる方が囲みやすい

        ここの上下は画面の画素（下が正、Qt の座標） 枠もトラックの帯も同じ画面の
        座標で持っているので、そのまま比べてよい 映像の中の位置（上が正）とは
        別のもので、混ぜて計算しない
        """
        first = self._layout.frame_at(max(rect.left(), Metrics.TRACK_HEADER_WIDTH))
        last = self._layout.frame_at(rect.right())
        return [
            clip.id
            for band in self._layout.bands(self._project.timeline)
            if band.bottom > rect.top() and band.top < rect.bottom()
            for clip in clips_in_range(band.track, first, last)
        ]

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
        """マウスの下のクリップ マウスが動くたびに呼ばれる

        トラックを縦位置で決めてから、そのトラックの中を二分探索で探す 見えている
        クリップを全部舐めると、全体表示の 1 万本でマウスを動かすだけで重くなる

        探す範囲は前後 1 画素ぶんのフレーム 全体表示では 1 画素が数十フレームに
        あたるので、フレームの前後 1 つだけを見ると画素の中のクリップを取りこぼす
        1 画素に満たないクリップは矩形が丸めで隣の画素へずれるので、矩形で当たらな
        ければ、その画素の真ん中のフレームを含むクリップを選ぶ
        """
        if position.x() < Metrics.TRACK_HEADER_WIDTH:
            return None
        band = self._layout.band_at(self._project.timeline, position.y())
        if band is None or not (band.top < position.y() < band.bottom - 2):
            return None
        first = self._layout.frame_at(position.x() - 1)
        last = self._layout.frame_at(position.x() + 1)
        candidates = clips_in_range(band.track, first, last)
        for clip in candidates:
            rect = clip_rect_for(clip, band, self._layout, self.width())
            if rect is not None and rect.contains(position):
                return band.track.id, clip
        middle = int(self._layout.x_to_frame(position.x() + 0.5))
        for clip in candidates:
            if clip.contains(middle):
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

    def _resize_band_at(self, position: QPoint) -> TrackBand | None:
        """ヘッダの中で、トラックの下の境目の上にいればそのトラック

        ヘッダの中に限る タイムラインの側まで広げると、クリップの下端を掴んだ
        つもりが高さの変更になる
        """
        if position.x() >= Metrics.TRACK_HEADER_WIDTH or position.y() < Metrics.RULER_HEIGHT:
            return None
        for band in self._layout.bands(self._project.timeline):
            if abs(position.y() - band.bottom) <= RESIZE_GRAB:
                return band
        return None

    def _update_cursor(self, position: QPoint) -> None:
        if self._resize_band_at(position) is not None:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            return
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


def _action(menu: QMenu, text: str, slot: Callable[[], object]) -> QAction:
    """メニューに項目を足す ``triggered`` の引数（押されたかどうか）は捨てる"""
    action = menu.addAction(text)
    action.triggered.connect(lambda _checked=False: slot())
    return action
