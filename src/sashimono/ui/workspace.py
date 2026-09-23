"""画面配置とショートカットの保存先

どちらも「その人の使い方」で、プロジェクトではなく本人に付く ``%APPDATA%\\Sashimono``
に置くのはそのため（キャッシュや退避と違い、消えると作り直せない）

Qt の既定の保存先（Windows ではレジストリ）は使わない 自動更新で本体を入れ替えても
残ること、手で消したり他の機械へ持っていったりできることを優先して、ファイルにする
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings
from PySide6.QtWidgets import QMainWindow

from sashimono.core import userdirs
from sashimono.engine.encode import DEFAULT_PIPELINE_DEPTH, MAX_PIPELINE_DEPTH

__all__ = [
    "AUTO_QUALITY_HEIGHT",
    "LAYOUT_VERSION",
    "MAX_PREFETCH_MB",
    "MIN_PREFETCH_MB",
    "PreferenceStore",
    "Preferences",
    "ShortcutStore",
    "Workspace",
    "config_root",
    "find_conflicts",
]

#: 画面配置の版 パネルを足したり名前を変えたりしたら上げる
#: 古い配置をそのまま当てると、新しいパネルがどこにも出てこない
LAYOUT_VERSION = 1


def config_root() -> Path:
    """本人の設定を置く場所"""
    return userdirs.config_root()


class Workspace:
    """ウィンドウの大きさと、パネルの並び"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else config_root() / "workspace.ini"

    def save(self, window: QMainWindow) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        settings.setValue("geometry", window.saveGeometry())
        settings.setValue("state", window.saveState(LAYOUT_VERSION))
        settings.sync()

    def restore(self, window: QMainWindow) -> bool:
        """保存した並びに戻す 無い・版が違うときは何もせず偽を返す"""
        if not self.path.is_file():
            return False
        settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        geometry = settings.value("geometry")
        state = settings.value("state")
        if isinstance(geometry, QByteArray):
            window.restoreGeometry(geometry)
        return isinstance(state, QByteArray) and window.restoreState(state, LAYOUT_VERSION)


