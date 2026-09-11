"""AviUtl のオブジェクトファイル（``.exo``）とエイリアス（``.exa`` ``.object``）を読む。

どちらも INI に似た形で、節の名前が構造を表す。

- ``[exedit]`` — 全体の設定（解像度・フレームレート）
- ``[0]`` ``[1]`` … — オブジェクト 1 つ
- ``[0.0]`` ``[0.1]`` … — そのオブジェクトに積まれたフィルタ 1 つ

エイリアスは中身がほぼ同じで、オブジェクト 1 つ分だけが入っている。同じ読み手で
扱えるので、区別せずに読んでから使う側で解釈する。

世代が 2 つある。読み方が変わるのは次の 4 点だけで、あとは同じ。

============  ====================  ==============================
              AviUtl1 (``.exa``)    AviUtl2 (``.object``)
============  ====================  ==============================
節の名前      ``[0]`` ``[0.1]``     ``[Object]`` ``[Object.1]``
要素の名前    ``_name=テキスト``    ``effect.name=テキスト``
区間          ``start=`` ``end=``   ``frame=0,179``
テキスト欄    UTF-16LE の 16 進     素のまま（改行は ``\\n``）
============  ====================  ==============================

開始フレームの数え方も違う（1 始まり／0 始まり）。ここで **0 始まりに揃えて**
から返す。使う側が世代を気にしなくて済むようにするため。

ここでは**解釈しない**。値は文字列のまま持ち、内部のモデルへの対応付けは
:mod:`kumiki.compat.aviutl.mapping` が行う。分けておくと、未知のフィルタが
来ても読み込み自体は成功する。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from kumiki.compat.aviutl.encoding import decode_utf16_hex, read_text

__all__ = [
    "ALIAS_SUFFIXES",
    "ExoEntry",
    "ExoFile",
    "ExoObject",
    "ExoParseError",
    "load_exo",
    "parse_exo",
]

#: エイリアスとして読む拡張子。``.object`` は AviUtl2 世代のエイリアス。
ALIAS_SUFFIXES = (".exa", ".exa2", ".object", ".exo", ".exo2")

#: AviUtl2 の節の名前の頭。``[Object]`` ``[Object.1]``。
_ALIAS_SECTION = "object"


class ExoParseError(ValueError):
    """``.exo`` / ``.exa`` として読めない。"""


@dataclass(frozen=True, slots=True)
class ExoEntry:
    """オブジェクトに積まれた 1 つの要素。

    先頭の 1 つは中身（テキスト・動画・図形など）で、以降がフィルタ。最後は
    ふつう ``標準描画`` か ``拡張描画`` になる。
    """

    #: ``_name`` / ``effect.name`` の値。``テキスト`` ``標準描画`` など。
    name: str
    params: dict[str, str] = field(default_factory=dict)
    #: ``1`` か ``2``。テキスト欄の入り方とパラメータ名がこれで変わる。
    generation: int = 1

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

    def text(self, key: str = "") -> str:
        """テキスト欄を読む。

        AviUtl1 は UTF-16LE の 16 進、AviUtl2 は素のまま入っている。後者は
        改行が ``\\n`` の 2 文字として書かれているので、そこだけ戻す。
        """
        if self.generation >= 2:
            raw = self.params.get(key or "テキスト", "")
            return _unescape(raw)
        stored = self.params.get(key or "text")
        return decode_utf16_hex(stored) if stored else ""

    def value(self, *keys: str, default: str = "") -> str:
        """名前が世代で違う項目を、どちらの名前でも引く。"""
        for key in keys:
            found = self.params.get(key)
            if found is not None:
                return found
        return default

    def numeric(self, *keys: str, default: float = 0.0) -> float:
        for key in keys:
            if key in self.params:
                return self.number(key, default)
        return default


@dataclass(frozen=True, slots=True)
class ExoObject:
    """タイムライン上の 1 オブジェクト。"""

    index: int
    #: 開始・終了フレーム。**0 始まりに揃えてある**（終端は含む側）。
    #: AviUtl1 の ``start=1`` も AviUtl2 の ``frame=0,…`` もここでは 0 になる。
    start: int
    end: int
    layer: int = 1
    group: int = 0
    overlay: int = 1
    camera: int = 0
    entries: tuple[ExoEntry, ...] = ()
    #: ファイルが区間を書いていたか。エイリアスは書かないことがある。
    span_given: bool = True

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
    #: ``1`` か ``2``。どちらの世代の書き方だったか。
    generation: int = 1

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

    generation = _generation_of(sections)
    objects = _build_objects(sections, order, generation)
    if not objects and not settings:
        raise ExoParseError("オブジェクトも設定も見つからない")
    return ExoFile(settings=settings, objects=objects, encoding=encoding, generation=generation)


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
    """節の名前を ``(オブジェクト, 要素)`` へ。全体設定なら ``None``。

    ``[0.1]``（AviUtl1）と ``[Object.1]``（AviUtl2）の両方を受ける。後者は
    エイリアスにしか現れず、オブジェクトは 1 つだけなので 0 番として扱う。
    """
    head, separator, tail = name.partition(".")
    index = 0 if head.strip().lower() == _ALIAS_SECTION else None
    if index is None:
        if not head.isdigit():
            return None
        index = int(head)
    if not separator:
        return index, None
    return (index, int(tail)) if tail.isdigit() else None


def _generation_of(sections: dict[tuple[int, int | None], dict[str, str]]) -> int:
    """どちらの世代で書かれているか。

    要素の名前がどのキーに入っているかで決まる。``effect.name`` は AviUtl2 に
    しか無く、``_name`` は AviUtl1 にしか無い。
    """
    for values in sections.values():
        if "effect.name" in values:
            return 2
        if "_name" in values:
            return 1
    return 1


def _build_objects(
    sections: dict[tuple[int, int | None], dict[str, str]],
    order: list[tuple[int, int | None]],
    generation: int = 1,
) -> tuple[ExoObject, ...]:
    indices = sorted({index for index, part in order if part is None})
    if not indices:
        # 要素だけがある形（壊れたエイリアス）。オブジェクト 0 として扱う。
        indices = sorted({index for index, _ in order})

    built = []
    for index in indices:
        header = sections.get((index, None), {})
        parts = sorted(part for owner, part in sections if owner == index and part is not None)
        entries = tuple(_build_entry(sections[(index, part)], generation) for part in parts)
        start, end = _span(header, generation)
        built.append(
            ExoObject(
                index=index,
                start=start,
                end=end,
                layer=_as_int(header.get("layer"), 1),
                group=_as_int(header.get("group"), 0),
                overlay=_as_int(header.get("overlay"), 1),
                camera=_as_int(header.get("camera"), 0),
                entries=entries,
                span_given=bool(header.keys() & {"frame", "start", "end"}),
            )
        )
    return tuple(built)


def _span(header: dict[str, str], generation: int) -> tuple[int, int]:
    """区間を 0 始まりで返す。

    AviUtl1 は ``start=1`` ``end=60``（1 始まり）、AviUtl2 は ``frame=0,179``。
    数え方の違いをここで吸収しておかないと、写した先が 1 フレームずれる。
    """
    if generation >= 2:
        first, _, last = header.get("frame", "").partition(",")
        start = _as_int(first, 0)
        return start, max(start, _as_int(last, start))
    start = max(0, _as_int(header.get("start"), 1) - 1)
    return start, max(start, _as_int(header.get("end"), 1) - 1)


def _build_entry(values: dict[str, str], generation: int = 1) -> ExoEntry:
    name = values.get("effect.name") or values.get("_name", "")
    params = {key: value for key, value in values.items() if key not in ("_name", "effect.name")}
    return ExoEntry(name=name, params=params, generation=generation)


def _unescape(value: str) -> str:
    r"""AviUtl2 のテキスト欄に書かれた ``\n`` を本物の改行へ。"""
    return value.replace(chr(92) + "n", chr(10)).replace(chr(92) + chr(92), chr(92))


def _as_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(float(value))
    except ValueError:
        return default
