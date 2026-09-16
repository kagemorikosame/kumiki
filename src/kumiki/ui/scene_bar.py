"""タイムラインの上に置く、シーンの切り替えバー

どのシーンを編集しているかは窓が持つ ここは選ぶ・作る・名前を変える・消す・置く、の
入口を並べて、押されたことを信号で知らせるだけ（名前を尋ねるダイアログも窓が出す）
"""

from __future__ import annotations

from PySide6.QtCore import Signal, SignalInstance
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QWidget

from kumiki.core.model import Project, SceneId

__all__ = ["MAIN_SCENE_LABEL", "SceneBar"]

#: メインのタイムラインの表示名 シーンと同じ並びで選べるようにする
MAIN_SCENE_LABEL = "メイン"


class SceneBar(QWidget):
    #: 編集するシーンを選んだ 引数はシーンの ID、メインなら空文字列
    scene_selected = Signal(str)
    add_requested = Signal()
    rename_requested = Signal()
    remove_requested = Signal()
    place_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.addWidget(QLabel("シーン", self))

        self._combo = QComboBox(self)
        self._combo.setMinimumWidth(160)
        self._combo.setAccessibleName("編集するシーン")
        self._combo.currentIndexChanged.connect(self._on_index_changed)
        layout.addWidget(self._combo)

        self._add_button = self._button("新規", "空のシーンを作って開く", self.add_requested)
        self._rename_button = self._button(
            "名前", "開いているシーンの名前を変える", self.rename_requested
        )
        self._remove_button = self._button(
            "削除",
            "開いているシーンを消す（どこにも置かれていないときだけ）",
            self.remove_requested,
        )
        self._place_button = self._button(
            "置く",
            "ほかのシーンを、再生ヘッドの位置へ 1 本のクリップとして置く",
            self.place_requested,
        )
        for button in (
            self._add_button,
            self._rename_button,
            self._remove_button,
            self._place_button,
        ):
            layout.addWidget(button)
        layout.addStretch(1)

    def _button(self, text: str, tip: str, signal: SignalInstance) -> QPushButton:
        button = QPushButton(text, self)
        button.setToolTip(tip)
        button.clicked.connect(lambda _checked=False: signal.emit())
        return button

    def set_project(self, project: Project, active: SceneId | None) -> None:
        """一覧を作り直す 選び直しの信号は出さない（窓が決めた状態を映すだけ）"""
        self._updating = True
        try:
            self._combo.clear()
            self._combo.addItem(MAIN_SCENE_LABEL, "")
            for scene in project.scenes:
                self._combo.addItem(scene.name, str(scene.id))
            index = self._combo.findData(str(active) if active is not None else "")
            self._combo.setCurrentIndex(max(0, index))
        finally:
            self._updating = False
        # メインは名前を変えたり消したりできない 押せるのにエラーになると壊れて見える
        editing_scene = active is not None
        self._rename_button.setEnabled(editing_scene)
        self._remove_button.setEnabled(editing_scene)
        # 開いているシーン自身は置けない 置ける候補が無いのに押せると、押してから断られる
        self._place_button.setEnabled(any(scene.id != active for scene in project.scenes))

    def _on_index_changed(self, index: int) -> None:
        if self._updating or index < 0:
            return
        self.scene_selected.emit(str(self._combo.itemData(index) or ""))
