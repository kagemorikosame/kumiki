"""テンプレートの棚 AviUtl のエイリアスと YMM4 のアイテムテンプレートを並べる

配布されている字幕デザインは、見本の文字が入ったテキストオブジェクトとして
配られている 使い方は 2 通りあり、両方できるようにしてある

* **タイムラインへ置く** — 見本の文字ごと置く 新しくテロップを作るとき
* **選択中のクリップに適用** — 今の文字と長さを残して、見た目だけ着せ替える
  すでに打ってある字幕にデザインを当てるとき こちらが本命

下絵は**文字と図形だけ**を描いたもの 縁取りやグラデーションは GPU のエフェクト
として積まれるので、ここには出ない 出せない部分を出せているように見せると、
選ぶときの判断を誤らせる
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.catalog import (
    TemplateCatalog,
    TemplateEntry,
    TemplateError,
    template_catalog,
)
from sashimono.compat.mapped import MappedObject
from sashimono.ui.theme import Colors

__all__ = ["TemplateDialog"]

#: 下絵の大きさ 一覧の横に置くので、縦横比だけ合わせた小さめのもの
_PREVIEW = (384, 216)

#: 下絵を描くときの実寸 配布物は 1080p を前提にしている
_CANVAS = (1920, 1080)


class TemplateDialog(QDialog):
    """テンプレートを選んで、置くか着せるかを決める"""

    def __init__(
        self, catalog: TemplateCatalog | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("テンプレート")
        self.resize(820, 520)
        self._catalog = catalog if catalog is not None else template_catalog()
        self._report = CompatibilityReport()
        self._loaded: list[MappedObject] = []

        self._tree = QTreeWidget(self)
        self._tree.setHeaderLabels(["名前"])
        self._tree.setColumnCount(1)
        self._tree.currentItemChanged.connect(lambda *_: self._on_selected())

        self._preview = QLabel(self)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setMinimumSize(*_PREVIEW)
        self._preview.setStyleSheet(f"background: {Colors.VIEWER_BACKGROUND.name()};")

        self._detail = QLabel(self)
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet(f"color: {Colors.TEXT_MUTED.name()};")

        self._notes = QListWidget(self)
        self._notes.setMaximumHeight(90)

        self._place_button = QPushButton("タイムラインへ置く", self)
        self._restyle_button = QPushButton("選択中のクリップに適用", self)
        self._place_button.clicked.connect(lambda: self._finish("place"))
        self._restyle_button.clicked.connect(lambda: self._finish("restyle"))

        rescan = QPushButton("読み直す", self)
        rescan.clicked.connect(self.refresh)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close is not None:
            close.setText("閉じる")
        buttons.addButton(rescan, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self._restyle_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self._place_button, QDialogButtonBox.ButtonRole.AcceptRole)

        side = QVBoxLayout()
        side.addWidget(self._preview)
        side.addWidget(self._detail)
        side.addWidget(self._notes)
        side.addStretch(1)

        columns = QHBoxLayout()
        columns.addWidget(self._tree, 1)
        columns.addLayout(side, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(columns, 1)
        layout.addWidget(buttons)

        #: 選ばれた結果 ``("place" | "restyle", 写した結果)``
        self.choice: tuple[str, list[MappedObject]] | None = None
        #: 選ばれたテンプレートの置き場 書かれた素材のパスに無いとき、ここで探す
        self.origin: Path | None = None
        self.refresh()

    # --- 一覧 ---

    def refresh(self) -> None:
        """棚を読み直して並べ直す"""
        from sashimono.compat.catalog import default_template_roots

        self._catalog.scan(default_template_roots())
        self._tree.clear()

        groups: dict[str, QTreeWidgetItem] = {}
        for entry in self._catalog.all():
            label = f"{entry.folder}（{'YMM4' if entry.source == 'ymm4' else 'AviUtl'}）"
            group = groups.get(label)
            if group is None:
                group = QTreeWidgetItem([label])
                groups[label] = group
                self._tree.addTopLevelItem(group)
            node = QTreeWidgetItem([entry.name])
            node.setData(0, Qt.ItemDataRole.UserRole, entry)
            group.addChild(node)

        self._tree.expandAll()
        if not groups:
            self._detail.setText(
                "テンプレートが見つかりません"
                "AviUtl2 の Alias フォルダか、YMM4 の ItemTemplate フォルダを探します"
            )
        self._on_selected()

    def _selected_entry(self) -> TemplateEntry | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        return data if isinstance(data, TemplateEntry) else None

    def _on_selected(self) -> None:
        entry = self._selected_entry()
        self._loaded = []
        self._notes.clear()
        self._preview.setPixmap(QPixmap())
        self._place_button.setEnabled(entry is not None)
        self._restyle_button.setEnabled(False)
        if entry is None:
            return

        self._report.clear()
        try:
            self._loaded = entry.load(report=self._report)
        except TemplateError as exc:
            self._detail.setText(f"読み込めません: {exc}")
            self._place_button.setEnabled(False)
            return

        self._detail.setText(self._describe(entry))
        # 絵を持たないテンプレート（YMM4 のアニメーション効果など）は置けない
        # 着せることしかできないので、そちらだけを押せるようにする
        self._place_button.setEnabled(any(item.has_picture for item in self._loaded))
        self._restyle_button.setEnabled(
            any(
                inner.clip.source and inner.clip.source.kind == "text"
                for inner in self._walk_loaded()
            )
            or self._is_effects_only()
        )
        self._notes.addItems(self._report.lines())
        self._show_preview()

    def _walk_loaded(self) -> list[MappedObject]:
        """まとめた中身（YMM4 の合成するグループ）も含めた全部 文字は中にあることがある"""
        return [inner for item in self._loaded for inner in item.walk()]

    def _is_effects_only(self) -> bool:
        return bool(self._loaded) and not any(item.has_picture for item in self._loaded)

    def _describe(self, entry: TemplateEntry) -> str:
        effects = sum(len(item.clip.effects) for item in self._walk_loaded())
        if self._is_effects_only():
            return (
                f"{entry.path}\n"
                f"エフェクトだけのテンプレート（{effects} 段）\n"
                "中身は持ちません 選んだクリップに効果を足す形で使います"
            )

        kinds = [item.kind or "?" for item in self._loaded]
        return (
            f"{entry.path}\n"
            f"{len(self._loaded)} オブジェクト（{'、'.join(kinds)}）／エフェクト {effects} 段\n"
            "下絵は文字と図形だけ 縁取りやグラデーションは含まれていません"
        )

    def _show_preview(self) -> None:
        """先頭のオブジェクトの中身だけを描く

        まとめた中身（YMM4 の合成するグループ）の中まで探す 合成するグループは
        それ自体が中身を持たないので、上だけを見ると吹き出しの字幕テンプレートの
        下絵が何も出ない
        """
        from sashimono.engine.sources import render_source

        source = next((item.clip.source for item in self._walk_loaded() if item.clip.source), None)
        if source is None:
            return
        # 1080p で描いてから縮める 文字の大きさは 1080p を前提に決められて
        # いるので、小さい画面にそのまま描くと画面からはみ出す
        array = render_source(source, *_CANVAS)
        if array is None:  # pragma: no cover - 既知の種別なら必ず描ける
            return
        image = QImage(
            array.tobytes(), _CANVAS[0], _CANVAS[1], QImage.Format.Format_RGBA8888
        ).copy()
        self._preview.setPixmap(
            QPixmap.fromImage(image).scaled(
                *_PREVIEW,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _finish(self, action: str) -> None:
        if not self._loaded:
            return
        self.choice = (action, self._loaded)
        entry = self._selected_entry()
        self.origin = entry.path.parent if entry is not None else None
        self.accept()
