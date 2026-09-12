"""メディアプール 読み込んだ素材の一覧"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from kumiki.core.model import MediaId, MediaItem, Project
from kumiki.core.timebase import FrameRate, format_timecode, seconds_to_frame

__all__ = ["MediaPoolWidget"]

#: 読み込みダイアログのフィルタ
MEDIA_FILTER = (
    "メディア (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wav *.mp3 *.aac *.flac *.m4a "
    "*.png *.jpg *.jpeg *.bmp *.webp);;すべてのファイル (*)"
)


class MediaPoolWidget(QWidget):
    """素材の一覧と、読み込み・タイムラインへの配置"""

    #: 読み込みが要求された 引数はパスの一覧
    import_requested = Signal(list)
    #: 素材をタイムラインへ置くよう要求された 引数は素材 ID
    insert_requested = Signal(str)
    #: 字幕を起こすよう要求された 引数は素材 ID
    transcribe_requested = Signal(str)
    #: メディアプールから外すよう要求された 引数は素材 ID
    remove_requested = Signal(str)

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project = project

        self._list = QListWidget(self)
        self._list.itemDoubleClicked.connect(self._on_double_click)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_menu)

        import_button = QPushButton("読み込み…", self)
        import_button.clicked.connect(self._choose_files)
        self._insert_button = QPushButton("タイムラインへ", self)
        self._insert_button.clicked.connect(self._insert_selected)
        self._insert_button.setEnabled(False)
        self._list.itemSelectionChanged.connect(
            lambda: self._insert_button.setEnabled(bool(self._list.selectedItems()))
        )

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(import_button)
        buttons.addWidget(self._insert_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addLayout(buttons)
        layout.addWidget(self._list)

        self.setAcceptDrops(True)
        self.set_project(project)

    def set_project(self, project: Project) -> None:
        """一覧を作り直す

        選択は素材 ID で復元する 行番号で覚えると、素材を消したときに
        別のものが選ばれる
        """
        selected = self.selected_media_id()
        self._project = project

        self._list.clear()
        for media in project.media:
            item = QListWidgetItem(_describe(media, project.rate))
            item.setData(Qt.ItemDataRole.UserRole, str(media.id))
            item.setToolTip(str(media.path))
            self._list.addItem(item)
            if media.id == selected:
                self._list.setCurrentItem(item)

    def selected_media_id(self) -> MediaId | None:
        items = self._list.selectedItems()
        if not items:
            return None
        return MediaId(str(items[0].data(Qt.ItemDataRole.UserRole)))

    # --- 入力 ---

    def dragEnterEvent(self, event: object) -> None:  # noqa: N802 - Qt の命名規約
        mime = getattr(event, "mimeData", None)
        if mime is not None and mime().hasUrls():
            event.acceptProposedAction()  # type: ignore[attr-defined]

    def dropEvent(self, event: object) -> None:  # noqa: N802 - Qt の命名規約
        mime = getattr(event, "mimeData", None)
        if mime is None:
            return
        paths = [Path(url.toLocalFile()) for url in mime().urls() if url.isLocalFile()]
        if paths:
            self.import_requested.emit(paths)
            event.acceptProposedAction()  # type: ignore[attr-defined]

    def _choose_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "素材を読み込む", "", MEDIA_FILTER)
        if names:
            self.import_requested.emit([Path(name) for name in names])

    def _insert_selected(self) -> None:
        media_id = self.selected_media_id()
        if media_id is not None:
            self.insert_requested.emit(str(media_id))

    def _on_double_click(self, item: QListWidgetItem) -> None:
        self.insert_requested.emit(str(item.data(Qt.ItemDataRole.UserRole)))

    def _show_menu(self, position: QPoint) -> None:
        item = self._list.itemAt(position)
        if item is None:
            return
        # 右クリックした行を選び直す 選んでいた別の行が対象になると、
        # 「消したつもりのない素材が消えた」になる
        self._list.setCurrentItem(item)
        menu = self.build_menu(MediaId(str(item.data(Qt.ItemDataRole.UserRole))))
        menu.exec(self._list.viewport().mapToGlobal(position))

    def build_menu(self, media_id: MediaId) -> QMenu:
        """素材 1 つに対する右クリックメニュー 表示と中身を分けてあるのはテストのため"""
        menu = QMenu(self)
        # triggered は押されたかどうか（bool）を渡してくる PySide6 は受け取れない
        # 引数を捨てて呼ぶが、タイムライン側と同じく明示的に受けて捨てる形にそろえる
        place = menu.addAction("タイムラインへ置く")
        place.triggered.connect(lambda _checked=False: self.insert_requested.emit(str(media_id)))
        transcribe = menu.addAction("字幕を起こす…")
        transcribe.triggered.connect(
            lambda _checked=False: self.transcribe_requested.emit(str(media_id))
        )
        media = self._project.find_media(media_id)
        transcribe.setEnabled(media is not None and media.has_audio)
        reveal = menu.addAction("ファイルの場所を開く")
        reveal.triggered.connect(lambda _checked=False: self._reveal(media_id))
        menu.addSeparator()
        remove = menu.addAction("プールから外す")
        remove.triggered.connect(lambda _checked=False: self.remove_requested.emit(str(media_id)))
        return menu

    def _reveal(self, media_id: MediaId) -> None:
        media = self._project.find_media(media_id)
        if media is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(media.path.parent)))


def _describe(media: MediaItem, rate: FrameRate) -> str:
    """一覧に出す 1 行 長さと中身の種類が一目で分かるようにする"""
    parts = [media.name]
    if media.is_still:
        parts.append("静止画")
    elif media.duration > 0:
        parts.append(format_timecode(seconds_to_frame(media.duration, rate), rate))

    kinds = []
    if media.has_video:
        stream = media.video_streams[0]
        width, height = stream.display_size
        kinds.append(f"{width}x{height}")
    if media.has_audio:
        count = len(media.audio_streams)
        kinds.append(f"音声{count}本" if count > 1 else "音声")
    if kinds:
        parts.append(" / ".join(kinds))

    return "   ".join(parts)
