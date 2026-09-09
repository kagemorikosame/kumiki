"""AviUtl のオブジェクトファイル（``.exo``）とエイリアス（``.exa``）を読む。

どちらも INI に似た形で、節の名前が構造を表す。

- ``[exedit]`` — 全体の設定（解像度・フレームレート）
- ``[0]`` ``[1]`` … — オブジェクト 1 つ
- ``[0.0]`` ``[0.1]`` … — そのオブジェクトに積まれたフィルタ 1 つ

エイリアスは中身がほぼ同じで、オブジェクト 1 つ分だけが入っている。同じ読み手で
扱えるので、区別せずに読んでから使う側で解釈する。

ここでは**解釈しない**。値は文字列のまま持ち、内部のモデルへの対応付けは
:mod:`novaedit.compat.aviutl.mapping` が行う。分けておくと、未知のフィルタが
来ても読み込み自体は成功する。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from novaedit.compat.aviutl.encoding import decode_utf16_hex, read_text

__all__ = [
    "ExoEntry",
    "ExoFile",
    "ExoObject",
    "ExoParseError",
    "load_exo",
    "parse_exo",
]


class ExoParseError(ValueError):
    """``.exo`` / ``.exa`` として読めない。"""


@dataclass(frozen=True, slots=True)
class ExoEntry:
    """オブジェクトに積まれた 1 つの要素。

    先頭の 1 つは中身（テキスト・動画・図形など）で、以降がフィルタ。最後は
    ふつう ``標準描画`` か ``拡張描画`` になる。
    """

    #: ``_name`` の値。``テキスト`` ``標準描画`` など。
    name: str
    params: dict[str, str] = field(default_factory=dict)

    def number(self, key: str, default: float = 0.0) -> float:
        """数値として読む。読めなければ ``default``。"""
        raw = self.params.get(key)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def integer(self, key: str, default: int = 0) -> int:
        raw = self.params.get(key)
        if raw is None:
            return default
        try:
            return int(float(raw))
        except ValueError:
            return default

    def text(self, key: str = "text") -> str:
        """UTF-16LE の 16 進で入っているテキスト欄を復号して返す。"""
        raw = self.params.get(key)
        return decode_utf16_hex(raw) if raw else ""


@dataclass(frozen=True, slots=True)
class ExoObject:
    """タイムライン上の 1 オブジェクト。"""

    index: int
    #: 開始・終了フレーム。AviUtl は 1 始まりで、終端を含む。
    start: int
    end: int
    layer: int = 1
    group: int = 0
    overlay: int = 1
    camera: int = 0
    entries: tuple[ExoEntry, ...] = ()

    @property
    def duration(self) -> int:
        """フレーム数。AviUtl の終端は含む側なので 1 足す。"""
        return max(1, self.end - self.start + 1)

    @property
    def content(self) -> ExoEntry | None:
        """中身（先頭の要素）。"""
        return self.entries[0] if self.entries else None

    def find(self, name: str) -> ExoEntry | None:
        return next((entry for entry in self.entries if entry.name == name), None)

    def filters(self) -> Iterator[ExoEntry]:
        """中身を除いた、積まれているフィルタ。"""
        return iter(self.entries[1:])


@dataclass(frozen=True, slots=True)
class ExoFile:
    """読み込んだファイル全体。"""

    settings: dict[str, str] = field(default_factory=dict)
    objects: tuple[ExoObject, ...] = ()
    #: 読み込みに使った文字コード。書き戻すときに合わせる。
    encoding: str = "utf-8"

    @property
    def width(self) -> int:
        return _as_int(self.settings.get("width"), 1920)

    @property
    def height(self) -> int:
        return _as_int(self.settings.get("height"), 1080)

    @property
    def frame_rate(self) -> tuple[int, int]:
        """``(分子, 分母)``。``scale`` が分母。"""
        rate = _as_int(self.settings.get("rate"), 30)
        scale = _as_int(self.settings.get("scale"), 1)
        return rate, max(1, scale)

    @property
    def is_alias(self) -> bool:
        """エイリアス（``.exa``）か。

        エイリアスはオブジェクト 1 つ分だけを持ち、全体設定を持たない。
        """
        return "width" not in self.settings and len(self.objects) <= 1


def parse_exo(text: str, *, encoding: str = "utf-8") -> ExoFile:
    """本文を読む。"""
    settings: dict[str, str] = {}
    # 節ごとの値。キーは ``(オブジェクト番号, 要素番号 or None)``。
    sections: dict[tuple[int, int | None], dict[str, str]] = {}
    order: list[tuple[int, int | None]] = []
    current: dict[str, str] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue

        if line.startswith("[") and line.endswith("]"):
            key = _section_key(line[1:-1])
            if key is None:
                current = settings
                continue
            if key not in sections:
                sections[key] = {}
                order.append(key)
            current = sections[key]
            continue

        if current is None:
            # 節の外の値。エイリアスでは先頭に置かれることがある。
            current = settings
        name, separator, value = line.partition("=")
        if separator:
            current[name.strip()] = value.strip()

    objects = _build_objects(sections, order)
    if not objects and not settings:
        raise ExoParseError("オブジェクトも設定も見つからない")
    return ExoFile(settings=settings, objects=objects, encoding=encoding)


def load_exo(path: Path) -> ExoFile:
    """ファイルを読む。文字コードは中身から判別する。"""
    target = Path(path)
    try:
        text, encoding = read_text(target)
    except OSError as exc:
        raise ExoParseError(f"開けない: {target} ({exc})") from exc
    try:
        return parse_exo(text, encoding=encoding)
    except ExoParseError as exc:
        raise ExoParseError(f"{target.name}: {exc}") from exc


def _section_key(name: str) -> tuple[int, int | None] | None:
    """節の名前を ``(オブジェクト, 要素)`` へ。全体設定なら ``None``。"""
    head, separator, tail = name.partition(".")
    if not head.isdigit():
        return None
    if not separator:
        return int(head), None
    return (int(head), int(tail)) if tail.isdigit() else None


def _build_objects(
    sections: dict[tuple[int, int | None], dict[str, str]],
    order: list[tuple[int, int | None]],
) -> tuple[ExoObject, ...]:
    indices = sorted({index for index, part in order if part is None})
    if not indices:
        # 要素だけがある形（壊れたエイリアス）。オブジェクト 0 として扱う。
        indices = sorted({index for index, _ in order})

    built = []
    for index in indices:
        header = sections.get((index, None), {})
        parts = sorted(part for owner, part in sections if owner == index and part is not None)
        entries = tuple(_build_entry(sections[(index, part)]) for part in parts)
        built.append(
            ExoObject(
                index=index,
                start=_as_int(header.get("start"), 1),
                end=_as_int(header.get("end"), 1),
                layer=_as_int(header.get("layer"), 1),
                group=_as_int(header.get("group"), 0),
                overlay=_as_int(header.get("overlay"), 1),
                camera=_as_int(header.get("camera"), 0),
                entries=entries,
            )
        )
    return tuple(built)


def _build_entry(values: dict[str, str]) -> ExoEntry:
    params = {key: value for key, value in values.items() if key != "_name"}
    return ExoEntry(name=values.get("_name", ""), params=params)


def _as_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(float(value))
    except ValueError:
        return default
