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

from sashimono.engine.cache.proxy import (
    BUDGET_MS,
    MEASURED_ONE_LAYER_MS,
    MEASURED_PREFETCH_MS,
    MEASURED_THREE_LAYERS_MS,
)
from sashimono.engine.encode import MEASURED_EXPORT_MS, MEASURED_EXPORT_TOTAL_MS
from sashimono.engine.render.prefetch import BYTES_PER_FRAME_PIXEL
from sashimono.ui.workspace import Preferences

__all__ = [
    "PIPELINE_DEPTHS",
    "PREFETCH_BUDGETS",
    "PROXY_HEIGHTS",
    "QUALITY_DIVISORS",
    "PreferencesDialog",
]

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

#: 先読みに使うメモリ（MB） 1080p の 1 枚が 8MB
PREFETCH_BUDGETS: tuple[tuple[str, int], ...] = (
    ("512MB（控えめ）", 512),
    ("1GB（既定）", 1024),
    ("2GB", 2048),
    ("4GB（たくさん貯める）", 4096),
)

#: 書き出しで、合成を書き込みの何枚ぶん先へ進めるか
#: 1 枚は画面 1 枚の RGBA（1080p で 8MB、4K で 33MB）
PIPELINE_DEPTHS: tuple[tuple[str, int], ...] = (
    ("重ねない（1 枚ずつ）", 0),
    ("2 枚先まで（既定）", 2),
    ("4 枚先まで", 4),
)


#: 目安を出すときの物差し **プロジェクトの解像度ではなく 1920x1080 で固定する**
#: 実際の 1 枚は画質の設定でも変わるが、設定を開く前から見当が付く数でないと
#: 「どれを選べばいいか」の助けにならない
FULL_HD_FRAME_BYTES = 1920 * 1080 * BYTES_PER_FRAME_PIXEL

#: 1GB に入る枚数と、30fps での秒数 設定の画面で目安として出す
PREFETCH_FRAMES_PER_GB = 1024 * 1024 * 1024 // FULL_HD_FRAME_BYTES
PREFETCH_SECONDS_PER_GB = PREFETCH_FRAMES_PER_GB // 30


