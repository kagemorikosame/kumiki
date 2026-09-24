"""UI の色と寸法

編集ソフトは長時間見続けるものなので暗色を基調にする 明るい背景だと、映像の
色を判断するときに目が順応してしまい、プレビューの見え方が変わる
"""

from __future__ import annotations

from PySide6.QtGui import QColor

from sashimono.core.commands.edit import DEFAULT_TRACK_HEIGHT, MAX_TRACK_HEIGHT, MIN_TRACK_HEIGHT
from sashimono.resources import SPIN_ARROWS, path_to

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
    #: 選んでいるタブの地 選んでいないタブ（窓の地）より一段明るく、上の文字が読める暗さ
    TAB_SELECTED = QColor("#34343c")

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

    #: 数値欄の増減ボタンの幅 ボタンの置き場をスタイルシートで決め打ちにするのに使う
    SPIN_BUTTON_WIDTH = 16


def _url(name: str) -> str:
    """同梱素材をスタイルシートの ``url()`` へ書ける形にする

    区切りは ``/`` にする ``\\`` のままだと、スタイルシートの字句で逃がし文字として
    読まれ、パスが壊れて絵が出ない
    """
    return f'url("{path_to(name).as_posix()}")'


_SPIN_UP, _SPIN_DOWN, _SPIN_UP_OFF, _SPIN_DOWN_OFF = (_url(name) for name in SPIN_ARROWS)

#: 数値欄（整数も小数も）の増減ボタン
#:
#: ボタンの置き場を右端の上下に決め打ちする 決めずに元の見た目（Windows 11）に
#: 任せると、ボタンが横に 2 つ並ぶのに、数字の欄はボタン 1 つぶんの幅しか空けずに
#: 広がる 上のボタンの大半が数字の欄の下に隠れ、押しても数字の欄が受け取って
#: 数が変わらなかった（Issue #27） 置き場を決めると、描く所と押せる所と数字の欄の
#: 幅が、どの見た目でも同じ計算から出る
#:
#: 置き場を決めると元の見た目の矢印は描かれなくなるので、矢印の絵も自前で持つ
_SPIN_BOX = f"""
QAbstractSpinBox {{ padding-right: {Metrics.SPIN_BUTTON_WIDTH + 4}px; }}
QAbstractSpinBox QLineEdit {{
    background: transparent;
    border: none;
    padding: 0;
}}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    subcontrol-origin: padding;
    width: {Metrics.SPIN_BUTTON_WIDTH}px;
    background-color: {Colors.PANEL.name()};
    border-left: 1px solid {Colors.BORDER.name()};
}}
QAbstractSpinBox::up-button {{
    subcontrol-position: top right;
    border-top-right-radius: 2px;
}}
QAbstractSpinBox::down-button {{
    subcontrol-position: bottom right;
    border-bottom-right-radius: 2px;
}}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
    background-color: {Colors.BORDER.name()};
}}
QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed {{
    background-color: {Colors.ACCENT.name()};
}}
QAbstractSpinBox::up-arrow {{ image: {_SPIN_UP}; width: 8px; height: 6px; }}
QAbstractSpinBox::down-arrow {{ image: {_SPIN_DOWN}; width: 8px; height: 6px; }}
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off {{ image: {_SPIN_UP_OFF}; }}
QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off {{
    image: {_SPIN_DOWN_OFF};
}}
"""

#: タブ（重ねたドックの「メディア」「字幕」など、設定の窓のタブ）
#:
#: 指定が無いと元の見た目のまま、選んだタブが明るい灰色になり、上から当てた
#: 白い文字が読めなかった（Issue #27） 選んだタブは地を少し明るくして文字を
#: 白に近くし、アクセント色の線を引く 選んでいないタブは文字を薄くするだけにして、
#: どれを見ているかが色の差と線の 2 つで分かるようにする
#: 線は画面の中身の側に引く ドックのタブは下に付くので、下に付くタブは上に引く
_TABS = f"""
QTabWidget::pane {{
    border: 1px solid {Colors.BORDER.name()};
    top: -1px;
}}
QTabBar {{
    /* タブの並びの右に残る枠（元の見た目の下敷き）を描かない 空の入力欄に見える */
    qproperty-drawBase: 0;
}}
QTabBar::tab {{
    background-color: {Colors.WINDOW.name()};
    color: {Colors.TEXT_MUTED.name()};
    border: 1px solid {Colors.BORDER.name()};
    padding: 4px 12px;
}}
QTabBar::tab:top {{ border-bottom: 2px solid transparent; margin-right: 1px; }}
QTabBar::tab:bottom {{ border-top: 2px solid transparent; margin-right: 1px; }}
QTabBar::tab:hover {{
    background-color: {Colors.PANEL_ALT.name()};
    color: {Colors.TEXT.name()};
}}
QTabBar::tab:selected {{
    background-color: {Colors.TAB_SELECTED.name()};
    color: {Colors.CLIP_LABEL.name()};
}}
QTabBar::tab:top:selected {{ border-bottom-color: {Colors.ACCENT.name()}; }}
QTabBar::tab:bottom:selected {{ border-top-color: {Colors.ACCENT.name()}; }}
"""

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
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QComboBox, QAbstractSpinBox, QLineEdit {{
    background-color: {Colors.PANEL_ALT.name()};
    border: 1px solid {Colors.BORDER.name()};
    border-radius: 3px;
    padding: 3px 6px;
}}
{_SPIN_BOX}
{_TABS}
QProgressBar {{
    background-color: {Colors.PANEL_ALT.name()};
    border: 1px solid {Colors.BORDER.name()};
    border-radius: 3px;
    text-align: center;
}}
QProgressBar::chunk {{ background-color: {Colors.ACCENT.name()}; }}
"""
