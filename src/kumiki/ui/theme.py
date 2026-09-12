"""UI の色と寸法

編集ソフトは長時間見続けるものなので暗色を基調にする 明るい背景だと、映像の
色を判断するときに目が順応してしまい、プレビューの見え方が変わる
"""

from __future__ import annotations

from PySide6.QtGui import QColor

from kumiki.core.commands.edit import DEFAULT_TRACK_HEIGHT, MAX_TRACK_HEIGHT, MIN_TRACK_HEIGHT

__all__ = ["STYLE_SHEET", "Colors", "Metrics"]


class Colors:
    """配色 値はすべて sRGB"""

    WINDOW = QColor("#1b1b1e")
    PANEL = QColor("#232327")
    PANEL_ALT = QColor("#26262b")
    BORDER = QColor("#3a3a40")
    TEXT = QColor("#d7d7db")
    TEXT_MUTED = QColor("#8b8b93")
    ACCENT = QColor("#7f8cf0")

    #: プレビューの周囲 映像の明るさを判断しやすいよう、真っ黒より少し上げる
    VIEWER_BACKGROUND = QColor("#0f0f11")

    TIMELINE_BACKGROUND = QColor("#191a1d")
    TIMELINE_RULER = QColor("#1f1f23")
    TRACK_HEADER = QColor("#202024")
    TRACK_SEPARATOR = QColor("#2c2c33")

    #: 再生ヘッド 素材の色と被らない色にする
    PLAYHEAD = QColor("#ff5c5c")

    VIDEO_CLIP = QColor("#33445f")
    VIDEO_CLIP_BORDER = QColor("#5b7bb0")
    AUDIO_CLIP = QColor("#26443a")
    AUDIO_CLIP_BORDER = QColor("#4c8a68")
    WAVEFORM = QColor("#7fd6ab")
    CLIP_LABEL = QColor("#e8eefc")
    SELECTION = QColor("#ffffff")

    #: トラックヘッダの切り替えボタン（押している間の色）
    #: 3 つとも違う色にする 同じ色だと、どれが効いているかを文字で読むことになる
    TRACK_MUTE = QColor("#c9563f")
    TRACK_SOLO = QColor("#d8b23a")
    TRACK_LOCK = QColor("#6c7a91")


class Metrics:
    """寸法"""

    TRACK_HEADER_WIDTH = 132
    RULER_HEIGHT = 22
    DEFAULT_TRACK_HEIGHT = DEFAULT_TRACK_HEIGHT
    #: トラックの高さの範囲は、変えるコマンドと同じ値を使う 別々に持つと、
    #: 描画では収まっているのに保存すると高さが変わる、が起きる
    MIN_TRACK_HEIGHT = MIN_TRACK_HEIGHT
    MAX_TRACK_HEIGHT = MAX_TRACK_HEIGHT
    CLIP_LABEL_HEIGHT = 14
    CLIP_RADIUS = 3

    #: クリップ端を掴んでトリムできる幅（ピクセル）
    TRIM_HANDLE_WIDTH = 6

    #: スナップが効く距離（ピクセル）
    SNAP_DISTANCE = 8


#: アプリ全体のスタイル ウィジェットごとに色を書くと、変えたいときに全部を
#: 探し回ることになるので 1 箇所にまとめる
STYLE_SHEET = f"""
QWidget {{
    background-color: {Colors.WINDOW.name()};
    color: {Colors.TEXT.name()};
    font-size: 12px;
}}
QMainWindow::separator {{
    background-color: {Colors.BORDER.name()};
    width: 1px;
    height: 1px;
}}
QDockWidget {{
    titlebar-close-icon: none;
    font-size: 12px;
}}
QDockWidget::title {{
    background-color: {Colors.PANEL.name()};
    padding: 4px 8px;
    border-bottom: 1px solid {Colors.BORDER.name()};
}}
QListWidget, QTreeWidget, QTableWidget {{
    background-color: {Colors.PANEL_ALT.name()};
    border: 1px solid {Colors.BORDER.name()};
    outline: none;
}}
QListWidget::item, QTreeWidget::item, QTableWidget::item {{ padding: 3px 6px; }}
QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected {{
    background-color: {Colors.ACCENT.name()};
    color: #12121a;
}}
QHeaderView::section {{
    background-color: {Colors.PANEL.name()};
    border: none;
    border-bottom: 1px solid {Colors.BORDER.name()};
    padding: 3px 6px;
}}
QPushButton, QToolButton {{
    background-color: {Colors.PANEL.name()};
    border: 1px solid {Colors.BORDER.name()};
    border-radius: 3px;
    padding: 4px 10px;
}}
QPushButton:hover, QToolButton:hover {{ background-color: {Colors.PANEL_ALT.name()}; }}
QPushButton:pressed, QToolButton:pressed {{ background-color: {Colors.ACCENT.name()}; }}
QPushButton:disabled {{ color: {Colors.TEXT_MUTED.name()}; }}
QMenuBar {{ background-color: {Colors.PANEL.name()}; }}
QMenuBar::item:selected {{ background-color: {Colors.ACCENT.name()}; }}
QMenu {{
    background-color: {Colors.PANEL.name()};
    border: 1px solid {Colors.BORDER.name()};
}}
QMenu::item:selected {{ background-color: {Colors.ACCENT.name()}; }}
QStatusBar {{ background-color: {Colors.PANEL.name()}; }}
QScrollBar:horizontal, QScrollBar:vertical {{
    background: {Colors.PANEL.name()};
    border: none;
}}
QScrollBar:horizontal {{ height: 10px; }}
QScrollBar:vertical {{ width: 10px; }}
QScrollBar::handle {{
    background: {Colors.BORDER.name()};
    border-radius: 5px;
    min-width: 24px;
    min-height: 24px;
}}
QScrollBar::handle:hover {{ background: {Colors.TEXT_MUTED.name()}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QComboBox, QSpinBox, QLineEdit {{
    background-color: {Colors.PANEL_ALT.name()};
    border: 1px solid {Colors.BORDER.name()};
    border-radius: 3px;
    padding: 3px 6px;
}}
QProgressBar {{
    background-color: {Colors.PANEL_ALT.name()};
    border: 1px solid {Colors.BORDER.name()};
    border-radius: 3px;
    text-align: center;
}}
QProgressBar::chunk {{ background-color: {Colors.ACCENT.name()}; }}
"""
