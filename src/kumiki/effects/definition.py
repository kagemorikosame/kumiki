"""エフェクトの定義と、その一覧

定義はデータでしかない GLSL のソースは文字列として持つだけで、コンパイルは
エンジン層（:mod:`kumiki.engine.gpu.effects`）が行う この分離のおかげで、
設定 UI もプリセットも GPU を持たずに扱える
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kumiki.core.model import Effect, ParamValue
from kumiki.effects.spec import ParameterGroup, ParameterSpec, ParamInput

__all__ = ["EffectDefinition", "EffectRegistry", "registry"]


@dataclass(frozen=True, slots=True)
class EffectDefinition:
    """1 種類のエフェクト

    ``fragment_shader`` は :data:`kumiki.engine.gpu.VERTEX_SHADER` と組み合わせる
    フラグメントシェーダ 次の uniform が自動で渡る

    ``u_texture``   入力（リニア、ストレートアルファ）
    ``u_size``      入力の大きさ（ピクセル）
    ``u_pass``      複数パスのときの通し番号（0 から）
    ``u_time``      クリップ先頭からの経過秒
    ``u_frame``     クリップ先頭からの経過フレーム

    パラメータは名前をそのまま uniform 名にする 数値は ``float``、チェックは
    ``bool``、色は ``vec4``（リニアへ変換済み）、選択肢は番号の ``int``
    """

    kind: str
    label: str
    #: 分類 設定 UI のツリーで使う
    category: str
    parameters: tuple[ParameterSpec, ...] = ()
    fragment_shader: str | None = None
    #: 何回シェーダを通すか ぼかしは横方向と縦方向で 2 回に分ける
    #: 1 回で 2 次元のカーネルを畳むと計算量が半径の 2 乗になる
    passes: int = 1
    #: 設定 UI での見出し分け 空なら並べるだけ
    groups: tuple[ParameterGroup, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.passes < 1:
            raise ValueError(f"{self.kind}: パス数は 1 以上必要")
        names = [spec.name for spec in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.kind}: パラメータ名が重複している")

    def spec(self, name: str) -> ParameterSpec | None:
        for parameter in self.parameters:
            if parameter.name == name:
                return parameter
        return None

    def default_params(self) -> dict[str, ParamValue]:
        return {spec.name: spec.default_value() for spec in self.parameters}

    def create(self, **overrides: ParamInput) -> Effect:
        """既定値で埋めたエフェクトを作る"""
        params = self.default_params()
        for name, value in overrides.items():
            spec = self.spec(name)
            if spec is not None:
                params[name] = spec.coerce(value)
        return Effect(kind=self.kind, params=params)

    def normalize(self, params: dict[str, ParamValue]) -> dict[str, ParamValue]:
        """外から来たパラメータを、定義に合う形へ整える

        プロジェクトファイルや配布エイリアスから読んだ値は、型も範囲も、
        そもそも存在するかも信用できない 欠けているものは既定値で埋め、
        定義に無いものは落とす
        """
        return {spec.name: spec.coerce(params.get(spec.name)) for spec in self.parameters}


class EffectRegistry:
    """エフェクト定義の一覧

    ``kind`` を鍵に引く プロジェクトファイルには ``kind`` しか入らないので、
    定義が見つからないエフェクトは「未知」として素通しにする（後述）
    """

    def __init__(self) -> None:
        self._definitions: dict[str, EffectDefinition] = {}

    def register(self, definition: EffectDefinition, *, replace: bool = False) -> EffectDefinition:
        """エフェクトを登録する

        既定では重複を拒む 自前のエフェクトで名前がぶつかるのは書き間違いで、
        黙って上書きすると、どちらが効いているのか分からなくなる

        ``replace`` は外から読み込む定義（AviUtl スクリプト）のためにある
        こちらは走査のたびに読み直すのが正しく、内容が変わっていれば新しい方を
        使ってほしい
        """
        if not replace and definition.kind in self._definitions:
            raise ValueError(f"すでに登録されているエフェクト: {definition.kind}")
        self._definitions[definition.kind] = definition
        return definition

    def unregister(self, kind: str) -> None:
        """登録を外す スクリプトのフォルダを変えたときに使う"""
        self._definitions.pop(kind, None)

    def get(self, kind: str) -> EffectDefinition | None:
        return self._definitions.get(kind)

    def require(self, kind: str) -> EffectDefinition:
        definition = self._definitions.get(kind)
        if definition is None:
            raise KeyError(f"未知のエフェクト: {kind}")
        return definition

    def all(self) -> tuple[EffectDefinition, ...]:
        """登録順ではなく、分類 → 表示名の順で返す UI の一覧用"""
        return tuple(sorted(self._definitions.values(), key=lambda d: (d.category, d.label)))

    def categories(self) -> tuple[str, ...]:
        seen: list[str] = []
        for definition in self.all():
            if definition.category not in seen:
                seen.append(definition.category)
        return tuple(seen)

    def __contains__(self, kind: object) -> bool:
        return kind in self._definitions

    def __len__(self) -> int:
        return len(self._definitions)


#: アプリ全体で使う一覧 :mod:`kumiki.effects.builtin` が読み込み時に登録する
registry = EffectRegistry()
