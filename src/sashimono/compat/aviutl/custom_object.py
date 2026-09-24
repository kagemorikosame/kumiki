"""カスタムオブジェクト（中身を作るスクリプト ``.obj`` ``.obj2``）をクリップにする形

専用の生成オブジェクトの種類は作らず、**空のテキストの最初のエフェクトにスクリプトを置く**
種類を足すと保存の形が変わり、形式の版を上げることになる（上げないと、前の版の本体は
知らない種類を黙って何も描かない） 空のテキストは何も描かないので、スクリプトが
``obj.load`` で作った絵だけが残り、AviUtl の「何も持たないオブジェクトから始める」と
同じ見え方になる

その代わり、どのクリップがカスタムオブジェクトかの見分けと、画面に出す名前をここに
まとめる 右クリックの〔追加〕と ``.exa`` の読み込みが別々の形で置くと、同じ物が
片方では「テキスト」、片方では中身の無いクリップになる（#147）
"""

from __future__ import annotations

from pathlib import PurePosixPath

from sashimono.compat.aviutl.catalog import KIND_LABELS, PREFIX, SCRIPT_SUFFIXES
from sashimono.core.model import Clip, Effect, GeneratedSource
from sashimono.effects.definition import registry
from sashimono.effects.sources import TEXT

__all__ = [
    "CUSTOM_OBJECT_LABEL",
    "custom_object_clip",
    "custom_object_script",
    "empty_object",
    "is_custom_object_kind",
    "script_label",
]

#: 種類として画面に出す言葉（タイムラインのクリップ・設定パネルの見出し）
CUSTOM_OBJECT_LABEL = KIND_LABELS["obj"]


def empty_object() -> GeneratedSource:
    """スクリプトの土台にする何も描かない生成オブジェクト"""
    return TEXT.create(text="")


def custom_object_clip(script: Effect, *, duration: int, timeline_start: int = 0) -> Clip:
    """スクリプト 1 本を中身にしたクリップ 描画の欄はまだ持たない（置く側が足す）"""
    return Clip(
        timeline_start=timeline_start,
        duration=duration,
        source=empty_object(),
        effects=(script,),
    )


def is_custom_object_kind(kind: str) -> bool:
    """エフェクトの種別が ``.obj`` ``.obj2`` のスクリプトか

    登録簿ではなく識別子（``aviutl:フォルダ/ファイル.obj:名前``）の拡張子で見る
    スクリプトが手元に無い機械で開いたときも見分けたいため 登録簿に頼ると、
    足りないスクリプトのクリップが「テキスト」と出て、何を探せばよいか分からない
    """
    path = _script_path(kind)
    return path is not None and SCRIPT_SUFFIXES.get(path.suffix.lower()) == "obj"


def custom_object_script(clip: Clip) -> Effect | None:
    """クリップがカスタムオブジェクトなら、中身を作っているスクリプト

    中身が空のテキストで、固定の欄（反転・配置など）を除いた最初のエフェクトが
    ``.obj`` のスクリプトであること 文字を打ったテキストや、アニメーション効果を
    掛けただけのテキストはテキストのまま 固定の欄を数に入れると、欄が付いたかどうかで
    見分けが変わる
    """
    source = clip.source
    if source is None or source.kind != TEXT.kind:
        return None
    text = source.params.get("text")
    if isinstance(text, str) and text.strip():
        return None
    first = next((effect for effect in clip.effects if not effect.fixed), None)
    if first is None or not is_custom_object_kind(first.kind):
        return None
    return first


def script_label(kind: str) -> str:
    """スクリプトの表示名 手元に無ければ識別子から作る

    識別子の末尾は節の名前（``@矩形`` の ``矩形``） 名前の無いファイル 1 本きりの
    スクリプトは節の番号になっているので、ファイル名を使う（AviUtl の一覧と同じ）
    """
    definition = registry.get(kind)
    if definition is not None:
        return definition.label
    path = _script_path(kind)
    if path is None:
        return kind
    name = kind.rpartition(":")[2]
    return path.stem if not name or name.isdigit() else name


def _script_path(kind: str) -> PurePosixPath | None:
    if not kind.startswith(PREFIX):
        return None
    path, separator, _name = kind[len(PREFIX) :].rpartition(":")
    return PurePosixPath(path) if separator else None
