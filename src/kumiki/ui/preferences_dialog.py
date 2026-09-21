"""本人の好みで変わる設定

プロジェクトの設定（解像度やフレームレート）とは分ける あちらは作品の持ち物で、
こちらは**その人とその機械**の持ち物 同じプロジェクトを速い機械で開いたら、
等倍で見たいことがある
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from kumiki.ui.workspace import Preferences

__all__ = ["PROXY_HEIGHTS", "QUALITY_DIVISORS", "PreferencesDialog"]

#: 控えの大きさ 小さいほど軽いが、文字の読みやすさが落ちる
PROXY_HEIGHTS: tuple[tuple[str, int], ...] = (
    ("360p（一番軽い）", 360),
    ("540p（既定）", 540),
    ("720p（きれい）", 720),
)

#: 自動で落とすときの分母
QUALITY_DIVISORS: tuple[tuple[str, int], ...] = (
    ("1/2 画質", 2),
    ("1/4 画質", 4),
)


class PreferencesDialog(QDialog):
    """プレビューの重さに関わる設定を変える"""

    def __init__(self, preferences: Preferences, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")

        form = QFormLayout()

        self._use_proxy = QCheckBox("プレビューに低解像度の控えを使う", self)
        self._use_proxy.setChecked(preferences.use_proxy)
        self._use_proxy.setToolTip(
            "大きい素材を読み込んだときに、裏で低解像度の控えを作って"
            "プレビューだけ差し替える 書き出しは必ず元の素材から行う"
        )
        form.addRow(self._use_proxy)

        self._proxy_height = QComboBox(self)
        for label, height in PROXY_HEIGHTS:
            self._proxy_height.addItem(label, height)
        self._select(self._proxy_height, preferences.proxy_height)
        form.addRow("控えの大きさ", self._proxy_height)

        self._auto_quality = QCheckBox("画面より大きい素材では、プレビューの画質を下げる", self)
        self._auto_quality.setChecked(preferences.auto_quality)
        self._auto_quality.setToolTip(
            "切ると、4K の素材でも等倍で描く 画質は上がるが、再生が追いつかなくなる"
        )
        form.addRow(self._auto_quality)

        self._auto_divisor = QComboBox(self)
        for label, divisor in QUALITY_DIVISORS:
            self._auto_divisor.addItem(label, divisor)
        self._select(self._auto_divisor, preferences.auto_quality_divisor)
        form.addRow("下げたときの画質", self._auto_divisor)

        # 測った値をそのまま置く 「なんとなく軽くなる」ではなく、
        # どの組が 60fps に入るのかを見て選べるようにする
        note = QLabel(
            "4K を 3 枚重ねて効果を積むと、60fps（1 コマ 16.7ms）に入れるには"
            "控えと画質下げの両方が要る\n"
            "実測: 元のまま 62.0ms ／ 画質だけ 59.5ms ／ 控えだけ 23.7ms ／ 両方 14.8ms\n"
            "（4K を 1 枚置いただけなら、元のままでも 11.3ms で収まる）",
            self,
        )
        note.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

        self._use_proxy.toggled.connect(self._proxy_height.setEnabled)
        self._auto_quality.toggled.connect(self._auto_divisor.setEnabled)
        self._proxy_height.setEnabled(preferences.use_proxy)
        self._auto_divisor.setEnabled(preferences.auto_quality)

    @staticmethod
    def _select(box: QComboBox, value: int) -> None:
        """その値の項目を選ぶ 一覧に無ければ先頭のまま

        設定ファイルを手で書き換えた人が、一覧に無い値を入れていることがある
        """
        index = box.findData(value)
        if index >= 0:
            box.setCurrentIndex(index)

    def preferences(self) -> Preferences:
        """画面で選ばれた設定"""
        return Preferences(
            use_proxy=self._use_proxy.isChecked(),
            proxy_height=int(self._proxy_height.currentData()),
            auto_quality=self._auto_quality.isChecked(),
            auto_quality_divisor=int(self._auto_divisor.currentData()),
        )