def _megabytes(size: int) -> float:
    """バイトを MB へ 小数で返す 切り捨てると 7.9MB が 7MB になり、
    1GB に何枚入るかの見当が合わなくなる
    """
    return size / (1024 * 1024)


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

        self._prefetch = QCheckBox("手が止まっている間に、先のコマを描いておく", self)
        self._prefetch.setChecked(preferences.prefetch)
        self._prefetch.setToolTip(
            "編集していない間に、再生ヘッドの先を描いて取っておく 貯まった所は"
            "出すだけで済むので、重い所でも再生が止まらない 描いている間は"
            "GPU を使うので、ほかの作業が重くなるなら切る"
        )
        form.addRow(self._prefetch)

        self._prefetch_budget = QComboBox(self)
        for label, budget in PREFETCH_BUDGETS:
            self._prefetch_budget.addItem(label, budget)
        self._select(self._prefetch_budget, preferences.prefetch_budget_mb)
        form.addRow("先読みに使うメモリ", self._prefetch_budget)

        self._pipeline_depth = QComboBox(self)
        for label, depth in PIPELINE_DEPTHS:
            self._pipeline_depth.addItem(label, depth)
        self._select(self._pipeline_depth, preferences.export_pipeline_depth)
        self._pipeline_depth.setToolTip(
            "書き出しで、GPU の合成と、CPU の色変換・エンコード・多重化を重ねて進める "
            "合成済みの絵を貯めるぶんメモリを使う（1080p で 1 枚 8MB、4K で 33MB）ので、"
            "深くすれば速いとは限らない 実測では 4K で 4 枚先まで貯めると 2 枚より遅くなった "
            "メモリが足りない機械では「重ねない」にする"
        )
        form.addRow("書き出しの先読み", self._pipeline_depth)

        self._native_modules = QCheckBox("AviUtl2 のスクリプトモジュール（DLL）を読み込む", self)
        self._native_modules.setChecked(preferences.native_modules)
        self._native_modules.setToolTip(
            "テレビ字幕のように、処理を DLL に切り出した配布スクリプトを動かす "
            "読み込むのはスクリプトフォルダに自分で置いた物だけ DLL は Sashimono と"
            "同じ権限で動くので、信頼できない物は置かない"
        )
        form.addRow(self._native_modules)

        # 測った値をそのまま置く 「なんとなく軽くなる」ではなく、
        # どの組が 60fps に入るのかを見て選べるようにする
        # 数は控えの側（sashimono.engine.cache.proxy）から取る ここへ直に書くと、
        # 測り直したときに画面の側だけ古くなる
        plain, proxied, both = MEASURED_THREE_LAYERS_MS
        filling, showing = MEASURED_PREFETCH_MS
        compose, readback, convert, muxing = MEASURED_EXPORT_MS
        serial_ms, pipelined_ms = MEASURED_EXPORT_TOTAL_MS
        note = QLabel(
            f"4K を 3 枚重ねたときの実測（1 コマ {BUDGET_MS:.1f}ms が 60fps の目安）\n"
            f"元のまま {plain}ms ／ 控えを使う {proxied}ms ／ さらに画質を下げる {both}ms\n"
            "効果を積むと控えだけでは足りず、画質下げと組にして入る"
            f"（4K を 1 枚置いただけなら、元のままでも {MEASURED_ONE_LAYER_MS}ms で収まる）\n"
            "この機械で測るには tools\\bench_proxy.py\n"
            f"先読みは 1 枚 {_megabytes(FULL_HD_FRAME_BYTES):.1f}MB（1920x1080）"
            f" 1GB でおよそ {PREFETCH_FRAMES_PER_GB} 枚＝{PREFETCH_SECONDS_PER_GB} 秒ぶん\n"
            f"貯めるのに 1 枚 {filling}ms 掛かる代わりに、貯まった所は {showing}ms で出せる"
            "（上と同じ 4K 3 枚 + blur）\n"
            f"書き出しの内訳は 1 枚あたり 合成 {compose}ms ／ 読み戻し {readback}ms ／"
            f" 色変換 {convert}ms ／ エンコード + 多重化 {muxing}ms"
            "（1920x1080 を 3 枚重ね、NVIDIA GPU）\n"
            f"後ろの 2 つを重ねると、書き出し全体で 1 枚 {serial_ms:.1f}ms の所が"
            f" {pipelined_ms:.1f}ms になる この機械で測るには tools\\bench_export.py",
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
        self._prefetch.toggled.connect(self._prefetch_budget.setEnabled)
        self._prefetch_budget.setEnabled(preferences.prefetch)
        self._auto_quality.toggled.connect(self._auto_divisor.setEnabled)
        self._proxy_height.setEnabled(preferences.use_proxy)
        self._auto_divisor.setEnabled(preferences.auto_quality)

    @staticmethod
    def _select(box: QComboBox, value: int) -> None:
        """その値の項目を選ぶ 一覧に無ければ、その値の項目を足してから選ぶ

        設定ファイルを手で書き換えた人が、一覧に無い値を入れていることがある
        先頭のままにすると、設定を開いて OK を押しただけで**黙って別の値に
        置き換わる** 触っていない項目が変わるのは、壊したのと同じ
        """
        index = box.findData(value)
        if index < 0:
            box.addItem(f"{value}（設定ファイルの値）", value)
            index = box.findData(value)
        box.setCurrentIndex(index)

    def preferences(self) -> Preferences:
        """画面で選ばれた設定"""
        return Preferences(
            use_proxy=self._use_proxy.isChecked(),
            proxy_height=int(self._proxy_height.currentData()),
            auto_quality=self._auto_quality.isChecked(),
            auto_quality_divisor=int(self._auto_divisor.currentData()),
            prefetch=self._prefetch.isChecked(),
            prefetch_budget_mb=int(self._prefetch_budget.currentData()),
            export_pipeline_depth=int(self._pipeline_depth.currentData()),
            native_modules=self._native_modules.isChecked(),
        )
