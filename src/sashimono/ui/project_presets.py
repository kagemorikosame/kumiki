"""本人が保存したプロジェクト設定のテンプレート（解像度・フレームレート・重ね合わせ）

プロジェクトではなく本人に付く いつも同じ組み合わせで作る人が、毎回数字を打たずに済むため
置き場は画面配置やショートカットと同じ ``%APPDATA%\\Sashimono``（:func:`config_root`）
:class:`~sashimono.ui.workspace.Preferences` とは別のファイルにする 名前付きの一覧で、
項目が 1 つずつ決まっている好みの設定とは形が違う 混ぜると、一覧が壊れたときに
好みの設定まで既定へ戻る
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sashimono.core.commands.edit import MAX_RESOLUTION, MIN_RESOLUTION
from sashimono.core.model import Blending
from sashimono.core.timebase import FrameRate
from sashimono.ui.workspace import config_root

__all__ = ["ProjectPreset", "ProjectPresetStore"]

#: ファイルの形の版 項目を足したり意味を変えたりしたら上げる
_VERSION = 1


@dataclass(frozen=True, slots=True)
class ProjectPreset:
    """名前を付けて保存した設定の組み合わせ"""

    name: str
    width: int
    height: int
    frame_rate: FrameRate
    blending: str

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            # 分数のまま持つ 29.97 を小数で書くと、読み戻したときに 30000/1001 へ戻らない
            "frame_rate": [self.frame_rate.num, self.frame_rate.den],
            "blending": self.blending,
        }


class ProjectPresetStore:
    """:class:`ProjectPreset` の一覧の読み書き

    壊れた項目は飛ばして残りを読む 1 つ壊れただけで全部が消えると、作り直す手間が大きい
    ファイルごと読めなくても起動は止めない（ショートカットの保存と同じ考え方）
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else config_root() / "project_presets.json"

    def load(self) -> list[ProjectPreset]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("presets") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        presets: list[ProjectPreset] = []
        for item in items:
            preset = _preset_from(item)
            # 同じ名前が 2 つあると、選んだ名前からどちらを消すのかが決まらない 先の方を残す
            if preset is not None and all(p.name != preset.name for p in presets):
                presets.append(preset)
        return presets

    def save(self, presets: list[ProjectPreset]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".writing")
        body = {"version": _VERSION, "presets": [preset.to_json() for preset in presets]}
        temporary.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        # 書き終えてから入れ替える 書いている途中で落ちると、一覧が半分だけ残る
        temporary.replace(self.path)

    def put(self, preset: ProjectPreset) -> list[ProjectPreset]:
        """同じ名前があれば置き換え、無ければ後ろへ足して保存する 並びは保存した順"""
        presets = self.load()
        names = [p.name for p in presets]
        if preset.name in names:
            presets[names.index(preset.name)] = preset
        else:
            presets.append(preset)
        self.save(presets)
        return presets

    def remove(self, name: str) -> list[ProjectPreset]:
        presets = [p for p in self.load() if p.name != name]
        self.save(presets)
        return presets


def _preset_from(item: object) -> ProjectPreset | None:
    """1 項目を読む 書き出せない値（奇数・範囲外）や知らない重ね合わせは捨てる

    手で書き換えたファイルの値をそのまま選べるようにすると、選んだ後に OK を押せない
    （奇数）か、プロジェクトの検査で断られる
    """
    if not isinstance(item, dict):
        return None
    name, rate, blending = item.get("name"), item.get("frame_rate"), item.get("blending")
    width, height = _size(item.get("width")), _size(item.get("height"))
    if not isinstance(name, str) or not name.strip() or width is None or height is None:
        return None
    if not isinstance(blending, str) or blending not in Blending.ALL:
        return None
    if not isinstance(rate, list) or len(rate) != 2:
        return None
    num, den = _integer(rate[0]), _integer(rate[1])
    if num is None or den is None or num <= 0 or den <= 0:
        return None
    return ProjectPreset(
        name=name.strip(),
        width=width,
        height=height,
        frame_rate=FrameRate(num, den),
        blending=blending,
    )


def _integer(value: object) -> int | None:
    # JSON の true は int として読めてしまう 1 として通すと、手で壊した値が黙って通る
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _size(value: object) -> int | None:
    """書き出せる縦横の画素数 偶数で、設定画面が受け付ける範囲の中"""
    number = _integer(value)
    if number is None or not MIN_RESOLUTION <= number <= MAX_RESOLUTION or number % 2:
        return None
    return number
