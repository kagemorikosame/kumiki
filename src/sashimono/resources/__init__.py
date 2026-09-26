"""アプリに同梱する素材

置いてあるもの:

=================  ==========================================================
``logo.svg``       ロゴの印だけ README や紹介ページ用
``icon.svg``       角丸の地に印を載せたもの アプリのアイコンの原本
``sashimono.ico``  ``icon.svg`` から作った Windows 用（16〜256 の 7 サイズ）
``spin_*.svg``     数値欄の増減ボタンの矢印 ``_off`` は押せないとき
``magnet*.svg``    タイムラインの磁石（吸着）の入り切りの印 ``_off`` は切のとき
=================  ==========================================================

``.ico`` と PNG は ``tools/build_icon.py`` が SVG から作る **原本は SVG だけ**
なので、形を直すときは SVG を触って作り直す

パッケージの中に置いてあるのは、PyInstaller でまとめたときに一緒に付いてくる
ようにするため アプリのフォルダの外に置くと、配布物で見失う
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

__all__ = [
    "BUNDLED_FILES",
    "ICON_FILE",
    "LOGO_FILE",
    "MAGNET_ICONS",
    "SPIN_ARROWS",
    "path_to",
]


def path_to(name: str) -> Path:
    """同梱素材の実際の場所"""
    with resources.as_file(resources.files(__name__) / name) as found:
        return Path(found)


#: アプリのアイコン（Windows 用）
ICON_FILE = "sashimono.ico"

#: ロゴの印
LOGO_FILE = "logo.svg"

#: 数値欄の増減ボタンの矢印 ``(上, 下, 上の押せないとき, 下の押せないとき)``
#: スタイルシートでボタンの置き場を決めると、元の見た目の矢印は描かれなくなるので自前で持つ
SPIN_ARROWS = ("spin_up.svg", "spin_down.svg", "spin_up_off.svg", "spin_down_off.svg")

#: タイムラインの磁石のボタンの印 ``(入, 切)`` 文字だけのボタンでは入と切が見分けにくかった
MAGNET_ICONS = ("magnet.svg", "magnet_off.svg")

#: 配る版に入っていなければならない素材 自己診断（``--self-check``）が揃っているかを見る
#: 組み立てで積み忘れると、ボタンの印が黙って消える
BUNDLED_FILES = (ICON_FILE, LOGO_FILE, *SPIN_ARROWS, *MAGNET_ICONS)
