"""メインウィンドウ。各パネルを組み立て、コマンドの実行を一手に引き受ける。

UI のどこから来た操作も、必ず :meth:`MainWindow.execute` を通って
:class:`~novaedit.core.commands.Document` に入る。AI エージェントも同じ入口を
使う予定なので、ここが増えないようにしておく。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from novaedit.core.commands import Command, Document, insert_media
from novaedit.core.io import SUFFIX, ProjectFileError, load_project, save_project
from novaedit.core.model import MediaId, Project, ProjectSettings
from novaedit.engine.cache import MediaAnalyzer
from novaedit.engine.decode import ProbeError, probe_media
from novaedit.ui.export_dialog import ExportDialog
from novaedit.ui.media_pool import MediaPoolWidget
from novaedit.ui.playback import PlaybackController
from novaedit.ui.preview import PreviewWidget
from novaedit.ui.theme import Colors
from novaedit.ui.timeline import TimelineView
from novaedit.ui.transport import TransportBar

__all__ = ["MainWindow"]

#: 解析の完了を画面へ反映する間隔（ミリ秒）。
#: 解析はワーカースレッドで終わるので、その通知を待って毎回描き直すのではなく、
#: まとめて一定間隔で描き直す。素材を 100 本入れたときに描画で埋もれないように。
ANALYSIS_REFRESH_MS = 250


class MainWindow(QMainWindow):
    """編集画面。"""

    project_changed = Signal(object)

    def __init__(self, project: Project | None = None) -> None:
        super().__init__()
        self.setWindowTitle("NovaEdit")
        self.resize(1440, 900)

        self._document = Document(project if project is not None else Project.create())
        self._path: Path | None = None
        self._analyzer = MediaAnalyzer(
            sample_rate=self._document.project.settings.sample_rate,
            channels=self._document.project.settings.channels,
        )
        self._analysis_dirty = False

        self._build_widgets()
        self._build_menus()
        self._connect()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(ANALYSIS_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._flush_analysis)
        self._refresh_timer.start()

        self._update_title()

    # --- 組み立て ---

    def _build_widgets(self) -> None:
        project = self._document.project

        self._preview = PreviewWidget(project, self)
        self._transport = TransportBar(project.rate, self)
        self._timeline = TimelineView(project, self._analyzer, self)
        self._media_pool = MediaPoolWidget(project, self)
        self._playback = PlaybackController(project, self)

        viewer = QWidget(self)
        viewer_layout = QVBoxLayout(viewer)
        viewer_layout.setContentsMargins(0, 0, 0, 0)
        viewer_layout.setSpacing(0)
        viewer_layout.addWidget(self._preview, 1)
        viewer_layout.addWidget(self._transport)
        viewer.setStyleSheet(f"background-color: {Colors.VIEWER_BACKGROUND.name()};")
        self.setCentralWidget(viewer)

        pool_dock = QDockWidget("メディア", self)
        pool_dock.setWidget(self._media_pool)
        pool_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, pool_dock)

        timeline_dock = QDockWidget("タイムライン", self)
        timeline_dock.setWidget(self._timeline)
        timeline_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, timeline_dock)
        self.resizeDocks([timeline_dock], [320], Qt.Orientation.Vertical)

        self.statusBar().showMessage("素材を読み込んでください")

    def _build_menus(self) -> None:
        file_menu = self._menu("ファイル")
        self._add(file_menu, "新規", QKeySequence.StandardKey.New, self.new_project)
        self._add(file_menu, "開く…", QKeySequence.StandardKey.Open, self.open_project)
        self._add(file_menu, "保存", QKeySequence.StandardKey.Save, self.save_project)
        self._add(file_menu, "名前を付けて保存…", QKeySequence("Ctrl+Shift+S"), self.save_as)
        file_menu.addSeparator()
        self._add(file_menu, "素材を読み込む…", QKeySequence("Ctrl+I"), self._import_dialog)
        self._add(file_menu, "書き出し…", QKeySequence("Ctrl+E"), self.export)
        file_menu.addSeparator()
        self._add(file_menu, "終了", QKeySequence.StandardKey.Quit, self.close)

        edit_menu = self._menu("編集")
        self._undo_action = self._add(
            edit_menu, "元に戻す", QKeySequence.StandardKey.Undo, self.undo
        )
        self._redo_action = self._add(
            edit_menu, "やり直す", QKeySequence.StandardKey.Redo, self.redo
        )
        edit_menu.addSeparator()
        self._add(
            edit_menu, "再生ヘッドで分割", QKeySequence("S"), self._timeline.split_at_playhead
        )
        self._add(edit_menu, "削除", QKeySequence("Del"), lambda: self._timeline.delete_selected())
        self._add(
            edit_menu,
            "削除して詰める",
            QKeySequence("Shift+Del"),
            lambda: self._timeline.delete_selected(ripple=True),
        )

        view_menu = self._menu("表示")
        self._add(
            view_menu, "拡大", QKeySequence.StandardKey.ZoomIn, lambda: self._timeline.zoom(1.25)
        )
        self._add(
            view_menu, "縮小", QKeySequence.StandardKey.ZoomOut, lambda: self._timeline.zoom(0.8)
        )
        self._add(view_menu, "全体を表示", QKeySequence("Shift+Z"), self._timeline.zoom_to_fit)

        playback_menu = self._menu("再生")
        self._add(playback_menu, "再生 / 停止", QKeySequence("Space"), self._playback.toggle)

        self._update_history_actions()

    def _add(
        self,
        menu: QMenu,
        text: str,
        shortcut: QKeySequence | QKeySequence.StandardKey,
        slot: Callable[[], object],
    ) -> QAction:
        action = QAction(text, self)
        action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _menu(self, title: str) -> QMenu:
        """メニューを 1 つ作る。``addMenu`` は None を返しうるので、ここで確定させる。"""
        menu = self.menuBar().addMenu(title)
        if menu is None:  # pragma: no cover - Qt が None を返すのは異常系のみ
            raise RuntimeError(f"メニューを作れない: {title}")
        return menu

    def _connect(self) -> None:
        self._timeline.commands_requested.connect(self.execute_all)
        self._timeline.playhead_moved.connect(self._on_playhead_moved)

        self._media_pool.import_requested.connect(self.import_media)
        self._media_pool.insert_requested.connect(self._insert_media_by_id)

        self._transport.play_toggled.connect(self._playback.toggle)
        self._transport.step_requested.connect(
            lambda delta: self._seek(self._timeline.playhead + delta)
        )
        self._transport.jump_requested.connect(self._seek)
        self._transport.quality_changed.connect(self._preview.set_quality)

        self._playback.frame_changed.connect(self._on_playback_frame)
        self._playback.state_changed.connect(self._transport.set_playing)
        self._playback.failed.connect(lambda message: self.statusBar().showMessage(message, 5000))

    # --- コマンドの実行 ---

    def execute(self, command: Command) -> None:
        """コマンドを 1 つ実行して、画面を更新する。

        失敗しても落とさず、状況をステータスバーへ出す。編集操作は思いどおりに
        いかないことが普通にあり、そのたびにダイアログが出ると邪魔になる。
        """
        try:
            self._document.execute(command)
        except (ValueError, KeyError) as exc:
            self.statusBar().showMessage(str(exc), 4000)
            return
        self._on_project_changed()

    def execute_all(self, commands: list[Command], label: str) -> None:
        """複数のコマンドを 1 回の Undo で戻せるようにまとめて実行する。"""
        if not commands:
            return
        try:
            with self._document.checkpoint(label):
                for command in commands:
                    self._document.execute(command)
        except (ValueError, KeyError) as exc:
            self.statusBar().showMessage(str(exc), 4000)
        self._on_project_changed()

    def undo(self) -> None:
        self._document.undo()
        self._on_project_changed()

    def redo(self) -> None:
        self._document.redo()
        self._on_project_changed()

    def _on_project_changed(self) -> None:
        project = self._document.project
        self._timeline.set_project(project)
        self._media_pool.set_project(project)
        self._preview.set_project(project)
        self._playback.set_project(project)
        self._transport.set_rate(project.rate)
        self._transport.set_duration(project.duration)
        self._update_history_actions()
        self._update_title()
        self.project_changed.emit(project)

    def _update_history_actions(self) -> None:
        self._undo_action.setEnabled(self._document.can_undo)
        self._redo_action.setEnabled(self._document.can_redo)
        undo_label = self._document.undo_label
        self._undo_action.setText(f"元に戻す: {undo_label}" if undo_label else "元に戻す")
        redo_label = self._document.redo_label
        self._redo_action.setText(f"やり直す: {redo_label}" if redo_label else "やり直す")

    def _update_title(self) -> None:
        name = self._path.name if self._path is not None else self._document.project.name
        self.setWindowTitle(f"{name} — NovaEdit")

    # --- 素材 ---

    def _import_dialog(self) -> None:
        from novaedit.ui.media_pool import MEDIA_FILTER

        names, _ = QFileDialog.getOpenFileNames(self, "素材を読み込む", "", MEDIA_FILTER)
        if names:
            self.import_media([Path(name) for name in names])

    def import_media(self, paths: list[Path]) -> None:
        """素材を読み込んでタイムラインへ置く。

        複数選ばれた場合もまとめて 1 回の Undo で戻せるようにする。
        10 本読み込んで 10 回取り消す、という操作は誰も望まない。
        """
        commands: list[Command] = []
        failures: list[str] = []
        project = self._document.project

        for path in paths:
            try:
                media = probe_media(path)
            except ProbeError as exc:
                failures.append(str(exc))
                continue
            batch = insert_media(project, media, at_frame=None)
            for command in batch:
                project = command.apply(project)
            commands.extend(batch)
            self._analyzer.request(media, on_ready=self._on_analysis_ready)

        if commands:
            self.execute_all(commands, f"素材を読み込み: {len(paths)} 件")
        if failures:
            self.statusBar().showMessage(failures[0], 5000)
        elif commands:
            self.statusBar().showMessage(f"{len(paths)} 件を読み込んだ", 3000)

    def _insert_media_by_id(self, media_id: str) -> None:
        project = self._document.project
        media = project.find_media(MediaId(media_id))
        if media is None:
            return
        self.execute_all(insert_media(project, media), f"配置: {media.name}")

    def _on_analysis_ready(self, media_id: MediaId) -> None:
        # ワーカースレッドから呼ばれる。ここでウィジェットに触ると Qt が落ちるので、
        # 印だけ付けてメインスレッドのタイマーに描き直させる。
        del media_id
        self._analysis_dirty = True

    def _flush_analysis(self) -> None:
        if not self._analysis_dirty:
            return
        self._analysis_dirty = False
        self._timeline.update()

    # --- 再生とシーク ---

    def _seek(self, frame: int) -> None:
        frame = max(0, min(frame, self._document.project.duration))
        self._timeline.set_playhead(frame)
        self._preview.set_frame(frame)
        self._transport.set_frame(frame)
        self._playback.set_frame(frame)

    def _on_playhead_moved(self, frame: int) -> None:
        self._preview.set_frame(frame)
        self._transport.set_frame(frame)
        self._playback.set_frame(frame)

    def _on_playback_frame(self, frame: int) -> None:
        self._timeline.set_playhead(frame)
        self._preview.set_frame(frame)
        self._transport.set_frame(frame)

    # --- ファイル ---

    def new_project(self) -> None:
        self._playback.stop()
        self._document.reset(Project.create(ProjectSettings()))
        self._path = None
        self._on_project_changed()
        self._seek(0)

    def open_project(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "プロジェクトを開く", "", f"NovaEdit プロジェクト (*{SUFFIX})"
        )
        if not name:
            return
        try:
            project = load_project(Path(name))
        except ProjectFileError as exc:
            QMessageBox.warning(self, "開けない", str(exc))
            return

        self._playback.stop()
        self._document.reset(project)
        self._path = Path(name)
        self._on_project_changed()
        self._seek(0)
        for media in project.media:
            self._analyzer.request(media, on_ready=self._on_analysis_ready)

    def save_project(self) -> None:
        if self._path is None:
            self.save_as()
            return
        save_project(self._document.project, self._path)
        self.statusBar().showMessage(f"保存した: {self._path}", 3000)

    def save_as(self) -> None:
        suggested = self._path or Path(f"{self._document.project.name}{SUFFIX}")
        name, _ = QFileDialog.getSaveFileName(
            self, "名前を付けて保存", str(suggested), f"NovaEdit プロジェクト (*{SUFFIX})"
        )
        if not name:
            return
        self._path = Path(name)
        self.save_project()
        self._update_title()

    def export(self) -> None:
        self._playback.stop()
        ExportDialog(self._document.project, self).exec()

    # --- 終了 ---

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt の命名規約
        # 解放の順番が大事。GL 資源はコンテキストが生きているうちに、
        # 再生スレッドはウィジェットが消える前に畳む。
        self._refresh_timer.stop()
        self._playback.close()
        self._analyzer.close()
        self._preview.shutdown()
        super().closeEvent(event)
