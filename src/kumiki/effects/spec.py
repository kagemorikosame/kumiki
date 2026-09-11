"""エフェクトのパラメータ定義

AviUtl のスクリプト制御文字（``--track@`` ``--check@`` ``--color@`` など）と
1 対 1 に対応させてある 自前のエフェクトも配布スクリプトも同じ定義形式に載るので、
設定 UI の自動生成もプリセットの保存も 1 つの実装で済む

別々の形式にすると、AviUtl 互換（P5）で UI 生成をもう一度書くことになる

GUI にも OpenGL にも依存しない 定義はただのデータで、それをどう表示するか・
どう描画するかは別の層が決める
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from kumiki.core.model import AnimatedValue, ParamValue

#: まだ整えていない入力 モデルに入る前の値はここまで緩い
#:
#: :data:`~kumiki.core.model.ParamValue` は「モデルに入った後」の型で、数値は
#: :class:`~kumiki.core.model.AnimatedValue` になっている 呼び出し側に毎回
#: それを組み立てさせるのは煩雑なので、素の数値と ``None`` も受ける
type ParamInput = ParamValue | float | None

__all__ = [
    "CheckSpec",
    "ColorSpec",
    "FileSpec",
    "FontSpec",
    "ParamInput",
    "ParameterKind",
    "ParameterSpec",
    "SelectSpec",
    "TextSpec",
    "TrackSpec",
    "ValueSpec",
]


class ParameterKind(Enum):
    """パラメータの種類 AviUtl の制御文字に対応する"""

    #: ``--track@`` 数値スライダー 時間で変化させられる唯一の種類
    TRACK = "track"
    #: ``--check@`` チェックボックス
    CHECK = "check"
    #: ``--color@`` 色
    COLOR = "color"
    #: ``--select@`` 選択肢
    SELECT = "select"
    #: ``--file@`` / ``--folder@`` パス
    FILE = "file"
    FOLDER = "folder"
    #: ``--font@`` フォント名
    FONT = "font"
    #: ``--text@`` 複数行テキスト、``--string@`` 1 行テキスト
    TEXT = "text"
    STRING = "string"
    #: ``--value@`` スライダーを持たない数値 時間で変化させられない
    VALUE = "value"


@dataclass(frozen=True, slots=True)
class TrackSpec:
    """数値スライダー キーフレームを打てる

    エフェクトの数値パラメータは原則これにする あとから「ここを動かしたい」と
    思ったときに、種類を変えずに済む
    """

    name: str
    label: str
    minimum: float
    maximum: float
    default: float
    #: スライダーの刻み 0.1 なら小数第 1 位まで
    step: float = 0.1
    #: 画面に添える単位（``"px"`` ``"%"`` ``"度"`` など）
    unit: str = ""

    kind = ParameterKind.TRACK

    def __post_init__(self) -> None:
        if self.minimum > self.maximum:
            raise ValueError(f"{self.name}: 最小値が最大値より大きい")
        if not self.minimum <= self.default <= self.maximum:
            raise ValueError(f"{self.name}: 既定値が範囲外")

    def default_value(self) -> AnimatedValue:
        return AnimatedValue(static=self.default)

    def clamp(self, value: float) -> float:
        return min(max(value, self.minimum), self.maximum)

    def coerce(self, value: ParamInput) -> AnimatedValue:
        """外から来た値を、この仕様に合う形へ寄せる

        プロジェクトファイルや AviUtl のエイリアスから読んだ値は、型も範囲も
        信用できない ここで 1 度だけ整える
        """
        if value is None:
            return self.default_value()
        if isinstance(value, AnimatedValue):
            return value
        if isinstance(value, bool | int | float):
            return AnimatedValue(static=self.clamp(float(value)))
        return self.default_value()


@dataclass(frozen=True, slots=True)
class CheckSpec:
    """チェックボックス"""

    name: str
    label: str
    default: bool = False

    kind = ParameterKind.CHECK

    def default_value(self) -> bool:
        return self.default

    def coerce(self, value: ParamInput) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return self.default


@dataclass(frozen=True, slots=True)
class ColorSpec:
    """色 値は sRGB の 0..1 で ``(R, G, B, A)``

    リニアではなく sRGB で持つのは、ユーザーが指定するのも画面に出すのも
    sRGB だから リニアへの変換は描画の直前に 1 度だけ行う
    """

    name: str
    label: str
    default: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    #: アルファを編集させるか 縁取りの色など、不透明が前提のものは偽
    with_alpha: bool = True

    kind = ParameterKind.COLOR

    def default_value(self) -> tuple[float, ...]:
        return self.default

    def coerce(self, value: ParamInput) -> tuple[float, ...]:
        if isinstance(value, tuple) and len(value) >= 3:
            values = [min(max(float(v), 0.0), 1.0) for v in value[:4]]
            while len(values) < 4:
                values.append(1.0)
            return tuple(values)
        return self.default


@dataclass(frozen=True, slots=True)
class SelectSpec:
    """選択肢 値は選ばれた項目の識別子"""

    name: str
    label: str
    #: ``(識別子, 表示名)`` の並び
    choices: tuple[tuple[str, str], ...]
    default: str = ""

    kind = ParameterKind.SELECT

    def __post_init__(self) -> None:
        if not self.choices:
            raise ValueError(f"{self.name}: 選択肢が空")
        if self.default and self.default not in dict(self.choices):
            raise ValueError(f"{self.name}: 既定値 {self.default!r} が選択肢に無い")
        if not self.default:
            object.__setattr__(self, "default", self.choices[0][0])

    def default_value(self) -> str:
        return self.default

    def coerce(self, value: ParamInput) -> str:
        if isinstance(value, str) and value in dict(self.choices):
            return value
        return self.default

    def index_of(self, value: str) -> int:
        """シェーダへ渡すための番号 GLSL に文字列は無い"""
        for index, (identifier, _) in enumerate(self.choices):
            if identifier == value:
                return index
        return 0


@dataclass(frozen=True, slots=True)
class TextSpec:
    """テキスト ``multiline`` が偽なら 1 行"""

    name: str
    label: str
    default: str = ""
    multiline: bool = True

    kind = ParameterKind.TEXT

    def default_value(self) -> str:
        return self.default

    def coerce(self, value: ParamInput) -> str:
        return value if isinstance(value, str) else self.default


@dataclass(frozen=True, slots=True)
class FileSpec:
    """ファイルまたはフォルダのパス"""

    name: str
    label: str
    default: str = ""
    #: 真ならフォルダを選ばせる
    directory: bool = False
    #: ファイル選択ダイアログのフィルタ
    filter: str = ""

    kind = ParameterKind.FILE

    def default_value(self) -> str:
        return self.default

    def coerce(self, value: ParamInput) -> str:
        return value if isinstance(value, str) else self.default


@dataclass(frozen=True, slots=True)
class FontSpec:
    """フォント名"""

    name: str
    label: str
    default: str = "Yu Gothic UI"

    kind = ParameterKind.FONT

    def default_value(self) -> str:
        return self.default

    def coerce(self, value: ParamInput) -> str:
        return value if isinstance(value, str) and value else self.default


@dataclass(frozen=True, slots=True)
class ValueSpec:
    """スライダーを持たない数値 時間で変化させられない

    シード値やループ回数のように、途中の値に意味が無いものに使う
    """

    name: str
    label: str
    default: int = 0
    minimum: int = -(2**31)
    maximum: int = 2**31 - 1

    kind = ParameterKind.VALUE

    def default_value(self) -> int:
        return self.default

    def coerce(self, value: ParamInput) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int | float):
            return min(max(int(value), self.minimum), self.maximum)
        return self.default


#: パラメータ定義の総称
type ParameterSpec = (
    TrackSpec | CheckSpec | ColorSpec | SelectSpec | TextSpec | FileSpec | FontSpec | ValueSpec
)


@dataclass(frozen=True, slots=True)
class ParameterGroup:
    """設定 UI で 1 つの見出しにまとめるパラメータ

    エフェクトのパラメータが 10 個を超えると、並べただけでは何がどこにあるか
    分からなくなる
    """

    label: str
    names: tuple[str, ...] = field(default_factory=tuple)