class ShortcutStore:
    """既定から変えたショートカットだけを持つ

    全部を書き出さないのは、新しい版で既定を変えたときに、本人が触っていない
    ものまで古い既定のまま固まってしまうため 値が空文字なら「割り当てなし」
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else config_root() / "shortcuts.json"

    def load(self) -> dict[str, str]:
        """壊れていても起動は止めない 既定のまま使えれば困らない"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): value for key, value in data.items() if isinstance(value, str)}

    def save(self, overrides: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".writing")
        temporary.write_text(
            json.dumps(dict(sorted(overrides.items())), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def find_conflicts(bindings: dict[str, str]) -> dict[str, list[str]]:
    """同じキーに 2 つ以上の操作が割り当たっているもの キー → 操作の一覧

    Qt は重なったショートカットを、どちらも実行しない（黙って何も起きない）
    押しても反応しないのは壊れたように見えるので、決める時点で止める
    """
    owners: dict[str, list[str]] = {}
    for action, key in bindings.items():
        if key:
            owners.setdefault(key, []).append(action)
    return {key: actions for key, actions in owners.items() if len(actions) > 1}


#: プレビューの画質を自動で落とし始める縦の画素数
#: 1080p までは等倍で 60fps に入るので、落とす値打ちが無い
AUTO_QUALITY_HEIGHT = 1081

#: 先読みに使えるメモリの下限（MB） 1080p の 1 枚が 8MB なので、
#: これより小さいと数えるほどしか置けない
MIN_PREFETCH_MB = 128

#: 上限（MB） 家庭用の GPU の載っているメモリを超えない所で止める
MAX_PREFETCH_MB = 8192


@dataclass(frozen=True, slots=True)
class Preferences:
    """本人の好みで変わる設定

    プロジェクトではなく本人に付く 同じプロジェクトを別の機械で開いたときに、
    その機械の速さに合った設定で開きたい（速い機械では等倍で見たい）

    既定は「自動」 4K を置いた人が、なぜ重いのか分からないまま使うのを避ける
    自動で画質が変わるのを嫌う人は、設定で止められる
    """

    #: プレビューで控え（プロキシ）を使う
    use_proxy: bool = True
    #: 控えの縦の画素数
    proxy_height: int = 540
    #: 画面より大きい素材を置いたら、プレビューの画質を自動で落とす
    auto_quality: bool = True
    #: 自動で落とすときの分母
    auto_quality_divisor: int = 2
    #: 手が止まっている間に、再生ヘッドの先を描いて取っておく
    #: 既定は入 効果を積んだ所で再生が飛ぶのは、なぜ飛ぶのか分からない側の人ほど
    #: 困る 貯めるのが重すぎる所は画面の側（ui/preview.py）で自分から止める
    prefetch: bool = True
    #: 先読みに使うメモリ（メガバイト）
    #: 上限を置くのは、デコードと効果の側が使う GPU のメモリを残すため
    #: 使い切ると、先読みではなくプレビューそのものが描けなくなる
    prefetch_budget_mb: int = 1024
    #: 書き出しで、GPU の合成を書き込み（色変換・エンコード・mux）の何枚ぶん先へ進めるか
    #: 0 で直列（1 枚ずつ、スレッドを使わない）
    #: 既定は 2 1 枚ぶんは画面 1 枚の RGBA（1080p で 8MB、4K で 33MB）なので、
    #: メモリの少ない機械では減らせるようにする 実測は
    #: :data:`sashimono.engine.encode.MEASURED_EXPORT_MS`
    export_pipeline_depth: int = DEFAULT_PIPELINE_DEPTH
    #: AviUtl2 のスクリプトモジュール（``.mod2`` の中身が DLL の物）を読む
    #: 既定は入 テレビ字幕のように、DLL が無いと絵が出ない配布スクリプトがある
    #: 読んだ DLL は Sashimono と同じ権限で動く（Lua の閉じ込めの外） 読むのは
    #: 本人がスクリプトフォルダへ置いた物だけだが、気になる人は切れるようにする
    native_modules: bool = True

    def prefetch_bytes(self) -> int:
        """先読みに使えるバイト数 切ってあれば 0

        0 を渡された側は「1 枚も置けない」と読んで、先読みそのものをやめる
        入り切りの旗を下まで配らずに済む
        """
        if not self.prefetch:
            return 0
        return self.prefetch_budget_mb * 1024 * 1024

    def quality_for(self, height: int) -> int:
        """その高さの素材に対して、プレビューに使う分母

        4K を 1 枚置いただけなら元の素材でも入るが、重ねた時点で外れる
        効果を積むと控えだけでも足りず、画面の側も落として初めて入る
        測った値は :mod:`sashimono.engine.cache.proxy` の表を見る
        （同じ数を何か所にも書くと、測り直したときに片方だけ古くなる）
        """
        if not self.auto_quality or height < AUTO_QUALITY_HEIGHT:
            return 1
        return self.auto_quality_divisor


class PreferenceStore:
    """:class:`Preferences` の読み書き

    壊れていても起動は止めない 既定のまま使えれば困らない
    （ショートカットの保存と同じ考え方）
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else config_root() / "preferences.json"

    def load(self) -> Preferences:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Preferences()
        if not isinstance(data, dict):
            return Preferences()
        plain = Preferences()
        return Preferences(
            use_proxy=_flag(data.get("use_proxy"), plain.use_proxy),
            proxy_height=_size(data.get("proxy_height"), plain.proxy_height),
            auto_quality=_flag(data.get("auto_quality"), plain.auto_quality),
            auto_quality_divisor=_divisor(
                data.get("auto_quality_divisor"), plain.auto_quality_divisor
            ),
            prefetch=_flag(data.get("prefetch"), plain.prefetch),
            prefetch_budget_mb=_budget(data.get("prefetch_budget_mb"), plain.prefetch_budget_mb),
            export_pipeline_depth=_depth(
                data.get("export_pipeline_depth"), plain.export_pipeline_depth
            ),
            native_modules=_flag(data.get("native_modules"), plain.native_modules),
        )

    def save(self, preferences: Preferences) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".writing")
        temporary.write_text(
            json.dumps(asdict(preferences), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)


def _flag(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _size(value: object, default: int) -> int:
    """控えの高さ 極端な値は既定へ戻す

    0 や負だと控えが作れず、大きすぎると元の素材より重くなる
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return value if 120 <= value <= 2160 else default


def _budget(value: object, default: int) -> int:
    """先読みに使うメモリ（MB） 極端な値は既定へ戻す

    小さすぎると 1 枚も置けず、設定を入れたのに何も起きない
    大きすぎると GPU のメモリを使い切り、デコードや効果の側が確保に失敗する
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return value if MIN_PREFETCH_MB <= value <= MAX_PREFETCH_MB else default


def _depth(value: object, default: int) -> int:
    """書き出しのパイプラインの深さ 範囲の外は既定へ戻す

    負だと ``queue`` の作り方が変わって意味が通らず、大きすぎると合成済みの絵を
    溜め込むだけでメモリを食う（4K なら 1 枚 33MB） 0 は「直列」なので許す
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return value if 0 <= value <= MAX_PIPELINE_DEPTH else default


def _divisor(value: object, default: int) -> int:
    """画面の分母 1・2・4 だけ 半端な値は合成の大きさが端数になる

    型も見る JSON は ``2.0`` と書けてしまい、``2.0 in (1, 2, 4)`` は真になる
    小数のまま通すと、描画先の大きさが小数になって型の食い違いで落ちる
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return value if value in (1, 2, 4) else default
