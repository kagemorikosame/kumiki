"""YMM4（ゆっくりMovieMaker4）の資産を読む

対応するのは**アイテムテンプレート**（``.ymmt``）と、その中の**文字装飾**
（``Decorations``） プロジェクト（``.ymmp``）まるごとの取り込みは範囲外だが、
読み手は同じ ``$type`` 振り分けで書いてあるので、そのまま広げられる

- :mod:`~sashimono.compat.ymm4.json` — ``$type`` 付き JSON の読み方
- :mod:`~sashimono.compat.ymm4.template` — アイテムテンプレート → クリップ
"""

from __future__ import annotations

from sashimono.compat.ymm4.template import Ymm4ParseError, load_template, map_template

__all__ = ["Ymm4ParseError", "load_template", "map_template"]
