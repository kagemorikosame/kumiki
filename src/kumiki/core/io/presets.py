"""プリセット エフェクト構成とキーフレームを名前付きで保存する

保存するのは「クリップに積んだエフェクトの列」 1 つのエフェクトだけを保存する
のではなく列ごと持つのは、見た目のほとんどが複数のエフェクトの組み合わせで
できているため（縁取り + 影 + グロー、など）

プロジェクトファイルと同じ JSON の形を使う AviUtl のエイリアス（.exa）からの
変換は P5 で、この形へ落とす経路として実装する
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from kumiki.core.io.serialize import (
    ProjectFileError,
    effect_from_json,
    effect_to_json,
    source_from_json,
    source_to_json,
)
from kumiki.core.model import Effect, GeneratedSource

__all__ = [
    "FORMAT_NAME",
    "SUFFIX",
    "Preset",
    "PresetStore",
    "default_preset_root",
]

FORMAT_NAME = "kumiki-preset"
FORMAT_VERSION = 1
SUFFIX = ".kmkp"

#: ファイル名に使えない文字 Windows の制限に合わせる
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True, slots=True)
class Preset:
    """名前付きのエフェクト構成"""

    name: str
    effects: tuple[Effect, ...] = ()
    #: テキストや図形のプリセットではここに中身が入る
    source: GeneratedSource | None = None
    #: 分類 UI のフォルダ分けに使う
    category: str = "ユーザー"

    def to_dict(self) -> dict[str, object]:
        return {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "name": self.name,
            "category": self.category,
            "effects": [effect_to_json(effect) for effect in self.effects],
            "source": source_to_json(self.source) if self.source is not None else None,
        }

    @classmethod
    def from_dict(cls, data: object) -> Preset:
        if not isinstance(data, dict):
            raise ProjectFileError("プリセットがオブジェクトではない")
        if data.get("format") != FORMAT_NAME:
            raise ProjectFileError("Kumiki のプリセットではない")
        version = data.get("version", 0)
        if not isinstance(version, int) or version > FORMAT_VERSION:
            raise ProjectFileError(f"新しい形式のプリセット (version {version})")

        effects_raw = data.get("effects", [])
        if not isinstance(effects_raw, list):
            raise ProjectFileError("effects が配列ではない")
        source_raw = data.get("source")

        return cls(
            name=str(data.get("name", "無題")),
            effects=tuple(effect_from_json(raw) for raw in effects_raw),
            source=source_from_json(source_raw) if source_raw is not None else None,
            category=str(data.get("category", "ユーザー")),
        )

    def instantiate(self) -> tuple[Effect, ...]:
        """このプリセットを適用するためのエフェクト列を返す

        ID を振り直す 同じプリセットを 2 回適用したときに ID が衝突すると、
        片方を消したつもりで両方消える
        """
        return tuple(
            Effect(kind=effect.kind, params=dict(effect.params), enabled=effect.enabled)
            for effect in self.effects
        )


def default_preset_root() -> Path:
    """プリセットを置く既定の場所

    キャッシュと違い、消えると作り直せない ``%APPDATA%`` に置く
    """
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "Kumiki" / "presets"
    return Path.home() / ".config" / "kumiki" / "presets"


@dataclass(slots=True)
class PresetStore:
    """プリセットの読み書き"""

    root: Path = field(default_factory=default_preset_root)

    def path_for(self, preset: Preset) -> Path:
        return self.root / preset.category / f"{_safe_name(preset.name)}{SUFFIX}"

    def save(self, preset: Preset) -> Path:
        target = self.path_for(preset)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 一時ファイルへ書いてから差し替える プリセットは作り直せないので、
        # 書き込み中に落ちて壊れると手作業で復元することになる
        temporary = target.with_name(target.name + ".writing")
        temporary.write_text(
            json.dumps(preset.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)
        return target

    def load(self, path: Path) -> Preset:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProjectFileError(f"プリセットを開けない: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ProjectFileError(f"プリセットが JSON として読めない: {path} ({exc})") from exc
        return Preset.from_dict(data)

    def all(self) -> tuple[Preset, ...]:
        """読めるものだけを返す

        壊れた 1 つで一覧全体が出なくなると、他のプリセットまで使えなくなる
        """
        found: list[Preset] = []
        if not self.root.exists():
            return ()
        for path in sorted(self.root.rglob(f"*{SUFFIX}")):
            try:
                found.append(self.load(path))
            except ProjectFileError:
                continue
        return tuple(found)

    def delete(self, preset: Preset) -> None:
        self.path_for(preset).unlink(missing_ok=True)


def _safe_name(name: str) -> str:
    """ファイル名に使える形へ 空になったら既定の名前を返す"""
    cleaned = _UNSAFE.sub("_", name).strip().strip(".")
    return cleaned or "無題"
